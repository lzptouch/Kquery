"""Milvus 向量库实现（与 FAISS VectorStore 同一接口，可替换）。

- 统一走 **Milvus 服务**（`deploy/docker` 里的 standalone，或自建集群），
  不再使用 pymilvus 内嵌的 Milvus Lite 单文件形态——生产与测试连同一个服务，
  避免「本地 lite 通过、上线换服务端行为不一致」这类只在部署后才暴露的差异；
- 块元数据（content/source/tenant_id 等）作为标量字段与向量同库存储，
  租户与文档过滤由 Milvus 标量过滤表达式一次完成；
- COSINE 度量 + 入库前归一化；
- 一致性级别可配（`MILVUS_CONSISTENCY_LEVEL`）：生产默认 Bounded 换取延迟，
  测试用 Strong 保证「写完立刻可查」。
"""
import threading

from pymilvus import DataType, MilvusClient

from app.domain.schemas import Chunk
from app.services.upstream import bulkhead
from app.core.errors import StorageCorruptedError, UpstreamUnavailableError
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

_OUTPUT_FIELDS = [
    "chunk_id", "doc_id", "seq", "content", "tenant_id", "source", "is_short",
    "chunker", "level", "parent_id", "modality",
]

_EXPECTED_FIELDS = set(_OUTPUT_FIELDS) | {"vector"}


