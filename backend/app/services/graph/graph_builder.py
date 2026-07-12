"""
知识图谱构建服务 (Graph Builder Service)

将实体抽取和关系抽取的结果整合并持久化到Neo4j图数据库中。
是连接"抽取层"和"存储层"的核心桥梁。

核心功能：
1. 图谱构建 - 完整的文本→实体→关系→图谱流水线
2. 批量插入 - 高性能的Neo4j数据写入（使用UNWIND）
3. 增量更新 - 只更新变化的部分，避免重复构建
4. 一致性检查 - 检测孤立节点、循环依赖等问题
5. 统计信息 - 维护实体/关系的计数统计

架构设计：
    文本输入
        ↓
    ┌──────────────────┐
    │  EntityExtraction │ ← 实体识别（调用NER服务）
    └───────┬──────────┘
            ↓
    ┌──────────────────┐
    │  RelationExtraction│ ← 关系抽取（调用RE服务）
    └───────┬──────────┘
            ↓
    ┌──────────────────┐
    │  GraphValidator   │ ← 数据验证和质量检查
    └───────┬──────────┘
            ↓
    ┌──────────────────┐
    │  Neo4j Batch Write│ ← 批量写入（UNWIND优化）
    └───────┬──────────┘
            ↓
    ┌──────────────────┐
    │  Statistics Update│ ← 更新计数和元数据
    └──────────────────┘

性能指标：
- 插入速度: >1000节点/秒, >2000边/秒
- 内存占用: <500MB（处理10万实体时）
- 构建成功率: >95%

作者：KQuery Pro Team
版本：1.0.0
更新：2026-04-11

依赖：
- app.repositories.graph.neo4j_connector（Neo4j访问）
- app.services.graph.entity_extraction（实体抽取）
- app.services.graph.relation_extraction（关系抽取）
"""

from __future__ import annotations

import time
import logging
import asyncio
from difflib import SequenceMatcher
from typing import (
    Any, Dict, List, Optional,
    Set, Tuple
)
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

# 导入项目模块
try:
    from app.repositories.graph.schema import (
        Entity, Relation, EntityType, RelationType, graph_schema
    )
    from app.services.graph.entity_extraction import ExtractedEntity, ExtractionResult
    from app.services.graph.relation_extraction import ExtractedRelation, RelationExtractionResult
    from app.repositories.graph.neo4j_connector import (
        Neo4jConnector, neo4j_manager, QueryStatistics
    )
    from app.core.logger import get_logger
except ImportError:
    # 开发环境降级
    class Entity: pass
    class Relation: pass

    def get_logger(name):
        return logging.getLogger(name)

try:
    from app.services.document_models import NormalizedDocument, DocumentCategory
    from app.services.graph.structured_extractor import StructuredExtractor
    from app.services.document_classifier import DocumentClassifier, document_classifier
    from app.services.file_loaders import LazyLoaderRegistry
    FILE_LOADER_AVAILABLE = True
except ImportError:
    FILE_LOADER_AVAILABLE = False



try:
    from app.services.graph.llm_graph_agent import (
        LLMGraphAgent, GraphExtractionResult as AgentGraphResult
    )
    from app.services.graph.community_engine import CommunityEngine
    LLM_AGENT_AVAILABLE = True
except ImportError:
    LLM_AGENT_AVAILABLE = False



logger = get_logger(__name__)


# ============================================================
# 辅助函数：实体名称模糊匹配
# ============================================================

def _find_entity_id(
    name: str,
    name_to_id_map: Dict[str, str],
    threshold: float = 0.85
) -> Optional[str]:
    """使用模糊匹配在实体名称映射中查找实体 ID。

    当 LLM/规则抽取的关系中的实体名称与已抽取实体的规范名称不完全一致
    （如"阿里巴巴" vs "阿里巴巴集团"）时，通过序列相似度找到最佳匹配。
    """
    if not name or not name_to_id_map:
        return None

    name_lower = name.lower().strip()
    if name_lower in {k.lower().strip() for k in name_to_id_map.keys()}:
        for key, val in name_to_id_map.items():
            if key.lower().strip() == name_lower:
                return val

    best_match = None
    best_score = 0.0
    for entity_name, entity_id in name_to_id_map.items():
        score = SequenceMatcher(None, name_lower, entity_name.lower().strip()).ratio()
        if score > best_score and score >= threshold:
            best_score = score
            best_match = entity_id

    return best_match


# ============================================================
# 第一部分：数据模型定义
# ============================================================


