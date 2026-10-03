"""BM25 关键词检索路（阶段 2 README §3.1）。

- 中文分词用 jieba（不能按字分，否则"电池"与"电池电压"无法匹配）；
- 参数 k1=1.5 / b=0.75（大多数场景够用）；
- 零分文档必须过滤，否则进入 RRF 排名计算污染链路；
- 索引与向量库同步增删，启动时从向量库元数据重建。
"""
import math
import threading
from collections import Counter

import jieba

from app.domain.schemas import Chunk
from app.services.retrieval.base import RetrievalCandidate

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

    def __init__(self, corpus: list[list[str]], k1: float = 1.5, b: float = 0.75):
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

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self._k1 = k1
        self._b = b
        self._lock = threading.Lock()
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
        with self._lock:
            self._chunks = {c.chunk_id: c for c in chunks}
            self._order = [c.chunk_id for c in chunks]
            self._rebuild_model()

    def add(self, chunks: list[Chunk]) -> None:
        with self._lock:
            for c in chunks:
                if c.chunk_id not in self._chunks:
                    self._order.append(c.chunk_id)
                self._chunks[c.chunk_id] = c
            self._rebuild_model()

    def delete_doc(self, doc_id: str) -> None:
        with self._lock:
            keep = [cid for cid in self._order if self._chunks[cid].doc_id != doc_id]
            for cid in set(self._order) - set(keep):
                self._chunks.pop(cid, None)
            self._order = keep
            self._rebuild_model()

    # ---- 检索 ----

    def search(
        self, query: str, top_n: int, tenant_id: str, doc_id: str | None = None
    ) -> list[RetrievalCandidate]:
        with self._lock:
            if self._bm25 is None or not self._order:
                return []
            scores = self._bm25.get_scores(tokenize(query))
            ranked = sorted(
                zip(self._order, scores), key=lambda x: x[1], reverse=True
            )
            results: list[RetrievalCandidate] = []
            for cid, score in ranked:
                if score <= 0:  # 零分文档过滤：不进入 RRF 排名
                    break
                chunk = self._chunks[cid]
                if chunk.tenant_id != tenant_id:
                    continue
                if doc_id is not None and chunk.doc_id != doc_id:
                    continue
                results.append(RetrievalCandidate(chunk=chunk, score=float(score)))
                if len(results) >= top_n:
                    break
            return results

    @property
    def total(self) -> int:
        return len(self._order)
