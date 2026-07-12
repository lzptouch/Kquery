"""GraphRAG混合检索引擎。

结合向量检索和知识图谱检索的优势，提升语义理解和推理能力。

核心流程：
    用户查询
        │
        ├──► 实体抽取（从查询中识别实体）
        │       │
        │       ▼
        │   图谱上下文扩展（查找实体的邻居、路径、子图）
        │
        ├──► 向量检索（传统RAG语义匹配）
        │
        └─► 智能融合（合并两种结果，加权排序）
                │
                ▼
            重排序 + 答案生成

使用示例：
    from app.services.graphrag.graphrag_engine import GraphRAGEngine

    engine = GraphRAGEngine()

    # 执行混合检索
    result = await engine.hybrid_search(
        query="张三在腾讯公司负责什么工作？",
        collection_name="kb_1",
        top_k=10
    )

    print(f"找到 {len(result.fused_results)} 个融合结果")
    for r in result.fused_results[:5]:
        print(f"  [{r.final_score:.3f}] {r.content[:100]}...")
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from app.core.logger import get_logger
from app.services.graph.entity_extraction import EntityExtractionService
from app.services.graph.graph_query import (
    GraphQueryService,
    HybridGraphSearchResult,
)
from app.services.rag_service import RAGService, QueryResult, RetrievalResult

logger = get_logger(__name__)


# ============================================================
# 数据模型定义
# ============================================================


class QuestionType(str, Enum):
    """问题类型枚举。"""
    
    FACTOID = "factoid"          # 事实型：谁、什么、哪里（"张三是谁？"）
    RELATION = "relation"        # 关系型：A和B的关系（"张三和李四是什么关系？"）
    REASONING = "reasoning"      # 推理型：为什么、如何（"为什么张三能成功？"）
    LISTING = "listing"          # 列举型：列出所有...（"列出所有AI工程师"）
    COMPARISON = "comparison"    # 比较型：A和B的区别（"腾讯和阿里的AI技术对比"）
    DEFINITION = "definition"    # 定义型：什么是...（"什么是深度学习？"）
    PROCEDURAL = "procedural"    # 程序型：如何做...（"如何部署Neo4j？"）
    UNKNOWN = "unknown"          # 未知类型


@dataclass
class ExtractedEntity:
    """从查询中抽取的实体。
    
    Attributes:
        name: 实体名称
        entity_type: 实体类型（推测）
        confidence: 抽取置信度 (0-1)
        position_start: 在查询中的起始位置
        position_end: 在查询中的结束位置
        matched_db_id: 匹配到的数据库实体ID（如果有）
    """
    name: str
    entity_type: Optional[str] = None
    confidence: float = 0.8
    position_start: int = 0
    position_end: int = 0
    matched_db_id: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "entity_type": self.entity_type,
            "confidence": self.confidence,
            "position": [self.position_start, self.position_end],
            "matched_db_id": self.matched_db_id,
        }


@dataclass
class GraphContext:
    """图谱上下文信息（支持 Global + Local Search）。

    Attributes:
        search_mode: 搜索模式 ("global" | "local" | "hybrid")
        global_context: 全局搜索上下文（社区摘要）
        local_context: 局部搜索上下文（实体邻居、路径）
        combined_context: 合并后的上下文文本
        center_entity_id: 中心实体ID（Local Search 用）
        neighbors: 邻居实体列表
        paths_to_other_entities: 到其他实体的路径
        subgraph_text: 子图的文本描述（用于注入到LLM prompt）
        raw_cypher_results: 原始Cypher查询结果（调试用）
    """
    search_mode: str = "hybrid"
    global_context: str = ""
    local_context: str = ""
    combined_context: str = ""
    center_entity_id: Optional[str] = None
    neighbors: List = field(default_factory=list)
    paths_to_other_entities: List[List[Dict]] = field(default_factory=list)
    subgraph_text: str = ""
    raw_cypher_results: List[Dict] = field(default_factory=list)

    @property
    def has_context(self) -> bool:
        """是否有有效的图谱上下文。"""
        return bool(self.combined_context) or bool(self.subgraph_text)


@dataclass
class FusedSearchResult:
    """融合后的搜索结果。
    
    Attributes:
        content: 内容文本
        source: 来源类型（"vector", "graph", "fused"）
        vector_score: 向量检索分数 (0-1)
        graph_score: 图谱检索分数 (0-1)
        final_score: 融合后的最终分数 (0-1)
        metadata: 元数据（来源文档、实体信息等）
        entities_mentioned: 提及的实体列表
        evidence: 支持证据（来自图谱的关系或路径）
    """
    content: str
    source: str = "fused"
    vector_score: float = 0.0
    graph_score: float = 0.0
    final_score: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    entities_mentioned: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "content": self.content,
            "source": self.source,
            "scores": {
                "vector": round(self.vector_score, 4),
                "graph": round(self.graph_score, 4),
                "final": round(self.final_score, 4),
            },
            "metadata": self.metadata,
            "entities": self.entities_mentioned,
            "evidence": self.evidence,
        }


@dataclass
class HybridSearchResult:
    """混合检索的完整结果。
    
    Attributes:
        query: 原始查询
        question_type: 问题类型
        extracted_entities: 从查询中抽取的实体
        graph_context: 图谱上下文
        vector_results: 纯向量检索结果
        fused_results: 融合后的结果（按final_score降序）
        performance_metrics: 性能指标（各阶段耗时）
    """
    query: str
    question_type: QuestionType = QuestionType.UNKNOWN
    extracted_entities: List[ExtractedEntity] = field(default_factory=list)
    graph_context: Optional[GraphContext] = None
    vector_results: List[RetrievalResult] = field(default_factory=list)
    fused_results: List[FusedSearchResult] = field(default_factory=list)
    performance_metrics: Dict[str, float] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "question_type": self.question_type.value,
            "entities": [e.to_dict() for e in self.extracted_entities],
            "has_graph_context": self.graph_context.has_context if self.graph_context else False,
            "vector_result_count": len(self.vector_results),
            "fused_result_count": len(self.fused_results),
            "top_results": [r.to_dict() for r in self.fused_results[:10]],
            "performance_ms": {
                k: round(v * 1000, 2) 
                for k, v in self.performance_metrics.items()
            },
        }


# ============================================================
# 第一层：查询实体抽取器
# ============================================================


class QueryEntityExtractor:
    """从用户查询中识别实体。
    
    使用两层策略：
    1. 规则快速匹配（关键词表、正则表达式）
    2. LLM精确抽取（调用实体抽取服务）
    
    设计原则：
    - 先用规则快速过滤明显实体（零延迟）
    - 再用LLM处理复杂情况（高准确率）
    - 缓存常见实体的映射关系
    """
    
    # 常见实体指示词（用于规则快速识别）
    ENTITY_INDICATORS = {
        "人物": ["谁", "某人", "老师", "工程师", "经理", "CEO", "创始人", "负责人"],
        "组织": ["公司", "企业", "机构", "部门", "团队", "腾讯", "阿里", "百度", "字节"],
        "产品": ["产品", "软件", "系统", "平台", "应用", "APP"],
        "技术": ["技术", "算法", "框架", "语言", "Python", "Java", "机器学习", "AI"],
        "地点": ["城市", "国家", "地点", "北京", "上海", "深圳"],
        "时间": ["时间", "日期", "年", "月", "2020", "2021", "2022", "2023", "2024", "2025"],
    }
    
    def __init__(
        self,
        use_llm: bool = True,
        min_confidence: float = 0.6,
        enable_cache: bool = True,
    ):
        """
        Args:
            use_llm: 是否启用LLM抽取（关闭则只用规则）
            min_confidence: 最低置信度阈值
            enable_cache: 是否缓存实体匹配结果
        """
        self.use_llm = use_llm
        self.min_confidence = min_confidence
        self.enable_cache = enable_cache
        
        # 组件实例（延迟初始化）
        self._entity_extraction_service: Optional[EntityExtractionService] = None
        self._entity_cache: Dict[str, List[ExtractedEntity]] = {}
        
        logger.info("查询实体抽取器初始化完成")
    
    def _get_extraction_service(self) -> EntityExtractionService:
        """获取实体抽取服务实例。"""
        if self._entity_extraction_service is None:
            self._entity_extraction_service = EntityExtractionService()
        return self._entity_extraction_service
    
    async def extract_from_query(
        self,
        query: str,
        kb_id: Optional[int] = None,
    ) -> List[ExtractedEntity]:
        """从用户查询中识别实体。
        
        Args:
            query: 用户查询文本
            kb_id: 知识库ID（用于限定搜索范围）
            
        Returns:
            List[ExtractedEntity]: 抽取的实体列表（按置信度降序）
            
        Example:
            extractor = QueryEntityExtractor()
            
            entities = await extractor.extract_from_query(
                query="张三在腾讯公司负责什么工作？"
            )
            # 返回: [
            #   ExtractedEntity(name="张三", entity_type="Person", confidence=0.95),
            #   ExtractedEntity(name="腾讯公司", entity_type="Organization", confidence=0.92),
            # ]
        """
        import time
        start_time = time.time()
        
        try:
            # 检查缓存
            cache_key = f"{query}:{kb_id}"
            if self.enable_cache and cache_key in self._entity_cache:
                logger.debug(f"从缓存返回实体: {cache_key}")
                return self._entity_cache[cache_key]
            
            # Step 1: 规则快速匹配
            rule_entities = self._rule_based_extraction(query)
            logger.debug(f"规则匹配发现 {len(rule_entities)} 个候选实体")
            
            # Step 2: LLM精确抽取（如果启用）
            llm_entities = []
            if self.use_llm and len(query.strip()) > 2:
                try:
                    llm_entities = await self._llm_based_extraction(query)
                    logger.debug(f"LLM抽取发现 {len(llm_entities)} 个实体")
                except Exception as e:
                    logger.warning(f"LLM实体抽取失败，回退到规则方法: {e}")
            
            # Step 3: 合并去重
            merged_entities = self._merge_entities(rule_entities, llm_entities)
            
            # Step 4: 过滤低置信度
            filtered_entities = [
                e for e in merged_entities 
                if e.confidence >= self.min_confidence
            ]
            
            # Step 5: 尝试在数据库中匹配实体
            if filtered_entities and kb_id:
                filtered_entities = await self._match_with_database(
                    filtered_entities, kb_id
                )
            
            # 按置信度降序排列
            filtered_entities.sort(key=lambda x: x.confidence, reverse=True)
            
            # 缓存结果
            if self.enable_cache:
                self._entity_cache[cache_key] = filtered_entities
            
            elapsed = time.time() - start_time
            logger.info(
                f"查询实体抽取完成: '{query}' → {len(filtered_entities)} 个实体, "
                f"耗时 {elapsed:.3f}s"
            )
            
            return filtered_entities
        
        except Exception as e:
            logger.error(f"查询实体抽取失败: {e}", exc_info=True)
            return []
    
    def _rule_based_extraction(self, query: str) -> List[ExtractedEntity]:
        """基于规则的快速实体提取。"""
        import re
        
        entities = []
        
        # 策略1: 匹配已知组织名
        org_patterns = [
            r"(腾讯|阿里巴巴|阿里巴巴集团|百度|字节跳动|字节|华为|小米|美团|京东|滴滴|网易|新浪|搜狐)",
            r"((?:腾讯|阿里|百度|字节|华为|小米|美团|京东)(?:公司|集团|科技|网络)?)",
        ]
        for pattern in org_patterns:
            matches = re.finditer(pattern, query)
            for m in matches:
                entities.append(ExtractedEntity(
                    name=m.group(),
                    entity_type="Organization",
                    confidence=0.9,
                    position_start=m.start(),
                    position_end=m.end(),
                ))
        
        # 策略2: 匹配中文人名（简单启发式）
        name_pattern = r"([\u4e00-\u9fa5]{2,4})(?:说|表示|认为|负责|担任|加入|离开|在)"
        matches = re.finditer(name_pattern, query)
        for m in matches:
            name = m.group(1)
            # 过滤掉明显的非人名
            if name not in ("公司", "企业", "技术", "产品", "系统"):
                entities.append(ExtractedEntity(
                    name=name,
                    entity_type="Person",
                    confidence=0.75,
                    position_start=m.start(),
                    position_end=m.end(),
                ))
        
        # 策略3: 匹配技术术语
        tech_keywords = [
            "人工智能", "机器学习", "深度学习", "自然语言处理", "计算机视觉",
            "知识图谱", "大语言模型", "LLM", "GPT", "BERT", "Transformer",
            "Python", "Java", "JavaScript", "React", "Vue", "FastAPI",
            "Neo4j", "Milvus", "Redis", "PostgreSQL", "Docker", "Kubernetes",
        ]
        for tech in tech_keywords:
            if tech.lower() in query.lower():
                idx = query.lower().index(tech.lower())
                entities.append(ExtractedEntity(
                    name=tech,
                    entity_type="Technology",
                    confidence=0.85,
                    position_start=idx,
                    position_end=idx + len(tech),
                ))
        
        # 策略4: 匹配时间表达式
        time_pattern = r"(20\d{2}年?(?:\d{1,2}月?)?|\d{4}[-/]\d{1,2}([-/]\d{1,2})?)"
        matches = re.finditer(time_pattern, query)
        for m in matches:
            entities.append(ExtractedEntity(
                name=m.group(),
                entity_type="Date",
                confidence=0.95,
                position_start=m.start(),
                position_end=m.end(),
            ))
        
        return entities
    
    async def _llm_based_extraction(self, query: str) -> List[ExtractedEntity]:
        """基于LLM的精确实体提取。"""
        try:
            service = self._get_extraction_service()
            
            # 使用实体抽取服务的extract_entities方法
            extraction_result = await service.extract_entities(
                text=query,
                doc_id=f"query-{hash(query) % 10000}",
                kb_id=0,  # 查询不属于任何特定KB
                context="用户查询",
            )
            
            # 转换为ExtractedEntity格式
            entities = []
            for entity_data in extraction_result.entities:
                entities.append(ExtractedEntity(
                    name=entity_data.get("name", ""),
                    entity_type=entity_data.get("type"),
                    confidence=entity_data.get("confidence", 0.8),
                ))
            
            return entities
        
        except Exception as e:
            logger.warning(f"LLM实体抽取异常: {e}")
            return []
    
    def _merge_entities(
        self,
        rule_entities: List[ExtractedEntity],
        llm_entities: List[ExtractedEntity],
    ) -> List[ExtractedEntity]:
        """合并规则和LLM的抽取结果，去重。"""
        merged = {}  # name_lower -> ExtractedEntity
        
        # 先添加规则实体
        for entity in rule_entities:
            key = entity.name.lower().strip()
            if key not in merged:
                merged[key] = entity
        
        # 再合并LLM实体（可能更新置信度）
        for entity in llm_entities:
            key = entity.name.lower().strip()
            if key in merged:
                # 取更高的置信度
                existing = merged[key]
                if entity.confidence > existing.confidence:
                    merged[key].confidence = entity.confidence
                    if entity.entity_type and not existing.entity_type:
                        merged[key].entity_type = entity.entity_type
            else:
                merged[key] = entity
        
        return list(merged.values())
    
    async def _match_with_database(
        self,
        entities: List[ExtractedEntity],
        kb_id: int,
    ) -> List[ExtractedEntity]:
        """尝试在知识库图谱中匹配实体。"""
        try:
            query_service = GraphQueryService()
            
            for entity in entities:
                # 在图谱中搜索同名实体
                search_result, total = await query_service.search_entities(
                    keyword=entity.name,
                    entity_types=[entity.entity_type] if entity.entity_type else None,
                    limit=3,
                )
                
                if search_result and total > 0:
                    best_match = search_result[0]
                    # 精确匹配或高度相似
                    if best_match.name == entity.name or total == 1:
                        entity.matched_db_id = best_match.id
                        entity.confidence = min(entity.confidence + 0.1, 1.0)
                        logger.debug(
                            f"实体匹配成功: '{entity.name}' → DB ID: {best_match.id}"
                        )
            
            return entities
        
        except Exception as e:
            logger.warning(f"数据库实体匹配失败: {e}")
            return entities


# ============================================================
# 第二层：图谱上下文扩展器
# ============================================================


class GraphContextExpander:
    """根据查询扩展图谱上下文（支持 Global + Local Search）。

    使用 GraphQueryService 的 hybrid_graph_search 方法，
    根据查询特征自动选择全局或局部搜索策略。

    Global Search 输出示例：
        【知识图谱全局概览】
        相关主题社区:
        1. 该社区围绕人工智能技术发展，包含机器学习、深度学习、自然语言处理等关键技术...
        2. 该社区主要涉及中国互联网巨头，包括阿里巴巴、腾讯、百度等公司...

    Local Search 输出示例：
        【知识图谱局部上下文】
        中心实体:
        - 张三 (Person)
        相关实体与关系:
          [employed_by]
            - 腾讯公司 (Organization)
          [manages]
            - AI研发团队 (Organization)
        实体间路径:
          - 张三 → 腾讯公司 → 人工智能 → 深度学习
    """

    def __init__(
        self,
        default_depth: int = 2,
        max_neighbors: int = 20,
        include_paths: bool = True,
        search_mode: str = "auto",
    ):
        """
        Args:
            default_depth: 默认邻居跳数
            max_neighbors: 每个实体最大邻居数量
            include_paths: 是否计算实体间的路径
            search_mode: 搜索模式 ("auto" | "global" | "local" | "hybrid")
        """
        self.default_depth = default_depth
        self.max_neighbors = max_neighbors
        self.include_paths = include_paths
        self.search_mode = search_mode

        self._query_service: Optional[GraphQueryService] = None

        logger.info("图谱上下文扩展器初始化完成 (Global + Local Search)")

    def _get_query_service(self) -> GraphQueryService:
        """获取图谱查询服务实例。"""
        if self._query_service is None:
            self._query_service = GraphQueryService()
        return self._query_service

    async def expand_context(
        self,
        query: str,
        entities: Optional[List[ExtractedEntity]] = None,
        depth: Optional[int] = None,
        kb_id: Optional[int] = None,
    ) -> GraphContext:
        """扩展图谱上下文（使用 Global + Local Search）。

        Args:
            query: 原始用户查询（用于判断搜索策略）
            entities: 从查询中抽取的实体列表（可选，用于增强 Local Search）
            depth: 邻居跳数（覆盖默认值）
            kb_id: 知识库ID（限定范围）

        Returns:
            GraphContext: 包含全局和局部上下文的上下文对象
        """
        import time
        start_time = time.time()

        context = GraphContext()

        try:
            query_service = self._get_query_service()

            # 使用 Hybrid Graph Search
            search_result = await query_service.hybrid_graph_search(
                query=query,
                kb_id=kb_id,
                top_k=self.max_neighbors,
                search_mode=self.search_mode,
            )

            context.search_mode = search_result.search_mode

            # 填充全局上下文
            if search_result.global_result:
                context.global_context = search_result.global_result.context_text
                context.subgraph_text = context.global_context

            # 填充局部上下文
            if search_result.local_result:
                context.local_context = search_result.local_result.context_text
                context.neighbors = search_result.local_result.neighbors

                if search_result.local_result.center_entities:
                    context.center_entity_id = search_result.local_result.center_entities[0].id

                if self.include_paths and search_result.local_result.paths:
                    context.paths_to_other_entities = [
                        {
                            "source": p.source_id,
                            "target": p.target_id,
                            "path_length": p.shortest_path_length,
                        }
                        for p in search_result.local_result.paths
                    ]

            # 合并上下文
            context.combined_context = search_result.combined_context
            context.subgraph_text = context.combined_context

            elapsed = time.time() - start_time
            logger.info(
                f"🌐 图谱上下文扩展完成 | 模式: {context.search_mode} | "
                f"全局: {len(context.global_context)} 字, "
                f"局部: {len(context.local_context)} 字, "
                f"耗时: {elapsed:.3f}s"
            )

            return context

        except Exception as e:
            logger.error(f"图谱上下文扩展失败: {e}", exc_info=True)
            return context


# ============================================================
# 第三层：结果融合引擎
# ============================================================


class ResultFusionEngine:
    """融合向量检索和图谱检索的结果。
    
    融合策略：
    1. 分数归一化（将不同来源的分数统一到[0,1]区间）
    2. 加权求和（默认：向量60% + 图谱40%）
    3. 重排序（按最终分数降序）
    4. 多样性调整（避免结果过于集中）
    
    可配置参数：
    - vector_weight: 向量检索权重（默认0.6）
    - graph_weight: 图谱权重（默认0.4）
    - diversity_penalty: 多样性惩罚系数（0-0.3）
    """
    
    def __init__(
        self,
        vector_weight: float = 0.6,
        graph_weight: float = 0.4,
        diversity_penalty: float = 0.1,
        top_k: int = 20,
    ):
        """
        Args:
            vector_weight: 向量检索权重
            graph_weight: 图谱检索权重
            diversity_penalty: 多样性惩罚系数（防止结果过于相似）
            top_k: 最终返回的结果数量
        """
        # 权重归一化
        total_weight = vector_weight + graph_weight
        self.vector_weight = vector_weight / total_weight
        self.graph_weight = graph_weight / total_weight
        self.diversity_penalty = diversity_penalty
        self.top_k = top_k
        
        logger.info(
            f"结果融合引擎初始化: 向量权重={self.vector_weight:.2f}, "
            f"图谱权重={self.graph_weight:.2f}"
        )
    
    def fuse_results(
        self,
        vector_results: List[RetrievalResult],
        graph_context: Optional[GraphContext],
        question_type: QuestionType = QuestionType.UNKNOWN,
    ) -> List[FusedSearchResult]:
        """融合向量检索和图谱检索结果。
        
        Args:
            vector_results: 向量检索结果列表（来自RAGEngine）
            graph_context: 图谱上下文（可为None）
            question_type: 问题类型（影响融合策略）
            
        Returns:
            List[FusedSearchResult]: 融合后并排序的结果列表
            
        Example:
            fusion = ResultFusionEngine(vector_weight=0.7, graph_weight=0.3)
            
            fused = fusion.fuse_results(
                vector_results=vector_search_results,
                graph_context=graph_ctx,
                question_type=QuestionType.REASONING
            )
        """
        import time
        start_time = time.time()
        
        try:
            # Step 1: 归一化向量分数
            normalized_vector = self._normalize_vector_scores(vector_results)
            
            # Step 2: 为向量结果计算图谱增强分数
            enhanced_results = self._enhance_with_graph_context(
                vector_results, normalized_vector, graph_context
            )
            
            # Step 3: 根据问题类型调整权重
            adjusted_weights = self._adjust_weights_by_question_type(question_type)
            
            # Step 4: 计算最终融合分数
            final_results = self._compute_final_scores(
                enhanced_results, adjusted_weights
            )
            
            # Step 5: 多样性重排
            diversified = self._apply_diversity_penalty(final_results)
            
            # Step 6: 截断到top_k
            top_results = diversified[:self.top_k]
            
            elapsed = time.time() - start_time
            logger.info(
                f"结果融合完成: {len(vector_results)} 个向量结果 → "
                f"{len(top_results)} 个融合结果, 耗时 {elapsed:.4f}s"
            )
            
            return top_results
        
        except Exception as e:
            logger.error(f"结果融合失败: {e}", exc_info=True)
            # 回退：直接包装向量结果
            fallback = []
            for i, vr in enumerate(vector_results[:self.top_k]):
                fallback.append(FusedSearchResult(
                    content=vr.content or "",
                    source="vector",
                    vector_score=getattr(vr, 'score', 0.7) or 0.7,
                    final_score=getattr(vr, 'score', 0.7) or 0.7,
                    metadata={"fallback": True},
                ))
            return fallback
    
    def _normalize_vector_scores(
        self,
        results: List[RetrievalResult],
    ) -> List[float]:
        """将向量检索分数归一化到[0,1]区间。"""
        if not results:
            return []
        
        scores = []
        for r in results:
            score = getattr(r, 'score', getattr(r, 'similarity', None))
            if score is not None:
                scores.append(float(score))
            else:
                scores.append(0.5)  # 默认中等分数
        
        if not scores:
            return [0.5] * len(results)
        
        min_score = min(scores)
        max_score = max(scores)
        score_range = max_score - min_score
        
        if score_range < 1e-6:
            # 所有分数几乎相同
            return [0.7] * len(scores)
        
        # Min-Max归一化
        normalized = [(s - min_score) / score_range for s in scores]
        
        return normalized
    
    def _enhance_with_graph_context(
        self,
        vector_results: List[RetrievalResult],
        normalized_scores: List[float],
        graph_context: Optional[GraphContext],
    ) -> List[Dict[str, Any]]:
        """利用图谱上下文增强向量结果的分数。"""
        enhanced = []
        
        for i, (vr, norm_score) in enumerate(zip(vector_results, normalized_scores)):
            content = vr.content or ""
            metadata = getattr(vr, 'metadata', {}) or {}
            
            # 计算图谱相关性得分
            graph_relevance = self._calculate_graph_relevance(
                content, graph_context
            )
            
            # 收集提及的实体
            mentioned_entities = self._extract_mentioned_entities(
                content, graph_context
            )
            
            # 收集证据
            evidence = self._collect_evidence(content, graph_context)
            
            enhanced.append({
                "content": content,
                "vector_score": norm_score,
                "graph_score": graph_relevance,
                "metadata": metadata,
                "entities_mentioned": mentioned_entities,
                "evidence": evidence,
                "original_index": i,
            })
        
        return enhanced
    
    def _calculate_graph_relevance(
        self,
        content: str,
        graph_context: Optional[GraphContext],
    ) -> float:
        """计算内容与图谱上下文的相关性。"""
        if not graph_context or not content:
            return 0.0
        
        relevance = 0.0
        content_lower = content.lower()
        
        # 检查是否包含邻居实体名称
        neighbor_matches = 0
        for neighbor in graph_context.neighbors:
            if neighbor.name.lower() in content_lower:
                neighbor_matches += 1
                relevance += 0.15
        
        # 检查是否包含关系类型
        relation_keywords = [
            "负责", "管理", "属于", "位于", "成立于", "开发", "研究",
            "员工", "子公司", "总部", "产品", "技术",
        ]
        for kw in relation_keywords:
            if kw in content:
                relevance += 0.05
        
        # 限制最大值
        return min(relevance, 1.0)
    
    def _extract_mentioned_entities(
        self,
        content: str,
        graph_context: Optional[GraphContext],
    ) -> List[str]:
        """从内容中提取提及的实体名称。"""
        if not graph_context or not content:
            return []
        
        mentioned = []
        content_lower = content.lower()
        
        for neighbor in graph_context.neighbors:
            if neighbor.name.lower() in content_lower:
                mentioned.append(neighbor.name)
        
        return mentioned[:10]  # 限制数量
    
    def _collect_evidence(
        self,
        content: str,
        graph_context: Optional[GraphContext],
    ) -> List[str]:
        """收集支持该内容的证据（来自图谱的关系）。"""
        if not graph_context:
            return []
        
        evidence = []
        
        # 如果内容涉及某实体，记录其相关关系
        for neighbor in graph_context.neighbors[:5]:
            if neighbor.name in content and neighbor.relation_type:
                evidence.append(
                    f"{neighbor.relation_type}: {neighbor.name}"
                )
        
        return evidence
    
    def _adjust_weights_by_question_type(
        self,
        question_type: QuestionType,
    ) -> Tuple[float, float]:
        """根据问题类型动态调整权重。"""
        base_vector = self.vector_weight
        base_graph = self.graph_weight
        
        adjustments = {
            QuestionType.FACTOID: (0.4, 0.6),     # 事实型：更依赖图谱
            QuestionType.RELATION: (0.3, 0.7),     # 关系型：高度依赖图谱
            QuestionType.REASONING: (0.5, 0.5),    # 推理型：平衡
            QuestionType.LISTING: (0.6, 0.4),      # 列举型：偏重向量
            QuestionType.COMPARISON: (0.5, 0.5),   # 比较型：平衡
            QuestionType.DEFINITION: (0.6, 0.4),   # 定义型：偏重向量
            QuestionType.PROCEDURAL: (0.7, 0.3),   # 程序型：偏重向量
            QuestionType.UNKNOWN: (base_vector, base_graph),
        }
        
        return adjustments.get(question_type, (base_vector, base_graph))
    
    def _compute_final_scores(
        self,
        enhanced_results: List[Dict[str, Any]],
        weights: Tuple[float, float],
    ) -> List[FusedSearchResult]:
        """计算最终融合分数。"""
        vector_w, graph_w = weights
        
        final_results = []
        for item in enhanced_results:
            final_score = (
                item["vector_score"] * vector_w +
                item["graph_score"] * graph_w
            )
            
            result = FusedSearchResult(
                content=item["content"],
                source=self._determine_source(item["vector_score"], item["graph_score"]),
                vector_score=item["vector_score"],
                graph_score=item["graph_score"],
                final_score=final_score,
                metadata=item["metadata"],
                entities_mentioned=item["entities_mentioned"],
                evidence=item["evidence"],
            )
            final_results.append(result)
        
        # 按最终分数降序排列
        final_results.sort(key=lambda x: x.final_score, reverse=True)
        
        return final_results
    
    def _determine_source(
        self,
        vector_score: float,
        graph_score: float,
    ) -> str:
        """确定结果的主要来源。"""
        threshold = 0.15
        
        if abs(vector_score - graph_score) < threshold:
            return "fused"
        elif vector_score > graph_score:
            return "vector"
        else:
            return "graph"
    
    def _apply_diversity_penalty(
        self,
        results: List[FusedSearchResult],
    ) -> List[FusedSearchResult]:
        """应用多样性惩罚，避免结果过于相似。"""
        if len(results) <= 1 or self.diversity_penalty <= 0:
            return results
        
        penalized = list(results)
        seen_content_prefixes: Set[str] = set()
        
        for i, result in enumerate(penalized):
            # 取前50个字符作为指纹
            prefix = result.content[:50].lower() if result.content else ""
            
            if prefix in seen_content_prefixes:
                # 降低重复内容的分数
                penalized[i] = FusedSearchResult(
                    content=result.content,
                    source=result.source,
                    vector_score=result.vector_score,
                    graph_score=result.graph_score,
                    final_score=result.final_score * (1 - self.diversity_penalty),
                    metadata={**result.metadata, "diversity_penalty_applied": True},
                    entities_mentioned=result.entities_mentioned,
                    evidence=result.evidence,
                )
            else:
                seen_content_prefixes.add(prefix)
        
        # 重新排序（惩罚后顺序可能变化）
        penalized.sort(key=lambda x: x.final_score, reverse=True)
        
        return penalized


# ============================================================
# 主入口：GraphRAG引擎
# ============================================================


class GraphRAGEngine:
    """GraphRAG混合检索引擎（主入口）。
    
    整合了完整的GraphRAG检索流程：
    1. 问题分类（判断问题类型）
    2. 实体抽取（从查询中识别关键实体）
    3. 图谱扩展（获取实体的图谱上下文）
    4. 向量检索（传统的语义检索）
    5. 结果融合（合并两种结果）
    
    设计为RAGService的直接替代品/增强版，
    可以无缝集成到现有的API和服务中。
    
    使用示例：
        engine = GraphRAGEngine()
        
        # 执行混合检索
        result = await engine.hybrid_search(
            query="张三在腾讯公司的工作职责是什么？",
            collection_name="kb_1",
            top_k=10
        )
        
        print(f"问题类型: {result.question_type}")
        print(f"识别实体: {[e.name for e in result.extracted_entities]}")
        print(f"融合结果数: {len(result.fused_results)}")
        
        # 直接获取增强后的答案
        answer = await engine.generate_answer(
            query="比较腾讯和阿里的AI战略",
            collection_name="tech_kb"
        )
        print(answer.response)
    """
    
    def __init__(
        self,
        vector_weight: float = 0.6,
        graph_weight: float = 0.4,
        enable_entity_extraction: bool = True,
        enable_graph_expansion: bool = True,
        enable_llm_extraction: bool = True,
        graph_depth: int = 2,
    ):
        """
        Args:
            vector_weight: 向量检索权重（默认0.6）
            graph_weight: 图谱检索权重（默认0.4）
            enable_entity_extraction: 是否启用实体抽取
            enable_graph_expansion: 是否启用图谱扩展
            enable_llm_extraction: 是否启用LLM进行实体抽取
            graph_depth: 图谱邻居查询深度
        """
        # 核心组件
        self.entity_extractor = QueryEntityExtractor(
            use_llm=enable_llm_extraction,
        )
        self.context_expander = GraphContextExpander(
            default_depth=graph_depth,
        )
        self.fusion_engine = ResultFusionEngine(
            vector_weight=vector_weight,
            graph_weight=graph_weight,
        )
        self.rag_service = RAGService()
        
        # 配置标志
        self.enable_entity_extraction = enable_entity_extraction
        self.enable_graph_expansion = enable_graph_expansion
        
        logger.info("GraphRAG引擎初始化完成")
    
    async def hybrid_search(
        self,
        query: str,
        collection_name: str,
        kb_id: Optional[int] = None,
        top_k: int = 10,
        enable_hyde: bool = True,
    ) -> HybridSearchResult:
        """执行GraphRAG混合检索。
        
        这是GraphRAG的核心方法，整合了完整的检索流程。
        
        Args:
            query: 用户查询
            collection_name: Milvus集合名称
            kb_id: 知识库ID（用于图谱查询）
            top_k: 返回结果数量
            enable_hyde: 是否启用HyDE假设性文档嵌入
            
        Returns:
            HybridSearchResult: 包含完整检索过程信息的详细结果
            
        Example:
            engine = GraphRAGEngine()
            
            result = await engine.hybrid_search(
                query="深度学习和机器学习的区别是什么？",
                collection_name="ai_knowledge_base",
                kb_id=1,
                top_k=10
            )
            
            # 查看融合结果
            for r in result.fused_results[:5]:
                print(f"[{r.final_score:.3f}] {r.source}: {r.content[:80]}...")
        """
        import time
        overall_start = time.time()
        
        result = HybridSearchResult(query=query)
        
        try:
            logger.info(f"🚀 开始GraphRAG混合检索: '{query}'")
            
            # ========== Phase 1: 问题分类 ==========
            phase1_start = time.time()
            result.question_type = classify_question(query)
            result.performance_metrics["question_classification"] = (
                time.time() - phase1_start
            )
            logger.info(f"  ✓ 问题类型: {result.question_type.value}")
            
            # ========== Phase 2: 实体抽取 ==========
            phase2_start = time.time()
            if self.enable_entity_extraction:
                result.extracted_entities = await self.entity_extractor.extract_from_query(
                    query=query,
                    kb_id=kb_id,
                )
            result.performance_metrics["entity_extraction"] = (
                time.time() - phase2_start
            )
            logger.info(
                f"  ✓ 实体抽取: {len(result.extracted_entities)} 个实体 → "
                f"{[e.name for e in result.extracted_entities]}"
            )
            
            # ========== Phase 3: 图谱上下文扩展（Global + Local Search）==========
            phase3_start = time.time()
            if self.enable_graph_expansion:
                # 使用新的 Global + Local Search 扩展上下文
                result.graph_context = await self.context_expander.expand_context(
                    query=query,
                    entities=result.extracted_entities,
                    kb_id=kb_id,
                )
            result.performance_metrics["graph_expansion"] = (
                time.time() - phase3_start
            )
            has_context = (
                result.graph_context.has_context if result.graph_context else False
            )
            search_mode = (
                result.graph_context.search_mode if result.graph_context else "none"
            )
            logger.info(
                f"  ✓ 图谱上下文: {'✅ 有' if has_context else '❌ 无'} "
                f"(模式: {search_mode})"
            )
            
            # ========== Phase 4: 向量检索（并行）==========
            phase4_start = time.time()
            vector_result = await self.rag_service.query_documents(
                query=query,
                collection_name=collection_name,
                top_k=top_k * 2,  # 获取更多候选以便融合
                enable_hyde=enable_hyde,
            )
            result.vector_results = vector_result.results
            result.performance_metrics["vector_search"] = (
                time.time() - phase4_start
            )
            logger.info(f"  ✓ 向量检索: {len(result.vector_results)} 个结果")
            
            # ========== Phase 5: 结果融合 ==========
            phase5_start = time.time()
            result.fused_results = self.fusion_engine.fuse_results(
                vector_results=result.vector_results,
                graph_context=result.graph_context,
                question_type=result.question_type,
            )
            result.performance_metrics["result_fusion"] = (
                time.time() - phase5_start
            )
            logger.info(f"  ✓ 结果融合: {len(result.fused_results)} 个最终结果")
            
            # 总耗时
            result.performance_metrics["total"] = time.time() - overall_start
            
            logger.info(
                f"🎉 GraphRAG检索完成! 总耗时 {result.performance_metrics['total']:.3f}s, "
                f"返回 {len(result.fused_results)} 个高质量结果"
            )
            
            return result
        
        except Exception as e:
            logger.error(f"❌ GraphRAG检索失败: {e}", exc_info=True)
            result.performance_metrics["total"] = time.time() - overall_start
            raise
    
    async def generate_answer(
        self,
        query: str,
        collection_name: str,
        kb_id: Optional[int] = None,
        system_prompt: Optional[str] = None,
    ) -> Dict[str, Any]:
        """基于GraphRAG检索结果生成增强答案。
        
        与hybrid_search的区别：
        - hybrid_search: 返回原始检索结果（给开发者自己组装答案）
        - generate_answer: 直接返回生成的答案文本（端到端解决方案）
        
        生成的答案特点：
        - 自动引用图谱中的实体和关系作为证据
        - 标注来源文档和知识库
        - 支持追问和上下文对话
        
        Args:
            query: 用户问题
            collection_name: Milvus集合名称
            kb_id: 知识库ID
            system_prompt: 自定义的系统提示词（可选）
            
        Returns:
            Dict: {
                "answer": "答案文本...",
                "sources": [...],  // 来源引用
                "entities": [...],  // 相关实体
                "graph_context_used": True/False,
                "performance_metrics": {...}
            }
        """
        import time
        start_time = time.time()
        
        try:
            logger.info(f"🧠 开始生成增强答案: '{query}'")
            
            # Step 1: 执行混合检索
            search_result = await self.hybrid_search(
                query=query,
                collection_name=collection_name,
                kb_id=kb_id,
                top_k=8,
            )
            
            # Step 1.5: 多模态内容检索（v2.0 增强）
            multimodal_context = []
            try:
                from app.services.rag_service import RAGService
                rag_service = RAGService()
                # 检索图像描述和表格内容
                multimodal_results = await rag_service.similarity_search(
                    query=query,
                    collection_name=collection_name,
                    top_k=5,
                    filter_expression='chunk_type in ["image_description", "table", "table_description"]'
                )
                if multimodal_results:
                    for r in multimodal_results[:3]:
                        chunk_type = getattr(r, 'metadata', {}).get('chunk_type', 'unknown') if hasattr(r, 'metadata') else 'unknown'
                        multimodal_context.append({
                            'content': getattr(r, 'content', '')[:400],
                            'chunk_type': chunk_type,
                            'source': getattr(r, 'metadata', {}).get('source', 'unknown') if hasattr(r, 'metadata') else 'unknown',
                            'score': getattr(r, 'score', 0.0),
                        })
                    logger.info(f"🎨 多模态检索完成: {len(multimodal_context)} 条相关结果")
            except Exception as mm_err:
                logger.warning(f"多模态检索失败（不影响主流程）: {mm_err}")
            
            # Step 2: 构建增强prompt（包含多模态上下文）
            enhanced_prompt = self._build_answer_prompt(
                query, search_result, system_prompt, multimodal_context=multimodal_context
            )
            
            # Step 3: 调用LLM生成答案
            answer_text = await self._call_llm_for_answer(enhanced_prompt)
            
            # Step 4: 组装返回结果
            result = {
                "answer": answer_text,
                "query": query,
                "question_type": search_result.question_type.value,
                "sources": [
                    {
                        "content": r.content[:200],
                        "score": r.final_score,
                        "source": r.source,
                        "metadata": r.metadata,
                        "file_name": r.metadata.get("file_name", "") if r.metadata else "",
                        "page_number": r.metadata.get("page_number") if r.metadata else None,
                        "char_offset": r.metadata.get("char_offset", 0) if r.metadata else 0,
                    }
                    for r in search_result.fused_results[:5]
                ],
                "entities": [e.to_dict() for e in search_result.extracted_entities],
                "graph_context_used": (
                    search_result.graph_context.has_context 
                    if search_result.graph_context else False
                ),
                "subgraph_context": (
                    search_result.graph_context.subgraph_text 
                    if search_result.graph_context else ""
                ),
                "multimodal_sources": multimodal_context,  # v2.0 新增：多模态来源
                "performance_metrics": search_result.performance_metrics,
                "generation_latency_sec": time.time() - start_time,
            }
            
            logger.info(f"✅ 答案生成完成, 耗时 {time.time() - start_time:.3f}s")
            
            return result
        
        except Exception as e:
            logger.error(f"❌ 答案生成失败: {e}", exc_info=True)
            raise
    
    async def _call_llm_for_answer(self, prompt: str) -> str:
        """调用LLM生成答案（带重试和 Mock 降级）。"""
        from app.core.llm_client import call_langchain_chat, is_llm_configured
        from app.core.settings import get_settings
        from langchain_core.messages import HumanMessage

        settings = get_settings()

        # 如果 LLM 未配置，使用 Mock 数据
        if not is_llm_configured("agent"):
            logger.warning("Agent API 未配置，答案生成使用 Mock 数据")
            from app.core.mock_data import get_mock_data
            mock_result = get_mock_data("qa_answer", question=prompt[:100])
            return mock_result

        return await call_langchain_chat(
            model=settings.llm.agent_model_name,
            messages=[HumanMessage(content=prompt)],
            api_key=settings.llm.agent_api_key,
            base_url=settings.llm.agent_api_base,
            temperature=0.3,
            timeout=120,
            mock_module="qa_answer",
        )
    
    def _build_answer_prompt(
        self,
        query: str,
        search_result: HybridSearchResult,
        custom_system_prompt: Optional[str],
        multimodal_context: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """构建用于答案生成的增强 Prompt（支持 Global + Local + 多模态上下文）。"""
        base_system = custom_system_prompt or """你是一个知识增强型问答助手。你的任务是：
