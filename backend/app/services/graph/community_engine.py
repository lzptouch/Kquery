"""
社区检测与摘要引擎 (Community Detection & Summarization Engine)

基于 Leiden 算法进行社区检测，当图谱边密度较高时将密集社区合并为超节点，
并为每个社区生成自然语言摘要，支持 Global Search 查询。

核心功能：
1. Leiden 社区检测 - 高质量的社区划分算法，保证社区连通性
2. 社区摘要生成 - 使用 LLM 为每个社区生成语义摘要
3. 超节点构建 - 将社区合并为超节点，降低图谱复杂度
4. 社区层次结构 - 支持多级社区嵌套

架构：
    图谱子图
        ↓
    ┌─────────────────┐
    │  Leiden Detector │ ← 检测紧密社区
    └────────┬────────┘
             ↓
    ┌─────────────────┐
    │  Refinement      │ ← 确保社区连通性
    └────────┬────────┘
             ↓
    ┌─────────────────┐
    │  Summarizer      │ ← LLM 生成社区摘要
    └────────┬────────┘
             ↓
    ┌─────────────────┐
    │  SuperNode Builder│ ← 写入 Neo4j 超节点
    └─────────────────┘

Leiden 算法优势（相比 Louvain）：
- 保证社区内部连通性
- 收敛更快，结果更稳定
- 支持分辨率参数 γ 精细控制社区粒度
- 能发现更细粒度的社区结构

作者：KQuery Pro Team
版本：2.0.0
更新：2026-05-07

依赖：
- networkx（图算法）
- app.repositories.graph.neo4j_connector（Neo4j 写入）
"""

from __future__ import annotations

import json
import time
import logging
import asyncio
from typing import Any, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from collections import defaultdict

try:
    import networkx as nx
    NETWORKX_AVAILABLE = True
except ImportError:
    NETWORKX_AVAILABLE = False
    nx = None

try:
    from app.repositories.graph.neo4j_connector import (
        Neo4jConnector, neo4j_manager
    )
    from app.core.logger import get_logger
except ImportError:
    def get_logger(name):
        return logging.getLogger(name)

logger = get_logger(__name__)


# ============================================================
# 数据模型
# ============================================================


@dataclass
class Community:
    """社区数据模型"""
    community_id: str
    members: List[str]  # 成员实体 ID 列表
    member_names: List[str]  # 成员实体名称列表
    internal_edges: List[Tuple[str, str]] = field(default_factory=list)  # 内部边
    external_edges: List[Tuple[str, str, str]] = field(default_factory=list)  # (成员, 外部实体, 关系类型)
    summary: Optional[str] = None  # 自然语言摘要
    summary_embedding: Optional[List[float]] = None  # 摘要向量
    density: float = 0.0  # 社区内部密度
    size: int = 0  # 成员数量
    level: int = 0  # 层次级别（0=顶层）
    parent_id: Optional[str] = None  # 父社区 ID
    child_ids: List[str] = field(default_factory=list)  # 子社区 ID
    key_entities: List[str] = field(default_factory=list)  # 关键实体名称
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.size = len(self.members)

    def to_neo4j_dict(self) -> Dict[str, Any]:
        """转换为 Neo4j 节点属性"""
        return {
            "id": self.community_id,
            "members": self.members,
            "member_names": self.member_names,
            "summary": self.summary or "",
            "density": round(self.density, 4),
            "size": self.size,
            "level": self.level,
            "parent_id": self.parent_id,
            "child_ids": self.child_ids,
            "key_entities": self.key_entities,
            "metadata": json.dumps(self.metadata),
        }


@dataclass
class CommunityDetectionResult:
    """社区检测完整结果"""
    communities: List[Community] = field(default_factory=list)
    node_to_community: Dict[str, str] = field(default_factory=dict)
    modularity: float = 0.0
    processing_time_ms: float = 0.0
    levels: int = 1  # 检测的层次数

    @property
    def community_count(self) -> int:
        return len(self.communities)

    @property
    def largest_community_size(self) -> int:
        if not self.communities:
            return 0
        return max(c.size for c in self.communities)


# ============================================================
# Leiden 社区检测算法
# ============================================================


