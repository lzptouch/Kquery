"""BM25 关键词检索路。

- 中文分词用 jieba（不能按字分，否则"电池"与"电池电压"无法匹配）；
- 参数 k1=1.5 / b=0.75（大多数场景够用）；
- 零分文档必须过滤，否则进入 RRF 排名计算污染链路；
- 索引与向量库同步增删，启动时从向量库元数据重建。
"""
import math
from collections import Counter

import jieba

from app.core.rwlock import ReadWriteLock
from app.domain.schemas import Chunk
from app.services.retrieval.base import RetrievalCandidate
from ..vectorstore import chunk_version, with_version

# 分词缓存：chunk 级预分词在索引构建时完成，查询分词量小不缓存
jieba.setLogLevel(20)


def tokenize(text: str) -> list[str]:
    return [t for t in jieba.lcut(text) if t.strip()]


class _BM25Scorer:
    """Lucene 风格 BM25：IDF = ln(1 + (N-n+0.5)/(n+0.5)) 恒正。

    保证"共享任一查询词的文档得分 > 0，无关文档恰为 0 分"——
    这是零分过滤语义成立的前提（rank_bm25 的负 IDF 下界处理在
    小语料下会产生负分，会把相关文档误杀）。
    """

    def __init__(self, corpus: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.n_docs = len(corpus)
        self.doc_len = [max(len(d), 1) for d in corpus]
        self.avgdl = sum(self.doc_len) / max(self.n_docs, 1)
        self.tf = [Counter(d) for d in corpus]
        df: Counter = Counter()
        for doc in corpus:
            for term in set(doc):
                df[term] += 1
        self.idf = {
            t: math.log(1 + (self.n_docs - n + 0.5) / (n + 0.5)) for t, n in df.items()
        }

    def get_scores(self, query_tokens: list[str]) -> list[float]:
        scores: list[float] = []
        for i, tf in enumerate(self.tf):
            norm = 1 - self.b + self.b * self.doc_len[i] / self.avgdl if self.avgdl else 1.0
            score = 0.0
            for term in query_tokens:
                idf = self.idf.get(term)
                if idf is None:
                    continue
                f = tf.get(term, 0)
                score += idf * (f * (self.k1 + 1)) / (f + self.k1 * norm)
            scores.append(score)
        return scores


class BM25Retriever:
    name = "keyword"

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self._k1 = k1
        self._b = b
        # 读共享 / 写独占（此前是普通 Lock，导致所有关键词检索串行）
        self._lock = ReadWriteLock()
        self._chunks: dict[str, Chunk] = {}
        self._order: list[str] = []
        self._bm25: _BM25Scorer | None = None

    # ---- 索引维护（与向量库同步）----

    def _rebuild_model(self) -> None:
        if not self._order:
            self._bm25 = None
            return
        corpus = [tokenize(self._chunks[cid].content) for cid in self._order]
        self._bm25 = _BM25Scorer(corpus, k1=self._k1, b=self._b)

    def rebuild(self, chunks: list[Chunk]) -> None:
        with self._lock.write():
            self._chunks = {c.chunk_id: c for c in chunks}
            self._order = [c.chunk_id for c in chunks]
            self._rebuild_model()

    def add(self, chunks: list[Chunk]) -> None:
        with self._lock.write():
            # 先拷贝再重绑：`self._order.append()` 会**原地修改**列表，
            # 而并发读者可能正持有该列表的引用（读锁只保证"不被写者打断"，
            # 不保证"看到一致的列表长度"）。重绑让读者要么看到旧列表、要么看到新列表。
            order = list(self._order)
            for c in chunks:
                if c.chunk_id not in self._chunks:
                    order.append(c.chunk_id)
                self._chunks[c.chunk_id] = c
            self._order = order
            self._rebuild_model()

    def delete_doc(self, doc_id: str) -> None:
        with self._lock.write():
            keep = [cid for cid in self._order if self._chunks[cid].doc_id != doc_id]
            for cid in set(self._order) - set(keep):
                self._chunks.pop(cid, None)
            self._order = keep
            self._rebuild_model()

    def rename_doc_version(self, doc_id: str, version: int) -> int:
        """把该文档**无版本号**的块 id 改写成 `@v{version}`，返回改名条数。

        reparse 时向量库会先把旧块改名成历史版本、再写入新版本。BM25 若不同步
        改名，两边 chunk_id 就对不上——关键词路命中的 id 在向量库/文档存储里已经
        不存在，用户点开引用是死链（`X-14`）。
        """
        with self._lock.write():
            chunks: dict[str, Chunk] = {}
            order: list[str] = []
            renamed = 0
            for cid in self._order:
                chunk = self._chunks[cid]
                new_id = cid
                if chunk.doc_id == doc_id and chunk_version(cid) == 0:
                    new_id = with_version(cid, version)
                    chunk = chunk.model_copy(update={"chunk_id": new_id})
                    renamed += 1
                chunks[new_id] = chunk
                order.append(new_id)
            if not renamed:
                return 0
            # 与 `add()` 同样的理由：整体重绑而不是原地改，并发读者要么看到旧表、
            # 要么看到新表，不会看到"改了一半的 id"
            self._chunks = chunks
            self._order = order
            self._rebuild_model()
            return renamed

    def delete_doc_versions(self, doc_id: str, keep_versions: int = 3) -> int:
        """只保留该文档最近 `keep_versions` 个版本的块，返回删除条数。

        与向量库 `delete_doc_versions` 同语义。不同步删除的后果有两层：已下线
        版本的块仍会命中关键词（召回里混入过期内容），且内存随 reparse 次数
        **线性增长**（每版一份全量块）。
        """
        with self._lock.write():
            versions = sorted({
                chunk_version(cid)
                for cid in self._order
                if self._chunks[cid].doc_id == doc_id and chunk_version(cid)
            })
            if len(versions) <= keep_versions:
                return 0
            drop = set(versions[: len(versions) - keep_versions])
            keep_ids = [
                cid
                for cid in self._order
                if not (self._chunks[cid].doc_id == doc_id and chunk_version(cid) in drop)
            ]
            removed = len(self._order) - len(keep_ids)
            for cid in set(self._order) - set(keep_ids):
                self._chunks.pop(cid, None)
            self._order = keep_ids
            self._rebuild_model()
            return removed

    # ---- 检索 ----

    def search(
        self, query: str, top_n: int, tenant_id: str, doc_id: str | None = None,
        filters=None,
    ) -> list[RetrievalCandidate]:
        """关键词检索。

        **先过滤再排序**（此前是"全量排序 → 逐个跳过不合格"，等价于让
        不合格文档参与排序并占位）。语义上两者结果相同，但显式过滤把 ACL 判定
        收敛到一处，也让"可见集合"成为排序输入而非事后剔除。
        """
        with self._lock.read():
            if self._bm25 is None or not self._order:
                return []
            scores = self._bm25.get_scores(tokenize(query))

            def _visible(cid: str) -> bool:
                chunk = self._chunks[cid]
                if chunk.tenant_id != tenant_id:
                    return False
                if doc_id is not None and chunk.doc_id != doc_id:
                    return False
                if filters is not None and not filters.allows(chunk.doc_id, chunk.tenant_id):
                    return False
                return True

            ranked = sorted(
                ((cid, score) for cid, score in zip(self._order, scores) if _visible(cid)),
                key=lambda x: x[1],
                reverse=True,
            )
            results: list[RetrievalCandidate] = []
            for cid, score in ranked:
                if score <= 0:  # 零分文档过滤：不进入 RRF 排名
                    break
                chunk = self._chunks[cid]
                results.append(RetrievalCandidate(chunk=chunk, score=float(score)))
                if len(results) >= top_n:
                    break
            return results

    @property
    def total(self) -> int:
        return len(self._order)