1. 基于提供的检索结果回答用户的问题
2. 引用知识图谱中的实体和关系作为证据
3. 对于宏观问题，利用社区摘要进行整体回答
4. 对于具体问题，利用实体邻居和路径进行详细回答
5. 如果信息不足，明确说明哪些方面缺少证据
6. 使用清晰的段落结构和适当的标题
7. 【v2.0 增强】当问题涉及图表、数据或图像时，充分利用多模态上下文中的图像描述和表格数据"""

        # 添加图谱上下文（Global + Local）
        graph_context_section = ""
        if search_result.graph_context and search_result.graph_context.has_context:
            gc = search_result.graph_context

            # 全局上下文（社区摘要）
            global_section = ""
            if gc.global_context:
                global_section = f"""

【全局知识图谱上下文 - 主题概览】
{gc.global_context}"""

            # 局部上下文（实体邻居）
            local_section = ""
            if gc.local_context:
                local_section = f"""

【局部知识图谱上下文 - 实体详情】
{gc.local_context}"""

            graph_context_section = f"""

{global_section}
{local_section}

请充分利用以上全局和局部图谱信息来丰富你的答案。"""

        # 添加多模态上下文（v2.0 增强）
        multimodal_section = ""
        if multimodal_context:
            multimodal_section = "\n\n【多模态上下文 - 图表与图像描述】\n"
            for i, item in enumerate(multimodal_context[:5]):
                type_label = {
                    "image_description": "图像描述",
                    "table": "结构化表格",
                    "table_description": "表格洞察",
                }.get(item.get("chunk_type", "unknown"), "多模态内容")
                multimodal_section += f"\n--- {type_label} {i+1} (相关度: {item.get('score', 0):.2f}) ---\n"
                multimodal_section += item.get("content", "")[:500]

        # 添加检索结果摘要
        results_section = "\n\n【相关文档片段】\n"
        for i, r in enumerate(search_result.fused_results[:8]):
            results_section += f"\n--- 片段 {i+1} (相关性: {r.final_score:.2%}) ---\n"
            results_section += r.content[:500]
            if r.entities_mentioned:
                results_section += f"\n[提及实体: {', '.join(r.entities_mentioned)}]"
            if r.evidence:
                results_section += f"\n[图谱证据: {'; '.join(r.evidence)}]"

        # 组装完整 prompt
        full_prompt = f"""{base_system}{graph_context_section}{multimodal_section}{results_section}

