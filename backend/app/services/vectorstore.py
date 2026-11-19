"""向量索引：FAISS（默认 HNSW，可配 FLAT）+ 元数据落盘。

- 向量已归一化，内积即余弦相似度（两种索引都用 `METRIC_INNER_PRODUCT`）；
- 索引与块元数据一同落盘（可重建，由原始文件重建）；
- 索引文件缺失 → 空索引启动（尚未上传文档）；损坏 → 拒绝启动，
  不允许以坏索引对外服务。
"""
import json
import re
import threading
from pathlib import Path

import faiss
import numpy as np

from app.domain.schemas import Chunk
from app.services.retrieval.filters import MAX_OVERFETCH_FACTOR, OverfetchState
from app.core.errors import StorageCorruptedError
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

INDEX_FILE = "index.faiss"
META_FILE = "chunks.json"

# 索引版本化：chunk_id 形如 `{doc_id}@v{n}#{seq}`，序号空间按版本隔离，
# 因此重新解析不会与历史版本的块撞号，历史答案里的引用仍能命中旧块做溯源。
_VERSION_RE = re.compile(r"@v(\d+)#")


def chunk_version(chunk_id: str) -> int:
    """从 chunk_id 解析版本号；无版本标记（存量数据）返回 0。"""
    match = _VERSION_RE.search(chunk_id or "")
    return int(match.group(1)) if match else 0


def with_version(chunk_id: str, version: int) -> str:
    """给 chunk_id 打上版本号（已带版本号的先剥掉，避免叠加）。"""
    base = _VERSION_RE.sub("#", chunk_id)
    doc_id, _, seq = base.rpartition("#")
    return f"{doc_id}@v{version}#{seq}" if doc_id else base


def is_current_version(chunk_id: str, doc_id: str, version: int) -> bool:
    """块是否属于文档的当前版本（历史版本不参与召回，只服务溯源）。"""
    found = chunk_version(chunk_id)
    return found == 0 or found == version


