"""租户级长期记忆。

## 为什么需要

项目此前是**完全无状态**的：每一轮问答都从零开始。后果是同一个坑会反复踩：

- 上周问「XX 项目的负责人是谁」答不出来（知识库没有），这周还是答不出来；
- 某个查询串总是召回不到东西，Agent 每隔几次就重试一次同样的无效查询；
- 用户提交过反馈说明「这个问题该看 A 文档」，但系统不记得。

长期记忆把这些"踩过的坑"沉淀下来，注入下一次规划——这是从「能跑」到
「越用越准」的关键一步。

## 设计约束（最重要的一条）

**记忆里不存用户原话。** 原因与审计一致（`audit.py` 只存 question_hash）：
记忆是长期驻留的数据，明文原话会成为一份绕过了审计纪律的"影子副本"。
因此记忆条目只存：
- `kind`：问题类型（由分类器产出，不是原文）
- `key`：归一化后的**实体/话题键**（如文档名、指标名），不是整句话
- `value`：结论（"该看 A 文档"）或无效查询的**指纹**

## 消费方式

记忆在 `think` / `plan` 之前注入提示词，形如：

```
以下是你之前在这个租户处理类似问题时的经验（供参考，不必盲从）：
- 问「项目负责人」这类问题时，知识库通常没有，建议先查组织架构文档
- 查询串 "Q3 营收明细" 从未召回任何结果，不要重复尝试
```

## 存储

JSONL 追加 + 内存索引（记忆条目少、读取频繁、写入偶发）。
**存储是本地 JSONL 文件**（单副本语义；共享状态层已随「多副本共享语义」下线）。
"""
import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

# 记忆类别
KIND_MISSING_DOC = "missing_doc"        # 知识库缺这类内容
KIND_INVALID_QUERY = "invalid_query"    # 该查询串从未召回
KIND_CORRECTION = "correction"          # 用户/人工确认过的正确指向
KIND_PREFERENCE = "preference"          # 答案风格偏好

MAX_ENTRIES_PER_TENANT = 500  # 上限：记忆不能无限增长（否则注入成本失控）
MAX_INJECT = 8                # 单次注入的条数上限


@dataclass
class MemoryEntry:
    tenant_id: str
    kind: str
    key: str          # 归一化键（实体/话题/查询指纹），**不含原话**
    value: str
    hits: int = 0     # 被命中（注入并采纳）的次数，用于排序与淘汰
    created_at: float = field(default_factory=time.time)
    last_used_at: float = 0.0


def query_fingerprint(question: str) -> str:
    """查询指纹：同一问题得到同一指纹，但不可反推原文。"""
    return hashlib.sha256((question or "").strip().lower().encode("utf-8")).hexdigest()[:16]


