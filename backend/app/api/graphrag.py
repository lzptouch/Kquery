"""知识图谱增强的RAG查询API。

提供基于GraphRAG的智能问答接口，支持：
- 混合检索（向量 + 图谱）
- 智能问答（自动选择最优策略）
- 答案生成（带证据引用）
- 图谱可视化数据

API端点：
    POST /api/v1/graphrag/search          - GraphRAG混合检索
    POST /api/v1/graphrag/ask             - 智能问答（端到端）
    GET  /api/v1/graphrag/question-type   - 问题分类测试
    POST /api/v1/graphrag/entities        - 从查询抽取实体
    GET  /api/v1/graphrag/statistics      - GraphRAG使用统计
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.core.logger import get_logger
from app.core.rate_limiter import get_rate_limit_dependency
from app.core.audit_logger import audit_logger
from app.models.graphrag import (
    HybridSearchRequest,
    AskRequest,
    EntityExtractionRequest,
    StandardResponse,
)
from app.services.graphrag.graphrag_engine import (
    GraphRAGEngine,
    classify_question,
    QuestionType,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/graphrag", tags=["GraphRAG - 知识图谱增强检索"])

# 全局引擎实例（延迟初始化）
_engine: Optional[GraphRAGEngine] = None


def get_graphrag_engine() -> GraphRAGEngine:
    """获取全局GraphRAG引擎实例。"""
    global _engine
    if _engine is None:
        _engine = GraphRAGEngine()
        logger.info("GraphRAG引擎实例创建完成")
    return _engine


# 请求/响应模型（已迁移至 app.models.graphrag）


# ============================================================
# API端点实现
# ============================================================


@router.post(
    "/search",
    response_model=StandardResponse,
    summary="GraphRAG混合检索",
    description="""执行GraphRAG混合检索，结合向量语义检索和知识图谱上下文扩展。

核心流程：
1. 从用户查询中识别关键实体
2. 在知识图谱中查找实体的邻居和关系路径
3. 执行向量语义检索获取相关文档片段
4. 智能融合两种结果，返回高质量排序结果

适用于需要高精度、可解释的搜索场景。

**性能目标**: 端到端延迟 < 1.5秒 (含LLM实体抽取)
""",
    dependencies=[Depends(get_rate_limit_dependency(
        requests=100, window_seconds=60, key_prefix="graphrag_search"
    ))],
)
async def hybrid_search(
    request: Request,
    request_body: HybridSearchRequest,
) -> StandardResponse:
    """执行GraphRAG混合检索。"""
    start_time = time.perf_counter()
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    try:
        engine = get_graphrag_engine()
        
        # 执行混合检索
        result = await engine.hybrid_search(
            query=request_body.query,
            collection_name=request_body.collection_name,
            kb_id=request_body.kb_id,
            top_k=request_body.top_k,
            enable_hyde=request_body.enable_hyde,
        )
        
        # 转换为可序列化的字典
        result_dict = result.to_dict()
        
        duration_ms = (time.perf_counter() - start_time) * 1000
        await audit_logger.log_query_access(
            request=request,
            endpoint="/api/v1/graphrag/search",
            query_text=request_body.query,
            collection_name=request_body.collection_name,
            response_count=len(result.fused_results),
            duration_ms=duration_ms,
            status="success",
            request_id=request_id,
        )
        
        return StandardResponse(
            success=True,
            message=f"GraphRAG检索完成，找到 {len(result.fused_results)} 个融合结果",
            data=result_dict,
        )
    
    except Exception as e:
        duration_ms = (time.perf_counter() - start_time) * 1000
        await audit_logger.log_query_access(
            request=request,
            endpoint="/api/v1/graphrag/search",
            query_text=request_body.query if request_body else None,
            collection_name=request_body.collection_name if request_body else None,
            duration_ms=duration_ms,
            status="failure",
            request_id=request_id,
        )
        logger.error(f"GraphRAG混合检索失败: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"检索失败: {str(e)}",
        )


@router.post(
    "/ask",
    response_model=StandardResponse,
    summary="GraphRAG智能问答",
    description="""端到端的智能问答接口，直接返回生成的答案。

与 /search 的区别：
- /search: 返回原始检索结果（开发者自行组装答案）
- /ask: 直接返回完整答案文本（开箱即用）

