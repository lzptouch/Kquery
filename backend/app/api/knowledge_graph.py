"""
知识图谱 RESTful API 端点

提供完整的知识图谱CRUD操作和查询接口。
是前端应用与后端服务交互的统一入口。

API设计原则：
- RESTful风格，资源导向
- 统一的响应格式
- 完善的错误处理
- 分页支持（列表查询）

端点总览：
实体管理：
    GET    /api/v1/graph/entities              - 获取实体列表/搜索
    GET    /api/v1/graph/entities/{id}          - 获取单个实体详情
    POST   /api/v1/graph/entities/build         - 从文本构建图谱

关系查询：
    GET    /api/v1/graph/entities/{id}/neighbors - 获取邻居节点
    GET    /api/v1/graph/path                  - 查询路径
    GET    /api/v1/graph/subgraph               - 提取子图

图算法：
    GET    /api/v1/graph/algorithms/pagerank    - PageRank排序
    GET    /api/v1/graph/algorithms/communities - 社区发现

统计信息：
    GET    /api/v1/graph/statistics             - 图谱统计概览
    GET    /api/vi/graph/schema                 - Schema定义

作者：KQuery Pro Team
版本：1.0.0
更新：2026-04-11
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, HTTPException, Depends, Path
from typing import Any, Dict, List, Optional

from app.core.logger import get_logger
from app.models.graph import (
    BuildGraphRequest,
    BuildGraphFromCollectionRequest,
    SearchEntitiesRequest,
    SubgraphRequest,
    VisualizationRequest,
    PathQueryRequest,
)
from app.models.common import StandardResponse

# 导入项目模块
try:
    from app.repositories.graph.schema import (
        Entity, Relation, EntityType, RelationType, graph_schema
    )
    from app.services.graph.graph_builder import (
        GraphBuilderService, ConsistencyCheckResult
    )
    from app.services.graph.graph_query import (
        GraphQueryService, NeighborResult, SubgraphResult,
        PathResult, GraphAlgorithmResult
    )
    from app.services.graph.entity_extraction import ExtractedEntity, ExtractionResult
    from app.services.graph.relation_extraction import ExtractedRelation, RelationExtractionResult
    from app.repositories.graph.neo4j_connector import neo4j_manager
    from app.services.lineage_service import lineage_service
    from app.core.logger import get_logger
    from app.services.graphrag.visualization_service import (
        VisualizationService, VisualizationFormat, LayoutAlgorithm
    )

except ImportError as e:
    logging.warning(f"知识图谱模块导入失败: {e}")

    # 提供空壳类以便开发时不会报错
    class Entity:
        pass
    class GraphBuilderService:
        pass
    class GraphQueryService:
        pass


logger = get_logger(__name__)

# 创建路由器
router = APIRouter(
    prefix="/graph",
    tags=["KnowledgeGraph"],
    responses={404: {"description": "Not found"}}
)

# 延迟初始化服务实例
_builder_service: Optional[GraphBuilderService] = None
_query_service: Optional[GraphQueryService] = None


def get_builder_service() -> GraphBuilderService:
    """获取或初始化图谱构建服务"""
    global _builder_service
    if _builder_service is None:
        _builder_service = GraphBuilderService()
    return _builder_service


def get_query_service() -> GraphQueryService:
    """获取或初始化图谱查询服务"""
    global _query_service
    if _query_service is None:
        _query_service = GraphQueryService()
    return _query_service


# 请求/响应模型（已迁移至 app.models.graph / app.models.common）


# ==================== 实体相关端点 ====================

@router.get("/entities", response_model=StandardResponse)
async def search_or_list_entities(
    keyword: Optional[str] = Query(None, description="搜索关键词"),
    entity_type: Optional[str] = Query(None, description="过滤类型"),
    kb_id: Optional[int] = Query(None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """
    搜索或列出实体
    
    支持两种模式：
    1. 有keyword参数：执行模糊搜索
    2. 无keyword：返回分页列表（可按type过滤）
    """
    query_service = get_query_service()
    
    try:
        if keyword:
            # 搜索模式
            type_filter = EntityType(entity_type) if entity_type else None
            entities, total = await query_service.search_entities(
                keyword=keyword,
                entity_type=type_filter,
                kb_id=kb_id,
                limit=limit,
                offset=offset
            )
            
            return StandardResponse(
                success=True,
                data={
                    "items": [e.model_dump() for e in entities],
                    "total": total,
                    "page": offset // limit + 1,
                    "has_more": (offset + limit) < total
                }
            )
        
        else:
            # 列表模式（按类型分组统计）
            if entity_type:
                type_enum = EntityType(entity_type)
                entities = await query_service.get_entities_by_type(
                    entity_type=type_enum,
                    kb_id=kb_id,
                    limit=limit
                )
                return StandardResponse(
                    success=True,
                    data={
                        "type": entity_type,
                        "count": len(entities),
                        "items": [e.model_dump() for e in entities]
                    }
                )
            else:
                # 返回所有类型的统计
                overview = await query_service.get_graph_overview(kb_id)
                return StandardResponse(success=True, data=overview)
                
    except Exception as e:
        logger.error(f"实体查询失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/entities/{entity_id}", response_model=StandardResponse)
async def get_entity_detail(
    entity_id: str = Path(..., description="实体ID"),
    include_neighbors: bool = Query(default=False),
    neighbor_depth: int = Query(default=1, ge=1, le=3),
):
    """获取单个实体的详细信息"""
    query_service = get_query_service()
    
    # Step 1: 获取实体详情
    entity = await query_service.get_entity_by_id(entity_id)
    
    if not entity:
        raise HTTPException(status_code=404, detail=f"实体不存在: {entity_id}")
    
    result_data = {
        "entity": entity.model_dump(),
        "neighbors": None
    }
    
    # Step 2: 可选获取邻居
    if include_neighbors:
        neighbors_result = await query_service.get_neighbors(
            entity_id=entity_id,
            depth=neighbor_depth,
            limit_per_level=50
        )
        result_data["neighbors"] = {
            "total_count": neighbors_result.total_count,
            "by_distance": {
                str(k): [n.__dict__ for n in v]
                for k, v in neighbors_result.by_distance.items()
            },
            "by_relation_type": {
                k: [n.__dict__ for n in v]
                for k, v in neighbors_result.by_relation_type.items()
            }
        }
    
    return StandardResponse(success=True, data=result_data)


@router.post("/entities/build", response_model=StandardResponse)
async def build_graph_from_text(
    request: BuildGraphRequest,
):
    """从文本构建知识图谱"""
    builder = get_builder_service()
    
    try:
        # 执行完整的构建流程
        build_result = await builder.build_graph_from_text(
            text=request.text,
            doc_id=request.doc_id,
            kb_id=request.kb_id,
            context=request.context
        )
        
        return StandardResponse(
            success=build_result.success,
            data=build_result.to_dict(),
            message=(
                f"成功! 新增{build_result.entities_created}个实体, "
                f"{build_result.relations_created}条关系"
                if build_result.success 
                else "部分成功"
            ),
            error="\n".join(build_result.errors[:5]) if build_result.errors else None
        )
        
    except Exception as e:
        logger.error(f"图谱构建失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"构建失败: {str(e)}")


@router.post("/build-from-collection", response_model=StandardResponse)
async def build_graph_from_collection(
    request: BuildGraphFromCollectionRequest,
):
    """从 Milvus 集合中读取已有文档文本并构建知识图谱

    适用于「文档管理」页面中，用户已上传文档后点击「构建知识图谱」的场景。
    """
    from app.repositories.database.milvus_connector import get_milvus_connector
    from pymilvus import Collection
    from app.repositories.database.milvus_connector import get_milvus_connection

    try:
        connector = get_milvus_connector()
        connection_name = get_milvus_connection()

        if not connection_name or not connector.has_collection(request.collection_name):
            raise HTTPException(status_code=404, detail=f"集合 '{request.collection_name}' 不存在")

        collection = Collection(request.collection_name, using=connection_name)
        collection.load()

        # 读取文档块文本
        results = collection.query(
            expr="id != ''",
            output_fields=["text", "metadata"],
            limit=request.doc_limit,
        )

        if not results:
            raise HTTPException(status_code=400, detail="集合中没有文档数据")

        # 解析每个 chunk 的 metadata，提取文档标识
        all_text_parts = []
        file_names = set()
        doc_id_to_names: Dict[str, Set[str]] = {}

        for r in results:
            text = r.get("text", "")
            meta = r.get("metadata") or {}
            if not text or not text.strip():
                continue

            # 兼容多种可能的文档 ID 字段
            doc_id = (
                meta.get("doc_id")
                or meta.get("document_id")
                or meta.get("source")
                or meta.get("filename")
                or meta.get("file_name")
                or "unknown"
            )
            doc_id = str(doc_id).strip()

            # 如果指定了 document_ids，仅保留匹配的 chunk
            if request.document_ids and doc_id not in request.document_ids:
                continue

            fn = meta.get("filename") or meta.get("file_name") or meta.get("source") or doc_id
            if fn:
                file_names.add(fn)
                doc_id_to_names.setdefault(doc_id, set()).add(fn)

            all_text_parts.append({
                "text": text.strip(),
                "doc_id": doc_id,
            })

        if request.document_ids and not all_text_parts:
            raise HTTPException(
                status_code=400,
                detail=f"指定文档 {request.document_ids} 在集合中未找到对应文本块"
            )

        chunks = [item["text"] for item in all_text_parts]
        context = f"集合: {request.collection_name}, 文档: {', '.join(file_names)[:200]}"

        # 构建 KG
        builder = get_builder_service()

        if request.use_chunk_build:
            build_result = await builder.build_graph_from_chunks(
                chunks=chunks,
                doc_id=request.collection_name,
                kb_id=request.kb_id,
                context=context,
            )
        else:
            full_text = "\n".join(chunks[:request.doc_limit])
            build_result = await builder.build_graph_from_text(
                text=full_text,
                doc_id=request.collection_name,
                kb_id=request.kb_id,
                context=context,
            )

        # 诊断信息
        diagnostics = {
            "chunks_total": len(results),
            "chunks_used": len(all_text_parts),
            "chunks_filtered": len(results) - len(all_text_parts),
            "document_ids_in_request": request.document_ids,
            "document_id_to_names": {
                doc_id: list(names) for doc_id, names in doc_id_to_names.items()
            },
            "files": list(file_names),
        }

        # 智能提示消息
        if build_result.success:
            if build_result.entities_created > 0 or build_result.relations_created > 0:
                message = (
                    f"KG构建成功! 新增{build_result.entities_created}个实体, "
                    f"{build_result.relations_created}条关系"
                )
            elif build_result.entities_updated > 0:
                message = (
                    f"KG构建完成，无新增实体/关系（更新 {build_result.entities_updated} 个实体）。"
                    "可能是重复构建或文本中无可识别关系。"
                )
            else:
                message = (
                    "KG构建完成，但未抽取到实体/关系。"
                    "可能原因：文本中无可识别实体、LLM 未配置或文档内容不匹配。"
                )
        else:
            message = "KG部分完成，请检查后端日志"

        return StandardResponse(
            success=build_result.success,
            data={
                **build_result.to_dict(),
                "text_chunks_used": len(all_text_parts),
                "collection_name": request.collection_name,
                "files": list(file_names),
                "diagnostics": diagnostics,
            },
            message=message,
            error="\n".join(build_result.errors[:5]) if build_result.errors else None,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"从集合构建KG失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"KG构建失败: {str(e)}")


@router.get("/check-collection", response_model=StandardResponse)
async def check_collection_kg_status(
    collection_name: str = Query(..., description="Milvus集合名称"),
):
    """检查指定集合是否已构建知识图谱

    通过查询 Neo4j 中 source_doc 为该集合名称的实体数量来判断。
    """
    from app.repositories.graph.neo4j_connector import neo4j_manager

    try:
        connector = neo4j_manager.connector
        # 检查连接是否就绪（兼容 neo4j 驱动未安装的降级场景）
        if not hasattr(connector, '_driver') or not connector._driver:
            return StandardResponse(
                success=True,
                data={"kg_built": False, "entity_count": 0, "relation_count": 0},
                message="Neo4j 未连接",
            )

        async with connector._driver.session(database=connector.config.database) as session:
            # 统计实体
            entity_result = await session.run(
                "MATCH (e:Entity) WHERE e.source_doc = $cn RETURN count(e) AS cnt",
                cn=collection_name,
            )
            entity_row = await entity_result.single()
            entity_count = entity_row["cnt"] if entity_row else 0

            # 统计关系
            relation_result = await session.run(
                "MATCH ()-[r:RELATED_TO]-() WHERE r.source_doc = $cn RETURN count(r) AS cnt",
                cn=collection_name,
            )
            relation_row = await relation_result.single()
            relation_count = relation_row["cnt"] if relation_row else 0

        return StandardResponse(
            success=True,
            data={
                "kg_built": entity_count > 0,
                "entity_count": entity_count,
                "relation_count": relation_count,
                "collection_name": collection_name,
            },
            message=(
                f"已构建 KG ({entity_count} 实体, {relation_count} 关系)"
                if entity_count > 0
                else "尚未构建 KG"
            ),
        )

    except Exception as e:
        logger.error(f"检查集合KG状态失败: {e}", exc_info=True)
        # 降级返回：不要因为KG状态检查失败而阻塞用户
        return StandardResponse(
            success=True,
            data={"kg_built": False, "entity_count": 0, "relation_count": 0},
            message=f"查询KG状态失败: {str(e)[:100]}",
        )


@router.get("/entities/{entity_id}/neighbors", response_model=StandardResponse)
async def get_entity_neighbors(
    entity_id: str = Path(...),
    depth: int = Query(default=1, ge=1, le=3),
    relation_types: Optional[str] = Query(None, 
                                          description="逗号分隔的关系类型"),
):
    """获取实体的N跳邻居"""
    query_service = get_query_service()
    
    # 解析关系类型过滤器
    rel_types = None
    if relation_types:
        try:
            rel_types = [
                RelationType(t.strip()) 
                for t in relation_types.split(',') 
                if t.strip()
            ]
        except ValueError:
            raise HTTPException(status_code=400, detail="无效的关系类型")
    
    neighbors_result = await query_service.get_neighbors(
        entity_id=entity_id,
        depth=depth,
        relation_types=rel_types,
        limit_per_level=100
    )
    
    return StandardResponse(
        success=True,
        data={
            "center_entity_id": entity_id,
            "total_neighbors": neighbors_result.total_count,
            "max_depth_reached": neighbors_result.max_depth_reached,
            "by_distance": {
                str(k): [n.__dict__ for n in v]
                for k, v in neighbors_result.by_distance.items()
            },
            "neighbors": [n.__dict__ for n in neighbors_result.neighbors],
            "query_time_ms": round(neighbors_result.query_time_ms, 1)
        }
    )


# ==================== 关系和路径查询 ====================

@router.get("/path", response_model=StandardResponse)
async def find_path_between_entities(
    source_id: str = Query(..., description="起点实体ID"),
    target_id: str = Query(..., description="终点实体ID"),
    max_depth: int = Query(default=5, ge=1, le=6),
    find_all: bool = Query(default=False, description="查找所有路径还是最短路径"),
):
    """查询两个实体间的路径"""
    query_service = get_query_service()
    
    try:
        if find_all:
            path_result = await query_service.find_all_paths(
                source_id=source_id,
                target_id=target_id,
                max_depth=max_depth,
                limit=20
            )
        else:
            path_result = await query_service.find_shortest_path(
                source_id=source_id,
                target_id=target_id,
                max_depth=max_depth
            )
        
        paths_data = []
        for path_steps in path_result.paths:
            paths_data.append({
                "length": len(path_steps) - 1,
                "steps": [s.__dict__ for s in path_steps],
                "node_names": [s.node_name for s in path_steps]
            })
        
        return StandardResponse(
            success=True,
            data={
                "source_id": source_id,
                "target_id": target_id,
                "paths_found": path_result.total_paths_found,
                "shortest_path_length": path_result.shortest_path_length,
                "paths": paths_data,
                "query_time_ms": round(path_result.query_time_ms, 1)
            }
        )
        
    except Exception as e:
        logger.error(f"路径查询失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/subgraph", response_model=StandardResponse)
async def extract_subgraph(
    request: SubgraphRequest,
):
    """提取局部子图（用于可视化）"""
    query_service = get_query_service()
    
    subgraph_result = await query_service.get_subgraph(
        center_entity_ids=request.center_ids,
        radius=request.radius,
        max_nodes=request.max_nodes
    )
    
    # 可视化优化：增强节点大小、边权重、颜色分组、布局提示（v2.1）
    viz_nodes = []
    for n in subgraph_result.nodes:
        mention_count = n.get('mention_count', 0)
        node_size = 20 + min(mention_count * 2, 40) if mention_count else 20
        viz_nodes.append({
            "id": n['id'],
            "label": n['name'],
            "group": n['type'],
            "value": mention_count,
            "isCenter": n.get('isCenter', False),
            "size": node_size,
            "color": _get_entity_color(n.get('type', 'unknown')),
        })
    
    viz_links = []
    for e in subgraph_result.edges:
        weight = e.get('weight', 1)
        viz_links.append({
            "source": e['source'],
            "target": e['target'],
            "type": e['type'],
            "weight": weight,
            "label": e['type'],
            "width": 1 + min(weight, 5),
            "style": "solid" if weight > 2 else "dashed",
        })
    
    return StandardResponse(
        success=True,
        data={
            "statistics": {
                "total_nodes": subgraph_result.total_nodes,
                "total_edges": subgraph_result.total_edges,
                "center_count": len(subgraph_result.center_ids),
                "query_time_ms": round(subgraph_result.query_time_ms, 1)
            },
            "nodes": subgraph_result.nodes,
            "edges": subgraph_result.edges,
            
            # 前端D3.js/ECharts可直接使用的格式（v2.1 增强）
            "visualization": {
                "format_version": "2.1",
                "layout": "force-directed",
                "layout_params": {
                    "linkDistance": 100,
                    "chargeStrength": -300,
                    "collisionRadius": 30,
                },
                "color_scheme": "type-based",
                "nodes": viz_nodes,
                "links": viz_links,
            }
        }
    )


def _get_entity_color(entity_type: str) -> str:
    """根据实体类型返回可视化颜色。"""
    color_map = {
        "person": "#5470c6",
        "organization": "#91cc75",
        "location": "#fac858",
        "event": "#ee6666",
        "concept": "#73c0de",
        "product": "#3ba272",
        "technology": "#fc8452",
        "unknown": "#9a60b4",
    }
    return color_map.get(entity_type.lower(), "#9a60b4")


# ==================== 可视化端点 ====================

@router.post("/visualization", response_model=StandardResponse)
async def get_visualization_data(
    request: VisualizationRequest,
):
    """获取知识图谱可视化数据（支持多种格式和布局）。"""
    viz_service = VisualizationService()
    
    try:
        # 将请求格式转换为枚举
        try:
            viz_format = VisualizationFormat(request.format)
        except ValueError:
            viz_format = VisualizationFormat.D3_FORCE
        
        try:
            layout_algo = LayoutAlgorithm(request.layout)
        except ValueError:
            layout_algo = LayoutAlgorithm.FORCE
        
        # 调用可视化服务
        viz_data = await viz_service.get_visualization_data(
            center_ids=request.center_ids,
            radius=request.radius,
            max_nodes=request.max_nodes,
            output_format=viz_format,
            layout_algorithm=layout_algo,
            include_properties=request.include_properties,
            highlight_paths=[tuple(p) for p in request.highlight_paths] if request.highlight_paths else None,
        )
        
        return StandardResponse(
            success=True,
            data=viz_data.to_dict(target_format=viz_format),
            message=f"可视化数据准备完成: {viz_data.node_count}节点, {viz_data.edge_count}边"
        )
    
    except Exception as e:
        logger.error(f"可视化数据生成失败: {e}", exc_info=True)
        return StandardResponse(
            success=False,
            data=None,
            message=f"可视化数据生成失败: {str(e)}"
        )


@router.get("/visualization/formats", response_model=StandardResponse)
async def list_visualization_formats(
):
    """列出支持的可视化格式和布局算法。"""
    return StandardResponse(
        success=True,
        data={
            "formats": [
                {"id": "d3_force", "name": "D3.js Force-Directed", "description": "力导向图，适合探索式浏览"},
                {"id": "cytoscape", "name": "Cytoscape.js", "description": "网络分析可视化，适合复杂关系分析"},
                {"id": "echarts_graph", "name": "ECharts Graph", "description": "关系图，适合展示社群结构"},
                {"id": "vis_network", "name": "vis.js Network", "description": "交互式网络，适合动态展示"},
                {"id": "raw_json", "name": "Raw JSON", "description": "原始JSON数据，通用格式"},
            ],
            "layouts": [
                {"id": "force", "name": "力导向布局", "description": "基于物理引力的自动布局，适合一般探索"},
                {"id": "hierarchical", "name": "层级布局", "description": "树形层级结构，适合组织结构图"},
                {"id": "circular", "name": "圆形布局", "description": "节点均匀分布在圆周上，适合社区发现"},
                {"id": "radial", "name": "放射状布局", "description": "以中心节点向外辐射，适合展示影响力"},
                {"id": "grid", "name": "网格布局", "description": "规整网格排列，适合节点数量较少"},
                {"id": "concentric", "name": "同心圆布局", "description": "按层级分布在同心圆上，适合分层结构"},
            ]
        }
    )


# ==================== 图算法端点 ====================

@router.get("/algorithms/pagerank", response_model=StandardResponse)
async def compute_entity_importance(
    limit: int = Query(default=20, ge=5, le=100),
    kb_id: Optional[int] = None,
):
    """PageRank算法 - 计算实体重要性排序"""
    query_service = get_query_service()
    
    pr_result = await query_service.compute_pagerank(
        kb_id=kb_id,
        limit=limit
    )
    
    return StandardResponse(
        success=True,
        data={
            "algorithm": pr_result.algorithm_name,
            "execution_time_ms": round(pr_result.execution_time_ms, 1),
            "top_entities": pr_result.results,
            "statistics": pr_result.statistics
        },
        message=f"计算完成! Top {len(pr_result.results)} 个重要实体"
    )


@router.get("/algorithms/communities", response_model=StandardResponse)
async def discover_communities(
    algorithm: str = Query(default="louvain",
                              description="louvain | label_propagation"),
    min_size: int = Query(default=3, ge=2),
    kb_id: Optional[int] = None,
):
    """社区发现 - 将图谱划分为紧密联系的群体"""
    query_service = get_query_service()
    
    community_result = await query_service.find_communities(
        algorithm=algorithm,
        min_community_size=min_size,
        kb_id=kb_id
    )
    
    return StandardResponse(
        success=True,
        data={
            "algorithm": community_result.algorithm_name,
            "execution_time_ms": round(community_result.execution_time_ms, 1),
            "communities": community_result.results,
            "statistics": community_result.statistics
        },
        message=f"发现 {len(community_result.results)} 个社区"
    )


# ==================== 统计和Schema ====================

@router.get("/statistics", response_model=StandardResponse)
async def get_graph_statistics(
    kb_id: Optional[int] = None,
):
    """获取知识图谱全局统计"""
    builder = get_builder_service()
    query_service = get_query_service()
    
    # 并行执行多个统计查询
    import asyncio
    
    stats_task = builder.get_graph_statistics(kb_id)
    overview_task = query_service.get_graph_overview(kb_id)
    
    stats, overview = await asyncio.gather(stats_task, overview_task)
    
    # 整合结果
    combined_stats = {}
    
    # 来自GraphBuilder的详细统计
    if isinstance(stats, dict):
        for key, value in stats.items():
            if isinstance(value, list):
                combined_stats[key] = value
            else:
                combined_stats[key] = value or 0
    
    # 来自GraphQuery的概览
    if isinstance(overview, dict):
        combined_stats.update({k: v for k, v in overview.items() if k not in combined_stats})
    
    return StandardResponse(
        success=True,
        data=combined_stats,
        message="统计信息获取成功"
    )


@router.get("/schema", response_model=StandardResponse)
async def get_knowledge_graph_schema(
):
    """获取知识图谱的Schema定义（实体类型、关系类型等）"""
    return StandardResponse(
        success=True,
        data={
            "entity_types": graph_schema.get_all_entity_types(),
            "relation_types": graph_schema.get_all_relation_types(),
            "schema_statistics": graph_schema.get_statistics(),
            "version": "2.0.0"
        }
    )


@router.get("/health")
async def check_graph_health():
    """检查Neo4j连接健康状态"""
    from fastapi.responses import JSONResponse
    health = await neo4j_manager.health_check()

    status_code = 200 if health.get("connected") else 503
    message = (
        f"Neo4j连接正常, 延迟: {health.get('latency_ms')}ms"
        if health.get("connected")
        else "Neo4j连接异常"
    )

    return JSONResponse(
        status_code=status_code,
        content={
            "success": health.get("connected", False),
            "data": health,
            "message": message,
        },
    )


# ==================== 一致性检查 ====================

@router.post("/consistency-check", response_model=StandardResponse)
async def perform_consistency_check(
    kb_id: Optional[int] = None,
    auto_fix: bool = Query(default=False, 
                                description="是否自动修复问题"),
):
    """执行图谱一致性检查"""
    builder = get_builder_service()
    
    check_result = await builder.check_consistency(kb_id)
    
    issues_summary = {
        "is_consistent": check_result.is_consistent,
        "total_issues": check_result.total_issues,
        "issue_breakdown": {
            "isolated_nodes": len(check_result.isolated_nodes),
            "orphan_nodes": len(check_result.orphan_nodes),
            "duplicate_entities": len(check_result.duplicate_entities),
            "low_confidence_entities": len(check_result.low_confidence_entities)
        },
        "sample_issues": {
            "isolated_samples": check_result.isolated_nodes[:10],
            "duplicate_samples": check_result.duplicate_entities[:10]
        } if check_result.has_issues else {}
    }
    
    return StandardResponse(
        success=check_result.is_consistent,
        data=issues_summary,
        message=(
            "✅ 图谱一致性检查通过" 
            if not check_result.has_issues 
            else f"⚠️ 发现 {check_result.total_issues} 个问题"
        )
    )


# ==================== 数据血缘追溯（v2.1 新增）====================

@router.get("/lineage/{document_id}", response_model=StandardResponse)
async def get_document_lineage(
    document_id: str = Path(..., description="文档ID"),
):
    """获取文档的数据血缘信息。
    
    追溯文档处理过程中生成的分块、实体和关系。
    """
    lineage = await lineage_service.get_lineage(document_id)
    
    if not lineage:
        raise HTTPException(
            status_code=404,
            detail=f"未找到文档 {document_id} 的血缘记录"
        )
    
    return StandardResponse(
        success=True,
        data=lineage.to_dict(),
        message="血缘信息获取成功"
    )


@router.get("/lineage/{document_id}/upstream", response_model=StandardResponse)
async def trace_lineage_upstream(
    document_id: str = Path(..., description="文档ID"),
    target_type: str = Query(default="entity", description="目标类型: entity | chunk | relationship"),
):
    """追溯文档的上游血缘链路。
    
    查找指定元素的来源文档和分块。
    """
    upstream = await lineage_service.trace_upstream(document_id, target_type)
    
    return StandardResponse(
        success=True,
        data={
            "target_id": document_id,
            "target_type": target_type,
            "upstream_chain": upstream,
        },
        message=f"找到 {len(upstream)} 条上游链路"
    )


# 导出路由器供main.py使用
__all__ = ['router']