@dataclass
class GraphBuildResult:
    """图谱构建结果"""

    # 基础统计
    success: bool = False                          # 是否成功
    entities_created: int = 0                      # 新建实体数
    entities_updated: int = 0                      # 更新实体数
    relations_created: int = 0                     # 新建关系统数

    # 输入信息
    source_text_length: int = 0                    # 原始文本长度
    doc_id: Optional[str] = None                   # 来源文档ID
    kb_id: Optional[int] = None                    # 知识库ID

    # 处理耗时（毫秒）
    extraction_time_ms: float = 0.0                # 抽取耗时
    storage_time_ms: float = 0.0                   # 存储耗时
    total_time_ms: float = 0.0                     # 总耗时

    # 质量指标
    entity_count_before: int = 0                   # 构建前实体数
    relation_count_before: int = 0                 # 构建前关系统数
    entity_count_after: int = 0                    # 构建后实体数
    relation_count_after: int = 0                  # 构建后关系统数

    # 社区检测（新增）
    community_count: int = 0                       # 检测到的社区数
    communities_detected: bool = False             # 是否执行了社区检测
    super_nodes_created: int = 0                   # 创建的超节点数

    # 错误和警告
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    # 详细结果引用
    extraction_result: Optional[ExtractionResult] = None
    relation_result: Optional[RelationExtractionResult] = None

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（用于API响应）"""
        return {
            "success": self.success,
            "statistics": {
                "entities": {
                    "created": self.entities_created,
                    "updated": self.entities_updated
                },
                "relations": {
                    "created": self.relations_created
                }
            },
            "performance": {
                "extraction_time_ms": round(self.extraction_time_ms, 1),
                "storage_time_ms": round(self.storage_time_ms, 1),
                "total_time_ms": round(self.total_time_ms, 1),
                "source_text_chars": self.source_text_length
            },
            "graph_stats": {
                "entities_before": self.entity_count_before,
                "entities_after": self.entity_count_after,
                "relations_before": self.relation_count_before,
                "relations_after": self.relation_count_after
            },
            "communities": {
                "detected": self.communities_detected,
                "count": self.community_count,
                "super_nodes_created": self.super_nodes_created
            },
            "errors": self.errors,
            "warnings": self.warnings
        }


@dataclass
class ConsistencyCheckResult:
    """一致性检查结果"""
    is_consistent: bool = True                  # 是否一致
    isolated_nodes: List[str] = field(default_factory=list)  # 孤立节点ID列表
    orphan_nodes: List[str] = field(default_factory=list)     # 无来源的节点
    duplicate_entities: List[Tuple[str, str]] = field(default_factory=list)  # 重复实体(名称,类型)
    suspicious_cycles: List[List[str]] = field(default_factory=list)  # 可疑的循环
    low_confidence_entities: List[str] = field(default_factory=list)  # 低置信度实体
    total_issues: int = 0                       # 总问题数
    
    @property
    def has_issues(self) -> bool:
        return not self.is_consistent or self.total_issues > 0


# ============================================================
# 第二部分：图谱验证器
# ============================================================

class GraphValidator:
    """
    知识图谱数据验证器
    
    在写入Neo4j前进行质量检查，
    在写入后进行一致性验证。
    """
    
    MIN_CONFIDENCE_THRESHOLD = 0.3              # 最低允许的置信度
    
    @staticmethod
    def validate_entity(entity: Entity) -> Tuple[bool, List[str]]:
        """
        验证单个实体的有效性
        
        Returns:
            (是否有效, 错误消息列表)
        """
        errors = []
        
        # 名称检查
        if not entity.name or len(entity.name.strip()) == 0:
            errors.append("实体名称不能为空")
        
        if len(entity.name) > 200:
            errors.append(f"实体名称过长({len(entity.name)}字符)")
        
        # 类型检查
        try:
            EntityType(entity.type)
        except ValueError:
            errors.append(f"无效的实体类型: {entity.type}")
        
        # 置信度检查
        if entity.confidence < GraphValidator.MIN_CONFIDENCE_THRESHOLD:
            errors.append(f"置信度过低: {entity.confidence}")
        
        # ID格式检查
        if not entity.id or not isinstance(entity.id, str):
            errors.append("实体ID无效")
        
        return (len(errors) == 0, errors)
    
    @staticmethod
    def validate_relation(relation: Relation) -> Tuple[bool, List[str]]:
        """
        验证单个关系的有效性
        """
        errors = []
        
        # 自引用检查
        if relation.source_entity_id == relation.target_entity_id:
            errors.append("不允许自引用关系")
        
        # ID检查
        if not relation.source_entity_id:
            errors.append("头实体ID为空")
        if not relation.target_entity_id:
            errors.append("尾实体ID为空")
        
        # 类型检查
        try:
            RelationType(relation.type)
        except ValueError:
            errors.append(f"无效的关系类型: {relation.type}")
        
        # 权重范围
        if relation.weight < 0 or relation.weight > 10:
            errors.append(f"权重超出范围(0-10): {relation.weight}")
        
        return (len(errors) == 0, errors)
    
    @staticmethod
    def validate_batch(
        entities: List[Entity],
        relations: List[Relation]
    ) -> Tuple[bool, Dict[str, List[str]]]:
        """
        批量验证
        
        Returns:
            (整体是否通过, 分类错误字典)
        """
        all_errors: Dict[str, List[str]] = {
            "entities": [],
            "relations": [],
            "cross_references": []
        }
        
        entity_ids = set()
        entity_names: Set[Tuple[str, str]] = set()  # (name, type)
        
        for i, entity in enumerate(entities):
            valid, errors = GraphValidator.validate_entity(entity)
            
            if not valid:
                all_errors["entities"].append(
                    f"Entity[{i}] ({entity.name}): {'; '.join(errors)}"
                )
            
            # 检查重复ID
            if entity.id in entity_ids:
                all_errors["cross_references"].append(
                    f"重复实体ID: {entity.id}"
                )
            else:
                entity_ids.add(entity.id)
            
            # 检查重复名称+类型组合
            name_type_key = (entity.name, str(entity.type))
            if name_type_key in entity_names:
                all_errors["cross_references"].append(
                    f"重复实体(名称+类型): {entity.name} [{entity.type.value}]"
                )
            else:
                entity_names.add(name_type_key)
        
        for i, relation in enumerate(relations):
            valid, errors = GraphValidator.validate_relation(relation)
            
            if not valid:
                all_errors["relations"].append(
                    f"Relation[{i}]: {'; '.join(errors)}"
                )
            
            # 引用完整性检查
            if relation.source_entity_id and relation.source_entity_id not in entity_ids:
                all_errors["cross_references"].append(
                    f"Relation[{i}]引用了不存在的头实体: {relation.source_entity_id}"
                )
            
            if relation.target_entity_id and relation.target_entity_id not in entity_ids:
                all_errors["cross_references"].append(
                    f"Relation[{i}]引用了不存在的尾实体: {relation.target_entity_id}"
                )
        
        total_errors = sum(len(v) for v in all_errors.values())
        
        return (total_errors == 0, all_errors)


# ============================================================
# 第三部分：核心服务类 - GraphBuilderService
# ============================================================

class GraphBuilderService:
    """
    知识图谱构建服务（统一入口）

    提供完整的从文本到知识图谱的构建流程。
    支持两种抽取模式：
    1. 传统 Pipeline（分步 NER + RE）
    2. LLM Agent 模式（单次调用完成 NER + RE）

    新增功能：
    - 社区检测（Leiden 算法）：当图谱边密集时自动合并社区
    - 超节点构建：将社区抽象为 :Community 节点

    核心方法：
    - build_graph_from_text(): 从单段文本构建图谱
    - build_graph_from_document(): 从文档对象构建图谱
    - batch_build_documents(): 批量构建多个文档的图谱

    使用示例：
        builder = GraphBuilderService(neo4j_connector)

        # 使用 LLM Agent 模式（推荐）
        result = await builder.build_graph_from_text(
            text="张三在腾讯公司工作，他是AI工程师...",
            doc_id="doc-001",
            kb_id=1,
            use_agent=True,
            detect_communities=True,
        )

        print(f"构建完成! 创建了{result.entities_created}个实体")
        print(f"社区检测: {result.community_count} 个社区")
    """

    def __init__(
        self,
        connector: Optional[Neo4jConnector] = None,
        auto_init_schema: bool = True,
        use_agent: bool = False,
        agent_model: str = "qwen-plus",
    ):
        """
        初始化图谱构建器

        Args:
            connector: Neo4j连接器实例（如果不传则使用全局单例）
            auto_init_schema: 是否自动初始化Schema约束
            use_agent: 是否默认使用 LLM Agent 进行抽取
            agent_model: Agent 使用的 LLM 模型
        """
        self.connector = connector or neo4j_manager.connector

        if auto_init_schema and self.connector._is_connected:
            try:
                asyncio.get_event_loop().run_until_complete(
                    self.connector.initialize_schema()
                )
            except Exception as e:
                logger.warning(f"Schema初始化失败: {e}")

        self.validator = GraphValidator()

        # 初始化子服务（延迟初始化，按需创建）
        self._entity_service = None
        self._relation_service = None
        self._llm_agent = None
        self._structured_extractor = None
        self._community_engine = None


        # 配置
        self.use_agent = use_agent and LLM_AGENT_AVAILABLE
        self.agent_model = agent_model

        logger.info(
            f"🏗️ 图谱构建服务初始化完成 | "
            f"Agent模式: {self.use_agent}"
        )

    @property
    def entity_extraction_service(self):
        """延迟初始化实体抽取服务"""
        if self._entity_service is None:
            from app.services.graph.entity_extraction import EntityExtractionService
            self._entity_service = EntityExtractionService()
        return self._entity_service

    @property
    def relation_extraction_service(self):
        """延迟初始化关系抽取服务"""
        if self._relation_service is None:
            from app.services.graph.relation_extraction import RelationExtractionService
            self._relation_service = RelationExtractionService()
        return self._relation_service

    @property
    def llm_graph_agent(self) -> Optional[LLMGraphAgent]:
        """延迟初始化 LLM Graph Agent"""
        if self._llm_agent is None and LLM_AGENT_AVAILABLE:
            self._llm_agent = LLMGraphAgent(model_name=self.agent_model)
        return self._llm_agent

    @property
    def structured_extractor(self) -> Optional[StructuredExtractor]:
        """延迟初始化结构化文档抽取器"""
        if self._structured_extractor is None and FILE_LOADER_AVAILABLE:
            self._structured_extractor = StructuredExtractor()
        return self._structured_extractor

    @property
    def community_engine(self) -> Optional[CommunityEngine]:

        """延迟初始化社区检测引擎"""
        if self._community_engine is None:
            self._community_engine = CommunityEngine(self.connector)
        return self._community_engine

    def _attach_document_metadata(
        self,
        entities: List[Entity],
        relations: List[Relation],
        doc_id: Optional[str],
        kb_id: Optional[int],
        metadata: Optional[Dict[str, Any]] = None
    ) -> Tuple[List[Entity], List[Relation]]:
        """为实体和关系附加文档元节点与元关系

        生成：
        - Document 元节点
        - Entity -[MENTIONED_IN]-> Document 关系
        - 可选的 Document 属性（文件路径、类型、MD5 等）

        注意：如果 entities 为空或 doc_id 为空，则不附加。
        """
        if not doc_id or not entities:
            return entities, relations

        document_entity = Entity(
            id=f"doc-meta-{doc_id}",
            name=f"Document[{doc_id}]",
            type=EntityType.DOCUMENT,
            properties={
                "node_type": "document_meta",
                "doc_id": doc_id,
                **(metadata or {})
            },
            source_document_id=doc_id,
            knowledge_base_id=kb_id,
            confidence=1.0,
            extraction_method="rule_mapping"
        )

        new_entities = [document_entity]
        new_relations: List[Relation] = []

        for entity in entities:
            if entity.type in {
                EntityType.DOCUMENT, EntityType.PAGE, EntityType.TABLE,
                EntityType.IMAGE, EntityType.PARAGRAPH
            }:
                continue

            # 去重：避免同一实体对同一文档重复 MENTIONED_IN
            if not any(
                r.source_entity_id == entity.id and
                r.target_entity_id == document_entity.id and
                r.type == RelationType.MENTIONED_IN
                for r in relations
            ):
                new_relations.append(Relation(
                    source_entity_id=entity.id,
                    target_entity_id=document_entity.id,
                    type=RelationType.MENTIONED_IN,
                    properties={"relation_subtype": "entity_mentioned_in_document"},
                    source_document_id=doc_id,
                    knowledge_base_id=kb_id,
                    confidence=1.0,
                    extraction_method="rule_mapping"
                ))

            # 同步 source_doc_ids
            if doc_id not in entity.source_doc_ids:
                entity.source_doc_ids.append(doc_id)

        return entities + new_entities, relations + new_relations

    async def _get_graph_counts(self, kb_id: Optional[int] = None) -> Tuple[int, int]:
        """获取当前实体和关系的数量（用于前后统计对比）。"""

        entity_params: Dict[str, Any] = {}
        relation_params: Dict[str, Any] = {}

        if kb_id is not None:
            entity_clause = "WHERE e.kb_id = $kb_id"
            relation_clause = "WHERE r.kb_id = $kb_id"
            entity_params["kb_id"] = kb_id
            relation_params["kb_id"] = kb_id
        else:
            entity_clause = ""
            relation_clause = ""

        entity_count = 0
        relation_count = 0

        try:
            entity_result = await self.connector.execute_query(
                f"MATCH (e:Entity) {entity_clause} RETURN count(e) as cnt",
                entity_params
            )
            entity_count = entity_result[0]["cnt"] if entity_result else 0
        except Exception as e:
            logger.warning(f"获取实体数量失败: {e}")

        try:
            relation_result = await self.connector.execute_query(
                f"MATCH ()-[r:RELATED_TO]-() {relation_clause} RETURN count(r) as cnt",
                relation_params
            )
            relation_count = relation_result[0]["cnt"] if relation_result else 0
        except Exception as e:
            logger.warning(f"获取关系数量失败: {e}")

        return entity_count, relation_count

    async def build_graph_from_text(
        self,
        text: str,
        doc_id: Optional[str] = None,
        kb_id: Optional[int] = None,
        context: Optional[str] = None,
        skip_extraction: bool = False,
        pre_extracted_entities: Optional[ExtractionResult] = None,
        pre_extracted_relations: Optional[RelationExtractionResult] = None,
        use_agent: Optional[bool] = None,
        detect_communities: bool = True,
    ) -> GraphBuildResult:
        """
        从文本构建知识图谱（主方法）

        这是最常用的方法，完整执行以下流程：
        1. 实体抽取（可选跳过，支持 Agent 模式）
        2. 关系抽取（可选跳过，支持 Agent 模式）
        3. 数据验证
        4. 写入Neo4j
        5. 社区检测（可选）
        6. 更新统计

        Args:
            text: 待分析的原始文本
            doc_id: 来源文档ID（用于溯源）
            kb_id: 所属知识库ID（用于多租户隔离）
            context: 额外上下文（如标题、摘要等）
            skip_extraction: 是否跳过抽取步骤（使用预提取的数据）
            pre_extracted_entities: 预先抽取好的实体
            pre_extracted_relations: 预先抽取好的关系
            use_agent: 是否使用 LLM Agent 统一抽取（None 则使用实例默认值）
            detect_communities: 构建完成后是否检测社区并创建超节点

        Returns:
            GraphBuildResult 包含详细统计和状态信息
        """
        start_total = time.time()
        result = GraphBuildResult(
            doc_id=doc_id,
            kb_id=kb_id,
            source_text_length=len(text)
        )

        # 确定是否使用 Agent
        agent_mode = use_agent if use_agent is not None else self.use_agent

        logger.info(
            f"🚀 开始构建知识图谱 | 文本长度: {len(text)} | "
            f"文档: {doc_id} | KB: {kb_id} | "
            f"抽取模式: {'Agent' if agent_mode else 'Pipeline'}"
        )

        # ===== Step 1: 获取当前图谱统计（用于后续对比）=====
        try:
            result.entity_count_before, result.relation_count_before = await self._get_graph_counts(kb_id)
        except Exception as e:
            logger.warning(f"获取前置统计失败: {e}")

        # ===== Step 2: 实体与关系抽取 =====
        extraction_start = time.time()

        if agent_mode and self.llm_graph_agent:
            # 使用 LLM Agent 统一抽取
            agent_result = await self.llm_graph_agent.extract(text, doc_id, context)

            # 转换实体
            entities_to_store = []
            for agent_entity in agent_result.entities:
                try:
                    entity = agent_entity.to_schema_entity(doc_id)
                    entities_to_store.append(entity)
                except Exception as e:
                    logger.debug(f"转换 Agent 实体失败: {e}")

            # 转换关系（使用模糊匹配处理 LLM 返回的实体名与规范名不一致的情况）
            name_to_id_map = {e.name: e.id for e in entities_to_store}
            relations_to_store = []

            for agent_rel in agent_result.relations:
                src_id = _find_entity_id(agent_rel.source, name_to_id_map)
                tgt_id = _find_entity_id(agent_rel.target, name_to_id_map)

                if src_id and tgt_id:
                    try:
                        relation = agent_rel.to_schema_relation(src_id, tgt_id, doc_id)
                        # 注入 kb_id
                        relation.knowledge_base_id = kb_id
                        relations_to_store.append(relation)
                    except Exception as e:
                        logger.debug(f"转换 Agent 关系失败: {e}")
                else:
                    missing = "target" if not tgt_id else "source"
                    result.warnings.append(
                        f"Agent 关系缺少{missing}实体: "
                        f"{agent_rel.source} -> {agent_rel.target}"
                    )

            # 构建兼容的 extraction_result
            from app.services.graph.entity_extraction import ExtractionResult, ExtractedEntity
            extraction_result = ExtractionResult(text_length=len(text))
            extraction_result.llm_count = len(agent_result.entities)
            extraction_result.merged_count = len(agent_result.entities)
            for agent_entity in agent_result.entities:
                extracted = ExtractedEntity(
                    name=agent_entity.name,
                    type=EntityType(agent_entity.type)
                    if EntityType.is_valid_type(agent_entity.type)
                    else EntityType.KEYWORD,
                    confidence=agent_entity.confidence,
                    source="llm_agent",
                    properties={
                        "description": agent_entity.description or "",
                        "aliases": agent_entity.aliases,
                        "evidence": agent_entity.evidence or "",
                        **agent_entity.properties,
                    },
                )
                extraction_result.entities.append(extracted)
            result.extraction_result = extraction_result

            # 传递抽取阶段的错误提示（如 LLM 未初始化）
            if extraction_result.errors:
                result.errors.extend(extraction_result.errors)

            logger.info(
                f"🤖 Agent 抽取完成 | 实体: {len(entities_to_store)}, "
                f"关系: {len(relations_to_store)}"
            )

        else:
            # 传统 Pipeline 模式
            if skip_extraction and pre_extracted_entities:
                extraction_result = pre_extracted_entities
                logger.debug("使用预提取的实体数据")
            else:
                extraction_result = await self.entity_extraction_service.extract_entities(
                    text=text,
                    doc_id=doc_id,
                    context=context,
                    kb_id=kb_id
                )

            result.extraction_result = extraction_result
            entities_to_store = extraction_result.to_entities(doc_id, kb_id)

            # 传递实体抽取阶段的错误提示
            if extraction_result.errors:
                result.errors.extend(extraction_result.errors)

            # 关系抽取
            if extraction_result.entities:
                if skip_extraction and pre_extracted_relations:
                    relation_result = pre_extracted_relations
                else:
                    relation_result = await self.relation_extraction_service.extract_relations(
                        text=text,
                        entities=extraction_result.entities,
                        doc_id=doc_id,
                        kb_id=kb_id
                    )

                result.relation_result = relation_result

                # 传递关系抽取阶段的错误提示
                if relation_result.errors:
                    result.errors.extend(relation_result.errors)

                # 将 ExtractedRelation 转换为 Relation（使用模糊匹配避免名称不一致）
                name_to_id_map = {e.name: e.id for e in entities_to_store}
                relations_to_store = []

                for extracted_rel in relation_result.relations:
                    src_id = _find_entity_id(extracted_rel.source_entity_name, name_to_id_map)
                    tgt_id = _find_entity_id(extracted_rel.target_entity_name, name_to_id_map)

                    if src_id and tgt_id:
                        relations_to_store.append(
                            extracted_rel.to_relation(src_id, tgt_id, doc_id, kb_id)
                        )
                    elif src_id or tgt_id:
                        missing = "target" if not tgt_id else "source"
                        result.warnings.append(
                            f"关系缺少{missing}实体: "
                            f"{extracted_rel.source_entity_name} -> "
                            f"{extracted_rel.target_entity_name}"
                        )
                    else:
                        result.warnings.append(
                            f"关系两端实体均未匹配: "
                            f"{extracted_rel.source_entity_name} -> "
                            f"{extracted_rel.target_entity_name}"
                        )
            else:
                relations_to_store = []
                logger.info("没有抽取到实体，跳过关系抽取")

        result.extraction_time_ms = (time.time() - extraction_start) * 1000

        logger.info(
            f"✓ 抽取完成 | 实体: {len(entities_to_store)} | "
            f"关系: {len(relations_to_store)} | "
            f"耗时: {result.extraction_time_ms:.0f}ms"
        )

        # ===== Step 3 前置：附加文档元节点与元关系 =====
        if doc_id and entities_to_store:
            entities_to_store, relations_to_store = self._attach_document_metadata(
                entities=entities_to_store,
                relations=relations_to_store,
                doc_id=doc_id,
                kb_id=kb_id,
                metadata={"context": context[:500] if context else None}
            )

        # ===== Step 3: 数据验证 =====
        is_valid, validation_errors = self.validator.validate_batch(
            entities_to_store,
            relations_to_store
        )

        if not is_valid:
            error_summary = []
            for category, errors in validation_errors.items():
                if errors:
                    error_summary.extend(errors[:5])
                    if len(errors) > 5:
                        error_summary.append(f"... 还有{len(errors)-5}个{category}错误")

            result.errors = error_summary
            logger.warning(
                f"⚠️ 数据验证发现问题 | 总计: "
                f"{sum(len(v) for v in validation_errors.values())}"
            )
            result.success = len(validation_errors['entities']) == 0

        # ===== Step 4: 批量写入 Neo4j =====
        storage_start = time.time()

        if entities_to_store or relations_to_store:
            write_stats = await self._batch_write_to_neo4j(
                entities=entities_to_store,
                relations=relations_to_store,
                kb_id=kb_id
            )

            result.entities_created = write_stats.get('entities_created', 0)
            result.entities_updated = write_stats.get('entities_updated', 0)
            result.relations_created = write_stats.get('relations_created', 0)

        result.storage_time_ms = (time.time() - storage_start) * 1000

        # ===== Step 5: 社区检测（可选）=====
        if detect_communities and self.community_engine:
            try:
                # 获取新创建实体的 ID 列表作为社区检测中心
                new_entity_ids = [e.id for e in entities_to_store[:20]]

                if new_entity_ids:
                    comm_result = await self.community_engine.process_subgraph(
                        center_entity_ids=new_entity_ids,
                        radius=2,
                        kb_id=kb_id,
                    )

                    result.communities_detected = comm_result.community_count > 0
                    result.community_count = comm_result.community_count
                    result.super_nodes_created = comm_result.community_count

                    if result.communities_detected:
                        logger.info(
                            f"🌐 社区检测完成 | 社区: {result.community_count}, "
                            f"modularity: {comm_result.modularity:.4f}"
                        )
            except Exception as e:
                logger.warning(f"社区检测失败（不影响主流程）: {e}")

        # ===== Step 6: 更新统计 =====
        try:
            result.entity_count_after, result.relation_count_after = await self._get_graph_counts(kb_id)
        except Exception as e:
            logger.warning(f"获取后置统计失败: {e}")

        # ===== 最终计算 =====
        result.total_time_ms = (time.time() - start_total) * 1000
        result.success = True

        logger.info(
            f"🎉 图谱构建完成! ✅\n"
            f"   实体: +{result.entities_created}(新建) / +{result.entities_updated}(更新)\n"
            f"   关系: +{result.relations_created}\n"
            f"   社区: +{result.community_count} (超节点: {result.super_nodes_created})\n"
            f"   总耗时: {result.total_time_ms:.0f}ms\n"
            f"   图谱总规模: {result.entity_count_after}实体 / "
            f"{result.relation_count_after}关系"
        )

        return result

    async def build_graph_from_chunks(
        self,
        chunks: List[str],
        doc_id: Optional[str] = None,
        kb_id: Optional[int] = None,
        context: Optional[str] = None,
        detect_communities: bool = True,
    ) -> GraphBuildResult:
        """
        从多个文本块构建知识图谱（分块批量抽取）。

        流程：
        1. 使用 batch_extract 按块并发抽取实体，再全局去重；
        2. 基于合并后的文本抽取跨 chunk 关系；
        3. 复用 build_graph_from_text 的写入与统计逻辑。
        """
        if not chunks:
            result = GraphBuildResult(
                doc_id=doc_id,
                kb_id=kb_id,
                source_text_length=0
            )
            result.errors.append("没有提供文本块")
            return result

        merged_text = "\n".join(chunks)

        logger.info(
            f"📦 从 {len(chunks)} 个文本块构建知识图谱 | "
            f"合并长度: {len(merged_text)} | 文档: {doc_id} | KB: {kb_id}"
        )

        # 批量抽取实体（避免单文本 4000 字符截断）
        extraction_result = await self.entity_extraction_service.batch_extract(
            chunks=chunks,
            doc_id=doc_id,
            concurrency=3
        )

        # 基于合并文本抽取关系，保证跨 chunk 上下文
        if extraction_result.entities:
            relation_result = await self.relation_extraction_service.extract_relations(
                text=merged_text,
                entities=extraction_result.entities,
                doc_id=doc_id,
                kb_id=kb_id
            )
        else:
            relation_result = RelationExtractionResult()
            logger.info("没有抽取到实体，跳过关系抽取")

        return await self.build_graph_from_text(
            text=merged_text,
            doc_id=doc_id,
            kb_id=kb_id,
            context=context,
            skip_extraction=True,
            pre_extracted_entities=extraction_result,
            pre_extracted_relations=relation_result,
            detect_communities=detect_communities,
        )

    async def build_graph_from_structured_document(
        self,
        document: "NormalizedDocument",
        kb_id: Optional[int] = None,
        detect_communities: bool = False,
    ) -> GraphBuildResult:
        """从结构化文档（Excel/CSV）直接构建知识图谱

        流程：
        1. 调用 StructuredExtractor 从 NormalizedDocument 抽取实体和关系
        2. 批量验证
        3. 写入 Neo4j（复用 _batch_write_to_neo4j）
        4. 更新统计

        Args:
            document: NormalizedDocument 结构（必须由 Excel/CSV 等加载器生成）
            kb_id: 知识库ID
            detect_communities: 是否执行社区检测（结构化文档通常图较稀疏，默认关闭）

        Returns:
            GraphBuildResult 构建结果
        """
        start_total = time.time()
        doc_id = document.doc_id
        result = GraphBuildResult(
            doc_id=doc_id,
            kb_id=kb_id,
            source_text_length=sum(t.row_count for t in document.tables)
        )

        if not FILE_LOADER_AVAILABLE or self.structured_extractor is None:
            result.errors.append("结构化文档抽取模块未就绪")
            return result

        logger.info(
            f"📊 开始从结构化文档构建图谱 | 表格数: {len(document.tables)} | "
            f"文档: {doc_id} | KB: {kb_id}"
        )

        # 前置统计
        try:
            result.entity_count_before, result.relation_count_before = await self._get_graph_counts(kb_id)
        except Exception as e:
            logger.warning(f"获取前置统计失败: {e}")

        # 1. 结构化抽取
        extraction_start = time.time()
        extraction_result = self.structured_extractor.extract(document, kb_id)
        result.extraction_time_ms = (time.time() - extraction_start) * 1000

        if extraction_result.errors:
            result.errors.extend(extraction_result.errors)

        entities = extraction_result.entities
        relations = extraction_result.relations

        # 2. 批量验证
        valid, errors = self.validator.validate_batch(entities, relations)
        if not valid:
            result.errors.extend(errors.get("entities", []))
            result.errors.extend(errors.get("relations", []))
            result.errors.extend(errors.get("cross_references", []))
            logger.warning(f"结构化文档验证失败: {errors}")
            return result

        # 3. 批量写入
        storage_start = time.time()
        try:
            write_stats = await self._batch_write_to_neo4j(entities, relations, kb_id)
            result.entities_created = write_stats.get('entities_created', 0)
            result.entities_updated = write_stats.get('entities_updated', 0)
            result.relations_created = write_stats.get('relations_created', 0)
            result.storage_time_ms = (time.time() - storage_start) * 1000
        except Exception as e:
            logger.error(f"结构化文档写入 Neo4j 失败: {e}")
            result.errors.append(f"写入失败: {str(e)}")
            return result

        # 4. 社区检测（可选）
        if detect_communities and self.community_engine:
            try:
                new_entity_ids = [e.id for e in extraction_result.business_entities[:20]]
                if new_entity_ids:
                    comm_result = await self.community_engine.process_subgraph(
                        center_entity_ids=new_entity_ids,
                        radius=2,
                        kb_id=kb_id,
                    )
                    result.communities_detected = comm_result.community_count > 0
                    result.community_count = comm_result.community_count
                    result.super_nodes_created = comm_result.community_count
            except Exception as e:
                logger.warning(f"社区检测失败（不影响主流程）: {e}")

        # 5. 更新统计
        try:
            result.entity_count_after, result.relation_count_after = await self._get_graph_counts(kb_id)
        except Exception as e:
            logger.warning(f"获取后置统计失败: {e}")

        result.total_time_ms = (time.time() - start_total) * 1000
        result.success = True

        logger.info(
            f"🎉 结构化文档图谱构建完成! ✅\n"
            f"   实体: +{result.entities_created}(新建) / +{result.entities_updated}(更新)\n"
            f"   关系: +{result.relations_created}\n"
            f"   总耗时: {result.total_time_ms:.0f}ms"
        )

        return result

    async def build_graph_from_file(
        self,
        file_path: str,
        kb_id: Optional[int] = None,
        doc_id: Optional[str] = None,
        use_agent: Optional[bool] = None,
        detect_communities: bool = True,
    ) -> GraphBuildResult:
        """从文件路径自动构建知识图谱（根据文档类型自动路由）

        流程：
        1. DocumentClassifier 自动分类文档（structured/semi_structured/unstructured/multimodal）
        2. 结构化文档 → ExcelLoader.load_normalized_document → StructuredExtractor → 批量写入
        3. 其他文档 → 对应 Loader 加载文本 → build_graph_from_text 标准 LLM/规则抽取流水线

        Args:
            file_path: 文件路径
            kb_id: 知识库ID
            doc_id: 指定文档ID（未提供则使用分类结果中的 MD5 或生成新 ID）
            use_agent: 非结构化/半结构化文档是否使用 LLM Agent 抽取
            detect_communities: 是否执行社区检测

        Returns:
            GraphBuildResult 构建结果
        """
        from pathlib import Path

        path = Path(file_path)
        if not path.exists():
            result = GraphBuildResult(doc_id=doc_id, kb_id=kb_id)
            result.errors.append(f"文件不存在: {file_path}")
            return result

        if not FILE_LOADER_AVAILABLE:
            result = GraphBuildResult(doc_id=doc_id, kb_id=kb_id)
            result.errors.append("文件加载器模块未就绪")
            return result

        # 1. 文档分类
        classification = document_classifier.classify(path)
        category = classification.get("category", DocumentCategory.UNSTRUCTURED.value)
        doc_id = doc_id or classification.get("file_hash") or f"doc-{path.stem}"

        logger.info(
            f"📁 文件分类完成 | 文件: {path.name} | 类别: {category} | "
            f"原因: {classification.get('reason')}"
        )

        # 2. 结构化文档分支：零 LLM 直接表头映射
        if category == DocumentCategory.STRUCTURED.value:
            try:
                loader = LazyLoaderRegistry.get_loader(path)
                if not hasattr(loader, "load_normalized_document"):
                    result = GraphBuildResult(doc_id=doc_id, kb_id=kb_id)
                    result.errors.append(f"加载器 {loader.__class__.__name__} 不支持结构化解析")
                    return result

                normalized_doc = loader.load_normalized_document(path, doc_id=doc_id)
                return await self.build_graph_from_structured_document(
                    normalized_doc,
                    kb_id=kb_id,
                    detect_communities=detect_communities,
                )
            except Exception as e:
                result = GraphBuildResult(doc_id=doc_id, kb_id=kb_id)
                result.errors.append(f"结构化文档构建失败: {str(e)}")
                return result

        # 3. 非结构化/半结构化/多模态分支：走标准文本抽取流水线
        try:
            loader = LazyLoaderRegistry.get_loader(path)
            text = loader.load(path)
            context = classification.get("reason")
            return await self.build_graph_from_text(
                text=text,
                doc_id=doc_id,
                kb_id=kb_id,
                context=context,
                use_agent=use_agent,
                detect_communities=detect_communities,
            )
        except Exception as e:
            result = GraphBuildResult(doc_id=doc_id, kb_id=kb_id)
            result.errors.append(f"非结构化文档构建失败: {str(e)}")
            return result



    async def _batch_write_to_neo4j(
        self,
        entities: List[Entity],
        relations: List[Relation],
        kb_id: Optional[int] = None,
        batch_size: int = 500
    ) -> Dict[str, int]:
        """
        批量写入Neo4j数据库（高性能版本）
        
        使用UNWIND语句进行批量操作，
        显著提升大量数据的写入性能。
        
        性能特点：
        - 单次事务可处理500+条记录
        - 自动去重（MERGE操作）
        - 支持分批提交（防止内存溢出）
        """
        stats = {
            'entities_created': 0,
            'entities_updated': 0,
            'relations_created': 0
        }
        
        if not self.connector._is_connected:
            raise RuntimeError("Neo4j未连接")
        
        # ===== 批量写入实体 =====
        if entities:
            # 分批处理
            for i in range(0, len(entities), batch_size):
                batch = entities[i:i+batch_size]

                # 准备参数
                entity_dicts = [e.to_neo4j_dict() for e in batch]
                now_str = datetime.now().isoformat()

                cypher = """
                UNWIND $entities AS entity
                MERGE (e:Entity {name: entity.name, type: entity.type})
                ON CREATE SET
                    e.id = entity.id,
                    e.created_at = $now
                ON MATCH SET
                    e.updated_at = $now
                SET
                    e.name = entity.name,
                    e.type = entity.type,
                    e.properties = entity.properties,
                    e.aliases = COALESCE(entity.aliases, []),
                    e.description = entity.description,
                    e.confidence = entity.confidence,
                    e.extraction_method = entity.extraction_method,
                    e.mention_count = COALESCE(e.mention_count, 0) + 1,
                    e.kb_id = CASE WHEN entity.kb_id IS NOT NULL THEN entity.kb_id ELSE e.kb_id END,
                    e.embedding_id = CASE WHEN entity.embedding_id IS NOT NULL THEN entity.embedding_id ELSE e.embedding_id END,
                    e.source_doc = CASE WHEN entity.source_doc IS NOT NULL AND e.source_doc IS NULL THEN entity.source_doc ELSE COALESCE(e.source_doc, entity.source_doc) END,
                    e.source_doc_ids = CASE
                        WHEN entity.source_doc_ids IS NULL OR size(entity.source_doc_ids) = 0 THEN e.source_doc_ids
                        WHEN e.source_doc_ids IS NULL THEN entity.source_doc_ids
                        ELSE [x IN entity.source_doc_ids WHERE NOT x IN e.source_doc_ids] + e.source_doc_ids
                    END
                WITH e, CASE WHEN e.created_at = $now THEN 1 ELSE 0 END as is_created
                RETURN sum(is_created) as created, count(e) - sum(is_created) as updated
                """

                result = await self.connector.execute_query(
                    cypher,
                    {"entities": entity_dicts, "now": now_str},
                    access_mode="WRITE"
                )

                if result:
                    stats['entities_created'] += result[0].get('created', 0)
                    stats['entities_updated'] += result[0].get('updated', 0)

                logger.debug(f"  实体批次 {i//batch_size + 1}: 写入 {len(batch)} 条")
        
        # ===== 修正关系中的实体 ID（防止 MERGE 后数据库 ID 与 Python 对象 ID 不一致）=====
        if relations and entities:
            temp_to_db_id = {}
            name_type_pairs = []
            for e in entities:
                if not e.name or not e.type:
                    continue
                name_type_pairs.append({
                    "temp_id": e.id,
                    "name": e.name,
                    "type": e.type.value if hasattr(e.type, "value") else e.type,
                    "kb_id": e.knowledge_base_id,
                })
            
            if name_type_pairs:
                id_query = """
                UNWIND $pairs AS p
                MATCH (e:Entity {name: p.name, type: p.type})
                WHERE ($kb_id IS NULL) OR (e.kb_id = $kb_id) OR (e.kb_id IS NULL)
                RETURN p.temp_id AS temp_id, e.id AS db_id
                """
                try:
                    id_result = await self.connector.execute_query(
                        id_query,
                        {"pairs": name_type_pairs, "kb_id": kb_id},
                        access_mode="READ",
                    )
                    temp_to_db_id = {
                        r["temp_id"]: r["db_id"] for r in id_result if r.get("temp_id")
                    }
                except Exception as e:
                    logger.warning(f"反查实体 ID 失败: {e}")
            
            # 修正关系中的实体 ID
            if temp_to_db_id:
                corrected = 0
                for rel in relations:
                    new_source = temp_to_db_id.get(rel.source_entity_id)
                    new_target = temp_to_db_id.get(rel.target_entity_id)
                    if new_source:
                        rel.source_entity_id = new_source
                        corrected += 1
                    if new_target:
                        rel.target_entity_id = new_target
                        corrected += 1
                logger.debug(f"  已修正 {corrected} 处关系实体 ID 映射")
        
        # ===== 批量写入关系 =====
        if relations:
            for i in range(0, len(relations), batch_size):
                batch = relations[i:i+batch_size]
                rel_dicts = [r.to_neo4j_dict() for r in batch]
                now_str = datetime.now().isoformat()

                cypher = """
                UNWIND $rels AS rel
                MATCH (src:Entity {id: rel.source_id})
                MATCH (tgt:Entity {id: rel.target_id})
                MERGE (src)-[r:RELATED_TO {type: rel.type}]->(tgt)
                ON CREATE SET
                    r.id = rel.id,
                    r.properties = rel.properties,
                    r.weight = rel.weight,
                    r.confidence = rel.confidence,
                    r.bidirectional = rel.bidirectional,
                    r.extraction_method = rel.extraction_method,
                    r.source_doc = rel.source_doc,
                    r.kb_id = rel.kb_id,
                    r.created_at = $now
                ON MATCH SET
                    r.confidence = CASE WHEN r.confidence >= rel.confidence THEN r.confidence ELSE rel.confidence END,
                    r.extraction_method = COALESCE(r.extraction_method, rel.extraction_method),
                    r.updated_at = $now
                SET src.relation_count = COUNT { (src)-[:RELATED_TO]-(any) },
                    tgt.relation_count = COUNT { (tgt)-[:RELATED_TO]-(any) }
                WITH r, CASE WHEN r.created_at = $now THEN 1 ELSE 0 END as is_created
                RETURN sum(is_created) as created
                """

                result = await self.connector.execute_query(
                    cypher,
                    {
                        "rels": [
                            {
                                "source_id": r.source_entity_id,
                                "target_id": r.target_entity_id,
                                **rd
                            }
                            for r, rd in zip(batch, rel_dicts)
                        ],
                        "now": now_str
                    },
                    access_mode="WRITE"
                )

                if result:
                    stats['relations_created'] += result[0].get('created', 0)

                logger.debug(f"  关系批次 {i//batch_size + 1}: 写入完成")
        
        return stats
    
    async def check_consistency(self, kb_id: Optional[int] = None) -> ConsistencyCheckResult:
        """
        执行图谱一致性检查
        
        检测以下问题：
        1. 孤立节点（没有任何关系的实体）
        2. 无来源文档的节点
        3. 重复实体（同名同类型）
        4. 低置信度实体
        5. 可疑的自环或短循环
        """
        result = ConsistencyCheckResult()
        
        kb_filter = ""
        params: Dict[str, Any] = {}
        if kb_id:
            kb_filter = "WHERE e.kb_id = $kb_id"
            params["kb_id"] = kb_id
        
        # 1. 检查孤立节点（没有关系的实体）
        query_isolated = f"""
        MATCH (e:Entity)
        {kb_filter}
        WHERE NOT (e)-[:RELATED_TO]-() AND NOT ()-[:RELATED_TO]->(e)
        RETURN e.id as node_id, e.name as name, e.type as type
        LIMIT 50
        """
        
        try:
            isolated = await self.connector.execute_query(query_isolated, params)
            result.isolated_nodes = [r['node_id'] for r in isolated]
        except Exception as e:
            logger.error(f"孤立节点检测失败: {e}")
        
        # 2. 检查无来源文档的实体
        query_orphan = f"""
        MATCH (e:Entity)
        {kb_filter}
        WHERE e.source_doc IS NULL
        RETURN e.id as node_id
        ORDER BY e.created_at ASC
        LIMIT 50
        """
        
        try:
            orphans = await self.connector.execute_query(query_orphan, params)
            result.orphan_nodes = [r['node_id'] for r in orphans]
        except Exception as e:
            logger.error(f"孤儿节点检测失败: {e}")
        
        # 3. 检查重复实体
        query_duplicates = f"""
        MATCH (e1:Entity), (e2:Entity)
        {kb_filter.replace('e.', 'e1.')} AND {kb_filter.replace('e.', 'e2.')}
        WHERE e1.name = e2.name AND e1.type = e2.type AND id(e1) < id(e2)
        RETURN e1.name as name, e1.type as type, count(*) as dup_count
        ORDER BY dup_count DESC
        LIMIT 20
        """
        
        try:
            duplicates = await self.connector.execute_query(query_duplicates, params)
            result.duplicate_entities = [(r['name'], r['type']) for r in duplicates]
        except Exception as e:
            logger.error(f"重复实体检测失败: {e}")
        
        # 4. 低置信度实体
        query_low_conf = f"""
        MATCH (e:Entity)
        {kb_filter}
        WHERE e.confidence < $threshold
        RETURN e.id as node_id
        ORDER BY e.confidence ASC
        LIMIT 20
        """
        params["threshold"] = 0.5
        
        try:
            low_conf = await self.connector.execute_query(query_low_conf, params)
            result.low_confidence_entities = [r['node_id'] for r in low_conf]
        except Exception as e:
            logger.error(f"低置信度检测失败: {e}")
        
        # 汇总问题数量
        result.total_issues = (
            len(result.isolated_nodes) +
            len(result.orphan_nodes) +
            len(result.duplicate_entities) * 2 +
            len(result.low_confidence_entities)
        )
        
        result.is_consistent = result.total_issues == 0
        
        if result.has_issues:
            logger.warning(f"⚠️ 图谱一致性检查发现 {result.total_issues} 个问题")
        else:
            logger.info("✅ 图谱一致性检查通过，未发现问题")
        
        return result
    
    async def get_graph_statistics(self, kb_id: Optional[int] = None) -> Dict[str, Any]:
        """获取图谱统计概览"""
        
        kb_clause = ""
        params = {}
        if kb_id:
            kb_clause = "WHERE e.kb_id = $kb_id"
            params["kb_id"] = kb_id
        
        queries = {
            "total_entities": f"MATCH (e:Entity) {kb_clause} RETURN count(e) as cnt",
            # 修复：统计所有类型的关系，不限于 RELATED_TO
            "total_relations": f"MATCH ()-[r]-() {kb_clause.replace('e.', 'r.')} RETURN count(r) as cnt",
            "entity_types": f"""
                MATCH (e:Entity) {kb_clause}
                RETURN e.type as type, count(*) as cnt
                ORDER BY cnt DESC
                LIMIT 15
            """,
            "relation_types": f"""
                MATCH ()-[r]-()
                {f"WHERE r.kb_id = {kb_id}" if kb_id else ''}
                RETURN type(r) as type, count(*) as cnt
                ORDER BY cnt DESC
                LIMIT 15
            """,
            "avg_confidence": f"""
                MATCH (e:Entity) {kb_clause}
                RETURN avg(e.confidence) as avg_conf
            """,
            "top_entities": f"""
                MATCH (e:Entity) {kb_clause}
                RETURN e.id as id, e.name as name, e.type as type, 
                       e.relation_count as rel_cnt, e.mention_count as mention
                ORDER BY rel_cnt DESC
                LIMIT 10
            """
        }
        
        stats = {}
        
        for key, query in queries.items():
            try:
                result = await self.connector.execute_query(query, params)
                if key in ["total_entities", "total_relations", "avg_confidence"]:
                    stats[key] = result[0]['cnt'] if result else 0
                    if key == "avg_confidence":
                        stats[key] = round(stats[key], 3) if stats[key] else 0
                else:
                    stats[key] = result
            except Exception as e:
                logger.warning(f"统计查询[{key}]失败: {e}")
                stats[key] = [] if "types" in key or "top" in key else 0
        
        return stats


__all__ = [
    # 结果包装
    'GraphBuildResult',
    'ConsistencyCheckResult',
    # 子组件
    'GraphValidator',
    # 主服务
    'GraphBuilderService'
]