特点：
- 自动引用知识图谱中的实体和关系作为证据
- 标注来源文档和相关信息
- 支持多轮对话上下文
- 根据问题类型自动调整回答策略
""",
    dependencies=[Depends(get_rate_limit_dependency(
        requests=60, window_seconds=60, key_prefix="graphrag_ask"
    ))],
)
async def intelligent_qa(
    request: Request,
    request_body: AskRequest,
) -> StandardResponse:
    """执行智能问答并生成答案。"""
    start_time = time.perf_counter()
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    try:
        engine = get_graphrag_engine()
        
        # 生成答案
        result = await engine.generate_answer(
            query=request_body.query,
            collection_name=request_body.collection_name,
            kb_id=request_body.kb_id,
            system_prompt=request_body.system_prompt,
        )
        
        duration_ms = (time.perf_counter() - start_time) * 1000
        await audit_logger.log_query_access(
            request=request,
            endpoint="/api/v1/graphrag/ask",
            query_text=request_body.query,
            collection_name=request_body.collection_name,
            response_count=1,
            duration_ms=duration_ms,
            status="success",
            request_id=request_id,
        )
        
        return StandardResponse(
            success=True,
            message="智能问答完成",
            data=result,
        )
    
    except Exception as e:
        duration_ms = (time.perf_counter() - start_time) * 1000
        await audit_logger.log_query_access(
            request=request,
            endpoint="/api/v1/graphrag/ask",
            query_text=request_body.query if request_body else None,
            collection_name=request_body.collection_name if request_body else None,
            duration_ms=duration_ms,
            status="failure",
            request_id=request_id,
        )
        logger.error(f"智能问答失败: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"问答失败: {str(e)}",
        )


@router.get(
    "/question-type",
    response_model=StandardResponse,
    summary="问题分类",
    description="""
分析用户问题的类型，帮助理解系统的处理策略。

支持的问题类型：
- factoid: 事实型问题（谁、什么、哪里）
- relation: 关系型问题（A和B的关系）
- reasoning: 推理型问题（为什么、如何）
- listing: 列举型问题（列出所有...）
- comparison: 比较型问题（A和B的区别）
- definition: 定义型问题（什么是...）
- procedural: 程序型问题（如何做...）
""",
)
async def classify_query_type(
    query: str = Query(..., description="要分类的问题"),
) -> StandardResponse:
    """对用户问题进行分类。"""
    try:
        q_type = classify_question(query)
        
        type_descriptions = {
            QuestionType.FACTOID: "事实型问题（谁、什么、哪里）",
            QuestionType.RELATION: "关系型问题（A和B的关系）",
            QuestionType.REASONING: "推理型问题（为什么、如何）",
            QuestionType.LISTING: "列举型问题（列出所有...）",
            QuestionType.COMPARISON: "比较型问题（A和B的区别）",
            QuestionType.DEFINITION: "定义型问题（什么是...）",
            QuestionType.PROCEDURAL: "程序型问题（如何做...）",
            QuestionType.UNKNOWN: "未知类型",
        }
        
        return StandardResponse(
            success=True,
            message="问题分类完成",
            data={
                "query": query,
                "question_type": q_type.value,
                "description": type_descriptions.get(q_type, "未知"),
                "suggested_strategy": _get_strategy_for_type(q_type),
            },
        )
    
    except Exception as e:
        logger.error(f"问题分类失败: {e}", exc_info=True)
        return StandardResponse(
            success=False,
            message="分类失败",
            error=str(e),
        )


@router.post(
    "/entities",
    response_model=StandardResponse,
    summary="从查询中抽取实体",
    description="""
从用户查询文本中识别和提取关键实体。

使用双层策略：
1. 规则快速匹配（关键词表、正则表达式）→ 零延迟
2. LLM精确抽取（调用实体抽取服务） → 高准确率

返回信息包括：
- 实体名称和类型
- 抽取置信度
- 是否在数据库中找到匹配
""",
)
async def extract_entities_from_query(
    request: EntityExtractionRequest,
) -> StandardResponse:
    """从查询中提取实体。"""
    try:
        engine = get_graphrag_engine()
        
        entities = await engine.entity_extractor.extract_from_query(
            query=request.query,
            kb_id=request.kb_id,
        )
        
        return StandardResponse(
            success=True,
            message=f"成功提取 {len(entities)} 个实体",
            data={
                "query": request.query,
                "entities": [e.to_dict() for e in entities],
                "entity_count": len(entities),
            },
        )
    
    except Exception as e:
        logger.error(f"实体抽取失败: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"实体抽取失败: {str(e)}",
        )


@router.get(
    "/statistics",
    response_model=StandardResponse,
    summary="GraphRAG使用统计",
    description="""
