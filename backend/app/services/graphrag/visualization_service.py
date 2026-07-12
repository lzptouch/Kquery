"""知识图谱可视化数据服务。

为前端可视化库（D3.js、Cytoscape.js、ECharts、vis.js等）提供优化的数据格式。

核心功能：
1. 数据格式转换：将Neo4j查询结果转换为前端所需的JSON格式
2. 布局算法推荐：根据图结构推荐最优布局
3. 节点/边样式：根据实体类型和关系类型分配视觉属性
4. 交互数据支持：高亮路径、邻居扩展、子图懒加载
5. 性能优化：大规模图的分页、简化、聚合

支持的前端库：
- D3 Force-Directed Graph (力导向图)
- Cytoscape.js (网络分析可视化)
- ECharts Graph (关系图)
- vis.js Network (交互式网络)

使用示例：
    from app.services.graphrag.visualization_service import VisualizationService

    viz = VisualizationService()

    # 获取D3.js力导向图数据
    d3_data = await viz.get_d3_force_graph(
        center_entity_id="entity-xxx",
        radius=2,
        max_nodes=100
    )

    # 返回给前端直接渲染
    return JSONResponse(content=d3_data)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

import networkx as nx

from app.core.logger import get_logger
from app.services.graph.graph_query import (
    GraphQueryService,
    SubgraphResult,
)


logger = get_logger(__name__)


# ============================================================
# 可视化配置枚举
# ============================================================


class VisualizationFormat(str, Enum):
    """支持的可视化格式。"""
    
    D3_FORCE = "d3_force"              # D3.js 力导向布局
    CYTOSCAPE = "cytoscape"            # Cytoscape.js 格式
    ECHARTS_GRAPH = "echarts_graph"   # ECharts 关系图
    VIS_NETWORK = "vis_network"       # vis.js Network
    RAW_JSON = "raw_json"             # 原始JSON（通用）


class LayoutAlgorithm(str, Enum):
    """布局算法类型。"""
    
    FORCE = "force"                    # 力导向（默认，适合一般探索）
    HIERARCHICAL = "hierarchical"      # 层级布局（适合组织结构）
    CIRCULAR = "circular"              # 圆形布局（适合社区发现）
    RADIAL = "radial"                  # 放射状布局（以某节点为中心）
    GRID = "grid"                      # 网格布局
    CONCENTRIC = "concentric"          # 同心圆布局


@dataclass
class VisualNode:
    """可视化节点。
    
    Attributes:
        id: 节点唯一ID
        label: 显示标签
        type: 实体类型
        size: 节点大小（影响渲染尺寸）
        color: 填充颜色
        border_color: 边框颜色
        shape: 形状（circle/diamond/rectangle等）
        x: X坐标（预计算位置）
        y: Y坐标（预计算位置）
        properties: 额外属性（用于tooltip等）
        highlight: 是否高亮
        metadata: 元数据（来源信息等）
    """
    id: str
    label: str
    type: str = "default"
    size: float = 20.0
    color: str = "#4A90E2"
    border_color: str = "#2171C7"
    shape: str = "circle"
    x: Optional[float] = None
    y: Optional[float] = None
    properties: Dict[str, Any] = field(default_factory=dict)
    highlight: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        result = {
            "id": self.id,
            "label": self.label,
            "type": self.type,
            "size": self.size,
            "color": self.color,
            "borderColor": self.border_color,
            "shape": self.shape,
        }
        
        if self.x is not None and self.y is not None:
            result["x"] = self.x
            result["y"] = self.y
        
        if self.properties:
            result["properties"] = self.properties
        if self.highlight:
            result["highlighted"] = True
        if self.metadata:
            result["metadata"] = self.metadata
        
        return result


@dataclass 
class VisualEdge:
    """可视化边（关系）。
    
    Attributes:
        id: 边唯一ID
        source: 源节点ID
        target: 目标节点ID
        label: 显示的关系名称
        type: 关系类型
        width: 线宽（表示权重或重要性）
        color: 线条颜色
        style: 样式（solid/dashed/dotted）
        curved: 是否弯曲（避免多边重叠）
        properties: 额外属性
        arrow: 是否显示箭头
    """
    id: str
    source: str
    target: str
    label: str = ""
    type: str = ""
    width: float = 1.5
    color: str = "#999999"
    style: str = "solid"
    curved: bool = False
    properties: Dict[str, Any] = field(default_factory=dict)
    arrow: bool = True
    
    def to_dict(self) -> Dict[str, Any]:
        result = {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "label": self.label,
            "type": self.type,
            "width": self.width,
            "color": self.color,
            "style": self.style,
            "arrow": self.arrow,
        }
        
        if self.curved:
            result["curved"] = True
        if self.properties:
            result["properties"] = self.properties
        
        return result


@dataclass
class VisualizationData:
    """完整的可视化数据集。
    
    Attributes:
        nodes: 节点列表
        edges: 边列表
        layout: 推荐的布局算法
        format: 当前数据的格式
        statistics: 统计信息
        center_nodes: 中心节点ID列表
        options: 渲染选项建议
    """
    nodes: List[VisualNode] = field(default_factory=list)
    edges: List[VisualEdge] = field(default_factory=list)
    layout: LayoutAlgorithm = LayoutAlgorithm.FORCE
    format: VisualizationFormat = VisualizationFormat.D3_FORCE
    statistics: Dict[str, Any] = field(default_factory=dict)
    center_nodes: List[str] = field(default_factory=list)
    options: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def node_count(self) -> int:
        return len(self.nodes)
    
    @property
    def edge_count(self) -> int:
        return len(self.edges)
    
    def to_dict(self, target_format: Optional[VisualizationFormat] = None) -> Dict[str, Any]:
        """转换为指定格式的字典。"""
        fmt = target_format or self.format
        
        base = {
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "layout": self.layout.value,
            "format": fmt.value,
            "statistics": self.statistics,
            "centerNodes": self.center_nodes,
            "options": self.options,
        }
        
        # 根据目标格式调整输出
        if fmt == VisualizationFormat.CYTOSCAPE:
            base["elements"] = {
                "nodes": [{"data": n.to_dict()} for n in self.nodes],
                "edges": [{"data": e.to_dict()} for e in self.edges],
            }
        elif fmt == VisualizationFormat.ECHARTS_GRAPH:
            base["categories"] = self._generate_echarts_categories()
            base["graphData"] = {
                "nodes": [
                    {"id": n.id, "name": n.label, "value": n.size, 
                     "category": n.type, "symbolSize": n.size}
                    for n in self.nodes
                ],
                "links": [
                    {"source": e.source, "target": e.target, "value": e.width}
                    for e in self.edges
                ]
            }
        
        return base
    
    def _generate_echarts_categories(self) -> List[Dict]:
        """生成ECharts的分类定义。"""
        seen_types: Set[str] = set()
        categories = []
        
        for node in self.nodes:
            if node.type not in seen_types:
                seen_types.add(node.type)
                
                # 为每个类型生成配置
                config = self._get_type_config(node.type)
                categories.append({
                    "name": node.type,
                    "color": node.color or config.get("color", "#4A90E2"),
                })
        
        return categories


# ============================================================
# 样式映射表
# ============================================================


class VisualStyleMapper:
    """实体类型到视觉样式的映射。"""
    
    # 实体类型 → 视觉样式配置
    ENTITY_TYPE_STYLES: Dict[str, Dict[str, Any]] = {
        # 人物类 - 圆形，蓝色系
        "Person": {
            "shape": "circle",
            "size": 25,
            "color": "#4A90E2",
            "border_color": "#2171C7",
            "icon": "user",
        },
        "Organization": {
            "shape": "round-rectangle",
            "size": 35,
            "color": "#50C878",
            "border_color": "#2E8B57",
            "icon": "building",
        },
        "Position": {
            "shape": "diamond",
            "size": 22,
            "color": "#FFB347",
            "border_color": "#CC8400",
            "icon": "briefcase",
        },
        
        # 地点类 - 方形，绿色系
        "Country": {
            "shape": "hexagon",
            "size": 30,
            "color": "#9B59B6",
            "border_color": "#7D3C98",
            "icon": "globe",
        },
        "City": {
            "shape": "round-rectangle",
            "size": 26,
            "color": "#AF7AC5",
            "border_color": "#8E44AD",
            "icon": "map-marker",
        },
        "Location": {
            "shape": "square",
            "size": 20,
            "color": "#D2B4DE",
            "border_color": "#AF7AC5",
            "icon": "location-arrow",
        },
        
        # 技术类 - 圆角矩形，橙色系
        "Product": {
            "shape": "round-rectangle",
            "size": 28,
            "color": "#F39C12",
            "border_color": "#D68910",
            "icon": "cube",
        },
        "Technology": {
            "shape": "ellipse",
            "size": 24,
            "color": "#E67E22",
            "border_color": "#CA6F1E",
            "icon": "microchip",
        },
        "Project": {
            "shape": "parallelogram",
            "size": 26,
            "color": "#F0B27A",
            "border_color": "#DC7633",
            "icon": "project-diagram",
        },
        "Concept": {
            "shape": "octagon",
            "size": 22,
            "color": "#F5B041",
            "border_color": "#D4AC0D",
            "icon": "lightbulb",
        },
        
        # 时间数值类 - 小圆形，灰色系
        "Date": {
            "shape": "round-diamond",
            "size": 18,
            "color": "#95A5A6",
            "border_color": "#7F8C8D",
            "icon": "calendar",
        },
        "Time": {
            "shape": "vee",
            "size": 16,
            "color": "#BDC3C7",
            "border_color": "#95A5A6",
            "icon": "clock",
        },
        "Number": {
            "shape": "tag",
            "size": 16,
            "color": "#ABB2B9",
            "border_color": "#808B96",
            "icon": "hashtag",
        },
        "Money": {
            "shape": "round-diamond",
            "size": 20,
            "color": "#27AE60",
            "border_color": "#1E8449",
            "icon": "dollar-sign",
        },
        
        # 文档事件类
        "Event": {
            "shape": "star",
            "size": 24,
            "color": "#E74C3C",
            "border_color": "#CB4335",
            "icon": "calendar-check",
        },
        "Document": {
            "shape": "rectangle",
            "size": 22,
            "color": "#3498DB",
            "border_color": "#2874A6",
            "icon": "file-alt",
        },
        
        # 默认
        "default": {
            "shape": "ellipse",
            "size": 20,
            "color": "#95A5A6",
            "border_color": "#7F8C8D",
            "icon": "question",
        },
    }
    
    # 关系类型 → 边样式配置
    RELATION_TYPE_STYLES: Dict[str, Dict[str, Any]] = {
        # 层级关系 - 粗实线
        "part_of": {"width": 2.5, "color": "#3498DB", "style": "solid"},
        "subclass_of": {"width": 2.5, "color": "#2980B9", "style": "solid"},
        "contains": {"width": 2.0, "color": "#5DADE2", "style": "solid"},
        
        # 社会关系 - 中等虚线
        "employed_by": {"width": 2.0, "color": "#27AE60", "style": "solid"},
        "works_for": {"width": 1.8, "color": "#2ECC71", "style": "solid"},
        "founded": {"width": 2.2, "color": "#1ABC9C", "style": "dashed"},
        "manages": {"width": 2.0, "color": "#16A085", "style": "dotted"},
        
        # 关联关系 - 细线
        "related_to": {"width": 1.5, "color": "#95A5A6", "style": "solid"},
        "similar_to": {"width": 1.2, "color": "#BDC3C7", "style": "dotted"},
        "opposed_to": {"width": 1.8, "color": "#E74C3C", "style": "dashed"},
        
        # 时序关系 - 特殊颜色
        "before": {"width": 1.5, "color": "#F39C12", "style": "solid"},
        "after": {"width": 1.5, "color": "#E67E22", "style": "solid"},
        "during": {"width": 1.2, "color": "#D35400", "style": "dotted"},
        
        # 因果关系 - 粗箭头
        "causes": {"width": 2.5, "color": "#C0392B", "style": "solid"},
        "enables": {"width": 2.0, "color": "#E74C3C", "style": "solid"},
        "prevents": {"width": 2.0, "color": "#943126", "style": "dashed"},
        
        # 依赖关系
        "depends_on": {"width": 1.8, "color": "#8E44AD", "style": "dashed"},
        "requires": {"width": 2.0, "color": "#9B59B6", "style": "solid"},
        "uses": {"width": 1.5, "color": "#AF7AC5", "style": "solid"},
        
        # 默认
        "default": {"width": 1.5, "color": "#999999", "style": "solid"},
    }
    
    @classmethod
    def get_entity_style(cls, entity_type: str) -> Dict[str, Any]:
        """获取实体类型的视觉样式。"""
        return cls.ENTITY_TYPE_STYLES.get(
            entity_type, cls.ENTITY_TYPE_STYLES["default"]
        )
    
    @classmethod
    def get_relation_style(cls, relation_type: str) -> Dict[str, Any]:
        """获取关系类型的边样式。"""
        return cls.RELATION_TYPE_STYLES.get(
            relation_type, cls.RELATION_TYPE_STYLES["default"]
        )
    
    @classmethod
    def get_type_config(cls, entity_type: str) -> Dict[str, Any]:
        """获取类型配置（用于ECharts等需要分类信息的库）。"""
        return cls.ENTITY_TYPE_STYLES.get(entity_type, cls.ENTITY_TYPE_STYLES["default"])


# ============================================================
# 主服务类
# ============================================================


class VisualizationService:
    """可视化数据服务主入口。
    
    提供统一的接口，将知识图谱查询结果转换为各种前端可视化库所需的数据格式。
    
    使用示例：
        viz = VisualizationService()
        
        # 获取D3力导向图数据
        d3_data = await viz.get_visualization_data(
            center_ids=["entity-xxx"],
            radius=2,
            max_nodes=100,
            output_format=VisualizationFormat.D3_FORCE
        )
        
        # 直接返回给前端API
        return JSONResponse(d3_data.to_dict())
    """
    
    def __init__(
        self,
        default_format: VisualizationFormat = VisualizationFormat.D3_FORCE,
        default_layout: LayoutAlgorithm = LayoutAlgorithm.FORCE,
        enable_layout_precompute: bool = True,
    ):
        """
        Args:
            default_format: 默认输出格式
            default_layout: 默认布局算法
            enable_layout_precompute: 是否预计算节点坐标
        """
        self.default_format = default_format
        self.default_layout = default_layout
        self.enable_layout_precompute = enable_layout_precompute
        
        self._query_service: Optional[GraphQueryService] = None
        
        logger.info(f"可视化服务初始化完成 (格式={default_format.value}, 布局={default_layout.value})")
    
    @property
    def query_service(self) -> GraphQueryService:
        """获取图谱查询服务实例。"""
        if self._query_service is None:
            self._query_service = GraphQueryService()
        return self._query_service
    
    async def get_visualization_data(
        self,
        center_ids: List[str],
        radius: int = 2,
        max_nodes: int = 100,
        kb_id: Optional[int] = None,
        output_format: Optional[VisualizationFormat] = None,
        layout_algorithm: Optional[LayoutAlgorithm] = None,
        include_properties: bool = False,
        highlight_paths: Optional[List[Tuple[str, str]]] = None,
    ) -> VisualizationData:
        """获取完整的可视化数据集（核心方法）。
        
        Args:
            center_ids: 中心实体ID列表
            radius: 扩展半径（跳数）
            max_nodes: 最大节点数限制
            kb_id: 知识库ID
            output_format: 输出格式（覆盖默认值）
            layout_algorithm: 布局算法（覆盖默认值）
            include_properties: 是否包含详细属性
            highlight_paths: 需要高亮的路径 [(start, end), ...]
            
        Returns:
            VisualizationData: 完整的可视化数据对象
            
        Example:
            viz = VisualizationService()
            
            data = await viz.get_visualization_data(
                center_ids=["entity-tencent"],
                radius=2,
                max_nodes=50,
                output_format=VisualizationFormat.D3_FORCE
            )
            
            # 直接返回给前端
            return JSONResponse(content=data.to_dict())
        """
        import time
        start_time = time.time()
        
        try:
            logger.info(
                f"🎨 开始准备可视化数据: {len(center_ids)} 个中心节点, "
                f"半径={radius}, 上限={max_nodes}节点"
            )
            
            # Step 1: 从Neo4j获取子图
            subgraph = await self.query_service.get_subgraph(
                center_entity_ids=center_ids,
                radius=radius,
                limit=max_nodes,
                kb_id=kb_id,
            )
            
            # Step 2: 转换为可视化节点和边
            viz_data = await self._convert_subgraph_to_viz_data(
                subgraph=subgraph,
                center_ids=center_ids,
                include_properties=include_properties,
            )
            
            # Step 3: 设置布局和其他元信息
            layout = layout_algorithm or self.default_layout
            viz_data.layout = layout
            viz_data.format = output_format or self.default_format
            viz_data.center_nodes = center_ids
            
            # Step 4: 预计算布局（如果启用）
            if self.enable_layout_precompute:
                viz_data = await self._precompute_layout(viz_data, layout)
            
            # Step 5: 应用路径高亮（如果有）
            if highlight_paths:
                viz_data = self._apply_path_highlights(viz_data, highlight_paths)
            
            # Step 6: 计算统计信息
            viz_data.statistics = self._calculate_statistics(viz_data)
            
            # Step 7: 设置渲染选项建议
            viz_data.options = self._generate_render_options(viz_data)
            
            elapsed = time.time() - start_time
            logger.info(
                f"✅ 可视化数据准备完成! "
                f"{viz_data.node_count}节点 + {viz_data.edge_count}边, "
                f"耗时={elapsed:.3f}s"
            )
            
            return viz_data
        
        except Exception as e:
            logger.error(f"❌ 可视化数据准备失败: {e}", exc_info=True)
            raise
    
    async def get_d3_force_graph(
        self,
        center_entity_id: str,
        radius: int = 2,
        max_nodes: int = 100,
        **kwargs,
    ) -> Dict[str, Any]:
        """获取D3.js力导向图数据（便捷方法）。"""
        viz_data = await self.get_visualization_data(
            center_ids=[center_entity_id],
            radius=radius,
            max_nodes=max_nodes,
            output_format=VisualizationFormat.D3_FORCE,
            **kwargs,
        )
        return viz_data.to_dict(VisualizationFormat.D3_FORCE)
    
    async def get_cytoscape_data(
        self,
        center_entity_id: str,
        radius: int = 2,
        max_nodes: int = 100,
        **kwargs,
    ) -> Dict[str, Any]:
        """获取Cytoscape.js数据（便捷方法）。"""
        viz_data = await self.get_visualization_data(
            center_ids=[center_entity_id],
            radius=radius,
            max_nodes=max_nodes,
            output_format=VisualizationFormat.CYTOSCAPE,
            **kwargs,
        )
        return viz_data.to_dict(VisualizationFormat.CYTOSCAPE)
    
    async def get_echarts_graph_data(
        self,
        center_entity_id: str,
        radius: int = 2,
        max_nodes: int = 100,
        **kwargs,
    ) -> Dict[str, Any]:
        """获取ECharts关系图数据（便捷方法）。"""
        viz_data = await self.get_visualization_data(
            center_ids=[center_entity_id],
            radius=radius,
            max_nodes=max_nodes,
            output_format=VisualizationFormat.ECHARTS_GRAPH,
            **kwargs,
        )
        return viz_data.to_dict(VisualizationFormat.ECHARTS_GRAPH)
    
    async def expand_node_neighbors(
        self,
        node_id: str,
        current_node_ids: Set[str],
        depth: int = 1,
        max_new_nodes: int = 20,
    ) -> Dict[str, Any]:
        """增量加载节点的邻居（用于交互式探索）。
        
        Args:
            node_id: 要展开的中心节点ID
            current_node_ids: 已显示的节点ID集合
            depth: 展开深度
            max_new_nodes: 新增节点上限
            
        Returns:
            Dict: 包含新增节点和边的增量数据
        """
        try:
            # 获取该节点的邻居
            neighbor_result = await self.query_service.get_neighbors(
                entity_id=node_id,
                depth=depth,
            )
            
            # 过滤已存在的节点
            new_nodes = []
            new_edges = []
            
            for neighbor in neighbor_result.neighbors:
                if neighbor.id not in current_node_ids:
                    # 创建新的可视化节点
                    style = VisualStyleMapper.get_entity_style(
                        neighbor.entity_type.value if hasattr(neighbor.entity_type, 'value') else str(neighbor.entity_type)
                    )
                    
                    visual_node = VisualNode(
                        id=neighbor.id,
                        label=neighbor.name,
                        type=str(neighbor.entity_type).split('.')[-1] if neighbor.entity_type else "default",
                        size=style.get("size", 20),
                        color=style.get("color", "#4A90E2"),
                        border_color=style.get("border_color", "#2171C7"),
                        shape=style.get("shape", "circle"),
                        properties=neighbor.properties if include_property_check else {},
                    )
                    new_nodes.append(visual_node.to_dict())
                
                # 添加边（即使目标已存在也要添加边，可能之前没这条边）
                edge_id = f"edge-{node_id}-{neighbor.id}"
                edge_style = VisualStyleMapper.get_relation_style(neighbor.relation_type)
                
                visual_edge = VisualEdge(
                    id=edge_id,
                    source=node_id,
                    target=neighbor.id,
                    label=neighbor.relation_type.replace("_", " "),
                    type=neighbor.relation_type,
                    width=edge_style.get("width", 1.5),
                    color=edge_style.get("color", "#999999"),
                    style=edge_style.get("style", "solid"),
                )
                new_edges.append(visual_edge.to_dict())
            
            return {
                "success": True,
                "new_nodes": new_nodes[:max_new_nodes],
                "new_edges": new_edges[:max_new_nodes * 2],  # 边数可能是节点数的2倍
                "total_new_nodes": len(new_nodes),
                "total_new_edges": len(new_edges),
            }
        
        except Exception as e:
            logger.error(f"节点展开失败: {e}", exc_info=True)
            return {"success": False, "error": str(e), "new_nodes": [], "new_edges": []}
    
    # ============================================================
    # 内部方法：转换逻辑
    # ============================================================
    
    async def _convert_subgraph_to_viz_data(
        self,
        subgraph: SubgraphResult,
        center_ids: List[str],
        include_properties: bool = False,
    ) -> VisualizationData:
        """将子图查询结果转换为可视化数据。"""
        viz_data = VisualizationData()
        
        # 转换节点
        for node_data in subgraph.nodes:
            entity_type = str(node_data.get("entity_type", "default"))
            style = VisualStyleMapper.get_entity_style(entity_type)
            
            name = node_data.get("name", node_data.get("id", ""))
            node_id = node_data.get("id", "")
            
            visual_node = VisualNode(
                id=node_id,
                label=name,
                type=entity_type,
                size=style.get("size", 20),
                color=style.get("color", "#4A90E2"),
                border_color=style.get("border_color", "#2171C7"),
                shape=style.get("shape", "circle"),
                highlight=(node_id in center_ids),
                properties=(
                    node_data.get("properties", {}) if include_properties else {}
                ),
                metadata={
                    "original_data": node_data,
                    "is_center": node_id in center_ids,
                },
            )
            viz_data.nodes.append(visual_node)
        
        # 转换边
        seen_edges: Set[Tuple[str, str, str]] = set()
        
        for edge_data in subgraph.edges:
            source = edge_data.get("source", "")
            target = edge_data.get("target", "")
            rel_type = edge_data.get("type", edge_data.get("relation_type", ""))
            
            # 避免重复边（同一对节点同一种关系只保留一条）
            edge_key = (source, target, rel_type)
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            
            edge_style = VisualStyleMapper.get_relation_style(rel_type)
            
            visual_edge = VisualEdge(
                id=edge_data.get("id", f"edge-{source}-{target}"),
                source=source,
                target=target,
                label=rel_type.replace("_", " ") if rel_type else "",
                type=rel_type,
                width=edge_style.get("width", 1.5),
                color=edge_style.get("color", "#999999"),
                style=edge_style.get("style", "solid"),
                curved=len([e for e in viz_data.edges 
                           if e.source == source and e.target == target]) > 0,
                properties=edge_data.get("properties", {}) if include_properties else {},
            )
            viz_data.edges.append(visual_edge)
        
        return viz_data
    
    async def _precompute_layout(
        self,
        viz_data: VisualizationData,
        layout: LayoutAlgorithm,
    ) -> VisualizationData:
        """预计算节点布局坐标。"""
        if not viz_data.nodes:
            return viz_data
        
        try:
            # 构建NetworkX图
            G = nx.Graph()
            
            for node in viz_data.nodes:
                G.add_node(node.id, size=node.size)
            
            for edge in viz_data.edges:
                if edge.source in G.nodes and edge.target in G.nodes:
                    G.add_edge(edge.source, edge.target, weight=edge.width)
            
            # 选择布局算法
            pos = None
            
            if layout == LayoutAlgorithm.FORCE:
                # 力导向布局
                pos = nx.spring_layout(
                    G, k=2/len(G.nodes)**0.5, iterations=50, seed=42
                )
            elif layout == LayoutAlgorithm.HIERARCHICAL:
                # 层级布局（使用最短路径长度近似层级）
                if viz_data.center_nodes:
                    lengths = nx.single_source_shortest_path_length(
                        G, viz_data.center_nodes[0]
                    )
                    pos = {}
                    for node, length in lengths.items():
                        angle = hash(node) % 360
                        pos[node] = (length * 200, length * angle * 3.14 / 180 * 150)
            elif layout == LayoutAlgorithm.CIRCULAR:
                # 圆形布局
                pos = nx.circular_layout(G)
            elif layout == LayoutAlgorithm.RADIAL:
                # 放射状布局
                if viz_data.center_nodes:
                    pos = {}
                    center = viz_data.center_nodes[0]
                    if center in G.nodes:
                        lengths = nx.single_source_shortest_path_length(G, center)
                        for i, (node, length) in enumerate(lengths.items()):
                            angle = 2 * 3.14 * i / len(lengths)
                            r = length * 180
                            pos[node] = (r * (1 + 0.3 * (i % 3)), r * (1 + 0.3 * ((i+1) % 3)))
            elif layout == LayoutAlgorithm.GRID:
                # 网格布局
                import math
                n = len(viz_data.nodes)
                cols = math.ceil(math.sqrt(n))
                pos = {}
                for i, node in enumerate(viz_data.nodes):
                    row, divmod_val = divmod(i, cols)
                    col = divmod_val
                    pos[node.id] = (col * 120, row * 80)
            else:
                # 默认使用力导向
                pos = nx.spring_layout(G, seed=42)
            
            # 应用坐标到节点
            if pos:
                for node in viz_data.nodes:
                    if node.id in pos:
                        node.x = float(pos[node.id][0]) * 100  # 缩放到合理范围
                        node.y = float(pos[node.id][1]) * 100
            
            return viz_data
        
        except Exception as e:
            logger.warning(f"布局计算失败，使用默认位置: {e}")
            return viz_data
    
    def _apply_path_highlights(
        self,
        viz_data: VisualizationData,
        paths: List[Tuple[str, str]],
    ) -> VisualizationData:
        """应用路径高亮效果。"""
        path_edges: Set[str] = set()
        
        for start, end in paths:
            # 在图中查找从start到end的路径
            try:
                # 这里简化处理：标记所有涉及这两个节点的边
                for edge in viz_data.edges:
                    if (edge.source == start and edge.target == end) or \
                       (edge.source == end and edge.target == start):
                        path_edges.add(edge.id)
                        
                        # 高亮边
                        edge.width += 1.0
                        edge.color = "#E74C3C"
            except Exception:
                continue
        
        # 高亮路径上的节点
        path_nodes: Set[str] = set()
        for start, end in paths:
            path_nodes.add(start)
            path_nodes.add(end)
        
        for node in viz_data.nodes:
            if node.id in path_nodes:
                node.highlight = True
                node.size *= 1.2
                node.border_color = "#E74C3C"
        
        return viz_data
    
    def _calculate_statistics(self, viz_data: VisualizationData) -> Dict[str, Any]:
        """计算图的统计信息。"""
        # 统计各类型的节点数量
        type_counts: Dict[str, int] = {}
        for node in viz_data.nodes:
            type_counts[node.type] = type_counts.get(node.type, 0) + 1
        
        # 统计各类型的边数量
        edge_type_counts: Dict[str, int] = {}
        for edge in viz_data.edges:
            edge_type_counts[edge.type] = edge_type_counts.get(edge.type, 0) + 1
        
        # 计算平均连接度
        avg_degree = (
            (2 * viz_data.edge_count / viz_data.node_count) 
            if viz_data.node_count > 0 else 0
        )
        
        return {
            "nodeCount": viz_data.node_count,
            "edgeCount": viz_data.edge_count,
            "avgDegree": round(avg_degree, 2),
            "nodeTypeDistribution": type_counts,
            "edgeTypeDistribution": edge_type_counts,
            "centerNodeCount": len(viz_data.center_nodes),
        }
    
    def _generate_render_options(self, viz_data: VisualizationData) -> Dict[str, Any]:
        """生成渲染选项建议（供前端参考）。"""
        options = {
            "physics": {
                "enabled": True,
                "stabilization": {"iterations": 100},
            },
            "interaction": {
                "hover": True,
                "tooltipDelay": 200,
                "zoomView": True,
                "dragView": True,
            },
            "nodes": {
                "font": {"size": 12},
                "borderWidth": 2,
            },
            "edges": {
                "font": {"size": 10, "align": "middle"},
                "smooth": {"type": "continuous"},
                "arrows": {"to": {"enabled": True}},
            },
        }
        
        # 根据图规模调整参数
        if viz_data.node_count > 200:
            options["physics"]["solver"] = "forceAtlas2Based"
            options["nodes"]["scaling"] = {"min": 10, "max": 30}
        elif viz_data.node_count > 500:
            options['physics']['enabled'] = False  # 大图关闭物理模拟
            options['nodes']['scaling'] = {'min': 5, 'max': 15}
        
        return options


# 全局辅助函数
def include_property_check():
    """占位函数，实际使用时替换为配置项"""
    return False