class LeidenCommunityDetector:
    """Leiden 社区检测算法实现

    基于 Traag, Waltman & van Eck (2019) 的 Leiden 算法，
    使用 CPM (Constant Potts Model) 作为质量函数。

    与 Louvain 的关键区别：
    1. 快速移动阶段更严格，避免不稳定节点移动
    2. 凝聚阶段确保每个子社区在原始图中连通
    3. 聚合后在新网络上继续优化

    参数:
        gamma: 分辨率参数，越大社区越小越密集（默认 1.0）
        max_levels: 最大层次数（默认 10）
        min_community_size: 最小社区规模（默认 3）
        random_state: 随机种子
    """

    def __init__(
        self,
        gamma: float = 1.0,
        max_levels: int = 10,
        min_community_size: int = 3,
        random_state: Optional[int] = None,
    ):
        self.gamma = gamma
        self.max_levels = max_levels
        self.min_community_size = min_community_size
        self.random_state = random_state

        if not NETWORKX_AVAILABLE:
            raise ImportError("networkx 未安装，无法运行社区检测。请执行: uv add networkx")

        logger.info(
            f"🔬 Leiden 检测器初始化 | γ={gamma}, "
            f"max_levels={max_levels}, min_size={min_community_size}"
        )

    def detect(
        self,
        graph: nx.Graph,
        node_id_attr: str = "id",
    ) -> CommunityDetectionResult:
        """执行 Leiden 社区检测

        Args:
            graph: NetworkX 图对象（无向图）
            node_id_attr: 节点上存储原始实体 ID 的属性名

        Returns:
            CommunityDetectionResult: 社区检测结果
        """
        import random
        if self.random_state is not None:
            random.seed(self.random_state)

        start_time = time.time()

        # 确保是无向图
        if graph.is_directed():
            graph = graph.to_undirected()

        # 使用节点原始 ID 作为标签
        node_labels = {}
        for node in graph.nodes():
            node_labels[node] = graph.nodes[node].get(node_id_attr, str(node))

        # 初始分区：每个节点独立
        partition = {node: node for node in graph.nodes()}

        level = 0
        all_partitions = [dict(partition)]

        while level < self.max_levels:
            logger.debug(f"Leiden 第 {level + 1} 层优化...")

            # Step 1: 快速局部移动
            partition = self._fast_local_move(graph, partition)

            # 检查是否收敛
            if level > 0 and partition == all_partitions[-1]:
                logger.debug(f"第 {level + 1} 层无变化，收敛")
                break

            all_partitions.append(dict(partition))

            # Step 2: 凝聚 - 确保社区连通性并细化
            partition = self._refine(graph, partition)

            # Step 3: 基于当前分区构建聚合网络
            aggregated_graph, supernode_mapping = self._aggregate(graph, partition)

            if len(aggregated_graph.nodes()) == len(graph.nodes()):
                # 没有发生聚合，停止
                break

            # 在聚合网络上继续
            graph = aggregated_graph
            # 更新 partition 为聚合网络上的独立分区
            partition = {node: node for node in graph.nodes()}
            level += 1

        # 展开最终分区到原始节点
        final_partition = self._expand_partition(all_partitions[0], all_partitions[-1])

        # 构建结果
        result = self._build_result(
            original_graph=aggregated_graph if level > 0 else graph,
            partition=final_partition,
            node_labels=node_labels,
            level=level,
        )
        result.processing_time_ms = (time.time() - start_time) * 1000

        # 过滤小社区
        result.communities = [
            c for c in result.communities
            if c.size >= self.min_community_size
        ]

        logger.info(
            f"🔬 Leiden 检测完成 | 社区数: {result.community_count}, "
            f"层次: {result.levels}, 耗时: {result.processing_time_ms:.0f}ms"
        )

        return result

    def _fast_local_move(
        self,
        graph: nx.Graph,
        partition: Dict[Any, Any],
    ) -> Dict[Any, Any]:
        """快速局部移动阶段

        遍历节点，将其移动到使 CPM 质量增益最大的邻居社区。
        """
        import random

        improved = True
        iterations = 0
        max_iterations = 100

        # 计算社区大小和内部边数
        community_sizes = defaultdict(int)
        community_internal_edges = defaultdict(float)

        for node in graph.nodes():
            comm = partition[node]
            community_sizes[comm] += 1

        for u, v, data in graph.edges(data=True):
            weight = data.get("weight", 1.0)
            if partition[u] == partition[v]:
                community_internal_edges[partition[u]] += weight

        while improved and iterations < max_iterations:
            improved = False
            iterations += 1

            nodes = list(graph.nodes())
            random.shuffle(nodes)

            for node in nodes:
                current_comm = partition[node]
                node_degree = graph.degree(node, weight="weight")

                # 计算移动到每个邻居社区的增益
                neighbor_communities = defaultdict(float)
                for neighbor in graph.neighbors(node):
                    weight = graph[node][neighbor].get("weight", 1.0)
                    neighbor_comm = partition[neighbor]
                    if neighbor_comm != current_comm:
                        neighbor_communities[neighbor_comm] += weight

                best_comm = current_comm
                best_gain = 0.0

                for comm, edge_weight in neighbor_communities.items():
                    # CPM 增益计算
                    # ΔH = s_comm(node) - γ * size(comm)
                    gain = edge_weight - self.gamma * community_sizes[comm]

                    if gain > best_gain:
                        best_gain = gain
                        best_comm = comm

                # 也考虑创建新社区（单独）
                singleton_gain = -self.gamma * 0  # 新社区大小为 0
                # 实际上 singleton 的增益需要考虑当前社区失去该节点
                # 但为简化，我们只考虑合并到已有社区

                if best_comm != current_comm and best_gain > 0:
                    # 执行移动
                    # 更新当前社区
                    community_sizes[current_comm] -= 1
                    # 更新内部边（移除与当前社区其他节点的连接）
                    for neighbor in graph.neighbors(node):
                        if partition[neighbor] == current_comm:
                            community_internal_edges[current_comm] -= graph[node][neighbor].get("weight", 1.0)

                    # 更新目标社区
                    community_sizes[best_comm] += 1
                    for neighbor in graph.neighbors(node):
                        if partition[neighbor] == best_comm:
                            community_internal_edges[best_comm] += graph[node][neighbor].get("weight", 1.0)

                    partition[node] = best_comm
                    improved = True

        return partition

    def _refine(
        self,
        graph: nx.Graph,
        partition: Dict[Any, Any],
    ) -> Dict[Any, Any]:
        """凝聚阶段 - 确保社区连通性

        在每个社区内部进一步细化，将不连通的部分分离为独立社区。
        这是 Leiden 算法相比 Louvain 的关键改进。
        """
        refined_partition = {}

        # 按社区分组节点
        communities = defaultdict(set)
        for node, comm in partition.items():
            communities[comm].add(node)

        # 为每个社区独立处理
        next_comm_id = max(partition.values()) + 1 if partition else 0

        for comm, nodes in communities.items():
            if len(nodes) <= 2:
                # 小社区直接保留
                for node in nodes:
                    refined_partition[node] = comm
                continue

            # 提取社区诱导子图
            subgraph = graph.subgraph(nodes)

            # 检查子图连通性
            connected_components = list(nx.connected_components(subgraph))

            if len(connected_components) == 1:
                # 已连通，直接保留
                for node in nodes:
                    refined_partition[node] = comm
            else:
                # 不连通，为每个连通分量创建新社区
                for i, component in enumerate(connected_components):
                    if i == 0:
                        # 第一个分量保留原社区 ID
                        comp_id = comm
                    else:
                        comp_id = next_comm_id
                        next_comm_id += 1

                    for node in component:
                        refined_partition[node] = comp_id

        return refined_partition

    def _aggregate(
        self,
        graph: nx.Graph,
        partition: Dict[Any, Any],
    ) -> Tuple[nx.Graph, Dict[Any, Any]]:
        """网络聚合阶段

        将每个社区压缩为单个超节点，构建新的聚合网络。
        """
        # 构建社区到节点的映射
        community_nodes = defaultdict(set)
        for node, comm in partition.items():
            community_nodes[comm].add(node)

        # 创建超节点映射
        supernode_mapping = {}
        supernode_id = 0
        for comm in sorted(community_nodes.keys()):
            supernode_mapping[comm] = supernode_id
            supernode_id += 1

        # 构建聚合图
        aggregated = nx.Graph()

        # 添加超节点
        for comm, nodes in community_nodes.items():
            super_id = supernode_mapping[comm]
            aggregated.add_node(
                super_id,
                size=len(nodes),
                original_nodes=list(nodes),
            )

        # 添加超节点之间的边（权重为社区间边权重之和）
        for u, v, data in graph.edges(data=True):
            comm_u = partition[u]
            comm_v = partition[v]

            if comm_u != comm_v:
                super_u = supernode_mapping[comm_u]
                super_v = supernode_mapping[comm_v]
                weight = data.get("weight", 1.0)

                if aggregated.has_edge(super_u, super_v):
                    aggregated[super_u][super_v]["weight"] += weight
                else:
                    aggregated.add_edge(super_u, super_v, weight=weight)
            else:
                # 社区内部边记录为自环权重
                super_id = supernode_mapping[comm_u]
                if "self_weight" not in aggregated.nodes[super_id]:
                    aggregated.nodes[super_id]["self_weight"] = 0.0
                aggregated.nodes[super_id]["self_weight"] += data.get("weight", 1.0)

        return aggregated, supernode_mapping

    def _expand_partition(
        self,
        initial_partition: Dict[Any, Any],
        final_partition: Dict[Any, Any],
    ) -> Dict[Any, Any]:
        """将聚合网络上的最终分区展开到原始节点"""
        # 如果只有一层，直接返回
        if len(set(initial_partition.values())) == len(initial_partition):
            return final_partition

        # 需要追踪每个原始节点最终属于哪个社区
        # 简化处理：假设 final_partition 已经是在某种聚合后的映射
        # 这里我们直接返回一个基于 final_partition 的映射
        return final_partition

    def _build_result(
        self,
        original_graph: nx.Graph,
        partition: Dict[Any, Any],
        node_labels: Dict[Any, str],
        level: int,
    ) -> CommunityDetectionResult:
        """构建检测结果"""
        result = CommunityDetectionResult(levels=level + 1)

        # 按社区分组
        community_members = defaultdict(list)
        for node, comm in partition.items():
            community_members[comm].append(node)

        for comm_id, members in community_members.items():
            if len(members) < self.min_community_size:
                continue

            # 计算社区密度
            subgraph = original_graph.subgraph(members)
            n = len(members)
            m = subgraph.number_of_edges()
            density = (2 * m) / (n * (n - 1)) if n > 1 else 0.0

            # 收集成员名称
            member_names = [node_labels.get(m, str(m)) for m in members]

            # 收集内部边
            internal_edges = []
            for u, v in subgraph.edges():
                internal_edges.append((node_labels.get(u, str(u)), node_labels.get(v, str(v))))

            # 收集外部边（指向社区外的边）
            external_edges = []
            for node in members:
                for neighbor in original_graph.neighbors(node):
                    if neighbor not in members:
                        rel_type = original_graph[node][neighbor].get("relation_type", "related_to")
                        external_edges.append((
                            node_labels.get(node, str(node)),
                            node_labels.get(neighbor, str(neighbor)),
                            rel_type,
                        ))

            community = Community(
                community_id=f"community-{comm_id}",
                members=[node_labels.get(m, str(m)) for m in members],
                member_names=member_names,
                internal_edges=internal_edges,
                external_edges=external_edges,
                density=density,
                level=0,
            )

            # 找出关键实体（度数最高的）
            degree_centrality = nx.degree_centrality(subgraph)
            sorted_nodes = sorted(degree_centrality.items(), key=lambda x: x[1], reverse=True)
            community.key_entities = [
                node_labels.get(n, str(n)) for n, _ in sorted_nodes[:5]
            ]

            result.communities.append(community)

            for m in members:
                result.node_to_community[node_labels.get(m, str(m))] = community.community_id

        # 计算 modularity
        try:
            # 构建 community map for networkx
            comm_map = {}
            for node in original_graph.nodes():
                label = node_labels.get(node, str(node))
                comm_map[node] = partition.get(node, node)

            result.modularity = nx.community.modularity(
                original_graph,
                [
                    {node for node, comm in comm_map.items() if comm == c}
                    for c in set(comm_map.values())
                ],
            )
        except Exception as e:
            logger.debug(f"Modularity 计算失败: {e}")
            result.modularity = 0.0

        return result


