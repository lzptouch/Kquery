"""图构建编排。

chunk → 抽取 → 消歧 → upsert → 社区检测 → 摘要（入向量空间）。
以块为最小更新单位：重建文档时先删该文档块贡献的关系。
社区摘要写入统一向量库（doc_id=__communities__），
与正文共存于同一检索空间——抽象问题命中摘要，具体问题命中实体。
"""
import time
from pathlib import Path

from app.domain.schemas import Chunk, EntityNode, RelationEdge
from app.services.embeddings import Embeddings
from app.services.graph.community import HierarchicalCommunityDetector, build_communities
from app.services.graph.extractor import Extractor
from app.services.graph.resolver import EntityResolver
from app.services.graph.store import BaseGraphStore
from app.services.llm import LLM
from app.services.vectorstore import VectorStore
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

COMMUNITY_DOC_ID = "__communities__"


class GraphBuilder:
    def __init__(
        self,
        extractor: Extractor,
        resolver: EntityResolver,
        store: BaseGraphStore,
        embeddings: Embeddings,
        vector_store: VectorStore,
        llm: LLM | None,
        tenant_id: str = "default",
        resolver_path: Path | None = None,
        model_version: str = "",
        max_communities: int = 20,
        community_levels: int = 1,
        community_merge_threshold: float = 0.3,
    ) -> None:
        self._extractor = extractor
        self._resolver = resolver
        self._store = store
        self._embeddings = embeddings
        self._vector_store = vector_store
        self._llm = llm
        self._tenant = tenant_id
        self._resolver_path = resolver_path
        self._model_version = model_version
        # 社区数量上限来自配置（成本护栏：每个社区都要调一次摘要模型）
        self._max_communities = max(1, int(max_communities))
        # 层级数 = 1 时退化为单层连通分量，与旧行为完全一致；
        # >1 时逐级重叠合并出粗粒度主题（）
        self._community_levels = max(1, int(community_levels))
        self._community_detector = (
            HierarchicalCommunityDetector(
                merge_threshold=community_merge_threshold, max_level=self._community_levels
            )
            if self._community_levels > 1 else None
        )

    def build_from_chunks(
        self, chunks: list[Chunk], rebuild_doc_id: str | None = None, tenant_id: str | None = None
    ) -> dict:
        """对块集合做抽取与归并。rebuild_doc_id 指定时先删除该文档块的旧关系。

        租户纪律（KQ-06）：实体/关系写入块所属租户的图分区。租户取调用方显式传入值，
        缺省取第一个块的 tenant_id，再缺省回落构造租户——绝不跨租户混写。
        """
        started = time.monotonic()
        tenant = tenant_id or (chunks[0].tenant_id if chunks else self._tenant)
        if rebuild_doc_id:
            removed = self._store.delete_doc_chunks(
                {c.chunk_id for c in chunks if c.doc_id == rebuild_doc_id}
            )
            logger.info("重建模式：删除文档 %s 旧关系 %d 条", rebuild_doc_id, removed)

        n_entities = n_relations = 0
        for chunk in chunks:
            result = self._extractor.extract(chunk.content)
            # 消歧：实体名归一（同类型才合并）
            name_map: dict[str, str] = {}
            for e in result["entities"]:
                canonical, _ = self._resolver.resolve(
                    e["name"], e["type"], e["description"], tenant_id=tenant
                )
                name_map[e["name"]] = canonical
                self._store.upsert_entity(EntityNode(
                    entity_id=canonical, name=canonical, type=e["type"],
                    description=e["description"], tenant_id=tenant,
                ))
                n_entities += 1
            for r in result["relations"]:
                source = name_map.get(r["source"])
                target = name_map.get(r["target"])
                if not source or not target:
                    # 关系端点未在实体表中 → 按 Concept 补建（关系不丢弃）
                    source = source or self._resolver.resolve(r["source"], "Concept", tenant_id=tenant)[0]
                    target = target or self._resolver.resolve(r["target"], "Concept", tenant_id=tenant)[0]
                    self._store.upsert_entity(EntityNode(entity_id=source, name=source, tenant_id=tenant))
                    self._store.upsert_entity(EntityNode(entity_id=target, name=target, tenant_id=tenant))
                self._store.upsert_relation(RelationEdge(
                    source=source, target=target, relation=r["relation"],
                    description=r["evidence"],  # 关系描述 = 支撑原文（可溯源）
                    weight=1.0, source_chunks=[chunk.chunk_id], tenant_id=tenant,
                ))
                n_relations += 1
        self._persist()
        return {
            "chunks_processed": len(chunks),
            "entities_upserted": n_entities,
            "relations_upserted": n_relations,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }

    def rebuild_communities(self, tenant_id: str | None = None) -> dict:
        """社区重算 + 摘要入向量库（图更新后触发，防摘要过时）。

        按租户重建（KQ-06）：社区检测只纳入本租户关系，摘要块写入租户专属
        doc_id（`__communities__@{tenant}`），重建不再清掉其它租户的摘要。
        """
        tenant = tenant_id or self._tenant
        communities = build_communities(
            self._store, self._llm, model_version=self._model_version,
            tenant_id=tenant, max_communities=self._max_communities,
            detector=self._community_detector,
        )
        community_doc = f"{COMMUNITY_DOC_ID}@{tenant}"
        # 摘要入统一向量空间（同 text-embedding-v3，不另起索引）
        self._vector_store.delete_doc(community_doc)
        summary_chunks = []
        for c in communities:
            if not c.summary:
                continue
            summary_chunks.append(Chunk(
                chunk_id=f"{community_doc}#{c.community_id}",
                doc_id=community_doc,
                seq=0,
                content=f"社区主题摘要（含实体：{'、'.join(c.members[:10])}）：{c.summary}",
                source=f"知识图谱·{c.community_id}",
                tenant_id=tenant,
            ))
        if summary_chunks:
            vectors = self._embeddings.embed_texts([c.content for c in summary_chunks])
            self._vector_store.add(summary_chunks, vectors)
            self._vector_store.save()
        self._persist()
        return {
            "communities": len(communities),
            "summaries_indexed": len(summary_chunks),
        }

    def _persist(self) -> None:
        self._store.save()
        if self._resolver_path:
            import json
            self._resolver_path.write_text(
                json.dumps(self._resolver.export(), ensure_ascii=False), encoding="utf-8"
            )
