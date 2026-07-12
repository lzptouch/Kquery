"""GraphRAG（Graph + Retrieval-Augmented Generation）服务包。

将知识图谱能力与传统RAG检索融合，实现：
- 混合检索（向量语义 + 图谱结构）
- 智能问答（问题分类、多策略处理）
- 答案增强（证据追溯、图谱上下文注入）
- 可视化数据（D3.js/Cytoscape.js/ECharts格式）

核心模块：
- graphrag_engine.py: GraphRAG混合检索引擎
- qa_service.py: 智能问答增强服务  
- visualization_service.py: 可视化数据服务

使用示例:
    from app.services.graphrag import GraphRAGEngine, QAService

    # 混合检索
    engine = GraphRAGEngine()
    result = await engine.hybrid_search(
        query="张三在腾讯公司负责什么？",
        collection_name="kb_1",
        kb_id=1,
        top_k=10
    )

    # 智能问答
    qa = QAService()
    answer = await qa.ask(
        question="深度学习和机器学习的区别？",
        collection_name="tech_kb"
    )
    print(answer.text)
"""

from app.services.graphrag.graphrag_engine import (
    GraphRAGEngine,
    QueryEntityExtractor,
    GraphContextExpander,
    ResultFusionEngine,
    QuestionType,
    ExtractedEntity,
    GraphContext,
    FusedSearchResult,
    HybridSearchResult,
    classify_question,
)

from app.services.graphrag.qa_service import (
    QAService,
    AnswerStrategyManager,
    Evidence,
    Answer,
    ConversationTurn,
    ConversationSession,
)

from app.services.graphrag.visualization_service import (
    VisualizationService,
    VisualStyleMapper,
    VisualizationData,
    VisualNode,
    VisualEdge,
    VisualizationFormat,
    LayoutAlgorithm,
)

__all__ = [
    # 核心引擎
    "GraphRAGEngine",
    "QueryEntityExtractor",
    "GraphContextExpander",
    "ResultFusionEngine",
    
    # 数据模型
    "QuestionType",
    "ExtractedEntity",
    "GraphContext",
    "FusedSearchResult",
    "HybridSearchResult",
    "classify_question",
    
    # 问答服务
    "QAService",
    "AnswerStrategyManager",
    "Evidence",
    "Answer",
    "ConversationTurn",
    "ConversationSession",
    
    # 可视化
    "VisualizationService",
    "VisualStyleMapper",
    "VisualizationData",
    "VisualNode",
    "VisualEdge",
    "VisualizationFormat",
    "LayoutAlgorithm",
]
