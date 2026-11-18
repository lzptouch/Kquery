"""**外置状态层**：把进程内状态搬到共享存储，使多副本语义成立（批次 4）。

**要解决的问题**：会话 / 检索配置 / 入库任务三类状态此前都在**进程内**
（JSON 文件 + 内存字典）。多副本部署时会出现：
- 用户在副本 A 建的会话，切到副本 B 就"不存在"；
- 灰度作用域配置写在副本 A 的磁盘上，副本 B 看不到 → 灰度"时灵时不灵"；
- 入库任务状态在副本 A，副本 B 的任务列表永远为空。

**设计**（刻意保持简单，可被审查）：

1. **文档 + 索引** 双键模型
   - 文档：`{prefix}:{id}` → JSON 字符串（`set` / `get` / `delete`）
   - 索引：zset（member = 文档 id，score = 时间戳）→ 按时间排序列举
   选 zset 而不是 SCAN：SCAN 在大 keyspace 上不保证一次拿全，
   而"最近 N 个会话"正是 zset 的强项（O(logN+M)，且排序免费）。

2. **单写者纪律 + 乐观并发**
   读-改-写在 Redis 下**不是原子的**。这里用**版本号检测冲突**：
   写入时带上读到的 `version`，若落盘前发现对方的版本更大 → 放弃本次写并让调用方重试。
   **诚实边界**：这不是 CAS（无 Lua/MULTI 就做不到原子 CAS），
   极端竞态下仍可能丢一次更新。宁可承认这一点，也不要假装是强一致。

3. **降级纪律**
   Redis 不可用 → 自动回落到进程内（`SharedState` 的既有 Fail-Open 纪律），
   并由 `degraded()` 暴露状态：**多副本悄悄退化成单副本语义必须可观测**，
   否则"限流额度翻倍"这类问题会变成玄学。
"""
from __future__ import annotations

import json
import time

from app.core.shared_state import SharedState
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

#: 版本号字段名（放在文档顶层）
VERSION_FIELD = "_v"


class ExternalDocStore:
    """共享文档存储（JSON 文档 + zset 索引）。

    用法：
        docs = ExternalDocStore(state, prefix="session")
        docs.put("abc", {"title": "…"}, index="sessions:t1:u1", score=now)
        doc = docs.get("abc")
        ids = docs.list_ids("sessions:t1:u1")
    """

    def __init__(self, state: SharedState, prefix: str) -> None:
        self._state = state
        self._prefix = prefix.rstrip(":")

    # ---- 键名 ----

    def doc_key(self, doc_id: str) -> str:
        return f"{self._prefix}:{doc_id}"

    def index_key(self, index: str) -> str:
        return f"{self._prefix}:idx:{index}"

    # ---- 文档 ----

    def get(self, doc_id: str) -> dict | None:
        raw = self._state.get(self.doc_key(doc_id))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            # 损坏的文档按"不存在"处理：业务层会走 404，而不是整站 500
            logger.error("共享状态文档损坏（按不存在处理）: %s", doc_id)
            return None
        return data if isinstance(data, dict) else None

    def put(
        self,
        doc_id: str,
        doc: dict,
        index: str | None = None,
        score: float | None = None,
        ttl_s: int = 0,
        expect_version: int | None = None,
    ) -> bool:
        """写入文档（可选登记进索引）。

        `expect_version` 不为 None 时做**乐观并发检查**：
        共享状态里的版本比它大 → 说明别人写过了 → 本次**放弃**（返回 False），
        让调用方重新读后重试。版本相等或更大（自己读到的）才写入。
        """
        current = self.get(doc_id)
        current_version = int((current or {}).get(VERSION_FIELD, 0) or 0)
        if expect_version is not None and current_version > int(expect_version):
            return False
        payload = dict(doc)
        payload[VERSION_FIELD] = current_version + 1
        payload.setdefault("updated_at", time.time())
        self._state.set(
            self.doc_key(doc_id),
            json.dumps(payload, ensure_ascii=False, default=str),
            ttl_s=ttl_s,
        )
        if index:
            self._state.zadd(
                self.index_key(index), doc_id, float(score if score is not None else time.time())
            )
        return True

    def delete(self, doc_id: str, index: str | None = None) -> None:
        self._state.delete(self.doc_key(doc_id))
        if index:
            self._state.zrem(self.index_key(index), doc_id)

    # ---- 索引 ----

    def list_ids(self, index: str, limit: int = 200, newest_first: bool = True) -> list[str]:
        """列出索引里的文档 id（按时间排序）。"""
        ids = self._state.zrange(self.index_key(index), 0.0, float("inf"))
        if newest_first:
            ids = list(reversed(ids))
        return ids[:limit]

    def list_docs(self, index: str, limit: int = 200, newest_first: bool = True) -> list[dict]:
        """列出索引里的文档（缺失的自动跳过：索引可能比文档活得久）。"""
        out: list[dict] = []
        for doc_id in self.list_ids(index, limit=limit, newest_first=newest_first):
            doc = self.get(doc_id)
            if doc is not None:
                out.append(doc)
        return out

    # ---- 状态 ----

    def backend(self) -> str:
        return self._state.backend

    def degraded(self) -> bool:
        """是否已降级到进程内（多副本语义**已失效**，必须可观测）。"""
        return self._state.backend != "redis"

    def version_of(self, doc_id: str) -> int:
        doc = self.get(doc_id) or {}
        return int(doc.get(VERSION_FIELD, 0) or 0)