---

用户问题：{query}

请基于以上信息给出详细准确的答案："""

        return full_prompt
    
    def _generate_template_answer(
        self,
        search_result: HybridSearchResult,
    ) -> str:
        """生成模板化答案（当没有LLM集成时的回退方案）。"""
        # 这只是一个简单的模板实现
        # 实际应用中应该调用真正的LLM API
        
        parts = []
        
        # 开头
        parts.append(f"关于「{search_result.query}」的回答：\n")
        
        # 问题类型说明
        type_descriptions = {
            QuestionType.FACTOID: "这是一个事实性问题。",
            QuestionType.RELATION: "这涉及到实体间的关系。",
            QuestionType.REASONING: "这需要一定的推理分析。",
            QuestionType.LISTING: "以下是相关信息列举。",
            QuestionType.COMPARISON: "以下是从多个角度进行的对比。",
            QuestionType.DEFINITION: "以下是概念解释。",
            QuestionType.PROCEDURAL: "以下是步骤说明。",
        }
        if search_result.question_type in type_descriptions:
            parts.append(type_descriptions[search_result.question_type])
            parts.append("")
        
        # 主要内容（从检索结果中提取）
        if search_result.fused_results:
            parts.append("**核心信息：**\n")
            for i, r in enumerate(search_result.fused_results[:3]):
                content_preview = r.content[:300] + ("..." if len(r.content) > 300 else "")
                parts.append(f"{i+1}. {content_preview}\n")
        
        # 实体信息
        if search_result.extracted_entities:
            parts.append("\n**相关实体：**\n")
            for e in search_result.extracted_entities[:5]:
                entity_info = f"- {e.name}"
                if e.entity_type:
                    entity_info += f" ({e.entity_type})"
                if e.matched_db_id:
                    entity_info += f" [已匹配]"
                parts.append(entity_info)
        
        # 图谱上下文提示
        if search_result.graph_context and search_result.graph_context.has_context:
            parts.append("\n**知识图谱补充信息：**\n")
            parts.append(
                "本次查询使用了知识图谱增强，关联了 "
                f"{len(search_result.graph_context.neighbors)} 个相关知识节点。"
            )
        
        # 结尾
        parts.append("\n---\n*此答案由KQuery Pro GraphRAG引擎生成*")
        
        return "\n".join(parts)


# ============================================================
# 辅助函数
# ============================================================


def classify_question(question: str) -> QuestionType:
    """简单的基于规则的问题分类器。
    
    判断问题类型以选择最优的检索策略。
    
    Args:
        question: 用户问题文本
        
    Returns:
        QuestionType: 问题类型枚举值
    """
    if not question:
        return QuestionType.UNKNOWN
    
    q = question.strip().lower()
    
    # 关系型模式
    relation_patterns = [
        r"(.+?)和(.+?)的?关系",
        r"(.+?)与(.+?)的?联系",
        r"(.+?)跟(.+?)的?区别",
        r"谁.*(?:创立|创建|建立|成立|发明|发现).*(.+?)",
    ]
    import re
    for pattern in relation_patterns:
        if re.search(pattern, q):
            return QuestionType.RELATION
    
    # 推理型关键词
    reasoning_keywords = ["为什么", "原因", "理由", "原理", "机制", "如何", "怎么", "怎样", "为何"]
    if any(kw in q for kw in reasoning_keywords):
        return QuestionType.REASONING
    
    # 列举型关键词
    listing_keywords = ["列出", "所有", "有哪些", "什么是", "那些", "多少", "几个"]
    if any(kw in q for kw in listing_keywords):
        return QuestionType.LISTING
    
    # 比较型关键词
    comparison_keywords = ["对比", "比较", "区别", "差异", "优劣", "各自"]
    if any(kw in q for kw in comparison_keywords):
        return QuestionType.COMPARISON
    
    # 定义型模式
    definition_patterns = [r"什么是", r"何为", r"定义", r"概念", r"意思", r"解释.*是"]
    for pattern in definition_patterns:
        if re.search(pattern, q):
            return QuestionType.DEFINITION
    
    # 程序型关键词
    procedural_keywords = ["步骤", "方法", "流程", "教程", "操作", "部署", "安装"]
    if any(kw in q for kw in procedural_keywords):
        return QuestionType.PROCEDURAL
    
    # 事实型（默认）
    factoid_indicators = ["谁", "哪个", "哪里", "何时", "什么时间", "多大", "多长"]
    if any(kw in q for kw in factoid_indicators):
        return QuestionType.FACTOID
    
    return QuestionType.UNKNOWN
