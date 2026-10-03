"""社区检测与摘要（阶段 7 §2.4）。

- 默认零依赖实现：连通分量（生产可换 Leiden，算法可替换落在接口上）；
- **社区编号必须稳定**：节点排序归一化后按"最小成员名"排序赋号，
  同一份图重复跑出相同编号（否则摘要挂错社区）；
- 社区数量上限：过小社区（< min_size）不生成摘要（摘要成本与社区数线性相关）；
- 摘要带生成时刻与模型版本（过时判据）。
"""
import logging
import time
from typing import Protocol

from app.domain.schemas import Community
from app.services.graph.store import InMemoryGraphStore
from app.services.llm import LLM

logger = logging.getLogger(__name__)

COMMUNITY_SUMMARY_PROMPT = """为下面的实体社区生成一段主题摘要（80 字以内）。
摘要回答：这个社区涉及什么主题、有哪些关键实体、实体间的核心关系。

实体及关系：
{content}

直接输出摘要文本，不要输出其他内容。"""


class CommunityDetector(Protocol):
    def detect(self, store: InMemoryGraphStore) -> list[list[str]]: ...


class ConnectedComponentsDetector:
    """连通分量（零依赖默认实现）：无向弱连通分组。"""

    def detect(self, store: InMemoryGraphStore) -> list[list[str]]:
        adjacency: dict[str, set[str]] = {}
        for rel in store.all_relations():
            adjacency.setdefault(rel.source, set()).add(rel.target)
            adjacency.setdefault(rel.target, set()).add(rel.source)
        # 节点排序归一化（编号稳定的前提）
        all_names = sorted(adjacency.keys())
        visited: set[str] = set()
        groups: list[list[str]] = []
        for name in all_names:
            if name in visited:
                continue
            group, stack = [], [name]
            while stack:
                node = stack.pop()
                if node in visited:
                    continue
                visited.add(node)
                group.append(node)
                stack.extend(sorted(adjacency.get(node, ()) - visited))
            groups.append(sorted(group))
        # 按"最小成员名"排序 → 编号稳定
        groups.sort(key=lambda g: g[0])
        return groups


def build_communities(
    store: InMemoryGraphStore,
    llm: LLM | None,
    detector: CommunityDetector | None = None,
    min_size: int = 2,
    max_communities: int = 20,
    model_version: str = "",
    tenant_id: str = "default",
) -> list[Community]:
    """检测社区并生成摘要（小社区跳过摘要，控制 LLM 成本）。"""
    detector = detector or ConnectedComponentsDetector()
    groups = detector.detect(store)
    communities: list[Community] = []
    for idx, members in enumerate(groups[:max_communities], start=1):
        cid = f"c-{idx:03d}"  # 稳定编号：同图同编号
        community = Community(
            community_id=cid, members=members, size=len(members),
            generated_at=time.time(), model_version=model_version, tenant_id=tenant_id,
        )
        if len(members) >= min_size and llm is not None:
            content = _community_brief(store, members)
            try:
                community.summary = llm.generate(COMMUNITY_SUMMARY_PROMPT.format(content=content)).strip()
            except Exception as exc:  # noqa: BLE001
                logger.warning("社区 %s 摘要生成失败: %s", cid, exc)
        store.set_community(community)
        communities.append(community)
    return communities


def _community_brief(store: InMemoryGraphStore, members: list[str]) -> str:
    member_set = set(members)
    lines = []
    for rel in store.all_relations():
        if rel.source in member_set and rel.target in member_set:
            lines.append(f"{rel.source} —{rel.relation}→ {rel.target}")
    return f"成员：{'、'.join(members[:20])}\n关系：\n" + "\n".join(lines[:30])