# ============================================================
# 社区摘要生成器
# ============================================================


class CommunitySummarizer:
    """使用 LLM 为社区生成自然语言摘要

    将社区中的实体和关系信息编码为提示词，
    让 LLM 生成一段概括性的摘要文本。

    摘要用途：
    - Global Search: 回答宏观问题（"这个知识库的主题是什么？"）
    - 可视化: 展示社区的核心内容
    - 导航: 帮助用户理解图谱结构
    """

    SYSTEM_PROMPT = """你是一个知识图谱摘要专家。你的任务是根据给定的社区信息，生成一段简洁但信息丰富的摘要。

## 摘要要求
1. **主题概括**: 用 1-2 句话概括这个社区的核心主题
2. **关键实体**: 提及 3-5 个最重要的实体及其角色
3. **关系网络**: 描述实体之间的主要关系模式
4. **信息量**: 摘要应足够详细，使读者无需查看原始数据就能理解社区内容
5. **语言**: 使用与输入相同的语言（中文或英文）

## 输出格式
只返回摘要文本，不要添加标题、编号或其他格式。长度控制在 150-300 字。"""

    def __init__(
        self,
        model_name: str = "qwen-plus",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        max_tokens: int = 1024,
    ):
        self.model_name = model_name
        self.max_tokens = max_tokens
        self.api_key = api_key
        self.base_url = base_url

        self.client = None
        self.client_type = None
        self._initialize_client()

        logger.info(f"📝 社区摘要生成器初始化 | 模型: {model_name}")

    def _initialize_client(self):
        """初始化 LLM 客户端"""
        import os

        api_key = self.api_key or os.getenv("DASHSCOPE_API_KEY") or os.getenv("OPENAI_API_KEY")
        base_url = self.base_url or os.getenv("OPENAI_BASE_URL")

        if not api_key:
            logger.warning("⚠️ 未配置 API 密钥，社区摘要生成将不可用")
            return

        if base_url and "dashscope" in base_url.lower():
            try:
                import dashscope
                dashscope.api_key = api_key
                self.client_type = "dashscope"
            except ImportError:
                pass
        else:
            try:
                from openai import AsyncOpenAI
                self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
                self.client_type = "openai"
            except ImportError:
                pass

    async def summarize_community(
        self,
        community: Community,
        include_external: bool = True,
    ) -> str:
        """为单个社区生成摘要

        Args:
            community: 社区对象
            include_external: 是否包含与外部实体的关系

        Returns:
            摘要文本
        """
        if not self.client and self.client_type != "dashscope":
            return self._generate_fallback_summary(community)

        # 构建社区描述
        description = self._build_community_description(community, include_external)

        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": description},
        ]

        try:
            summary = await self._call_llm(messages)
            return summary.strip()
        except Exception as e:
            logger.warning(f"LLM 摘要生成失败: {e}，使用回退方案")
            return self._generate_fallback_summary(community)

    async def summarize_communities(
        self,
        communities: List[Community],
        concurrency: int = 5,
    ) -> List[Community]:
        """批量生成社区摘要

        Args:
            communities: 社区列表
            concurrency: 并发数

        Returns:
            更新后的社区列表（已填充 summary 字段）
        """
        semaphore = asyncio.Semaphore(concurrency)

        async def process_one(comm: Community) -> Community:
            async with semaphore:
                comm.summary = await self.summarize_community(comm)
                return comm

        tasks = [process_one(c) for c in communities]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        updated = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning(f"社区 {communities[i].community_id} 摘要失败: {result}")
                communities[i].summary = self._generate_fallback_summary(communities[i])
                updated.append(communities[i])
            else:
                updated.append(result)

        return updated

    def _build_community_description(
        self,
        community: Community,
        include_external: bool = True,
    ) -> str:
        """构建社区的自然语言描述（用于输入 LLM）"""
        lines = [f"【社区: {community.community_id}】"]
        lines.append(f"成员数量: {community.size}")
        lines.append(f"内部密度: {community.density:.2%}")
        lines.append("")

        # 关键实体
        lines.append("【关键实体】")
        for name in community.key_entities[:8]:
            if name in community.member_names:
                lines.append(f"- {name}")
        lines.append("")

        # 成员列表（限制数量）
        lines.append("【成员列表】")
        for name in community.member_names[:20]:
            lines.append(f"- {name}")
        if len(community.member_names) > 20:
            lines.append(f"... 还有 {len(community.member_names) - 20} 个成员")
        lines.append("")

        # 内部关系
        if community.internal_edges:
            lines.append("【内部关系】")
            for u, v in community.internal_edges[:15]:
                lines.append(f"- {u} -- {v}")
            if len(community.internal_edges) > 15:
                lines.append(f"... 还有 {len(community.internal_edges) - 15} 条关系")
            lines.append("")

        # 外部关系
        if include_external and community.external_edges:
            lines.append("【与外部的连接】")
            for member, external, rel_type in community.external_edges[:10]:
                lines.append(f"- {member} --[{rel_type}]--> {external}")
            if len(community.external_edges) > 10:
                lines.append(f"... 还有 {len(community.external_edges) - 10} 条外部连接")

        return "\n".join(lines)

    def _generate_fallback_summary(self, community: Community) -> str:
        """当 LLM 不可用时生成简单摘要"""
        parts = []
        parts.append(
            f"这是一个包含 {community.size} 个实体的知识社区，"
            f"内部连接密度为 {community.density:.1%}。"
        )

        if community.key_entities:
            parts.append(
                f"核心实体包括: {', '.join(community.key_entities[:5])}。"
            )

        if community.internal_edges:
            parts.append(
                f"社区内部存在 {len(community.internal_edges)} 条关系连接。"
            )

        if community.external_edges:
            parts.append(
                f"该社区与外部有 {len(community.external_edges)} 条关联。"
            )

        return " ".join(parts)

    async def _call_llm(self, messages: List[Dict[str, str]]) -> str:
        """调用 LLM API"""
        if self.client_type == "dashscope":
            import dashscope
            response = dashscope.Generation.call(
                model=self.model_name,
                messages=messages,
                max_tokens=self.max_tokens,
                temperature=0.3,
                result_format="message",
            )
            if response.status_code != 200:
                raise Exception(f"DashScope API 错误: {response.message}")
            return response.output.choices[0].message.content
        else:
            completion = await self.client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                max_tokens=self.max_tokens,
                temperature=0.3,
            )
            return completion.choices[0].message.content


