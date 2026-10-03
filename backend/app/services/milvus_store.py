"""Milvus 向量库实现（与 FAISS VectorStore 同一接口，可替换）。

- 生产：Milvus Standalone（deploy/milvus，http://localhost:19530）；
- 测试：Milvus Lite（pymilvus 内嵌，本地文件 URI），同一套 MilvusClient API；
- 块元数据（content/source/tenant_id 等）作为标量字段与向量同库存储，
  租户与文档过滤由 Milvus 标量过滤表达式一次完成（阶段 1 §4.4 约定的落地）；
- COSINE 度量 + 入库前归一化（沿用阶段 1 约定）。
"""
import logging
import threading

from pymilvus import DataType, MilvusClient

from app.domain.schemas import Chunk

logger = logging.getLogger(__name__)

_OUTPUT_FIELDS = [
    "chunk_id", "doc_id", "seq", "content", "tenant_id", "source", "is_short",
    "chunker", "level", "parent_id", "modality",
]

_EXPECTED_FIELDS = set(_OUTPUT_FIELDS) | {"vector"}


def _row_to_chunk(row: dict) -> Chunk:
    return Chunk(
        chunk_id=row["chunk_id"],
        doc_id=row["doc_id"],
        seq=int(row["seq"]),
        content=row["content"],
        tenant_id=row["tenant_id"],
        source=row["source"],
        is_short=bool(row["is_short"]),
        chunker=row.get("chunker", "recursive"),
        level=int(row.get("level", 0)),
        parent_id=row.get("parent_id", "") or "",
        modality=row.get("modality", "text"),
    )


class MilvusVectorStore:
    """与 VectorStore（FAISS）相同的方法签名：load/save/add/search/delete_doc/
    doc_chunk_ids/get_chunk/all_chunks/total。"""

    def __init__(self, uri: str, dim: int, collection: str = "kquery_chunks"):
        self._uri = uri
        self._dim = dim
        self._collection = collection
        self._lock = threading.Lock()
        self._client = MilvusClient(uri=uri)

    # ---- 生命周期 ----

    def _create_collection(self) -> None:
        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=256)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self._dim)
        schema.add_field("doc_id", DataType.VARCHAR, max_length=256)
        schema.add_field("seq", DataType.INT64)
        schema.add_field("content", DataType.VARCHAR, max_length=4096)
        schema.add_field("tenant_id", DataType.VARCHAR, max_length=64)
        schema.add_field("source", DataType.VARCHAR, max_length=256)
        schema.add_field("is_short", DataType.BOOL)
        # 阶段 3 扩展字段
        schema.add_field("chunker", DataType.VARCHAR, max_length=32)
        schema.add_field("level", DataType.INT64)
        schema.add_field("parent_id", DataType.VARCHAR, max_length=256)
        schema.add_field("modality", DataType.VARCHAR, max_length=32)
        index = self._client.prepare_index_params()
        index.add_index(field_name="vector", index_type="FLAT", metric_type="COSINE")
        self._client.create_collection(
            collection_name=self._collection, schema=schema, index_params=index
        )
        logger.info("Milvus 集合已创建: %s（%d 维）", self._collection, self._dim)

    def load(self) -> None:
        """启动校验：集合不存在则创建；schema 版本不符则重建（数据可重灌）；
        创建/加载失败 → 拒绝启动（同 FAISS 语义）。"""
        try:
            if self._client.has_collection(self._collection):
                desc = self._client.describe_collection(self._collection)
                existing = {f["name"] for f in desc.get("fields", [])}
                if not _EXPECTED_FIELDS <= existing:
                    missing = _EXPECTED_FIELDS - existing
                    logger.warning(
                        "Milvus 集合 schema 版本不符（缺少 %s），重建集合（向量需重新灌入）", missing
                    )
                    self._client.drop_collection(self._collection)
                    self._create_collection()
            else:
                self._create_collection()
            self._client.load_collection(self._collection)
            logger.info("Milvus 已连接: %s（向量数=%d）", self._uri, self.total)
        except Exception as exc:
            raise RuntimeError(f"Milvus 向量库不可用，拒绝启动: {exc}") from exc

    def save(self) -> None:
        """Milvus 自动持久化；flush 保证落盘可见性。"""
        try:
            self._client.flush(self._collection)
        except Exception as exc:  # flush 失败不阻断写入路径（下一批会再刷）
            logger.warning("Milvus flush 失败: %s", exc)

    # ---- 读写 ----

    def add(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        if not chunks:
            return
        if len(chunks) != len(vectors):
            raise ValueError("chunks 与 vectors 数量不一致")
        rows = [
            {
                "chunk_id": c.chunk_id,
                "vector": v,
                "doc_id": c.doc_id,
                "seq": c.seq,
                "content": c.content,
                "tenant_id": c.tenant_id,
                "source": c.source,
                "is_short": c.is_short,
                "chunker": c.chunker,
                "level": c.level,
                "parent_id": c.parent_id,
                "modality": c.modality,
            }
            for c, v in zip(chunks, vectors)
        ]
        with self._lock:
            self._client.upsert(collection_name=self._collection, data=rows)

    def search(
        self,
        query_vector: list[float],
        top_k: int,
        tenant_id: str,
        doc_id: str | None = None,
    ) -> list[tuple[Chunk, float]]:
        expr = f'tenant_id == "{tenant_id}"'
        if doc_id is not None:
            expr += f' and doc_id == "{doc_id}"'
        results = self._client.search(
            collection_name=self._collection,
            data=[query_vector],
            filter=expr,
            limit=top_k,
            output_fields=_OUTPUT_FIELDS,
        )
        hits: list[tuple[Chunk, float]] = []
        for hit in results[0]:
            hits.append((_row_to_chunk(hit["entity"]), round(float(hit["distance"]), 6)))
        return hits

    def delete_doc(self, doc_id: str) -> int:
        with self._lock:
            before = self.total
            self._client.delete(
                collection_name=self._collection, filter=f'doc_id == "{doc_id}"'
            )
            return before - self.total

    def doc_chunk_ids(self, doc_id: str) -> list[str]:
        rows = self._client.query(
            collection_name=self._collection,
            filter=f'doc_id == "{doc_id}"',
            output_fields=["chunk_id"],
            limit=16384,
        )
        return [r["chunk_id"] for r in rows]

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        rows = self._client.query(
            collection_name=self._collection,
            filter=f'chunk_id == "{chunk_id}"',
            output_fields=_OUTPUT_FIELDS,
            limit=1,
        )
        return _row_to_chunk(rows[0]) if rows else None

    def all_chunks(self) -> list[Chunk]:
        rows = self._client.query(
            collection_name=self._collection,
            filter='chunk_id != ""',
            output_fields=_OUTPUT_FIELDS,
            limit=16384,
        )
        return [_row_to_chunk(r) for r in rows]

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    @property
    def total(self) -> int:
        stats = self._client.get_collection_stats(self._collection)
        return int(stats.get("row_count", 0))
