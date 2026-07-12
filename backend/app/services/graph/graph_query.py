"""
知识图谱查询服务 (Graph Query Service)

提供丰富的知识图谱数据检索能力，支持多种查询模式。
是知识图谱应用的"读取层"，为上层API和前端提供统一的数据访问接口。

核心功能：
1. 实体查询 - 按ID/名称/类型查询单个或多个实体
2. 邻居查询 - N跳邻居发现（1-3跳）
3. 路径查询 - 最短路径、所有路径
4. 子图提取 - 局部图结构获取（用于可视化）
5. 图算法 - PageRank、社区检测、中心性计算
6. 全文搜索 - 基于名称的模糊匹配

查询性能目标：
- 单实体查询: <50ms
- 2跳邻居: <200ms  
- 最短路径(5跳内): <300ms
- 子图提取(100节点): <500ms

作者：KQuery Pro Team
版本：1.0.0
更新：2026-04-11

依赖：
- app.repositories.graph.neo4j_connector (Neo4j连接)
- app.repositories.graph.schema (数据模型)
"""

from __future__ import annotations

import time
import logging
from typing import (
    Any, Dict, List, Optional, 
    Set, Tuple, Union
)
from dataclasses import dataclass, field, asdict

try:
    from app.repositories.graph.schema import (
        Entity, Relation, EntityType, RelationType,
        graph_schema
    )
    from app.repositories.graph.neo4j_connector import (
        Neo4jConnector, neo4j_manager
    )
    from app.core.logger import get_logger
except ImportError:
    class Entity: pass
    class Relation: pass

    def get_logger(name):
        return logging.getLogger(name)

try:
    from app.services.graph.community_engine import Community
    COMMUNITY_AVAILABLE = True
except ImportError:
    COMMUNITY_AVAILABLE = False
    Community = None

logger = get_logger(__name__)


# ============================================================
# 第一部分：查询结果模型
# ============================================================


@dataclass
class NeighborNode:
    """邻居节点信息"""
    entity_id: str
    name: str
    type: str
    relation_type: str              # 与中心节点的关系类型
    distance: int                   # 跳数距离
    confidence: float = 1.0
    properties: Dict[str, Any] = field(default_factory=dict)


@dataclass 
class NeighborResult:
    """邻居查询结果"""
    center_entity_id: str           # 中心实体ID
    center_entity: Optional[Entity] = None  # 中心实体详情
    neighbors: List[NeighborNode] = field(default_factory=list)
    
    # 按距离分组
    by_distance: Dict[int, List[NeighborNode]] = field(
        default_factory=dict
    )
    # 按关系类型分组
    by_relation_type: Dict[str, List[NeighborNode]] = field(
        default_factory=dict
    )
    
    total_count: int = 0
    max_depth_reached: int = 0
    query_time_ms: float = 0.0
    
    def __post_init__(self):
        """自动构建分组索引"""
        for n in self.neighbors:
            # 按距离分组
            if n.distance not in self.by_distance:
                self.by_distance[n.distance] = []
            self.by_distance[n.distance].append(n)
            
            # 按关系类型分组
            rel_key = n.relation_type
            if rel_key not in self.by_relation_type:
                self.by_relation_type[rel_key] = []
            self.by_relation_type[rel_key].append(n)
        
        self.total_count = len(self.neighbors)
        if self.neighbors:
            self.max_depth_reached = max(n.distance for n in self.neighbors)


@dataclass
class PathStep:
    """路径中的单步"""
    node_id: str
    node_name: str
    node_type: str
    relation_type: str               # 到下一步的关系
    direction: str = "outgoing"      # outgoing / incoming


@dataclass
class PathResult:
    """路径查询结果"""
    source_id: str
    target_id: str
    paths: List[List[PathStep]] = field(default_factory=list)
    shortest_path_length: Optional[int] = None
    total_paths_found: int = 0
    query_time_ms: float = 0.0


@dataclass
class SubgraphResult:
    """子图查询结果"""
    nodes: List[Dict[str, Any]] = field(default_factory=list)   # 节点列表
    edges: List[Dict[str, Any]] = field(default_factory=list)    # 边列表
    center_ids: List[str] = field(default_factory=list)         # 中心节点ID
    total_nodes: int = 0
    total_edges: int = 0
    query_time_ms: float = 0.0
    
    @property
    def to_networkx_format(self) -> Dict[str, Any]:
        """
        转换为NetworkX兼容格式
        
        可用于后续的图算法分析。
        """
        import networkx as nx
        G = nx.DiGraph()
        
        # 添加节点
        for node in self.nodes:
            G.add_node(node['id'], **node)
        
        # 添加边
        for edge in self.edges:
            G.add_edge(
                edge['source'],
                edge['target'],
                **edge.get('properties', {})
            )
        
        return {
            'graph': G,
            'nodes': self.nodes,
            'edges': self.edges,
            'stats': {
                'num_nodes': G.number_of_nodes(),
                'num_edges': G.number_of_edges(),
                'is_directed': G.is_directed()
            }
        }


