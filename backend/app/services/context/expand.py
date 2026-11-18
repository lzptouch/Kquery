"""父块展开（`ST-03`）：把命中子块展开为父块全文。

## 为什么抽出来

父块展开此前**内联在 `services/pipeline.py`**（线性路径），Agent 路径没有——
于是两条路径对「同一份证据」的呈现**不一致**：线性路径给父块全文，Agent 只见子块碎片，
长文档上下文会断章。

抽到这里后，两条路径共用同一套语义。

## 关键纪律：去重键语义**不变**

去重键 = **父块 ID**（`parent:{parent_id}`），不是 chunk_id：
同一父块的多个子块命中只保留**分数最高**的那次（先出现的那次）。
改动这个语义会**改变线上召回行为**（多召回或少召回父块），所以保持原样。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ExpandItem:
    """展开后的一条证据。"""

    key: str            # 去重键（`parent:{id}` 或 chunk_id）
    chunk_id: str
    doc_id: str
    content: str        # 父块全文（无父块时为子块内容）
    parent_id: str = ""
    expanded: bool = False  # 是否真的展开了父块


def expand_parents(
    items: list[dict],
    parent_lookup,
) -> tuple[list[ExpandItem], list[dict]]:
    """把命中子块展开为父块；返回（保留项，被丢弃项）。

    `items`：每项至少含 `chunk_id` / `doc_id` / `content` / `parent_id` / `score`。
    `parent_lookup(parent_id) -> 父块对象 | None`：取父块的回调（可注入，便于测试）。

    **丢弃项带原因**（`duplicate_parent`）：同一父块的多次命中只留一条，
    丢弃的那些要能解释「为什么没进上下文」（前端要展示）。
    """
    kept: list[ExpandItem] = []
    dropped: list[dict] = []
    seen: dict[str, ExpandItem] = {}

    for item in items:
        chunk_id = str(item.get("chunk_id") or "")
        doc_id = str(item.get("doc_id") or "")
        content = str(item.get("content") or "")
        parent_id = str(item.get("parent_id") or "")
        dedupe_key = chunk_id
        expanded = False
        if parent_id:
            parent = None
            try:
                parent = parent_lookup(parent_id)
            except Exception:  # noqa: BLE001 - 取父块失败不该让整条证据失效
                parent = None
            if parent is not None:
                content = str(getattr(parent, "content", "") or content)
                dedupe_key = f"parent:{parent_id}"
                expanded = True
        if dedupe_key in seen:
            dropped.append({
                "key": chunk_id,
                "reason": f"duplicate_parent（与 {seen[dedupe_key].chunk_id} 同父块，保留分数更高的一条）",
            })
            continue
        entry = ExpandItem(
            key=dedupe_key,
            chunk_id=chunk_id,
            doc_id=doc_id,
            content=content,
            parent_id=parent_id,
            expanded=expanded,
        )
        seen[dedupe_key] = entry
        kept.append(entry)
    return kept, dropped