def _vector_dim(desc: dict) -> int:
    """从 `describe_collection` 结果里取向量字段维度（取不到返回 0）。

    Milvus 把维度放在字段的 `params.dim` 里；不同版本/实现可能给字符串，
    所以统一 `int()` 兜底——这里只做「与配置是否一致」的判断，取不到就跳过校验。
    """
    for field in desc.get("fields", []):
        if field.get("name") == "vector":
            try:
                return int((field.get("params") or {}).get("dim") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


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

    def __init__(
        self,
        uri: str,
        dim: int,
        collection: str = "kquery_chunks",
        index_type: str = "hnsw",
        m: int = 32,
        ef_construction: int = 200,
        ef_search: int = 64,
        consistency_level: str = "Bounded",
    ) -> None:
        self._uri = uri
        self._dim = dim
        self._collection = collection
        self._consistency_level = consistency_level
        self._lock = threading.Lock()
        self._client = MilvusClient(uri=uri)
        # `A-26`：索引类型可配置。`flat` 保留给「小库对拍 / 排查召回差异」的场景
        self._index_type = "FLAT" if str(index_type).lower() == "flat" else "HNSW"
        self._m = max(4, int(m))
        self._ef_construction = max(8, int(ef_construction))
        self._ef_search = max(int(ef_search), 1)

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
        #  扩展字段
        schema.add_field("chunker", DataType.VARCHAR, max_length=32)
        schema.add_field("level", DataType.INT64)
        schema.add_field("parent_id", DataType.VARCHAR, max_length=256)
        schema.add_field("modality", DataType.VARCHAR, max_length=32)
        index = self._client.prepare_index_params()
        if self._index_type == "HNSW":
            index.add_index(
                field_name="vector",
                index_type="HNSW",
                metric_type="COSINE",
                params={"M": self._m, "efConstruction": self._ef_construction},
            )
        else:
            index.add_index(field_name="vector", index_type="FLAT", metric_type="COSINE")
        self._client.create_collection(
            collection_name=self._collection,
            schema=schema,
            index_params=index,
            consistency_level=self._consistency_level,
        )
        logger.info(
            "Milvus 集合已创建: %s（%d 维，索引 %s，一致性 %s）",
            self._collection, self._dim, self._index_type, self._consistency_level,
        )

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
                elif (existing_dim := _vector_dim(desc)) and existing_dim != self._dim:
                    # 维度是集合 schema 的硬约束：换向量模型（1024 → 1536）后插入会报
                    # 「dimension mismatch」，但错误出现在入库时、离配置改动很远。
                    logger.warning(
                        "Milvus 集合维度不符（集合 %d 维 / 配置 %d 维），重建集合（向量需重新灌入）",
                        existing_dim, self._dim,
                    )
                    self._client.drop_collection(self._collection)
                    self._create_collection()
            else:
                self._create_collection()
            self._client.load_collection(self._collection)
            logger.info("Milvus 已连接: %s（向量数=%d）", self._uri, self.total)
        except Exception as exc:
            raise UpstreamUnavailableError("Milvus 向量库不可用，拒绝启动") from exc

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
            raise StorageCorruptedError("chunks 与 vectors 数量不一致")
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
        filters=None,
    ) -> list[tuple[Chunk, float]]:
        """向量检索。

        Milvus 支持表达式过滤，因此把 `tenant_id` / `doc_id` 条件**下推到召回阶段**——
        不满足条件的块根本不参与 top-k 竞争，从根上消除「后过滤吃掉名额」。
        """
        if filters is not None:
            expr = filters.to_milvus_expr()
        else:
            expr = f'tenant_id == "{tenant_id}"'
            if doc_id is not None:
                expr += f' and doc_id == "{doc_id}"'
        # 批次 2：上游隔离仓（向量库并发与熔断独立于 LLM 通道）
        with bulkhead("milvus").acquire(timeout_s=30.0):
            results = self._client.search(
                collection_name=self._collection,
                data=[query_vector],
                filter=expr,
                limit=top_k,
                output_fields=_OUTPUT_FIELDS,
                # HNSW 的查询宽度：不传就用服务端默认值，「召回略降」于是变成
                # 一个看不见的默认差异（不同版本的服务端默认值并不保证一致）
                search_params={"ef": max(self._ef_search, int(top_k))},
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

    _QUERY_PAGE_SIZE = 1000  # Milvus 单次 query 硬上限 16384，分页拉取避免静默截断

    def _query_all(self, filter: str, output_fields: list[str]) -> list[dict]:
        """分页拉取全部匹配行。

        此前 doc_chunk_ids / rename_doc_version / all_chunks 都用 limit=16384 硬编码，
        超过 16k 块后静默只取前 16k——BM25 重建、版本清理、doc_versions 全部不完整。
        """
        rows: list[dict] = []
        offset = 0
        while True:
            page = self._client.query(
                collection_name=self._collection,
                filter=filter,
                output_fields=output_fields,
                limit=self._QUERY_PAGE_SIZE,
                offset=offset,
            )
            if not page:
                break
            rows.extend(page)
            if len(page) < self._QUERY_PAGE_SIZE:
                break
            offset += self._QUERY_PAGE_SIZE
        if len(rows) > 16384:
            logger.warning("单次全量拉取超过 16384 行（%d），已分页补齐（历史版本此处会被静默截断）", len(rows))
        return rows

    def doc_chunk_ids(self, doc_id: str) -> list[str]:
        rows = self._query_all(f'doc_id == "{doc_id}"', ["chunk_id"])
        return [r["chunk_id"] for r in rows]

    def delete_chunks(self, chunk_ids: set[str]) -> int:
        """按 chunk_id 删除块（M2，镜像 FAISS 实现）。

        Milvus 的 delete 用表达式：`chunk_id in [...]`。
        分批是为了避开超长表达式（块多时单条 SQL 会超长度限制）。
        """
        if not chunk_ids:
            return 0
        removed = 0
        ids = sorted(chunk_ids)
        for i in range(0, len(ids), 500):
            batch = ids[i : i + 500]
            quoted = ", ".join(f'"{cid}"' for cid in batch)
            try:
                self._client.delete(
                    collection_name=self._collection,
                    filter=f"chunk_id in [{quoted}]",
                )
                removed += len(batch)
            except Exception as exc:  # noqa: BLE001 - 删不掉要报错，不能静默
                logger.warning("Milvus 按 chunk_id 删除失败（%d 批）: %s", i // 500, exc)
                raise
        return removed

    def doc_versions(self, doc_id: str) -> list[int]:
        """文档已存在的索引版本号（从 chunk_id 里的 `@v{n}` 解析）。"""
        # 破环：vectorstore 反向依赖 milvus_store
        from app.services.vectorstore import chunk_version

        return sorted({v for v in (chunk_version(cid) for cid in self.doc_chunk_ids(doc_id)) if v})

    def delete_doc_versions(self, doc_id: str, keep_versions: int = 3) -> int:
        """删除该文档最旧的若干历史版本块，只保留最近 `keep_versions` 版。

        与 VectorStore 同语义：版本号来自 chunk_id，删除按 chunk_id 精确执行。
        """
        # 破环：vectorstore 反向依赖 milvus_store
        from app.services.vectorstore import chunk_version

        versions = self.doc_versions(doc_id)
        if len(versions) <= keep_versions:
            return 0
        drop = set(versions[: len(versions) - keep_versions])
        stale = [cid for cid in self.doc_chunk_ids(doc_id) if chunk_version(cid) in drop]
        if not stale:
            return 0
        quoted = ", ".join(f'"{cid}"' for cid in stale)
        with self._lock:
            self._client.delete(
                collection_name=self._collection, filter=f"chunk_id in [{quoted}]"
            )
        return len(stale)

    def rename_doc_version(self, doc_id: str, version: int) -> list[str]:
        """把该文档的无版本 chunk_id 改写成带版本号的形式（供 reparse 保留历史块）。

        Milvus 的主键即 chunk_id，改主键只能"读出含向量的整行 → 以新主键 upsert →
        删旧主键"。因此这里一次性把整批行搬完，不做逐条改写。
        """
        # 破环：vectorstore 反向依赖 milvus_store
        from app.services.vectorstore import chunk_version, with_version

        rows = self._query_all(f'doc_id == "{doc_id}"', [*_OUTPUT_FIELDS, "vector"])
        stale_ids = [r["chunk_id"] for r in rows if chunk_version(r["chunk_id"]) == 0]
        if not stale_ids:
            return []
        moved: list[dict] = []
        for row in rows:
            new_id = with_version(row["chunk_id"], version)
            if new_id != row["chunk_id"]:
                moved.append({**row, "chunk_id": new_id})
        if moved:
            with self._lock:
                self._client.upsert(collection_name=self._collection, data=moved)
                quoted = ", ".join(f'"{cid}"' for cid in stale_ids)
                self._client.delete(
                    collection_name=self._collection, filter=f"chunk_id in [{quoted}]"
                )
        return [r["chunk_id"] for r in moved]

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        rows = self._client.query(
            collection_name=self._collection,
            filter=f'chunk_id == "{chunk_id}"',
            output_fields=_OUTPUT_FIELDS,
            limit=1,
        )
        return _row_to_chunk(rows[0]) if rows else None

    def all_chunks(self) -> list[Chunk]:
        rows = self._query_all('chunk_id != ""', _OUTPUT_FIELDS)
        return [_row_to_chunk(r) for r in rows]

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - Milvus 客户端关闭失败：进程正在退出，不阻断关停
            pass

    @property
    def total(self) -> int:
        stats = self._client.get_collection_stats(self._collection)
        return int(stats.get("row_count", 0))