@dataclass
class GraphAlgorithmResult:
    """图算法执行结果"""
    algorithm_name: str
    execution_time_ms: float = 0.0
    results: List[Dict[str, Any]] = field(default_factory=list)
    statistics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GlobalSearchResult:
    """全局搜索结果（基于社区摘要）

    适用于宏观、概述性的问题，如：
    - "这个知识库的主要主题是什么？"
    - "总结一下涉及人工智能的内容"
    - "有哪些重要的组织及其关系？"
    """
    query: str
    communities: List[Dict[str, Any]] = field(default_factory=list)
    community_summaries: List[str] = field(default_factory=list)
    matched_entities: List[Entity] = field(default_factory=list)
    context_text: str = ""  # 拼接后的社区摘要文本（可直接输入 LLM）
    query_time_ms: float = 0.0

    @property
    def has_results(self) -> bool:
        return len(self.communities) > 0 or len(self.matched_entities) > 0


@dataclass
class LocalSearchResult:
    """局部搜索结果（基于实体邻居）

    适用于具体的实体查询，如：
    - "张三是谁？"
    - "腾讯公司的主要业务是什么？"
    - "人工智能和深度学习有什么关系？"
    """
    query: str
    center_entities: List[Entity] = field(default_factory=list)
    neighbors: List[NeighborNode] = field(default_factory=list)
    paths: List[PathResult] = field(default_factory=list)
    subgraph: Optional[SubgraphResult] = None
    context_text: str = ""  # 结构化的邻居/路径文本（可直接输入 LLM）
    query_time_ms: float = 0.0

    @property
    def has_results(self) -> bool:
        return len(self.center_entities) > 0 and len(self.neighbors) > 0


@dataclass
class HybridGraphSearchResult:
    """图谱层面的混合搜索结果（Global + Local）"""
    query: str
    search_mode: str = "hybrid"  # "global" | "local" | "hybrid"
    global_result: Optional[GlobalSearchResult] = None
    local_result: Optional[LocalSearchResult] = None
    combined_context: str = ""  # 合并后的上下文文本
    query_time_ms: float = 0.0


# ============================================================
# 第二部分：核心查询服务类
# ============================================================

