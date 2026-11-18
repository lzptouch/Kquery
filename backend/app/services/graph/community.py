"""社区检测与摘要。

- 默认零依赖实现：连通分量（生产可换 Leiden，算法可替换落在接口上）；
- **社区编号必须稳定**：节点排序归一化后按"最小成员名"排序赋号，
  同一份图重复跑出相同编号（否则摘要挂错社区）；
- 社区数量上限：过小社区（< min_size）不生成摘要（摘要成本与社区数线性相关）；
- 摘要带生成时刻与模型版本（过时判据）。
"""
import time
from typing import Protocol

from app.domain.schemas import Community
from app.services.graph.store import BaseGraphStore
from app.services.llm import LLM
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

COMMUNITY_SUMMARY_PROMPT = """为下面的实体社区生成一段主题摘要（80 字以内）。
摘要回答：这个社区涉及什么主题、有哪些关键实体、实体间的核心关系。

实体及关系：
{content}

直接输出摘要文本，不要输出其他内容。"""


class CommunityDetector(Protocol):
    def detect(self, store: BaseGraphStore, tenant_id: str | None = None) -> list[list[str]]: ...


class ConnectedComponentsDetector:
    """连通分量（零依赖默认实现）：无向弱连通分组。"""

    def detect(self, store: BaseGraphStore, tenant_id: str | None = None) -> list[list[str]]:
        adjacency: dict[str, set[str]] = {}
        # KQ-06：社区检测只纳入本租户关系，跨租户实体不得并入同一社区
        for rel in store.all_relations(tenant_id):
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


class HierarchicalCommunityDetector:
    """多级社区检测（§5.2/§5.3）：L1 细粒度连通分量 → L2/L3 逐级聚合。

    为什么要有层级：单层连通分量里，社区内所有实体直接相连，于是"某某产品的保修政策"
    和"某某产品的退货政策"会被塞进同一个社区——因为它们都跟"产品"相连。单层图谱
    天然分不出主题层级，只有把 L1 社区按成员重叠度逐级合并，才能得到 L2（产品线）、
    L3（全库主题）这种粒度。

    合并规则（贪心重叠合并，§5.2）：
    - 两个社区成员重叠比例 ≥ `merge_threshold` 才合并；
    - 反复合并直到没有可合并的对或达到 `max_level`。

    编号稳定性：同图同参数必须给出同编号——层内按"最小成员名"排序，
    聚合时按重叠度降序 + 成员名字典序做 tie-break。
    """

    def __init__(self, merge_threshold: float = 0.3, max_level: int = 3) -> None:
        self._merge_threshold = merge_threshold
        self._max_level = max_level

    def detect_levels(self, store: BaseGraphStore, tenant_id: str | None = None) -> list[list[list[str]]]:
        """逐层聚合，返回 [[L1 社区], [L2 社区], ...]。

        **每层的 members 都是实体名**（不是下层 community_id）。理由：
        - 重叠度要在实体粒度上算，用 community_id 算等于数"几个孩子重合"，
          一次合并就把层级压扁了，拿不到真正的粗粒度主题；
        - 前端展开任一层都要能直接列出实体，不必递归回溯；
        - 层级关系由 `Community.level` + `parent_id` 承载，不靠 members 编码。

        这与 `schemas.Community.members` 的注释（level>0 时为下层 community_id）
        冲突，取前者：数据可用性优先，注释在下一次改动里一并更正。
        """
        base = ConnectedComponentsDetector().detect(store, tenant_id)
        levels: list[list[list[str]]] = [base] if base else []
        current = base
        for _ in range(1, self._max_level):
            merged = self._merge_once(current)
            # 合并后层数没减少说明已经收敛，再合并也不会更粗
            if len(merged) >= len(current) or not merged:
                break
            levels.append(merged)
            current = merged
        return levels

    def detect(self, store: BaseGraphStore) -> list[list[str]]:
        """Protocol 要求的最细一层（level 0）。"""
        levels = self.detect_levels(store)
        return levels[0] if levels else []

    def _overlap_ratio(self, a: list[str], b: list[str]) -> float:
        """重叠率：较小社区被覆盖的比例。

        用"小社区被覆盖比例"而不是 Jaccard：一个大社区与几十个小社区共享一个枢纽实体时，
        Jaccard 会给出极低分导致永远合并不了，而小社区其实已经基本被吞并了。
        """
        set_a, set_b = set(a), set(b)
        if not set_a or not set_b:
            return 0.0
        return len(set_a & set_b) / min(len(set_a), len(set_b))

    def _merge_once(self, groups: list[list[str]]) -> list[list[str]]:
        groups = [sorted(g) for g in groups if g]
        if len(groups) < 2:
            return groups
        merged: list[list[str]] = []
        consumed = [False] * len(groups)
        for i in range(len(groups)):
            if consumed[i]:
                continue
            current = list(groups[i])
            # 收集所有与 current 重叠达阈值的候选，按重叠度降序（稳定 tie-break）
            candidates = [
                (self._overlap_ratio(current, groups[j]), j)
                for j in range(i + 1, len(groups))
                if not consumed[j]
            ]
            candidates = [c for c in candidates if c[0] >= self._merge_threshold]
            candidates.sort(key=lambda item: (-item[0], groups[item[1]][0]))
            for _, j in candidates:
                if consumed[j]:
                    continue
                current = sorted(set(current) | set(groups[j]))
                consumed[j] = True
            consumed[i] = True
            merged.append(current)
        merged.sort(key=lambda g: g[0])  # 编号稳定：按最小成员名排序
        return merged