获取GraphRAG引擎的使用统计信息和配置状态。
""",
)
async def get_graphrag_statistics(
) -> StandardResponse:
    """获取GraphRAG统计信息。"""
    try:
        engine = get_graphrag_engine()
        
        stats = {
            "configuration": {
                "vector_weight": engine.fusion_engine.vector_weight,
                "graph_weight": engine.fusion_engine.graph_weight,
                "entity_extraction_enabled": engine.enable_entity_extraction,
                "graph_expansion_enabled": engine.enable_graph_expansion,
                "llm_extraction_enabled": engine.entity_extractor.use_llm,
                "default_graph_depth": engine.context_expander.default_depth,
            },
            "capabilities": {
                "supported_question_types": [qt.value for qt in QuestionType],
                "entity_types_supported": [
                    "Person", "Organization", "Product", "Technology", 
                    "Location", "Date", "Event", "Concept"
                ],
                "fusion_strategies": [
                    "vector_weighted", "graph_enhanced", "diversity_adjusted"
                ],
            },
            "status": {
                "initialized": True,
                "cache_enabled": engine.entity_extractor.enable_cache,
                "cache_size": len(engine.entity_extractor._entity_cache),
            },
        }
        
        return StandardResponse(
            success=True,
            message="统计信息获取成功",
            data=stats,
        )
    
    except Exception as e:
        logger.error(f"获取统计信息失败: {e}", exc_info=True)
        return StandardResponse(
            success=False,
            message="获取统计信息失败",
            error=str(e),
        )


@router.get(
    "/health",
    response_model=StandardResponse,
    summary="GraphRAG健康检查",
    description="""
检查GraphRAG各组件的健康状态。
""",
)
async def health_check(
) -> StandardResponse:
    """健康检查。"""
    try:
        # 先触发引擎初始化（若尚未初始化），避免报告 engine_initialized: false
        engine = get_graphrag_engine()
        
        checks = {
            "engine_initialized": engine is not None,
            "entity_extractor_ready": engine.entity_extractor is not None,
            "context_expander_ready": engine.context_expander is not None,
            "fusion_engine_ready": engine.fusion_engine is not None,
            "rag_service_ready": engine.rag_service is not None,
        }
        
        all_healthy = all(checks.values())
        
        return StandardResponse(
            success=all_healthy,
            message="GraphRAG组件全部正常" if all_healthy else "部分组件未就绪",
            data={"checks": checks},
        )
    
    except Exception as e:
        logger.error(f"GraphRAG健康检查失败: {e}")
        return StandardResponse(
            success=False,
            message="健康检查失败",
            error=str(e),
            data={
                "checks": {
                    "engine_initialized": False,
                    "entity_extractor_ready": False,
                    "context_expander_ready": False,
                    "fusion_engine_ready": False,
                    "rag_service_ready": False,
                }
            },
        )


# ============================================================
# 辅助函数
# ============================================================


def _get_strategy_for_type(q_type: QuestionType) -> Dict[str, str]:
    """根据问题类型返回推荐的策略说明。"""
    strategies = {
        QuestionType.FACTOID: {
            "primary": "直接实体查询",
            "graph_usage": "查找实体的详细属性",
            "vector_usage": "补充背景信息",
            "example_query": "张三是谁？",
        },
        QuestionType.RELATION: {
            "primary": "路径查询",
            "graph_usage": "计算实体间的关系路径",
            "vector_usage": "验证关系的文档证据",
            "example_query": "张三和李四是什么关系？",
        },
        QuestionType.REASONING: {
            "primary": "子图查询 + LLM推理",
            "graph_usage": "提取相关子图作为推理依据",
            "vector_usage": "提供支撑文档",
            "example_query": "为什么张三能成功？",
        },
        QuestionType.LISTING: {
            "primary": "邻居查询",
            "graph_usage": "列举实体的相关邻居",
            "vector_usage": "确保完整性",
            "example_query": "列出所有AI工程师",
        },
        QuestionType.COMPARISON: {
            "primary": "双实体对比",
            "graph_usage": "分别查询两个实体的属性",
            "vector_usage": "查找对比相关的讨论",
            "example_query": "腾讯和阿里的AI技术对比",
        },
        QuestionType.DEFINITION: {
            "primary": "概念查询",
            "graph_usage": "查找概念的层次结构和属性",
            "vector_usage": "获取定义和解释文档",
            "example_query": "什么是深度学习？",
        },
        QuestionType.PROCEDURAL: {
            "primary": "步骤检索",
            "graph_usage": "查找前置依赖和工具",
            "vector_usage": "获取教程和指南文档",
            "example_query": "如何部署Neo4j？",
        },
        QuestionType.UNKNOWN: {
            "primary": "通用混合检索",
            "graph_usage": "尝试实体抽取和上下文扩展",
            "vector_usage": "主要依赖向量语义匹配",
            "example_query": "任意查询",
        },
    }
    return strategies.get(q_type, strategies[QuestionType.UNKNOWN])
