"""会话存档。

- 会话标识 = 租户 + 用户 + 随机号（防枚举）；服务端校验归属；
- 快照 JSON 落盘（单实例方案，接口可换 Redis/DB）：30 天自动过期、
  写入带轮次号防乱序覆盖、同一会话串行（会话维度锁）、
  跨版本读取（缺字段按默认值补齐）；
- 上下文膨胀治理：证据上限 + 去重（默认启用）；**历史摘要压缩**由
  `services/context/history.py` 实现（`ST-02`），开关见 `history_compaction_enabled`
  （默认开启）——此处只负责**轮次条数上限**，内容压缩在压缩器里做；
- 删除会话必须连同快照一并删除。
"""
import json
import os
import secrets
import threading
import time
from pathlib import Path

from app.services.agent.state import sanitize_snapshot
from app.services.external_state import ExternalDocStore
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

SESSION_TTL_S = 30 * 86400  # 30 天自动过期（与业务承诺一致）
EVIDENCE_CARRY_LIMIT = 8  # 跨轮携带的证据上限（旧的按轮次淘汰）
HISTORY_CARRY_LIMIT = 6  # 跨轮携带的历史问答上限
# 会话文件里保留的轮次上限。此前 `turns` 是**无上限 append**，而每轮都带完整
# 问答原文（长答案 2~8KB）→ 100 轮的会话快照可达数百 KB，且每次 save_turn 都要
# 全量重写这个 JSON（写放大 O(轮数)）。跨轮上下文只取最近 `HISTORY_CARRY_LIMIT`
# 轮，更早的内容由 `services/context/history.py` 摘要，所以这里留 40 轮足够
# 排障与前端翻页，超出部分丢弃并记数（`trimmed_turns`）——**不静默丢**。
MAX_TURNS_PER_SESSION = 40
# 会话锁的空闲回收窗口：会话 30 天过期，但锁只是串行化手段；
# 空闲 1 小时且无人持有即可回收，否则 `_locks` 随会话数无界增长（KQ-23）。
LOCK_IDLE_S = 3600
LOCK_PRUNE_EVERY = 256  # 每 N 次取锁清一次，避免每请求 O(n) 扫描


