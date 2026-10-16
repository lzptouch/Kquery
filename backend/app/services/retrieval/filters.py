"""检索过滤条件与 ACL 下推（TASK-142）。

## 要解决的问题（DEF-R-6：后过滤吃掉 top-k 名额）

三个检索后端都是**先按分数取 N 条，再在应用层过滤 tenant/doc**。加入 ACL 后
这个顺序会出问题：

```
用户可读 10 份文档中的 7 份，不可读的 3 份恰好排在最前
→ 取 top-4 → 命中 3 条不可读被过滤 → 只剩 1 条
→ 极端情况下可见证据为 0 → 误判为「无权限」直接拒答
```

用户明明有权看那 7 份文档，却因为"排名更高的不可见文档占了名额"而拿不到答案。

## 两道防线

**第一道（本模块核心）：动态超额取数。**
不再固定 `top_k * 4`，而是根据**实际过滤结果**迭代扩取：过滤后不足 `top_k`
就扩大候选池重取，直到取满或候选耗尽。这对三个后端是统一逻辑，且**不需要
预先知道可见文档集合**（那个信息在多租户下代价高昂）。

**第二道：表达式下推（Milvus 专属）。**
当调用方**已经**持有可见文档清单（`visible_doc_ids`）时，把它编成
`doc_id in [...]` 下推到 Milvus，让向量库在召回阶段就排除不可见文档——
这样即使可见文档排名很靠后也能被取到。

**降级纪律**：可见清单 > `PUSHDOWN_MAX_DOCS` 时不构造 `in [...]`（Milvus 表达式
有长度上限，超长会直接报错），退回只下推 tenant + 第一道防线。
"""
from dataclasses import dataclass, field

# Milvus 表达式长度上限约 64KB；单个 doc_id 按 40 字符估算，
# 取 500 作为保守阈值（约 20KB），留出余量给 tenant/版本等其它条件。
PUSHDOWN_MAX_DOCS = 500

# 超额取数的上限倍数：避免极端情况下把整个索引拉进内存。
# 达到该倍数后不再扩取，宁可少给几条也不做全量扫描。
MAX_OVERFETCH_FACTOR = 32


@dataclass
class RetrievalFilters:
    """一次检索的过滤条件。

    `visible_doc_ids` 是**允许清单**（None = 不做文档级限制，例如 ACL 未启用）。
    刻意只支持 allow 语义：deny 与多身份判定必须留在应用层（Milvus 表达式
    无法优雅表达"不含"的多身份语义），见 acl.py 的判定树。
    """

    tenant_id: str = ""
    doc_id: str | None = None          # 单文档检索（文档内 probe）
    visible_doc_ids: set[str] | None = None  # ACL 允许清单（None = 不限制）
    current_version_only: bool = True   # 只召回当前索引版本（P0-7）

    def allows(self, doc_id: str, tenant_id: str) -> bool:
        """应用层判定（第一道防线的过滤谓词）。"""
        if tenant_id != self.tenant_id:
            return False
        if self.doc_id is not None and doc_id != self.doc_id:
            return False
        if self.visible_doc_ids is not None and doc_id not in self.visible_doc_ids:
            return False
        return True

    def overfetch_limit(self, top_k: int, factor: int) -> int:
        """本轮应取多少候选（含 tenant/doc/ACL 过滤的余量）。"""
        return max(top_k, top_k * max(1, min(factor, MAX_OVERFETCH_FACTOR)))

    def to_milvus_expr(self, extra: list[str] | None = None) -> str:
        """编 Milvus 过滤表达式（第二道防线）。

        可见清单过大时**不**加入 `doc_id in [...]`：超长表达式会让 Milvus 直接
        报错（比"少过滤一点"严重得多），此时退回 tenant 粗过滤 + 应用层精确过滤。
        """
        parts = [f'tenant_id == "{_escape(self.tenant_id)}"']
        if self.doc_id is not None:
            parts.append(f'doc_id == "{_escape(self.doc_id)}"')
        elif self.visible_doc_ids is not None and len(self.visible_doc_ids) <= PUSHDOWN_MAX_DOCS:
            if not self.visible_doc_ids:
                # 空允许清单：没有任何可见文档，构造一个恒假条件（比不过滤安全）
                parts.append('doc_id == "__none__"')
            else:
                ids = ", ".join(f'"{_escape(d)}"' for d in sorted(self.visible_doc_ids))
                parts.append(f"doc_id in [{ids}]")
        parts.extend(extra or [])
        return " and ".join(parts)

    def pushdown_degraded(self) -> bool:
        """是否因清单过大而放弃了 doc 级下推（供日志与指标观测）。"""
        return (
            self.doc_id is None
            and self.visible_doc_ids is not None
            and len(self.visible_doc_ids) > PUSHDOWN_MAX_DOCS
        )


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
