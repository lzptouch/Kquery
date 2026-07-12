"""结构化文档抽取器 (Structured Extractor)

将 NormalizedDocument（结构化文档，如 Excel/CSV）直接转换为可写入 Neo4j 的实体与关系。
全程零 LLM 调用，依赖 TableMapper 完成表头->实体/关系的规则映射。

职责：
1. 创建文档元节点（Document/Table）
2. 调用 TableMapper 生成业务实体和关系
3. 附加文档元关系（CONTAINS, MENTIONED_IN, HAS_ROW）
4. 输出可直接批量写入 Neo4j 的 Entity 和 Relation 列表

作者：KQuery Pro Team
版本：1.0.0
更新：2026-07-05
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


from app.services.document_models import NormalizedDocument, NormalizedTable
from app.services.graph.table_mapper import TableMapper
from app.services.graph.entity_extraction import ExtractedEntity
from app.services.graph.relation_extraction import ExtractedRelation
from app.repositories.graph.schema import Entity, Relation, EntityType, RelationType
from app.core.logger import get_logger

logger = get_logger(__name__)


@dataclass
class StructuredExtractionResult:
    """结构化文档抽取结果"""
    entities: List[Entity] = field(default_factory=list)
    relations: List[Relation] = field(default_factory=list)
    document_entity: Optional[Entity] = None
    table_entities: List[Entity] = field(default_factory=list)
    business_entities: List[Entity] = field(default_factory=list)
    business_relations: List[Relation] = field(default_factory=list)
    meta_relations: List[Relation] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class StructuredExtractor:
    """结构化文档抽取器
    
    将 Excel/CSV 等结构化表格文档直接转换为知识图谱实体和关系。
    """

    def __init__(self, table_mapper: Optional[TableMapper] = None):
        self.table_mapper = table_mapper or TableMapper()

    def extract(self, document: NormalizedDocument, kb_id: Optional[int] = None) -> StructuredExtractionResult:
        """从 NormalizedDocument 抽取实体和关系"""
        result = StructuredExtractionResult()

        if not document.tables:
            result.errors.append("文档不包含任何表格，无法按结构化方式抽取")
            return result

        doc_id = document.doc_id
        source_file = document.source_file

        # 1. 创建 Document 元节点（使用唯一名称，避免跨文档合并）
        document_entity = Entity(
            id=f"doc-meta-{doc_id}",
            name=f"Document[{doc_id}]",
            type=EntityType.DOCUMENT,
            properties={
                "node_type": "document_meta",
                "file_path": source_file,
                "file_type": document.metadata.get("file_extension", "unknown"),
                "file_size": document.metadata.get("file_size"),
                "md5_hash": document.metadata.get("md5_hash"),
                "upload_time": document.metadata.get("modified_time"),
                "doc_id": doc_id
            },
            source_document_id=doc_id,
            knowledge_base_id=kb_id,
            confidence=1.0,
            extraction_method="rule_mapping"
        )
        result.document_entity = document_entity
        result.entities.append(document_entity)

        # 2. 处理每个表格
        for table in document.tables:
            table_entity = Entity(
                id=f"table-{table.table_id}",
                name=f"Table[{table.table_id}]",
                type=EntityType.TABLE,
                properties={
                    "node_type": "document_meta",
                    "table_id": table.table_id,
                    "sheet_name": table.sheet_name,
                    "row_count": table.row_count,
                    "col_count": table.col_count,
                    "position_meta": table.position_meta
                },
                source_document_id=doc_id,
                knowledge_base_id=kb_id,
                confidence=1.0,
                extraction_method="rule_mapping"
            )
            result.table_entities.append(table_entity)
            result.entities.append(table_entity)

            # Document -> CONTAINS -> Table
            result.meta_relations.append(Relation(
                source_entity_id=document_entity.id,
                target_entity_id=table_entity.id,
                type=RelationType.CONTAINS,
                properties={
                    "relation_subtype": "document_contains_table",
                    "table_id": table.table_id
                },
                source_document_id=doc_id,
                knowledge_base_id=kb_id,
                confidence=1.0,
                extraction_method="rule_mapping"
            ))

            # 3. 表头映射抽取业务实体和关系
            extracted_entities, extracted_relations = self.table_mapper.map_table(table, doc_id)

            # 4. 转换为标准 Entity 并建立 name+type -> Entity ID 映射
            name_type_to_entity: Dict[Tuple[str, str], Entity] = {}
            for extracted in extracted_entities:
                entity_id = f"entity-{extracted.name}-{extracted.type.value}-{table.table_id}"
                # 清理非法字符，避免 ID 异常
                entity_id = re.sub(r"[^\w\-]", "_", entity_id)[:100]
                entity = Entity(
                    id=entity_id,
                    name=extracted.name,
                    type=extracted.type,
                    properties=extracted.properties,
                    source_document_id=doc_id,
                    source_doc_ids=[doc_id],
                    knowledge_base_id=kb_id,
                    confidence=extracted.confidence,
                    extraction_method=extracted.source
                )
                key = (entity.name, entity.type.value)
                if key not in name_type_to_entity:
                    name_type_to_entity[key] = entity
                    result.business_entities.append(entity)
                    result.entities.append(entity)

                    # Table -> HAS_ROW -> Entity
                    result.meta_relations.append(Relation(
                        source_entity_id=table_entity.id,
                        target_entity_id=entity.id,
                        type=RelationType.HAS_ROW,
                        properties={
                            "relation_subtype": "table_has_row",
                            "table_id": table.table_id
                        },
                        source_document_id=doc_id,
                        knowledge_base_id=kb_id,
                        confidence=1.0,
                        extraction_method="rule_mapping"
                    ))

                    # Entity -> MENTIONED_IN -> Document
                    result.meta_relations.append(Relation(
                        source_entity_id=entity.id,
                        target_entity_id=document_entity.id,
                        type=RelationType.MENTIONED_IN,
                        properties={
                            "relation_subtype": "entity_mentioned_in_document",
                            "doc_id": doc_id
                        },
                        source_document_id=doc_id,
                        knowledge_base_id=kb_id,
                        confidence=1.0,
                        extraction_method="rule_mapping"
                    ))

            # 5. 转换业务关系
            for extracted_rel in extracted_relations:
                source_key = (extracted_rel.source_entity_name, self._infer_entity_type_from_relation(extracted_rel, is_source=True).value)
                target_key = (extracted_rel.target_entity_name, self._infer_entity_type_from_relation(extracted_rel, is_source=False).value)

                source_entity = name_type_to_entity.get(source_key)
                # 如果 source 没找到，按名称模糊匹配（简单回退：找同名任意类型）
                if source_entity is None:
                    source_entity = self._find_entity_by_name(extracted_rel.source_entity_name, name_type_to_entity)
                target_entity = name_type_to_entity.get(target_key)
                if target_entity is None:
                    target_entity = self._find_entity_by_name(extracted_rel.target_entity_name, name_type_to_entity)

                if source_entity is None or target_entity is None:
                    continue

                relation = Relation(
                    source_entity_id=source_entity.id,
                    target_entity_id=target_entity.id,
                    type=extracted_rel.type,
                    properties=extracted_rel.properties,
                    source_document_id=doc_id,
                    knowledge_base_id=kb_id,
                    confidence=extracted_rel.confidence,
                    extraction_method=extracted_rel.source
                )
                result.business_relations.append(relation)
                result.relations.append(relation)

        # 汇总
        result.relations.extend(result.meta_relations)
        return result

    def _infer_entity_type_from_relation(self, extracted_rel: ExtractedRelation, is_source: bool) -> EntityType:
        """根据关系类型推断缺失的实体类型"""
        # 默认推断
        if is_source:
            # 来源实体通常默认 PRODUCT 或根据关系推断
            if extracted_rel.type in (RelationType.SUPPLIED_BY, RelationType.USES):
                return EntityType.PRODUCT
            if extracted_rel.type in (RelationType.PART_OF,):
                return EntityType.PROJECT
            return EntityType.PRODUCT
        else:
            if extracted_rel.type in (RelationType.SUPPLIED_BY, RelationType.COLLABORATES_WITH):
                return EntityType.ORGANIZATION
            if extracted_rel.type in (RelationType.MANAGES, RelationType.EMPLOYED_BY, RelationType.WORKS_FOR):
                return EntityType.PERSON
            if extracted_rel.type == RelationType.LOCATED_IN:
                return EntityType.LOCATION
            if extracted_rel.type == RelationType.USES:
                return EntityType.TECHNOLOGY
            return EntityType.ORGANIZATION

    @staticmethod
    def _find_entity_by_name(name: str, mapping: Dict[Tuple[str, str], Entity]) -> Optional[Entity]:
        """按名称在实体映射中查找"""
        for (entity_name, _), entity in mapping.items():
            if entity_name == name:
                return entity
        return None