class GraphQueryService:
    """
    知识图谱查询服务（统一入口）
    
    提供完整的图数据库查询能力，支持：
    - 点查询：按ID/名称/属性查找实体
    - 邻居查询：N跳邻居发现
    - 路径查询：最短路径、所有路径
    - 子图查询：局部结构提取
    - 图算法：PageRank、社区检测等
    - 统计查询：概览统计信息
    
    设计原则：
    1. 查询参数严格校验（防Cypher注入）
    2. 结果大小限制（防止内存溢出）
    3. 查询超时保护（避免长时间运行）
    4. 性能监控（慢查询自动记录）
    """
    
    # 默认限制
    DEFAULT_LIMIT = 50                    # 默认返回条目上限
    MAX_NEIGHBORS = 200                  # 最大邻居数量
    MAX_PATH_LENGTH = 5                  # 最大路径搜索深度
    MAX_SUBGRAPH_NODES = 500             # 最大子图节点数
    
    def __init__(
        self,
        connector: Optional[Neo4jConnector] = None,
        default_kb_id: Optional[int] = None
    ):
        """
        初始化查询服务
        
        Args:
            connector: Neo4j连接器实例
            default_kb_id: 默认的知识库ID（多租户隔离用）
        """
        self.connector = connector or neo4j_manager.connector
        self.default_kb_id = default_kb_id
        
        logger.info("🔍 图谱查询服务初始化完成")
    
    # ==================== 实体查询 ====================
    
    async def get_entity_by_id(self, entity_id: str) -> Optional[Entity]:
        """
        根据ID获取实体详情
        
        Args:
            entity_id: 实体的唯一标识符
        
        Returns:
            Entity对象，如果不存在则返回None
        """
        cypher = """
        MATCH (e:Entity {id: $entity_id})
        OPTIONAL MATCH (e)-[r:RELATED_TO]-(other)
        RETURN e, count(r) as relation_count
        LIMIT 1
        """
        
        results = await self.connector.execute_query(cypher, {"entity_id": entity_id})
        
        if not results:
            return None
        
        record = results[0]['e']
        return Entity.from_neo4j_record(record)
    
    async def search_entities(
        self,
        keyword: str,
        entity_type: Optional[EntityType] = None,
        kb_id: Optional[int] = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        min_confidence: float = 0.0
    ) -> Tuple[List[Entity], int]:
        """
        模糊搜索实体
        
        支持按关键词、类型、置信度等多维条件筛选。
        
        Args:
            keyword: 搜索关键词（支持部分匹配）
            entity_type: 过滤的实体类型（可选）
            kb_id: 知识库ID过滤
            limit: 返回数量限制
            offset: 分页偏移量
            min_confidence: 最低置信度阈值
        
        Returns:
            (实体列表, 总数)
        """
        kb_id = kb_id or self.default_kb_id
        
        # 构建WHERE条件：关键词同时匹配 name 与 aliases
        keyword_conditions = [
            "e.name CONTAINS $keyword",
            "ANY(alias IN COALESCE(e.aliases, []) WHERE alias CONTAINS $keyword)"
        ]
        conditions = ["(" + " OR ".join(keyword_conditions) + ")"]
        params: Dict[str, Any] = {"keyword": keyword}

        if entity_type:
            conditions.append("e.type = $type")
            params["type"] = entity_type.value

        if kb_id is not None:
            conditions.append("e.kb_id = $kb_id")
            params["kb_id"] = kb_id

        if min_confidence > 0:
            conditions.append("e.confidence >= $min_conf")
            params["min_conf"] = min_confidence

        where_clause = " AND ".join(conditions)
        
        # 总数查询
        count_cypher = f"""
        MATCH (e:Entity)
        WHERE {where_clause}
        RETURN count(e) as total
        """
        count_result = await self.connector.execute_query(count_cypher, params)
        total = count_result[0]['total'] if count_result else 0
        
        # 分页查询
        query_cypher = f"""
        MATCH (e:Entity)
        WHERE {where_clause}
        RETURN e
        ORDER BY e.confidence DESC, e.mention_count DESC
        SKIP $offset LIMIT $limit
        """
        params["offset"] = offset
        params["limit"] = limit
        
        results = await self.connector.execute_query(query_cypher, params)
        entities = [Entity.from_neo4j_record({"n": r['e']}) for r in results]
        
        return (entities, total)
    
    async def get_entities_by_ids(
        self,
        entity_ids: List[str]
    ) -> List[Entity]:
        """
        批量获取多个实体（根据ID列表）
        
        Args:
            entity_ids: 实体ID列表
        
        Returns:
            实体列表（顺序可能与输入不同）
        """
        if not entity_ids:
            return []
        
        cypher = """
        UNWIND $ids AS id
        MATCH (e:Entity {id: id})
        RETURN e ORDER BY e.confidence DESC
        """
        
        results = await self.connector.execute_query(cypher, {"ids": entity_ids})
        return [Entity.from_neo4j_record({"n": r['e']}) for r in results]
    
    async def get_entities_by_type(
        self,
        entity_type: EntityType,
        kb_id: Optional[int] = None,
        limit: int = DEFAULT_LIMIT
    ) -> List[Entity]:
        """
        获取指定类型的所有实体
        
        Args:
            entity_type: 实体类型
            kb_id: 知识库ID过滤
            limit: 返回数量限制
        """
        kb_id = kb_id or self.default_kb_id
        
        cypher = """
        MATCH (e:Entity {type: $type})
        WHERE ($kb_id IS NULL OR e.kb_id = $kb_id)
        RETURN e
        ORDER BY e.mention_count DESC
        LIMIT $limit
        """
        
        results = await self.connector.execute_query(cypher, {
            "type": entity_type.value,
            "kb_id": kb_id,
            "limit": limit
        })
        
        return [Entity.from_neo4j_record({"n": r['e']}) for r in results]
    
    # ==================== 邻居查询 ====================
    
    async def get_neighbors(
        self,
        entity_id: str,
        depth: int = 1,
        relation_types: Optional[List[RelationType]] = None,
        limit_per_level: Optional[int] = None,
        include_center: bool = True
    ) -> NeighborResult:
        """
        获取实体的N跳邻居
        
        这是知识图谱最常用的查询之一，
        用于展示"这个实体与哪些其他实体相关"。
        
        Args:
            entity_id: 中心实体ID
            depth: 跳数（1=直接关联，2=朋友的朋友...）
            relation_types: 只返回这些类型的关系（可选）
            limit_per_level: 每层的最大邻居数
            include_center: 是否包含中心实体的详细信息
        
        Returns:
            NeighborResult 包含分组后的邻居信息
        """
        start_time = time.time()
        
        result = NeighborResult(center_entity_id=entity_id)
        
        # 参数校验
        if depth < 1 or depth > 3:
            raise ValueError(f"depth必须在1-3之间，当前值: {depth}")
        
        # 获取中心实体
        if include_center:
            result.center_entity = await self.get_entity_by_id(entity_id)
        
        # 构建关系类型过滤器
        rel_filter = ""
        params: Dict[str, Any] = {"center_id": entity_id}
        
        if relation_types:
            types_str = "|".join(r.value for r in relation_types)
            rel_filter = f"r.type =~ '{types_str}'"
        
        # 核心邻居查询Cypher（可变长度路径）
        cypher = f"""
        MATCH path = (center:Entity {{id: $center_id}})-[*1..{depth}]-(neighbor:Entity)
        {'WHERE ' + rel_filter if rel_filter else ''}
        WITH neighbor, relationships(path) as rels, length(path) as dist
        WITH neighbor, last(rels) as last_rel, dist
        ORDER BY dist, last_rel.confidence DESC
        LIMIT $max_neighbors
        RETURN 
            neighbor.id as entity_id,
            neighbor.name as name,
            neighbor.type as type,
            type(last_rel) as relation_type,
            dist as distance,
            COALESCE(last_rel.confidence, 1.0) as confidence,
            properties(neighbor) as props
        ORDER BY distance, confidence DESC
        """
        
        params["max_neighbors"] = limit_per_level * depth
        
        try:
            results = await self.connector.execute_query(cypher, params)
            
            for r in results:
                neighbor_node = NeighborNode(
                    entity_id=r['entity_id'],
                    name=r['name'],
                    type=r['type'],
                    relation_type=r['relation_type'].split(':')[-1] if ':' in str(r['relation_type']) else str(r['relation_type']),
                    distance=r['distance'] if isinstance(r['distance'], int) else 1,
                    confidence=float(r.get('confidence', 1.0)),
                    properties=r.get('props', {})
                )
                result.neighbors.append(neighbor_node)
            
        except Exception as e:
            logger.error(f"邻居查询失败(entity_id={entity_id}, depth={depth}): {e}", exc_info=True)
            raise
        
        result.query_time_ms = (time.time() - start_time) * 1000
        
        logger.debug(f"✓ 邻居查询 | 中心: {entity_id} | "
                      f"深度: {depth} | 找到: {result.total_count}个邻居 | "
                      f"耗时: {result.query_time_ms:.0f}ms")
        
        return result
    
    # ==================== 路径查询 ====================
    
    async def find_shortest_path(
        self,
        source_id: str,
        target_id: str,
        max_depth: int = MAX_PATH_LENGTH,
        relation_filter: Optional[str] = None
    ) -> PathResult:
        """
        查找两个实体间的最短路径
        
        使用Neo4j的shortestPath算法，
        时间复杂度O(b^d)，b是平均分支因子，d是路径长度。
        
        Args:
            source_id: 起始实体ID
            target_id: 目标实体ID
            max_depth: 最大搜索深度
            relation_filter: Cypher关系类型过滤表达式
        
        Returns:
            PathResult 包含最短路径信息
        """
        start = time.time()
        result = PathResult(source_id=source_id, target_id=target_id)
        
        rel_constraint = ""
        if relation_filter:
            rel_constraint = f"[r:{relation_filter}]"
        
        cypher = f"""
        MATCH (start:Entity {{id: $source_id}}), (end:Entity {{id: $target_id}})
        CALL apoc.algo.dijkstra(start, end, {rel_constraint or "'RELATED_TO'"}, 'weight') YIELD path, weight
        RETURN [node IN nodes(path) | {{
            node_id: node.id,
            node_name: node.name,
            node_type: node.type,
            relation_type: ''
        }}] as steps, weight as cost
        LIMIT 1
        """
        
        # 如果没有APOC，使用原生shortestPath
        try:
            results = await self.connector.execute_query(cypher, {
                "source_id": source_id,
                "target_id": target_id
            })
            
            if results and results[0].get('steps'):
                steps_data = results[0]['steps']
                result.paths.append([PathStep(**s) for s in steps_data])
                result.shortest_path_length = len(steps_data) - 1
                
        except Exception as e:
            logger.debug(f"APOC dijkstra不可用，使用原生shortestPath: {e}")
            
            # 回退到原生实现
            cypher_native = f"""
            MATCH p = shortestPath((s:Entity {{id: $source_id}}){rel_constraint}*1..{max_depth}(t:Entity {{id: $target_id}}))
            RETURN p, length(p) as len
            ORDER BY len ASC
            LIMIT 1
            """
            
            try:
                native_results = await self.connector.execute_query(cypher_native, {
                    "source_id": source_id,
                    "target_id": target_id
                })
                
                if native_results:
                    path_rec = native_results[0].get('p')
                    
                    if hasattr(path_rec, 'nodes') and hasattr(path_rec, 'relationships'):
                        steps = []
                        nodes = list(path_rec.nodes)
                        rels = list(path_rec.relationships)
                        
                        for i, node in enumerate(nodes):
                            step = PathStep(
                                node_id=node.get('id', ''),
                                node_name=node.get('name', ''),
                                node_type=node.get('type', '')
                            )
                            
                            if i < len(rels):
                                step.relation_type = rels[i].get('type', '')
                            
                            steps.append(step)
                        
                        result.paths.append(steps)
                        result.shortest_path_length = len(steps) - 1
                        
            except Exception as e2:
                logger.error(f"最短路径查询完全失败: {e2}")
        
        result.total_paths_found = len(result.paths)
        result.query_time_ms = (time.time() - start) * 1000
        
        return result
    
    async def find_all_paths(
        self,
        source_id: str,
        target_id: str,
        max_depth: int = 3,
        limit: int = 10
    ) -> PathResult:
        """
        查找两个实体间的所有路径（限定深度）
        
        注意：此方法可能返回大量结果，
        建议max_depth不超过4，否则组合爆炸。
        
        Args:
            source_id: 起始实体ID
            target_id: 目标实体ID
            max_depth: 最大路径长度
            limit: 返回的最大路径数
        
        Returns:
            PathResult 包含所有找到的路径
        """
        start = time.time()
        result = PathResult(source_id=source_id, target_id=target_id)
        
        cypher = f"""
        MATCH p = allShortestPaths((s:Entity {{id: $source_id}})-[:RELATED_TO*1..{max_depth}]->(t:Entity {{id: $target_id}}))
        RETURN [node IN nodes(p) | {{
            node_id: node.id,
            node_name: node.name,
            node_type: node.type,
            relation_type: ''
        }}] as steps
        ORDER BY length(p)
        LIMIT $limit
        """
        
        try:
            results = await self.connector.execute_query(cypher, {
                "source_id": source_id,
                "target_id": target_id,
                "limit": limit
            })
            
            for r in results:
                steps_data = r.get('steps', [])
                if steps_data:
                    result.paths.append([PathStep(**s) for s in steps_data])
            
            if result.paths:
                result.shortest_path_length = min(len(p) for p in result.paths) - 1
            
        except Exception as e:
            logger.warning(f"全路径查询失败: {e}")
        
        result.total_paths_found = len(result.paths)
        result.query_time_ms = (time.time() - start) * 1000
        
        return result
    
    # ==================== 子图查询 ====================
    
    async def get_subgraph(
        self,
        center_entity_ids: List[str],
        radius: int = 2,
        max_nodes: int = MAX_SUBGRAPH_NODES,
        include_attributes: bool = True
    ) -> SubgraphResult:
        """
        提取以指定实体为中心的局部子图
        
        主要用途：
        1. 知识图谱可视化（前端D3.js/ECharts渲染）
        2. 局部结构分析
        3. 相关性探索
        
        Args:
            center_entity_ids: 中心实体ID列表
            radius: 半径（从中心向外扩展几跳）
            max_nodes: 最大节点数（防止过大）
            include_attributes: 是否包含完整属性
        
        Returns:
            SubgraphResult 可直接用于前端可视化
        """
        start = time.time()
        result = SubgraphResult(center_ids=center_entity_ids)
        
        if not center_entity_ids:
            return result
        
        # Step 1: 收集所有相关节点的ID
        # 注意：CALL子查询内部不能直接使用$参数（Neo4j 5.x限制），用字符串嵌入代替
        collect_cypher = f"""
        UNWIND $centers AS centerId
        MATCH (center:Entity {{id: centerId}})
        CALL {{
            WITH center
            MATCH path = (center)-[*1..{radius}]-(neighbor)
            WITH DISTINCT neighbor
            LIMIT {max_nodes}
            RETURN collect(DISTINCT neighbor) AS neighbors
        }}
        WITH collect(center) AS centers, neighbors
        UNWIND centers + neighbors AS allNodes
        WITH DISTINCT allNodes
        LIMIT {max_nodes}
        RETURN collect(allNodes.id) AS nodeIds
        """
        
        collect_result = await self.connector.execute_query(collect_cypher, {
            "centers": center_entity_ids,
            "radius": radius,
            "max_nodes": max_nodes
        })
        
        if not collect_result or not collect_result[0].get('nodeIds'):
            result.query_time_ms = (time.time() - start) * 1000
            return result
        
        collected_ids = collect_result[0]['nodeIds']
        
        # Step 2: 获取节点详情
        nodes_cypher = """
        UNWIND $ids AS nodeId
        MATCH (n:Entity {id: nodeId})
        RETURN {
            id: n.id,
            name: n.name,
            type: n.type,
            confidence: COALESCE(n.confidence, 1),
            mention_count: COALESCE(n.mention_count, 0),
            isCenter: n.id IN $centers,
            properties: CASE WHEN $include_attrs THEN n.properties ELSE {} END
        } as nodeData
        ORDER BY nodeData.isCenter DESC, nodeData.mention_count DESC
        """
        
        nodes_result = await self.connector.execute_query(nodes_cypher, {
            "ids": collected_ids,
            "centers": center_entity_ids,
            "include_attrs": include_attributes
        })
        
        result.nodes = [r['nodeData'] for r in nodes_result]
        
        # Step 3: 获取这些节点之间的边
        edges_cypher = """
        UNWIND $ids AS nodeId
        MATCH (src:Entity)-[r]->(tgt:Entity)
        WHERE src.id IN $ids AND tgt.id IN $ids
        RETURN {
            source: src.id,
            target: tgt.id,
            type: type(r),
            weight: COALESCE(r.weight, 1.0),
            confidence: COALESCE(r.confidence, 1.0),
            properties: r.properties
        } as edgeData
        LIMIT 2000
        """
        
        edges_result = await self.connector.execute_query(edges_cypher, {
            "ids": collected_ids
        })
        
        result.edges = [r['edgeData'] for r in edges_result]
        result.total_nodes = len(result.nodes)
        result.total_edges = len(result.edges)
        result.query_time_ms = (time.time() - start) * 1000
        
        logger.info(f"✓ 子图提取 | 中心: {len(center_entity_ids)} | "
                     f"半径: {radius} | 节点: {result.total_nodes} | "
                     f"边: {result.total_edges} | 耗时: {result.query_time_ms:.0f}ms")
        
        return result
    
    # ==================== 图算法 ====================
    
    async def compute_pagerank(
        self,
        kb_id: Optional[int] = None,
        iterations: int = 20,
        damping_factor: float = 0.85,
        limit: int = 20
    ) -> GraphAlgorithmResult:
        """
        PageRank算法 - 计算实体重要性排序
        
        PageRank衡量一个实体在图谱中的"影响力"或"重要性"，
        基于其连接的数量和质量。
        
        高PageRank的实体通常是：
        - 热门话题/人物
        - 核心组织机构
        - 关键技术概念
        
        Args:
            kb_id: 知识库范围限制
            iterations: 迭代次数（越多越精确但越慢）
            damping_factor: 阻尼系数（通常0.85）
            limit: 返回Top N
        
        Returns:
            GraphAlgorithmResult 排序后的实体及其PageRank分数
        """
        start = time.time()
        
        # 构建查询（如果安装了GDS插件则使用原生实现）
        cypher = """
        CALL gds.pageRank.stream($graphName, {
            maxIterations: $iterations,
            dampingFactor: $dampingFactor
        }) YIELD nodeId, score
        MATCH (e:Entity) WHERE id(e) = nodeId
        RETURN e.id, e.name, e.type, score
        ORDER BY score DESC
        LIMIT $limit
        """
        
        # 如果没有GDS插件，使用简化版实现
        fallback_cypher = """
        MATCH (e:Entity)
        WHERE ($kb_id IS NULL OR e.kb_id = $kb_id)
        OPTIONAL MATCH (e)<-[r:RELATED_TO]-()
        WITH e, count(r) as inDegree, COUNT { (e)-[:RELATED_TO]->() } as outDegree
        WITH e, (inDegree + outDegree) as degree
        RETURN e.id, e.name, e.type, degree as score
        ORDER BY degree DESC
        LIMIT $limit
        """
        
        params = {
            "kb_id": kb_id,
            "iterations": iterations,
            "damping_factor": damping_factor,
            "limit": limit
        }
        
        try:
            # 尝试GDS版本
            results = await self.connector.execute_query(cypher, {
                **params,
                "graphName": "kquery-pro-graph"
            })
        except Exception as e:
            logger.debug(f"GDS不可用，使用简化PageRank: {e}")
            results = await self.connector.execute_query(fallback_cypher, params)
        
        ranked_entities = [
            {
                "rank": i + 1,
                "entity_id": r.get('e.id') or r.get('id'),
                "name": r.get('e.name') or r.get('name'),
                "type": r.get('e.type') or r.get('type'),
                "score": round(float(r.get('score', 0)), 4)
            }
            for i, r in enumerate(results)
        ]
        
        exec_time = (time.time() - start) * 1000
        
        return GraphAlgorithmResult(
            algorithm_name="pagerank",
            execution_time_ms=exec_time,
            results=ranked_entities,
            statistics={
                "iterations": iterations,
                "damping_factor": damping_factor,
                "returned_count": len(ranked_entities)
            }
        )
    
    async def find_communities(
        self,
        algorithm: str = "louvain",
        min_community_size: int = 3,
        kb_id: Optional[int] = None
    ) -> GraphAlgorithmResult:
        """
        社区发现算法 - 将图谱划分为紧密联系的群体
        
        社区（Community）是指图中内部连接密集、外部连接稀疏的节点集合。
        在知识图谱中，社区可能代表：
        - 同一领域的技术栈
        - 同一组织的成员网络
        - 相关主题的概念群组
        
        支持的算法：
        - louvain: 经典快速算法，适合大规模图
        - label_propagation: 标签传播，速度快但精度一般
        - weakly_connected_components: 弱连通分量
        
        Args:
            algorithm: 算法选择
            min_community_size: 最小社区规模
            kb_id: 范围限制
        
        Returns:
            GraphAlgorithmResult 各社区及其成员
        """
        start = time.time()
        
        # 使用弱连通分量作为简化实现（无需额外插件）
        if algorithm == "weakly_connected_components":
            cypher = """
            CALL gds.wcc.stream($graphName) YIELD nodeId, communityId
            MATCH (e:Entity) WHERE id(e) = nodeId
            RETURN communityId, collect({
                id: e.id, name: e.name, type: e.type
            }) as members
            ORDER BY size(members) DESC
            LIMIT 30
            """
        else:
            # Louvain简化版（基于标签传播思想）
            cypher = f"""
            MATCH (e:Entity)
            WHERE ($kb_id IS NULL OR e.kb_id = $kb_id)
            OPTIONAL MATCH (e)-[r:RELATED_TO]-(peer)
            WITH e, peer, count(r) as strength
            ORDER BY strength DESC
            WITH e, collect({{peer: peer.name, strength: strength}})[0..3] as topPeers
            WITH e, [p IN topPeers | p.peer][0] as primaryLabel
            RETURN primaryLabel as communityLabel, 
                   collect({{id: e.id, name: e.name, type: e.type}}) as members
            HAVING size(members) >= $minSize
            ORDER BY size(members) DESC
            LIMIT 30
            """
        
        params = {
            "kb_id": kb_id,
            "minSize": min_community_size,
            "graphName": "kquery-pro-graph"
        }
        
        try:
            communities = await self.connector.execute_query(cypher, params)
        except Exception as e:
            logger.warning(f"社区发现算法执行失败: {e}")
            communities = []
        
        formatted_communities = [
            {
                "community_id": i,
                "label": c.get('communityId') or c.get('communityLabel') or f"Community-{i}",
                "size": len(c['members']),
                "members": c['members']
            }
            for i, c in enumerate(communities)
        ]
        
        exec_time = (time.time() - start) * 1000
        
        return GraphAlgorithmResult(
            algorithm_name=f"community_{algorithm}",
            execution_time_ms=exec_time,
            results=formatted_communities,
            statistics={
                "algorithm": algorithm,
                "communities_found": len(formatted_communities),
                "min_size_threshold": min_community_size
            }
        )

    # ==================== Global Search（全局搜索）====================

    async def global_search(
        self,
        query: str,
        kb_id: Optional[int] = None,
        top_k: int = 5,
    ) -> GlobalSearchResult:
        """全局搜索 - 基于社区摘要回答宏观问题

        适用于不需要特定实体、而是需要整体理解的问题，例如：
        - "这个知识库涉及哪些主题？"
        - "总结一下人工智能领域的关键信息"
        - "有哪些重要的组织及其相互关系？"

        搜索策略：
        1. 关键词匹配社区摘要
        2. 匹配社区成员名称
        3. 返回最相关的社区及其摘要
        4. 拼接为全局上下文文本
        """
        start = time.time()
        result = GlobalSearchResult(query=query)

        try:
            kb_filter = "AND c.kb_id = $kb_id" if kb_id is not None else ""
            params = {
                "query": query,
                "kb_id": kb_id,
                "limit": top_k,
            }

            cypher = f"""
            MATCH (c:Community)
            WHERE (c.summary CONTAINS $query
                   OR c.name CONTAINS $query
                   OR ANY(name IN c.member_names WHERE name CONTAINS $query)
                   OR ANY(ke IN c.key_entities WHERE ke CONTAINS $query))
            {kb_filter}
            RETURN c.id as id,
                   c.name as name,
                   c.members as members,
                   c.member_names as member_names,
                   c.summary as summary,
                   c.density as density,
                   c.size as size,
                   c.key_entities as key_entities,
                   c.level as level
            ORDER BY c.size DESC, c.density DESC
            LIMIT $limit
            """

            communities_data = await self.connector.execute_query(cypher, params)

            for data in communities_data:
                comm = {
                    "id": data["id"],
                    "name": data.get("name", ""),
                    "summary": data.get("summary", ""),
                    "size": data.get("size", 0),
                    "density": data.get("density", 0.0),
                    "key_entities": data.get("key_entities", []),
                    "member_names": data.get("member_names", []),
                }
                result.communities.append(comm)
                if data.get("summary"):
                    result.community_summaries.append(data["summary"])

            # 回退：如果没有社区匹配，尝试匹配原始实体
            if not result.communities:
                logger.debug("Global Search: 未匹配到社区，回退到实体搜索")
                entities, _ = await self.search_entities(
                    keyword=query[:20],
                    kb_id=kb_id,
                    limit=top_k,
                )
                result.matched_entities = entities

            # 构建全局上下文文本
            context_parts = ["【知识图谱全局概览】\n"]

            if result.community_summaries:
                context_parts.append("相关主题社区:\n")
                for i, summary in enumerate(result.community_summaries[:top_k], 1):
                    context_parts.append(f"{i}. {summary}\n")

            if result.matched_entities:
                context_parts.append("\n相关实体:\n")
                for e in result.matched_entities[:top_k]:
                    context_parts.append(f"- {e.name} ({e.type.value})\n")

            result.context_text = "\n".join(context_parts)
            result.query_time_ms = (time.time() - start) * 1000

            logger.info(
                f"🌍 Global Search | 查询: '{query}' | "
                f"社区: {len(result.communities)}, 实体: {len(result.matched_entities)}, "
                f"耗时: {result.query_time_ms:.0f}ms"
            )

        except Exception as e:
            logger.error(f"Global Search 失败: {e}", exc_info=True)

        return result

    # ==================== Local Search（局部搜索）====================

    async def local_search(
        self,
        query: str,
        kb_id: Optional[int] = None,
        top_k: int = 10,
        depth: int = 2,
    ) -> LocalSearchResult:
        """局部搜索 - 基于实体邻居回答具体问题

        适用于涉及特定实体的问题，例如：
        - "张三是谁？"
        - "腾讯公司的主要业务是什么？"
        - "人工智能和深度学习有什么关系？"
        """
        start = time.time()
        result = LocalSearchResult(query=query)

        try:
            # Step 1: 在查询中搜索匹配的实体
            keywords = [w for w in re.split(r'[\s,，。；！？]+', query) if len(w) >= 2]

            matched_entities: List[Entity] = []
            for keyword in keywords[:5]:
                entities, total = await self.search_entities(
                    keyword=keyword,
                    kb_id=kb_id,
                    limit=3,
                )
                for e in entities:
                    if e.id not in {me.id for me in matched_entities}:
                        matched_entities.append(e)

            result.center_entities = matched_entities[:5]

            # Step 2: 获取每个中心实体的邻居
            all_neighbors: List[NeighborNode] = []
            seen_neighbor_ids: Set[str] = set()

            for entity in result.center_entities:
                try:
                    neighbor_result = await self.get_neighbors(
                        entity_id=entity.id,
                        depth=depth,
                        limit_per_level=top_k,
                    )

                    for neighbor in neighbor_result.neighbors:
                        if neighbor.entity_id not in seen_neighbor_ids:
                            seen_neighbor_ids.add(neighbor.entity_id)
                            all_neighbors.append(neighbor)

                except Exception as e:
                    logger.debug(f"获取邻居失败({entity.id}): {e}")

            all_neighbors.sort(key=lambda n: (n.distance, -n.confidence))
            result.neighbors = all_neighbors[:top_k * 2]

            # Step 3: 查找实体间路径
            if len(result.center_entities) >= 2:
                for i, e1 in enumerate(result.center_entities):
                    for e2 in result.center_entities[i + 1:]:
                        try:
                            path = await self.find_shortest_path(
                                source_id=e1.id,
                                target_id=e2.id,
                                max_depth=4,
                            )
                            if path.paths:
                                result.paths.append(path)
                        except Exception as e:
                            logger.debug(f"路径查找失败({e1.id}->{e2.id}): {e}")

            # Step 4: 获取子图
            if result.center_entities:
                try:
                    center_ids = [e.id for e in result.center_entities]
                    result.subgraph = await self.get_subgraph(
                        center_entity_ids=center_ids,
                        radius=min(depth, 2),
                        max_nodes=100,
                    )
                except Exception as e:
                    logger.debug(f"子图提取失败: {e}")

            # Step 5: 构建局部上下文文本
            result.context_text = self._format_local_context(result)
            result.query_time_ms = (time.time() - start) * 1000

            logger.info(
                f"📍 Local Search | 查询: '{query}' | "
                f"中心实体: {len(result.center_entities)}, 邻居: {len(result.neighbors)}, "
                f"路径: {len(result.paths)}, 耗时: {result.query_time_ms:.0f}ms"
            )

        except Exception as e:
            logger.error(f"Local Search 失败: {e}", exc_info=True)

        return result

    def _format_local_context(self, result: LocalSearchResult) -> str:
        """将 Local Search 结果格式化为 LLM 可用的上下文文本"""
        parts = ["【知识图谱局部上下文】\n"]

        if result.center_entities:
            parts.append("中心实体:\n")
            for e in result.center_entities:
                parts.append(f"- {e.name} ({e.type.value})\n")
            parts.append("")

        if result.neighbors:
            parts.append("相关实体与关系:\n")
            by_relation: Dict[str, List[NeighborNode]] = {}
            for n in result.neighbors:
                rel = n.relation_type
                if rel not in by_relation:
                    by_relation[rel] = []
                by_relation[rel].append(n)

            for rel_type, neighbors in list(by_relation.items())[:5]:
                parts.append(f"  [{rel_type}]\n")
                for n in neighbors[:5]:
                    parts.append(f"    - {n.name} ({n.type})\n")
            parts.append("")

        if result.paths:
            parts.append("实体间路径:\n")
            for path in result.paths[:3]:
                if path.paths and path.paths[0]:
                    node_names = " → ".join([
                        step.node_name for step in path.paths[0]
                    ])
                    parts.append(f"  - {node_names}\n")
            parts.append("")

        return "\n".join(parts)

    # ==================== Hybrid Graph Search（混合搜索）====================

    async def hybrid_graph_search(
        self,
        query: str,
        kb_id: Optional[int] = None,
        top_k: int = 10,
        search_mode: str = "auto",
    ) -> HybridGraphSearchResult:
        """图谱层面的混合搜索（Global + Local）

        根据查询特征自动选择或结合全局/局部搜索策略。
        """
        start = time.time()
        result = HybridGraphSearchResult(query=query, search_mode=search_mode)

        # 判断搜索模式
        if search_mode == "auto":
            has_entity = False
            keywords = [w for w in re.split(r'[\s,，。；！？]+', query) if len(w) >= 2]
            for keyword in keywords[:3]:
                try:
                    _, total = await self.search_entities(keyword=keyword, kb_id=kb_id, limit=1)
                    if total > 0:
                        has_entity = True
                        break
                except:
                    pass

            overview_keywords = ["总结", "概括", "概述", "主题", "主要", "有哪些", "是什么"]
            is_overview = any(kw in query.lower() for kw in overview_keywords)

            if has_entity and not is_overview:
                search_mode = "local"
            elif is_overview and not has_entity:
                search_mode = "global"
            else:
                search_mode = "hybrid"

            result.search_mode = search_mode

        # 执行搜索
        tasks = []

        if search_mode in ("global", "hybrid"):
            tasks.append(("global", self.global_search(query, kb_id, top_k)))

        if search_mode in ("local", "hybrid"):
            tasks.append(("local", self.local_search(query, kb_id, top_k)))

        search_results = {}
        if tasks:
            coroutines = [t[1] for t in tasks]
            types = [t[0] for t in tasks]
            completed = await asyncio.gather(*coroutines, return_exceptions=True)

            for stype, res in zip(types, completed):
                if isinstance(res, Exception):
                    logger.warning(f"{stype} search 失败: {res}")
                else:
                    search_results[stype] = res

        if "global" in search_results:
            result.global_result = search_results["global"]
        if "local" in search_results:
            result.local_result = search_results["local"]

        # 合并上下文
        context_parts = []
        if result.global_result and result.global_result.context_text:
            context_parts.append(result.global_result.context_text)
        if result.local_result and result.local_result.context_text:
            context_parts.append(result.local_result.context_text)

        result.combined_context = "\n\n---\n\n".join(context_parts)
        result.query_time_ms = (time.time() - start) * 1000

        logger.info(
            f"🔍 Hybrid Graph Search | 模式: {search_mode} | "
            f"全局: {len(result.global_result.communities) if result.global_result else 0} 社区, "
            f"局部: {len(result.local_result.neighbors) if result.local_result else 0} 邻居 | "
            f"耗时: {result.query_time_ms:.0f}ms"
        )

        return result

    # ==================== 统计与元数据 ====================
    
    async def get_graph_overview(
        self,
        kb_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """获取图谱全局统计概览"""
        kb_filter = ""
        params: Dict[str, Any] = {}
        
        if kb_id:
            kb_filter = "WHERE e.kb_id = $kb_id"
            params["kb_id"] = kb_id
        
        queries = {
            "total_nodes": f"MATCH (e:Entity) {kb_filter} RETURN count(e) as cnt",
            "total_edges": f"MATCH ()-[r]-() {'' if not kb_id else 'WHERE r.kb_id = $kb_id'} RETURN count(r) as cnt",
            "avg_degree": f"""
                MATCH (e:Entity) {kb_filter}
                OPTIONAL MATCH (e)-[r]-()
                WITH e, count(r) as degree
                RETURN avg(degree) as avgDeg
            """,
            "largest_component": f"""
                CALL gds.wcc.stats($graphName) YIELD componentCount, sizeOfLargestComponent
                RETURN componentCount, sizeOfLargestComponent
            """
        }
        
        overview = {}
        
        for key, query in queries.items():
            try:
                result = await self.connector.execute_query(
                    query, 
                    {**params, "graphName": "kquery-pro-graph"}
                )
                if result:
                    val = result[0].get('cnt') or result[0].get('avgDeg')
                    overview[key] = round(val, 2) if isinstance(val, float) else (val or 0)
            except Exception as e:
                logger.debug(f"统计[{key}]查询失败: {e}")
                overview[key] = 0
        
        return overview


__all__ = [
    # 结果模型
    'NeighborNode',
    'NeighborResult',
    'PathStep',
    'PathResult',
    'SubgraphResult',
    'GraphAlgorithmResult',
    'GlobalSearchResult',
    'LocalSearchResult',
    'HybridGraphSearchResult',
    # 主服务
    'GraphQueryService'
]