class SessionStore:
    """会话存档。

    `docs` 不为空时走**外置**路径（批次 4）：会话文档与索引存在共享状态里，
    多副本共享同一份会话（此前是本地 JSON 文件 → 切副本就"会话不见了"）。
    未配置 Redis 时仍走本地文件（单副本语义），行为与从前完全一致。
    """

    def __init__(
        self,
        sessions_dir: Path,
        ttl_s: int = SESSION_TTL_S,
        docs: "ExternalDocStore | None" = None,
    ) -> None:
        self._dir = sessions_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._ttl = ttl_s
        self._docs = docs
        self._locks: dict[str, threading.Lock] = {}
        # 每个锁最后一次被取用的时刻，供空闲回收判断（键 = session_id）
        self._locks_used_at: dict[str, float] = {}
        self._lock_calls = 0
        self._locks_guard = threading.Lock()

    # ---- 外置路径的键 ----

    @staticmethod
    def _index_key(tenant_id: str, user_id: str) -> str:
        return f"{tenant_id}:{user_id}"

    def _external(self) -> bool:
        return self._docs is not None

    # ---- 标识与归属 ----

    @staticmethod
    def make_session_id(tenant_id: str, user_id: str) -> str:
        return f"{tenant_id}:{user_id}:{secrets.token_urlsafe(8)}"

    def _lock_for(self, session_id: str) -> threading.Lock:
        now = time.time()
        with self._locks_guard:
            # 先记「刚被取用」再返回：并发回收线程只有看到这个时刻之后
            # 才可能判定空闲，否则刚取出的锁会被判 idle 并删掉 → 同一会话
            # 出现两把锁 = 串行化失效（比不回收更糟）
            self._locks_used_at[session_id] = now
            self._maybe_prune_locks(now)
            return self._locks.setdefault(session_id, threading.Lock())

    def _maybe_prune_locks(self, now: float) -> None:
        """回收空闲会话锁（调用方必须已持有 `_locks_guard`）。

        两个条件同时成立才删：**无人持有**（`lock.locked()` 为假——有线程在等或
        持有就绝不能动）且空闲超过 `LOCK_IDLE_S`。`delete()` 走的是立即回收，
        这里只兜「会话长期不再被访问但也没被删除」的那部分。
        """
        self._lock_calls += 1
        if self._lock_calls % LOCK_PRUNE_EVERY:
            return
        for sid, lock in list(self._locks.items()):
            if lock.locked():
                continue
            if now - self._locks_used_at.get(sid, 0.0) > LOCK_IDLE_S:
                self._locks.pop(sid, None)
                self._locks_used_at.pop(sid, None)

    def _path(self, session_id: str) -> Path:
        # 防路径穿越：仅允许会话字符
        safe = session_id.replace(":", "_")
        return self._dir / f"{safe}.json"

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        """原子写：先写临时文件再 os.replace。

        此前直接 write_text 覆盖，多 worker / 并发下会产生半截 JSON，
        而 get() 读到半截即抛 JSONDecodeError → 整个会话 500 且无法自愈。
        os.replace 在同一文件系统上是原子操作，读者要么看到旧内容要么看到新内容。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def _check_owner(self, session_id: str, tenant_id: str, user_id: str) -> None:
        if not session_id.startswith(f"{tenant_id}:{user_id}:"):
            # 按需载入：只在错误路径构造信封
            from app.core.envelope import ApiError, ErrCode
            raise ApiError(ErrCode.SESSION_NOT_FOUND, "会话不存在或不属于当前用户", http_status=404)

    # ---- CRUD ----

    def create(self, tenant_id: str, user_id: str, title: str = "新对话") -> dict:
        session_id = self.make_session_id(tenant_id, user_id)
        now = time.time()
        session = {
            "session_id": session_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "title": title,
            "turn": 0,
            "turns": [],  # [{question, answer, verdict}]
            "snapshot": None,  # 最近一轮结束时的状态快照（跨轮证据累积的载体）
            "created_at": now,
            "updated_at": now,
            "expires_at": now + self._ttl,
        }
        if self._external():
            assert self._docs is not None
            self._docs.put(
                session_id,
                session,
                index=self._index_key(tenant_id, user_id),
                score=now,
                ttl_s=self._ttl,
            )
            return session
        self._atomic_write(self._path(session_id), json.dumps(session, ensure_ascii=False))
        return session

    def get(self, session_id: str, tenant_id: str, user_id: str) -> dict:
        self._check_owner(session_id, tenant_id, user_id)
        if self._external():
            assert self._docs is not None
            session = self._docs.get(session_id)
            if session is None:
                # 按需载入：只在错误路径构造信封
                from app.core.envelope import ApiError, ErrCode
                raise ApiError(ErrCode.SESSION_NOT_FOUND, "会话不存在或已过期", http_status=404)
            if session.get("expires_at", 0) < time.time():
                self._docs.delete(
                    session_id, index=self._index_key(tenant_id, user_id)
                )
                # 按需载入：只在错误路径构造信封
                from app.core.envelope import ApiError, ErrCode
                raise ApiError(ErrCode.SESSION_EXPIRED, "会话已过期，请开启新对话", http_status=410)
            return session
        path = self._path(session_id)
        if not path.exists():
            # 按需载入：只在错误路径构造信封
            from app.core.envelope import ApiError, ErrCode
            raise ApiError(ErrCode.SESSION_NOT_FOUND, "会话不存在或已过期", http_status=404)
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # 损坏的会话文件此前直接抛 JSONDecodeError → 500。
            # 数据已不可用，按"不存在"处理并隔离坏文件，比整站 500 更合理。
            logger.error("会话文件损坏，已隔离: %s (%s)", path, exc)
            try:
                path.replace(path.with_suffix(f".corrupt-{int(time.time())}.json"))
            except OSError:
                path.unlink(missing_ok=True)
            # 按需载入：只在错误路径构造信封
            from app.core.envelope import ApiError, ErrCode
            raise ApiError(ErrCode.SESSION_NOT_FOUND, "会话数据已损坏，请开启新对话", http_status=404)
        if session.get("expires_at", 0) < time.time():
            path.unlink(missing_ok=True)
            # 按需载入：只在错误路径构造信封
            from app.core.envelope import ApiError, ErrCode
            raise ApiError(ErrCode.SESSION_EXPIRED, "会话已过期，请开启新对话", http_status=410)
        return session

    def list_sessions(self, tenant_id: str, user_id: str) -> list[dict]:
        if self._external():
            assert self._docs is not None
            now = time.time()
            out: list[dict] = []
            for s in self._docs.list_docs(self._index_key(tenant_id, user_id)):
                if s.get("expires_at", 0) < now:
                    continue  # 过期会话惰性清理（文档由 TTL 兜底）
                out.append({
                    "session_id": s.get("session_id", ""),
                    "title": s.get("title", "新对话"),
                    "message_count": len(s.get("turns", [])) * 2,
                    "updated_at": s.get("updated_at", 0),
                })
            out.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
            return out
        prefix = f"{tenant_id}_{user_id}_"
        sessions = []
        now = time.time()
        for path in self._dir.glob(f"{prefix}*.json"):
            try:
                s = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - 会话条目解析失败：跳过该条
                logger.debug("会话条目解析失败：跳过该条")
                continue
            if s.get("expires_at", 0) < now:
                path.unlink(missing_ok=True)  # 惰性清理过期快照
                continue
            sessions.append({
                "session_id": s["session_id"],
                "title": s.get("title", "新对话"),
                "message_count": len(s.get("turns", [])) * 2,
                "updated_at": s.get("updated_at", 0),
            })
        sessions.sort(key=lambda s: s["updated_at"], reverse=True)
        return sessions

    def rename(self, session_id: str, tenant_id: str, user_id: str, title: str) -> dict:
        with self._lock_for(session_id):
            session = self.get(session_id, tenant_id, user_id)
            session["title"] = title.strip()[:50]
            session["updated_at"] = time.time()
            if self._external():
                assert self._docs is not None
                self._docs.put(
                    session_id,
                    session,
                    index=self._index_key(tenant_id, user_id),
                    score=session["updated_at"],
                    ttl_s=self._ttl,
                    expect_version=self._docs.version_of(session_id) - 1,
                )
                return session
            self._atomic_write(self._path(session_id), json.dumps(session, ensure_ascii=False))
            return session

    def delete(self, session_id: str, tenant_id: str, user_id: str) -> None:
        """删除会话：快照与工具调用明细一并删除（不留残留原文）。"""
        with self._lock_for(session_id):
            self._check_owner(session_id, tenant_id, user_id)
            if self._external():
                assert self._docs is not None
                self._docs.delete(session_id, index=self._index_key(tenant_id, user_id))
            else:
                self._path(session_id).unlink(missing_ok=True)
        with self._locks_guard:
            self._locks.pop(session_id, None)
            self._locks_used_at.pop(session_id, None)

    # ---- 快照读写（同一会话串行 + 轮次号防乱序）----

    def load_context(self, session_id: str, tenant_id: str, user_id: str) -> dict:
        """加载跨轮上下文：历史问答 + 上轮证据（ capped & deduped ）。"""
        session = self.get(session_id, tenant_id, user_id)
        snapshot = sanitize_snapshot(session.get("snapshot") or {})
        return {
            "history": session.get("turns", [])[-HISTORY_CARRY_LIMIT:],
            "carried_evidence": snapshot.get("evidence", [])[-EVIDENCE_CARRY_LIMIT:],
            "turn": session.get("turn", 0),
        }

    @staticmethod
    def _append_turn(session: dict, entry: dict) -> None:
        """追加一轮并裁剪到 `MAX_TURNS_PER_SESSION`（就地修改）。

        裁剪时把丢弃条数累加进 `trimmed_turns`：会话文件里"只剩 40 轮"必须能
        与"数据丢了"区分开，否则排障时会误判成写入丢轮。
        """
        turns = session.setdefault("turns", [])
        turns.append(entry)
        overflow = len(turns) - MAX_TURNS_PER_SESSION
        if overflow > 0:
            del turns[:overflow]
            session["trimmed_turns"] = int(session.get("trimmed_turns", 0)) + overflow

    def save_turn(
        self,
        session_id: str,
        tenant_id: str,
        user_id: str,
        question: str,
        answer: str,
        verdict: str,
        state: dict,
        answer_source: str = "",
        trace_id: str = "",
    ) -> None:
        """轮次结束写快照：带轮次号，同会话串行，证据截断到上限。

        `trace_id` 一并落盘：恢复历史会话时前端要能重新打开「查看成本 / 复制
        调用链」入口——此前 `evidence: []` + 无 `trace_id`，历史消息上的引用角标
        与 trace 入口全是死的（`X-22` 残余）。
        """
        with self._lock_for(session_id):
            session = self.get(session_id, tenant_id, user_id)
            new_turn = session.get("turn", 0) + 1
            session["turn"] = new_turn
            self._append_turn(session, {
                "question": question, "answer": answer, "verdict": verdict, "turn": new_turn,
                "answer_source": answer_source, "trace_id": trace_id,
            })
            snapshot = dict(state)
            snapshot["evidence"] = state.get("evidence", [])[-EVIDENCE_CARRY_LIMIT:]
            snapshot["turn"] = new_turn
            session["snapshot"] = sanitize_snapshot(snapshot)
            session["updated_at"] = time.time()
            session["expires_at"] = time.time() + self._ttl
            # 首问生成会话标题
            if session.get("title") == "新对话" and question:
                session["title"] = question[:24]
            if self._external():
                assert self._docs is not None
                # 乐观并发：读到的版本若已被别人推进 → 本次放弃并重试一次。
                # 会话是单用户串行使用的，冲突极罕见；但不检查就可能丢一轮对话。
                for _attempt in range(3):
                    version = self._docs.version_of(session_id)
                    if self._docs.put(
                        session_id,
                        session,
                        index=self._index_key(tenant_id, user_id),
                        score=session["updated_at"],
                        ttl_s=self._ttl,
                        expect_version=version,
                    ):
                        break
                    session = self.get(session_id, tenant_id, user_id)
                    new_turn = session.get("turn", 0) + 1
                    session["turn"] = new_turn
                    self._append_turn(session, {
                        "question": question, "answer": answer,
                        "verdict": verdict, "turn": new_turn,
                        "answer_source": answer_source, "trace_id": trace_id,
                    })
                    snapshot = dict(state)
                    snapshot["evidence"] = state.get("evidence", [])[-EVIDENCE_CARRY_LIMIT:]
                    snapshot["turn"] = new_turn
                    session["snapshot"] = sanitize_snapshot(snapshot)
                    session["updated_at"] = time.time()
                    session["expires_at"] = time.time() + self._ttl
                    if session.get("title") == "新对话" and question:
                        session["title"] = question[:24]
                return
            self._atomic_write(
                self._path(session_id),
                json.dumps(session, ensure_ascii=False, default=str),
            )