class LongTermMemory:
    """租户级长期记忆。

    线程安全（内部一把锁）。存储为 JSONL（追加写 + 启动加载），
    条目数有上限，超出时按"命中次数最少且最久未使用"淘汰。
    """

    def __init__(self, path: Path | str | None = None, max_per_tenant: int = MAX_ENTRIES_PER_TENANT) -> None:
        self._path = Path(path) if path else None
        self._max = max(10, max_per_tenant)
        self._lock = threading.Lock()
        self._entries: dict[str, list[MemoryEntry]] = {}
        if self._path and self._path.exists():
            self._load()

    # ---- 持久化 ----

    def _load(self) -> None:
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        payload = json.loads(line)
                    except Exception:  # noqa: BLE001 - 半行跳过
                        logger.debug("记忆条目解析失败：跳过该条")
                        continue
                    entry = MemoryEntry(**payload)
                    self._entries.setdefault(entry.tenant_id, []).append(entry)
            total = sum(len(v) for v in self._entries.values())
            logger.info("长期记忆已加载：%d 条", total)
        except Exception as exc:  # noqa: BLE001 - 长期记忆加载失败
            logger.warning("长期记忆加载失败（从空开始）: %s", exc)

    def _append(self, entry: MemoryEntry) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("长期记忆写入失败（不影响主流程）: %s", exc)

    # ---- 写入 ----

    def remember(self, tenant_id: str, kind: str, key: str, value: str) -> MemoryEntry:
        """记一条经验。同 (tenant, kind, key) 幂等——重复记录只累加计数。

        **key 必须是归一化键，不能是用户原话**（见模块 docstring 的约束）。
        """
        key = (key or "").strip()[:120]
        value = (value or "").strip()[:300]
        with self._lock:
            bucket = self._entries.setdefault(tenant_id, [])
            for e in bucket:
                if e.kind == kind and e.key == key:
                    e.value = value or e.value
                    e.hits += 0
                    return e
            entry = MemoryEntry(tenant_id=tenant_id, kind=kind, key=key, value=value)
            bucket.append(entry)
            self._evict(bucket)
            self._append(entry)
            return entry

    def _evict(self, bucket: list[MemoryEntry]) -> None:
        """超出上限时按「命中少 + 最久未用」淘汰（保留真正有用的经验）。"""
        if len(bucket) <= self._max:
            return
        bucket.sort(key=lambda e: (e.hits, e.last_used_at, e.created_at), reverse=True)
        del bucket[self._max:]

    def forget(self, tenant_id: str, kind: str, key: str) -> bool:
        with self._lock:
            bucket = self._entries.get(tenant_id, [])
            for i, e in enumerate(bucket):
                if e.kind == kind and e.key == key:
                    bucket.pop(i)
                    return True
        return False

    # ---- 读取 ----

    def recall(self, tenant_id: str, kinds: list[str] | None = None,
               limit: int = MAX_INJECT) -> list[MemoryEntry]:
        """取该租户最值得注入的经验（按命中次数与新鲜度排序）。"""
        with self._lock:
            bucket = list(self._entries.get(tenant_id, []))
        if kinds:
            bucket = [e for e in bucket if e.kind in kinds]
        bucket.sort(key=lambda e: (e.hits, e.last_used_at, e.created_at), reverse=True)
        picked = bucket[:limit]
        now = time.time()
        with self._lock:
            for e in picked:
                e.hits += 1
                e.last_used_at = now
        return picked

    def list_entries(self, tenant_id: str) -> list[dict]:
        with self._lock:
            return [asdict(e) for e in self._entries.get(tenant_id, [])]

    def stats(self) -> dict:
        with self._lock:
            return {
                "tenants": len(self._entries),
                "entries": sum(len(v) for v in self._entries.values()),
                "max_per_tenant": self._max,
            }

    # ---- 注入提示词 ----

    @staticmethod
    def render_hint(entries: list[MemoryEntry]) -> str:
        """把记忆渲染成提示词片段。

        措辞刻意用"供参考，不必盲从"——记忆是**经验**不是**事实**：
        知识库更新后旧经验可能已失效，让模型有权忽略它。
        """
        if not entries:
            return ""
        lines = ["以下是你之前在这个租户处理类似问题时的经验（供参考，不必盲从）："]
        for e in entries:
            if e.kind == KIND_INVALID_QUERY:
                lines.append(f"- 查询「{e.key}」此前从未召回结果，不要重复尝试同一查询")
            elif e.kind == KIND_MISSING_DOC:
                lines.append(f"- 关于「{e.key}」的问题知识库通常没有内容，{e.value}")
            elif e.kind == KIND_CORRECTION:
                lines.append(f"- 「{e.key}」的正确指向是：{e.value}")
            else:
                lines.append(f"- 「{e.key}」：{e.value}")
        return "\n".join(lines)


def record_from_run(memory: LongTermMemory, tenant_id: str, state: dict) -> None:
    """从一次运行的结果中提炼经验（ 的写入侧）。

    三条提炼规则：
    1. **判定为拒答** → 记「这类问题知识库答不上来」；
    2. 工具调用了但一条结果都没有 → 记该查询为无效查询；
    3. 其余情况不记（**宁缺毋滥**：垃圾记忆会污染后续规划的提示词）。

    ⚠️ 规则 1 **不能**附加"证据为空"的条件。曾这样写过，结果是记忆永远为空：
    真实语义里"拒答"来自**反思判定证据不相关**，而不是"没检索到东西"——
    检索通常会返回若干条（向量检索几乎总能返回 top_k 条），只是都不顶用。
    因此 `refusal=True` 且 `evidence` 非空是**最常见**的拒答形态，
    加上 `not evidence` 反而把主路径排除掉了（该 bug 由线上探针 entries=0 发现）。
    """
    try:
        calls = state.get("tool_calls") or []
        question = state.get("question") or ""

        # 规则 2：零召回的工具调用（工具正常返回但一条都没有）
        for call in calls:
            if call.get("result_count") == 0 and not call.get("error"):
                key = str(call.get("arguments", {}).get("query") or call.get("query") or "#")[:80]
                if key and key != "#":
                    memory.remember(
                        tenant_id, KIND_INVALID_QUERY, key,
                        "该查询未召回任何内容",
                    )

        # 规则 1：拒答（无论是否召回到不相关的候选）
        if state.get("refusal") and question:
            memory.remember(
                tenant_id, KIND_MISSING_DOC, _topic_key(question),
                "建议先确认知识库是否已收录相关文档",
            )
    except Exception as exc:  # noqa: BLE001 - 记忆写入绝不能影响主流程
        logger.debug("长期记忆提炼失败（忽略）: %s", exc)


def _topic_key(question: str) -> str:
    """从问题提取话题键。

    刻意**不存原话**：只在短问题（≤24 字，通常是实体/指标名）时原样保留，
    长问题退化为指纹——避免把用户的长句描述写进长期存储。
    """
    q = (question or "").strip()
    if 0 < len(q) <= 24:
        return q
    return f"指纹:{query_fingerprint(q)}"