class VectorStore:
    # 超过这个规模再自动迁移索引就不划算了（重建期间内存翻倍 + 启动变慢），
    # 此时只告警，由运维选低峰期手动重建。
    _AUTO_MIGRATE_LIMIT = 50_000

    def __init__(
        self,
        index_dir: Path,
        dim: int,
        index_type: str = "hnsw",
        m: int = 32,
        ef_construction: int = 200,
        ef_search: int = 64,
    ) -> None:
        self._dir = index_dir
        self._dim = dim
        self._lock = threading.Lock()
        # `A-26`：默认近似索引。FLAT 时间复杂度随库线性增长，
        # 10 万块时每次查询要遍历全部向量（延迟不可控），换 HNSW 是对数级。
        self._index_type = "flat" if str(index_type).lower() == "flat" else "hnsw"
        self._m = max(4, int(m))
        self._ef_construction = max(8, int(ef_construction))
        self._ef_search = max(int(ef_search), 1)
        self._index = self._new_index()
        self._chunks: dict[str, Chunk] = {}
        self._order: list[str] = []  # faiss 行号 → chunk_id

    def _new_index(self):
        """按配置构造**空**索引（删除/迁移时的重建也走这里）。"""
        if self._index_type == "hnsw":
            # HNSW 不支持 add_with_ids 之外的增量删除，但支持 reconstruct，
            # 所以「删除 = 重建」的既有策略不变，只是重建目标也换成 HNSW。
            index = faiss.IndexHNSWFlat(self._dim, self._m, faiss.METRIC_INNER_PRODUCT)
            index.hnsw.efConstruction = self._ef_construction
            index.hnsw.efSearch = self._ef_search
            return index
        return faiss.IndexFlatIP(self._dim)

    def _maybe_upgrade_index(self) -> None:
        """加载到的是旧 FLAT 索引、但配置要 HNSW 时，就地重建。

        没这段的话「改配置」对**已有索引**完全无效——用户改了
        `VECTOR_INDEX_TYPE=hnsw` 却仍在跑全量比较，且没有任何提示。
        """
        if self._index_type != "hnsw":
            return
        if isinstance(self._index, faiss.IndexHNSWFlat):
            return
        total = self._index.ntotal
        if total == 0:
            self._index = self._new_index()
            return
        if total > self._AUTO_MIGRATE_LIMIT:
            logger.warning(
                "索引为 FLAT 但配置要求 HNSW，向量数 %d 超过自动迁移阈值 %d，"
                "本次仍用 FLAT（请低峰期重建索引）", total, self._AUTO_MIGRATE_LIMIT,
            )
            return
        logger.info("索引类型升级：FLAT → HNSW（%d 个向量，重建中）", total)
        upgraded = self._new_index()
        upgraded.add(np.vstack([self._index.reconstruct(r) for r in range(total)]))
        self._index = upgraded

    # ---- 持久化 ----

    def load(self) -> None:
        index_path = self._dir / INDEX_FILE
        meta_path = self._dir / META_FILE
        if not index_path.exists() and not meta_path.exists():
            logger.info("未找到既有索引，使用空索引启动")
            return
        try:
            self._index = faiss.read_index(str(index_path))
            # 维度必须与当前向量模型一致：换 provider（如 1024 维千问 → 1536 维 embo-01）
            # 后旧索引照样能读出来，但**第一次检索才会炸**
            # （faiss: "AssertionError ... d == ..."），报错完全看不出是配置问题。
            if int(self._index.d) != self._dim:
                raise StorageCorruptedError(
                    f"向量索引维度({self._index.d})与当前向量模型({self._dim} 维)不一致，"
                    "拒绝启动：请删除索引目录并重新入库（EMBEDDING_DIM 变了就必须重建）",
                    path=self._path,
                )
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
            self._chunks = {cid: Chunk.model_validate(c) for cid, c in raw["chunks"].items()}
            self._order = list(raw["order"])
            if self._index.ntotal != len(self._order):
                raise StorageCorruptedError(f"索引行数({self._index.ntotal})与元数据({len(self._order)})不一致")
            logger.info("已加载索引：%d 个向量", self._index.ntotal)
            self._maybe_upgrade_index()
        except Exception as exc:
            raise StorageCorruptedError("向量索引文件缺失或损坏，拒绝启动", path=self._path) from exc

    def save(self) -> None:
        with self._lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp_index = self._dir / (INDEX_FILE + ".tmp")
            tmp_meta = self._dir / (META_FILE + ".tmp")
            faiss.write_index(self._index, str(tmp_index))
            payload = {
                "chunks": {cid: c.model_dump() for cid, c in self._chunks.items()},
                "order": self._order,
            }
            tmp_meta.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp_index.replace(self._dir / INDEX_FILE)
            tmp_meta.replace(self._dir / META_FILE)

    # ---- 读写 ----

    def add(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        if not chunks:
            return
        if len(chunks) != len(vectors):
            raise StorageCorruptedError("chunks 与 vectors 数量不一致")
        arr = np.array(vectors, dtype=np.float32)
        with self._lock:
            self._index.add(arr)
            for chunk in chunks:
                self._chunks[chunk.chunk_id] = chunk
                self._order.append(chunk.chunk_id)

    def search(
        self,
        query_vector: list[float],
        top_k: int,
        tenant_id: str,
        doc_id: str | None = None,
        filters=None,
    ) -> list[tuple[Chunk, float]]:
        """余弦相似度检索（向量已归一化）。

        FAISS 不支持表达式过滤，因此靠**迭代扩取**兜住后过滤的名额损耗
        （固定 4 倍在"可见文档排名靠后"时会取不满，见 filters.py 的说明）。
        """
        if self._index.ntotal == 0:
            return []
        query = np.array([query_vector], dtype=np.float32)
        total = self._index.ntotal
        state = OverfetchState()
        collected: dict[str, tuple[Chunk, float]] = {}

        while True:
            k = min(top_k * state.factor, total)
            state.fetched = k
            state.history.append(k)
            scores, indices = self._index.search(query, k)
            collected.clear()
            for score, row in zip(scores[0], indices[0]):
                if row < 0:
                    continue
                chunk = self._chunks.get(self._order[row])
                if chunk is None or chunk.tenant_id != tenant_id:
                    continue
                if doc_id is not None and chunk.doc_id != doc_id:
                    continue
                if filters is not None and not filters.allows(chunk.doc_id, chunk.tenant_id):
                    continue
                collected[chunk.chunk_id] = (chunk, float(score))
                if len(collected) >= top_k:
                    break
            if len(collected) >= top_k or k >= total:
                break
            # 取不满且还有候选 → 扩大候选池重取。
            # 注意必须在这里翻倍 factor：不推进的话 `can_continue` 永远为真 → 死循环。
            if state.factor >= MAX_OVERFETCH_FACTOR:
                logger.warning(
                    "FAISS 检索扩取达上限（factor=%d，可见 %d/%d），可能因过滤导致召回不足",
                    state.factor, len(collected), top_k,
                )
                break
            if not state.can_continue(top_k, len(collected), total):
                break
            state.next_limit(top_k)

        if state.rounds:
            logger.debug("FAISS 迭代扩取 %d 轮，候选池 %s，命中 %d", state.rounds, state.history, len(collected))
        results = sorted(collected.values(), key=lambda x: x[1], reverse=True)[:top_k]
        return results

    def delete_doc(self, doc_id: str) -> int:
        """按文档删除其全部块。"""
        with self._lock:
            keep_ids = [cid for cid in self._order if self._chunks[cid].doc_id != doc_id]
            removed = len(self._order) - len(keep_ids)
            if removed == 0:
                return 0
            for cid in set(self._order) - set(keep_ids):
                self._chunks.pop(cid, None)
            # 重建索引：从磁盘向量重取代价高， 直接用空索引 + 全量重嵌入由上层处理；
            # 这里采用重建索引矩阵的方式（IndexFlat 支持 reconstruct）。
            new_index = self._new_index()
            if keep_ids:
                old_rows = [self._order.index(cid) for cid in keep_ids]
                vecs = np.vstack([self._index.reconstruct(r) for r in old_rows]).astype(np.float32)
                new_index.add(vecs)
            self._index = new_index
            self._order = keep_ids
            return removed

    def delete_chunks(self, chunk_ids: set[str]) -> int:
        """按 chunk_id 删除块（M2）。

        为什么需要 `delete_doc` 之外的接口：图像描述块住在**共享 doc_id**
        （`__images__`）里 —— 删除某张图片所属的文档时，`delete_doc(doc_id)`
        根本碰不到它们，残留块仍会被**通用向量检索**召回（用户已删的文档
        的内容还能被搜出来）。这是正确性 + 隐私问题，不是性能问题。
        """
        if not chunk_ids:
            return 0
        with self._lock:
            keep_ids = [cid for cid in self._order if cid not in chunk_ids]
            removed = len(self._order) - len(keep_ids)
            if removed == 0:
                return 0
            new_index = self._new_index()
            if keep_ids:
                old_rows = [self._order.index(cid) for cid in keep_ids]
                vecs = np.vstack([self._index.reconstruct(r) for r in old_rows]).astype(np.float32)
                new_index.add(vecs)
            self._index = new_index
            self._order = keep_ids
            for cid in chunk_ids:
                self._chunks.pop(cid, None)
            return removed

    def doc_chunk_ids(self, doc_id: str) -> list[str]:
        return [cid for cid in self._order if self._chunks[cid].doc_id == doc_id]

    def doc_versions(self, doc_id: str) -> list[int]:
        """文档已存在的索引版本号（从 chunk_id 里的 `@v{n}` 解析）。"""
        versions: set[int] = set()
        for cid in self.doc_chunk_ids(doc_id):
            version = chunk_version(cid)
            if version:
                versions.add(version)
        return sorted(versions)

    def delete_doc_versions(self, doc_id: str, keep_versions: int = 3) -> int:
        """删除该文档**最旧的**若干历史版本块，只保留最近 `keep_versions` 版。

         的"保留 3 版可回滚"落点。返回删除条数。
        """
        versions = self.doc_versions(doc_id)
        if len(versions) <= keep_versions:
            return 0
        drop = set(versions[: len(versions) - keep_versions])
        return self._delete_ids({
            cid for cid in self.doc_chunk_ids(doc_id)
            if chunk_version(cid) in drop
        })

    def rename_doc_version(self, doc_id: str, version: int) -> list[str]:
        """把该文档的无版本 chunk_id 改写成带版本号的形式，返回新 id 列表。

        供 reparse 使用：旧块改名为"历史版本"后既不与新版本撞号，又仍留在索引里
        服务引用溯源。向量本身不动（FAISS 行号随 _order 一起重建）。
        """
        renamed: list[str] = []
        with self._lock:
            for cid in list(self._order):
                chunk = self._chunks.get(cid)
                if chunk is None or chunk.doc_id != doc_id or chunk_version(cid) != 0:
                    continue
                new_id = with_version(cid, version)
                if new_id in self._chunks:
                    continue  # 目标已存在：保留原样，避免覆盖
                self._chunks.pop(cid)
                chunk.chunk_id = new_id
                self._chunks[new_id] = chunk
                self._order[self._order.index(cid)] = new_id
                renamed.append(new_id)
        return renamed

    def _delete_ids(self, drop: set[str]) -> int:
        if not drop:
            return 0
        with self._lock:
            keep_ids = [cid for cid in self._order if cid not in drop]
            for cid in drop:
                self._chunks.pop(cid, None)
            new_index = self._new_index()
            if keep_ids:
                old_rows = [self._order.index(cid) for cid in keep_ids]
                vecs = np.vstack([self._index.reconstruct(r) for r in old_rows]).astype(np.float32)
                new_index.add(vecs)
            self._index = new_index
            self._order = keep_ids
            return len(drop)

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        return self._chunks.get(chunk_id)

    def all_chunks(self) -> list[Chunk]:
        """全量块（启动时用于重建 BM25 等派生索引）。"""
        return [self._chunks[cid] for cid in self._order]

    def close(self) -> None:
        """释放资源（FAISS 无需释放，占位保持与 Milvus 实现同接口）。"""

    @property
    def total(self) -> int:
        return self._index.ntotal


def build_vector_store(settings, alias=None, suffix: str = ""):
    """向量库工厂（依赖注入：faiss 本地文件 / milvus 服务，可替换）。

    `suffix`：给**独立**的向量库起名（如图像向量路用 "_image"）。
    图像向量与文本向量维度不同、空间不同，**混在同一个集合里会污染文本检索**
    （文本查询会被图像向量抢走相似度），所以必须是独立集合/目录。

    `alias` 是 `IndexAliasManager`（可为 None）：
    - FAISS：物理目录 = 别名当前指向的目录（默认沿用 `settings.index_dir`，
      即首次不迁移既有索引）；
    - Milvus：物理集合 = 别名当前指向的集合（默认沿用 `settings.milvus_collection`）。
    别名切换后**需要重建 store 实例**才会读到新物理索引（见 `reload`）。
    """
    if settings.vector_store == "milvus":
        # 可选依赖：Milvus 后端按配置才装配
        from app.services.milvus_store import MilvusVectorStore

        collection = (
            alias.physical_name() if alias is not None else settings.milvus_collection
        )
        if suffix:
            collection = f"{collection}{suffix}"
        return MilvusVectorStore(
            settings.milvus_uri,
            settings.embedding_dim,
            collection,
            index_type=settings.vector_index_type,
            m=settings.vector_index_m,
            ef_construction=settings.vector_index_ef_construction,
            ef_search=settings.vector_index_ef_search,
            consistency_level=settings.milvus_consistency_level,
        )
    index_dir = (
        settings.data_dir / alias.physical_name() if alias is not None else settings.index_dir
    )
    if suffix:
        index_dir = index_dir.parent / f"{index_dir.name}{suffix}"
    return VectorStore(
        index_dir,
        settings.embedding_dim,
        index_type=settings.vector_index_type,
        m=settings.vector_index_m,
        ef_construction=settings.vector_index_ef_construction,
        ef_search=settings.vector_index_ef_search,
    )