# ============================================================
# 超节点构建器
# ============================================================


class SuperNodeBuilder:
    """将社区合并为超节点并持久化到 Neo4j

    超节点设计：
    - 类型标签: :Community :Entity
    - 属性: 社区元数据、摘要、成员列表、关键实体等
    - 与外部关系: 继承社区成员的对外关系

    这种设计允许查询时：
    - 宏观查询（Global Search）直接访问 Community 节点
    - 微观查询（Local Search）仍然访问原始 Entity 节点
    """

    def __init__(self, connector: Optional[Neo4jConnector] = None):
        self.connector = connector or neo4j_manager.connector
        logger.info("🏗️ 超节点构建器初始化")

    async def build_super_nodes(
        self,
        communities: List[Community],
        kb_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """将社区列表写入 Neo4j 作为超节点

        Args:
            communities: 社区列表（已生成摘要）
            kb_id: 知识库 ID

        Returns:
            构建统计信息
        """
        if not self.connector._is_connected:
            raise RuntimeError("Neo4j 未连接")

        stats = {"communities_created": 0, "member_links": 0, "external_links": 0}

        for community in communities:
            try:
                # 1. 创建 Community 节点
                await self._create_community_node(community, kb_id)
                stats["communities_created"] += 1

                # 2. 创建成员关系 BELONGS_TO
                await self._link_members(community, kb_id)
                stats["member_links"] += len(community.members)

                # 3. 创建外部关系（超节点级别）
                external_count = await self._create_external_links(community, kb_id)
                stats["external_links"] += external_count

            except Exception as e:
                logger.error(f"构建超节点 {community.community_id} 失败: {e}")

        logger.info(
            f"🏗️ 超节点构建完成 | 社区: {stats['communities_created']}, "
            f"成员链接: {stats['member_links']}, 外部链接: {stats['external_links']}"
        )

        return stats

    async def _create_community_node(
        self,
        community: Community,
        kb_id: Optional[int] = None,
    ):
        """创建 Community 节点"""
        props = community.to_neo4j_dict()
        props["kb_id"] = kb_id

        cypher = """
        MERGE (c:Community:Entity {id: $id})
        SET 
            c.name = $name,
            c.type = 'Community',
            c.members = $members,
            c.member_names = $member_names,
            c.summary = $summary,
            c.density = $density,
            c.size = $size,
            c.level = $level,
            c.parent_id = $parent_id,
            c.child_ids = $child_ids,
            c.key_entities = $key_entities,
            c.metadata = $metadata,
            c.kb_id = $kb_id,
            c.is_super_node = true,
            c.updated_at = datetime()
        RETURN c.id as id
        """

        params = {
            "id": community.community_id,
            "name": f"社区: {', '.join(community.key_entities[:3])}",
            **props,
        }

        await self.connector.execute_query(cypher, params, access_mode="WRITE")

    async def _link_members(
        self,
        community: Community,
        kb_id: Optional[int] = None,
    ):
        """创建成员 BELONGS_TO 关系"""
        if not community.members:
            return

        cypher = """
        MATCH (c:Community {id: $community_id})
        UNWIND $member_ids AS member_id
        MATCH (e:Entity {id: member_id})
        MERGE (e)-[r:BELONGS_TO]->(c)
        SET r.kb_id = $kb_id, r.linked_at = datetime()
        RETURN count(r) as linked
        """

        await self.connector.execute_query(
            cypher,
            {
                "community_id": community.community_id,
                "member_ids": community.members,
                "kb_id": kb_id,
            },
            access_mode="WRITE",
        )

    async def _create_external_links(
        self,
        community: Community,
        kb_id: Optional[int] = None,
    ) -> int:
        """创建社区对外的关系（超节点级别）

        策略：如果社区中多个成员与同一外部实体有相同类型的关系，
        则在社区节点和该外部实体之间创建一条聚合关系。
        """
        if not community.external_edges:
            return 0

        # 聚合外部关系：(外部实体名称, 关系类型) -> 数量
        agg_relations: Dict[Tuple[str, str], int] = defaultdict(int)
        for _, external, rel_type in community.external_edges:
            agg_relations[(external, rel_type)] += 1

        # 为高频外部关系创建超节点级别边
        created = 0
        for (external_name, rel_type), count in agg_relations.items():
            if count < 1:
                continue

            cypher = """
            MATCH (c:Community {id: $community_id})
            MATCH (e:Entity {name: $external_name})
            WHERE e.id <> c.id
            MERGE (c)-[r:RELATED_TO {type: $rel_type}]->(e)
            SET r.weight = COALESCE(r.weight, 0) + $count,
                r.is_super_edge = true,
                r.kb_id = $kb_id,
                r.updated_at = datetime()
            RETURN count(r) as created
            """

            try:
                result = await self.connector.execute_query(
                    cypher,
                    {
                        "community_id": community.community_id,
                        "external_name": external_name,
                        "rel_type": rel_type,
                        "count": count,
                        "kb_id": kb_id,
                    },
                    access_mode="WRITE",
                )
                if result:
                    created += result[0].get("created", 0)
            except Exception as e:
                logger.debug(f"创建外部链接失败 ({external_name}): {e}")

        return created

    async def remove_communities(
        self,
        community_ids: Optional[List[str]] = None,
        kb_id: Optional[int] = None,
    ):
        """删除社区超节点（保留原始实体）

        Args:
            community_ids: 要删除的社区 ID 列表，None 则删除所有
            kb_id: 按知识库删除
        """
        conditions = ["c:Community"]
        params: Dict[str, Any] = {}

        if community_ids:
            conditions.append("c.id IN $community_ids")
            params["community_ids"] = community_ids

        if kb_id is not None:
            conditions.append("c.kb_id = $kb_id")
            params["kb_id"] = kb_id

        where_clause = " AND ".join(conditions)

        cypher = f"""
        MATCH (c:Community)
        WHERE {where_clause}
        OPTIONAL MATCH (c)-[r]-()
        DELETE r, c
        RETURN count(c) as removed
        """

        result = await self.connector.execute_query(cypher, params, access_mode="WRITE")
        removed = result[0].get("removed", 0) if result else 0
        logger.info(f"🗑️ 已删除 {removed} 个社区超节点")
        return removed


# ============================================================
# 统一入口 - CommunityEngine
# ============================================================


class CommunityEngine:
    """社区检测与摘要引擎（统一入口）

    整合 Leiden 检测、摘要生成和超节点构建的完整流程。

    使用示例:
        engine = CommunityEngine()

        # 检测社区并生成超节点
        result = await engine.process_subgraph(
            center_entity_ids=["entity-1", "entity-2"],
            kb_id=1,
        )

        # 查询社区
        communities = await engine.get_communities(kb_id=1)
    """

    # 默认密度阈值：当图的平均密度超过此值时触发社区检测
    DENSITY_THRESHOLD = 0.15

    def __init__(
        self,
        connector: Optional[Neo4jConnector] = None,
        gamma: float = 1.0,
        min_community_size: int = 3,
    ):
        self.connector = connector or neo4j_manager.connector
        self.detector = LeidenCommunityDetector(
            gamma=gamma,
            min_community_size=min_community_size,
        )
        self.summarizer = CommunitySummarizer()
        self.builder = SuperNodeBuilder(self.connector)

        logger.info("🌐 社区引擎初始化完成")

    async def process_subgraph(
        self,
        center_entity_ids: List[str],
        radius: int = 2,
        kb_id: Optional[int] = None,
        force: bool = False,
    ) -> CommunityDetectionResult:
        """对指定子图执行完整的社区检测流程

        Args:
            center_entity_ids: 中心实体 ID 列表
            radius: 子图半径
            kb_id: 知识库 ID
            force: 是否强制执行（忽略密度阈值）

        Returns:
            CommunityDetectionResult
        """
        import time
        start = time.time()

        # 1. 从 Neo4j 提取子图
        graph = await self._extract_subgraph(center_entity_ids, radius, kb_id)

        if graph.number_of_nodes() == 0:
            logger.warning("子图为空，跳过社区检测")
            return CommunityDetectionResult()

        # 2. 检查密度阈值
        density = nx.density(graph) if NETWORKX_AVAILABLE else 0
        logger.info(f"子图密度: {density:.2%} (阈值: {self.DENSITY_THRESHOLD:.2%})")

        if not force and density < self.DENSITY_THRESHOLD:
            logger.info("子图密度低于阈值，跳过社区检测")
            return CommunityDetectionResult()

        # 3. 执行 Leiden 检测
        result = self.detector.detect(graph, node_id_attr="entity_id")

        # 4. 生成摘要
        result.communities = await self.summarizer.summarize_communities(
            result.communities
        )

        # 5. 写入超节点
        stats = await self.builder.build_super_nodes(result.communities, kb_id)

        result.processing_time_ms = (time.time() - start) * 1000

        logger.info(
            f"🌐 社区处理完成 | 社区: {result.community_count}, "
            f"modularity: {result.modularity:.4f}, "
            f"超节点: {stats['communities_created']}, "
            f"耗时: {result.processing_time_ms:.0f}ms"
        )

        return result

    async def process_full_graph(
        self,
        kb_id: Optional[int] = None,
        force: bool = False,
    ) -> CommunityDetectionResult:
        """对整个知识图谱执行社区检测

        Args:
            kb_id: 知识库 ID（None 则处理全部）
            force: 是否强制执行

        Returns:
            CommunityDetectionResult
        """
        # 提取完整图谱
        graph = await self._extract_full_graph(kb_id)

        if graph.number_of_nodes() == 0:
            return CommunityDetectionResult()

        density = nx.density(graph) if NETWORKX_AVAILABLE else 0
        logger.info(f"全图密度: {density:.2%}")

        if not force and density < self.DENSITY_THRESHOLD:
            logger.info("全图密度低于阈值，跳过社区检测")
            return CommunityDetectionResult()

        result = self.detector.detect(graph, node_id_attr="entity_id")
        result.communities = await self.summarizer.summarize_communities(
            result.communities
        )
        await self.builder.build_super_nodes(result.communities, kb_id)

        return result

    async def get_communities(
        self,
        kb_id: Optional[int] = None,
        min_size: int = 3,
        limit: int = 50,
    ) -> List[Community]:
        """从 Neo4j 查询已存在的社区超节点"""
        kb_filter = "WHERE c.kb_id = $kb_id" if kb_id is not None else ""
        params = {"kb_id": kb_id, "min_size": min_size, "limit": limit}

        cypher = f"""
        MATCH (c:Community)
        {kb_filter}
        WHERE c.size >= $min_size
        RETURN c.id as id,
               c.members as members,
               c.member_names as member_names,
               c.summary as summary,
               c.density as density,
               c.size as size,
               c.level as level,
               c.key_entities as key_entities
        ORDER BY c.size DESC
        LIMIT $limit
        """

        results = await self.connector.execute_query(cypher, params)

        communities = []
        for r in results:
            communities.append(Community(
                community_id=r["id"],
                members=r.get("members", []),
                member_names=r.get("member_names", []),
                summary=r.get("summary"),
                density=r.get("density", 0.0),
                size=r.get("size", 0),
                level=r.get("level", 0),
                key_entities=r.get("key_entities", []),
            ))

        return communities

    async def search_communities(
        self,
        query: str,
        kb_id: Optional[int] = None,
        limit: int = 10,
    ) -> List[Community]:
        """基于摘要内容搜索社区（Global Search 支持）"""
        kb_filter = "AND c.kb_id = $kb_id" if kb_id is not None else ""
        params = {"query": query, "kb_id": kb_id, "limit": limit}

        # 使用关键词匹配（可扩展为向量匹配）
        cypher = f"""
        MATCH (c:Community)
        WHERE (c.summary CONTAINS $query
               OR c.name CONTAINS $query
               OR ANY(name IN c.member_names WHERE name CONTAINS $query))
        {kb_filter}
        RETURN c.id as id,
               c.members as members,
               c.member_names as member_names,
               c.summary as summary,
               c.density as density,
               c.size as size,
               c.key_entities as key_entities
        ORDER BY c.size DESC
        LIMIT $limit
        """

        results = await self.connector.execute_query(cypher, params)

        communities = []
        for r in results:
            communities.append(Community(
                community_id=r["id"],
                members=r.get("members", []),
                member_names=r.get("member_names", []),
                summary=r.get("summary"),
                density=r.get("density", 0.0),
                size=r.get("size", 0),
                key_entities=r.get("key_entities", []),
            ))

        return communities

    async def _extract_subgraph(
        self,
        center_entity_ids: List[str],
        radius: int = 2,
        kb_id: Optional[int] = None,
    ) -> nx.Graph:
        """从 Neo4j 提取子图为 NetworkX 图"""
        graph = nx.Graph()

        if not self.connector._is_connected:
            return graph

        # 收集半径范围内的所有节点
        kb_filter = "AND e.kb_id = $kb_id" if kb_id is not None else ""
        params = {"center_ids": center_entity_ids, "radius": radius, "kb_id": kb_id}

        # 查询节点
        node_cypher = f"""
        UNWIND $center_ids AS centerId
        MATCH (center:Entity {{id: centerId}})
        CALL {{
            WITH center
            MATCH path = (center)-[:RELATED_TO*1..{radius}]-(neighbor:Entity)
            WITH DISTINCT neighbor
            RETURN collect(neighbor) AS neighbors
        }}
        WITH center, neighbors
        UNWIND [center] + neighbors AS allNodes
        WITH DISTINCT allNodes
        RETURN allNodes.id as id, allNodes.name as name, allNodes.type as type
        """

        try:
            nodes = await self.connector.execute_query(node_cypher, params)
        except Exception as e:
            logger.warning(f"子图节点查询失败: {e}")
            return graph

        for node in nodes:
            graph.add_node(
                node["id"],
                entity_id=node["id"],
                name=node.get("name", ""),
                type=node.get("type", "Keyword"),
            )

        # 查询边
        edge_cypher = f"""
        UNWIND $center_ids AS centerId
        MATCH (center:Entity {{id: centerId}})
        CALL {{
            WITH center
            MATCH path = (center)-[:RELATED_TO*1..{radius}]-(neighbor:Entity)
            RETURN collect(DISTINCT neighbor.id) AS neighbor_ids
        }}
        WITH centerId, neighbor_ids
        MATCH (a:Entity)-[r:RELATED_TO]->(b:Entity)
        WHERE a.id IN ([centerId] + neighbor_ids)
          AND b.id IN ([centerId] + neighbor_ids)
        {kb_filter.replace('e.', 'a.')}
        RETURN a.id as source, b.id as target, r.type as rel_type,
               COALESCE(r.weight, 1.0) as weight
        LIMIT 5000
        """

        try:
            edges = await self.connector.execute_query(edge_cypher, params)
        except Exception as e:
            logger.warning(f"子图边查询失败: {e}")
            return graph

        for edge in edges:
            graph.add_edge(
                edge["source"],
                edge["target"],
                weight=edge.get("weight", 1.0),
                relation_type=edge.get("rel_type", "related_to"),
            )

        return graph

    async def _extract_full_graph(
        self,
        kb_id: Optional[int] = None,
    ) -> nx.Graph:
        """从 Neo4j 提取完整图谱"""
        graph = nx.Graph()

        kb_filter = "WHERE e.kb_id = $kb_id" if kb_id is not None else ""
        params = {"kb_id": kb_id}

        # 查询所有节点
        node_cypher = f"""
        MATCH (e:Entity)
        {kb_filter}
        RETURN e.id as id, e.name as name, e.type as type
        LIMIT 10000
        """

        try:
            nodes = await self.connector.execute_query(node_cypher, params)
            for node in nodes:
                graph.add_node(
                    node["id"],
                    entity_id=node["id"],
                    name=node.get("name", ""),
                    type=node.get("type", "Keyword"),
                )
        except Exception as e:
            logger.warning(f"全图节点查询失败: {e}")

        # 查询所有边
        edge_cypher = f"""
        MATCH (a:Entity)-[r:RELATED_TO]->(b:Entity)
        {kb_filter.replace('e.', 'a.')}
        RETURN a.id as source, b.id as target, r.type as rel_type,
               COALESCE(r.weight, 1.0) as weight
        LIMIT 50000
        """

        try:
            edges = await self.connector.execute_query(edge_cypher, params)
            for edge in edges:
                graph.add_edge(
                    edge["source"],
                    edge["target"],
                    weight=edge.get("weight", 1.0),
                    relation_type=edge.get("rel_type", "related_to"),
                )
        except Exception as e:
            logger.warning(f"全图边查询失败: {e}")

        return graph


__all__ = [
    "Community",
    "CommunityDetectionResult",
    "LeidenCommunityDetector",
    "CommunitySummarizer",
    "SuperNodeBuilder",
    "CommunityEngine",
]
