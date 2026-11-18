"""检索过滤条件（租户 / 单文档）。

## 认证与授权已下线（2026-11-12）

本模块原先承载**文档级 ACL 下推**：调用方持有"可见文档清单"时，把它编成
`doc_id in [...]` 下推到 Milvus，让向量库在召回阶段就排除不可见文档。

该能力随文档级 ACL 一并删除。保留下来的是：

- `tenant_id` —— **数据隔离维度**（不是权限功能）：文档、索引、图谱、记忆、
  缓存键都按租户划分；
- `doc_id` —— 单文档检索（快问通道、文档内 probe）。

## 一道防线：动态超额取数（**目前只有 FAISS 后端实现了**）

三个检索后端都是**先按分数取 N 条，再在应用层过滤 tenant/doc**。这个顺序会出问题：

```
租户内 10 份文档中的 3 份被 tenant/doc 过滤掉，且恰好排在最前
→ 取 top-4 → 命中 3 条被过滤 → 只剩 1 条
```

因此不再固定 `top_k * 4`，而是根据**实际过滤结果**迭代扩取：过滤后不足 `top_k`
就扩大候选池重取，直到取满或候选耗尽。这对三个后端是统一逻辑，且**不需要预先知道
任何文档集合**。

**降级纪律**：达到 `MAX_OVERFETCH_FACTOR` 上限后不再扩取，宁可少给几条也不做全量扫描。
"""
from dataclasses import dataclass, field

# 超额取数的上限倍数：避免极端情况下把整个索引拉进内存。
# 达到该倍数后不再扩取，宁可少给几条也不做全量扫描。
MAX_OVERFETCH_FACTOR = 32


@dataclass
class RetrievalFilters:
    """一次检索的过滤条件。"""

    tenant_id: str = ""
    doc_id: str | None = None          # 单文档检索（文档内 probe / 快问）
    # 注：版本过滤不在这里——它由 `hybrid.py` 的 `is_current_version` 判定驱动
    # （`A-21.2` 曾在这里放一个 `current_version_only` 字段，全仓无读取点，
    #   留着只会让人以为「这里能控制版本过滤」）。

    def allows(self, doc_id: str, tenant_id: str) -> bool:
        """应用层判定（过滤谓词）。"""
        if tenant_id != self.tenant_id:
            return False
        if self.doc_id is not None and doc_id != self.doc_id:
            return False
        return True

    def to_milvus_expr(self, extra: list[str] | None = None) -> str:
        """编 Milvus 过滤表达式。

        只下推租户与单文档两个维度——文档级可见性下推已随 ACL 删除。
        """
        parts = [f'tenant_id == "{_escape(self.tenant_id)}"']
        if self.doc_id is not None:
            parts.append(f'doc_id == "{_escape(self.doc_id)}"')
        parts.extend(extra or [])
        return " and ".join(parts)


def _escape(value: str) -> str:
    """表达式字符串转义：doc_id 理论上由系统生成，但不做转义等于把注入面留在原地。"""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


@dataclass
class OverfetchState:
    """迭代扩取的进度（供后端在循环中复用）。"""

    factor: int = 4
    fetched: int = 0
    rounds: int = 0
    exhausted: bool = False
    history: list[int] = field(default_factory=list)

    def next_limit(self, top_k: int) -> int:
        self.factor = min(self.factor * 2, MAX_OVERFETCH_FACTOR)
        self.rounds += 1
        return top_k * self.factor

    def can_continue(self, wanted: int, got: int, total: int) -> bool:
        """还应继续扩取吗？

        三个终止条件：已取满 / 候选已耗尽 / 达到最大倍数。
        `exhausted` 表示"确实没有更多候选了"，据此可区分"取不到"与"本来就少"。
        """
        if got >= wanted:
            return False
        if self.factor >= MAX_OVERFETCH_FACTOR:
            return False
        if total and self.fetched >= total:
            self.exhausted = True
            return False
        return True
