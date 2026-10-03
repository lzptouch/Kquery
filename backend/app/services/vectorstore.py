"""向量索引：FAISS IndexFlatIP + 元数据落盘（阶段 1 README §3.2 / §4.5）。

- 向量已归一化，IndexFlatIP 的内积即余弦相似度；
- 索引与块元数据一同落盘（可重建，由原始文件重建）；
- 索引文件缺失 → 空索引启动（尚未上传文档）；损坏 → 拒绝启动，
  不允许以坏索引对外服务。
"""
import json
import logging
import threading
from pathlib import Path

import faiss
import numpy as np

from app.domain.schemas import Chunk

logger = logging.getLogger(__name__)

INDEX_FILE = "index.faiss"
META_FILE = "chunks.json"


class VectorStore:
    def __init__(self, index_dir: Path, dim: int):
        self._dir = index_dir
        self._dim = dim
        self._lock = threading.Lock()
        self._index = faiss.IndexFlatIP(dim)
        self._chunks: dict[str, Chunk] = {}
        self._order: list[str] = []  # faiss 行号 → chunk_id

    # ---- 持久化 ----

    def load(self) -> None:
        index_path = self._dir / INDEX_FILE
        meta_path = self._dir / META_FILE
        if not index_path.exists() and not meta_path.exists():
            logger.info("未找到既有索引，使用空索引启动")
            return
        try:
            self._index = faiss.read_index(str(index_path))
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
            self._chunks = {cid: Chunk.model_validate(c) for cid, c in raw["chunks"].items()}
            self._order = list(raw["order"])
            if self._index.ntotal != len(self._order):
                raise ValueError(f"索引行数({self._index.ntotal})与元数据({len(self._order)})不一致")
            logger.info("已加载索引：%d 个向量", self._index.ntotal)
        except Exception as exc:
            raise RuntimeError(f"向量索引文件缺失或损坏，拒绝启动: {exc}") from exc

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
            raise ValueError("chunks 与 vectors 数量不一致")
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
    ) -> list[tuple[Chunk, float]]:
        """余弦相似度检索（向量已归一化）。租户过滤在块上冗余字段内一次完成。"""
        if self._index.ntotal == 0:
            return []
        query = np.array([query_vector], dtype=np.float32)
        # 多取一些用于租户/文档过滤后兜底
        k = min(max(top_k * 4, top_k), self._index.ntotal)
        scores, indices = self._index.search(query, k)
        results: list[tuple[Chunk, float]] = []
        for score, row in zip(scores[0], indices[0]):
            if row < 0:
                continue
            chunk = self._chunks.get(self._order[row])
            if chunk is None or chunk.tenant_id != tenant_id:
                continue
            if doc_id is not None and chunk.doc_id != doc_id:
                continue
            results.append((chunk, float(score)))
            if len(results) >= top_k:
                break
        return results

    def delete_doc(self, doc_id: str) -> int:
        """按文档删除其全部块（阶段 1 直接重建索引，数据量小可接受）。"""
        with self._lock:
            keep_ids = [cid for cid in self._order if self._chunks[cid].doc_id != doc_id]
            removed = len(self._order) - len(keep_ids)
            if removed == 0:
                return 0
            for cid in set(self._order) - set(keep_ids):
                self._chunks.pop(cid, None)
            # 重建索引：从磁盘向量重取代价高，阶段 1 直接用空索引 + 全量重嵌入由上层处理；
            # 这里采用重建索引矩阵的方式（IndexFlat 支持 reconstruct）。
            new_index = faiss.IndexFlatIP(self._dim)
            if keep_ids:
                old_rows = [self._order.index(cid) for cid in keep_ids]
                vecs = np.vstack([self._index.reconstruct(r) for r in old_rows]).astype(np.float32)
                new_index.add(vecs)
            self._index = new_index
            self._order = keep_ids
            return removed

    def doc_chunk_ids(self, doc_id: str) -> list[str]:
        return [cid for cid in self._order if self._chunks[cid].doc_id == doc_id]

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


def build_vector_store(settings):
    """向量库工厂（依赖注入：faiss 本地文件 / milvus 服务或 lite 文件，可替换）。"""
    if settings.vector_store == "milvus":
        from app.services.milvus_store import MilvusVectorStore

        return MilvusVectorStore(settings.milvus_uri, settings.embedding_dim, settings.milvus_collection)
    return VectorStore(settings.index_dir, settings.embedding_dim)