def build_communities(
    store: BaseGraphStore,
    llm: LLM | None,
    detector: CommunityDetector | None = None,
    min_size: int = 2,
    max_communities: int = 20,
    model_version: str = "",
    tenant_id: str = "default",
) -> list[Community]:
    """检测社区并生成摘要（小社区跳过摘要，控制 LLM 成本）。

    detector 是 `HierarchicalCommunityDetector` 时产出多级社区（community_id 带
    `-L{n}` 后缀）；否则退化为单层 L1，行为与旧版完全一致。
    """
    if isinstance(detector, HierarchicalCommunityDetector):
        return _build_hierarchical(
            store, llm, detector, min_size, max_communities, model_version, tenant_id
        )
    detector = detector or ConnectedComponentsDetector()
    groups = detector.detect(store, tenant_id)
    communities: list[Community] = []
    for idx, members in enumerate(groups[:max_communities], start=1):
        cid = f"c-{idx:03d}"  # 稳定编号：同图同编号
        community = Community(
            community_id=cid, members=members, size=len(members), level=0,
            generated_at=time.time(), model_version=model_version, tenant_id=tenant_id,
        )
        if len(members) >= min_size and llm is not None:
            content = _community_brief(store, members, tenant_id)
            try:
                community.summary = llm.generate(COMMUNITY_SUMMARY_PROMPT.format(content=content)).strip()
            except Exception as exc:  # noqa: BLE001 - 社区 %s 摘要生成失败
                logger.warning("社区 %s 摘要生成失败: %s", cid, exc)
        store.set_community(community)
        communities.append(community)
    return communities


def _build_hierarchical(
    store: BaseGraphStore,
    llm: LLM | None,
    detector: HierarchicalCommunityDetector,
    min_size: int,
    max_communities: int,
    model_version: str,
    tenant_id: str,
) -> list[Community]:
    """多级社区构建：逐层编号、逐层摘要。

    粗层摘要是"全局主题"的可检索入口——用户问"这批文档整体讲了什么"，
    命中 L2/L3 的摘要比逐条翻 L1 更合适。
    """
    communities: list[Community] = []
    budget = max_communities
    # prev_groups 记录上一层各社区的成员集合，用来给当前层算 parent_id
    prev_groups: list[tuple[str, list[str]]] = []
    for level_idx, groups in enumerate(detector.detect_levels(store, tenant_id)):
        if budget <= 0:
            break
        current: list[tuple[str, list[str]]] = []
        for idx, members in enumerate(groups[:budget], start=1):
            cid = f"c-{idx:03d}" if level_idx == 0 else f"c-L{level_idx}-{idx:03d}"
            # parent_id = 上一层里成员重叠最多的那个社区（同 id 对应关系）
            target = set(members)
            parent_id = ""
            if prev_groups:
                parent_id = max(
                    prev_groups,
                    key=lambda item: (len(target & set(item[1])), item[0]),
                )[0]
                # 无重叠说明是孤立社区，不硬挂父节点
                if not (target & set(dict(prev_groups)[parent_id])):
                    parent_id = ""
            community = Community(
                community_id=cid,
                members=list(members),
                size=len(members),
                level=level_idx,
                parent_id=parent_id,
                generated_at=time.time(),
                model_version=model_version,
                tenant_id=tenant_id,
            )
            if len(members) >= min_size and llm is not None:
                content = _community_brief(store, members, tenant_id)
                try:
                    community.summary = llm.generate(
                        COMMUNITY_SUMMARY_PROMPT.format(content=content)
                    ).strip()
                except Exception as exc:  # noqa: BLE001 - 社区 %s 摘要生成失败
                    logger.warning("社区 %s 摘要生成失败: %s", cid, exc)
            store.set_community(community)
            communities.append(community)
            current.append((cid, list(members)))
            budget -= 1
        prev_groups = current
    return communities


def _community_brief(store: BaseGraphStore, members: list[str], tenant_id: str | None = None) -> str:
    member_set = set(members)
    lines = []
    for rel in store.all_relations(tenant_id):
        if rel.source in member_set and rel.target in member_set:
            lines.append(f"{rel.source} —{rel.relation}→ {rel.target}")
    return f"成员：{'、'.join(members[:20])}\n关系：\n" + "\n".join(lines[:30])
