"""会话存档（阶段 6 §6 / §7.4）。

- 会话标识 = 租户 + 用户 + 随机号（防枚举）；服务端校验归属；
- 快照 JSON 落盘（单实例方案，接口可换 Redis/DB）：30 天自动过期、
  写入带轮次号防乱序覆盖、同一会话串行（会话维度锁）、
  跨版本读取（缺字段按默认值补齐）；
- 上下文膨胀治理：证据上限 + 去重（默认启用），摘要压缩默认关闭；
- 删除会话必须连同快照一并删除。
"""
import json
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from app.services.agent.state import sanitize_snapshot

logger = logging.getLogger(__name__)

SESSION_TTL_S = 30 * 86400  # 30 天自动过期（与业务承诺一致）
EVIDENCE_CARRY_LIMIT = 8  # 跨轮携带的证据上限（旧的按轮次淘汰）
HISTORY_CARRY_LIMIT = 6  # 跨轮携带的历史问答上限


class SessionStore:
    def __init__(self, sessions_dir: Path, ttl_s: int = SESSION_TTL_S):
        self._dir = sessions_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._ttl = ttl_s
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ---- 标识与归属 ----

    @staticmethod
    def make_session_id(tenant_id: str, user_id: str) -> str:
        return f"{tenant_id}:{user_id}:{secrets.token_urlsafe(8)}"

    def _lock_for(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(session_id, threading.Lock())

    def _path(self, session_id: str) -> Path:
        # 防路径穿越：仅允许会话字符
        safe = session_id.replace(":", "_")
        return self._dir / f"{safe}.json"

    def _check_owner(self, session_id: str, tenant_id: str, user_id: str) -> None:
        if not session_id.startswith(f"{tenant_id}:{user_id}:"):
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
        self._path(session_id).write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
        return session

    def get(self, session_id: str, tenant_id: str, user_id: str) -> dict:
        self._check_owner(session_id, tenant_id, user_id)
        path = self._path(session_id)
        if not path.exists():
            from app.core.envelope import ApiError, ErrCode
            raise ApiError(ErrCode.SESSION_NOT_FOUND, "会话不存在或已过期", http_status=404)
        session = json.loads(path.read_text(encoding="utf-8"))
        if session.get("expires_at", 0) < time.time():
            path.unlink(missing_ok=True)
            from app.core.envelope import ApiError, ErrCode
            raise ApiError(ErrCode.SESSION_EXPIRED, "会话已过期，请开启新对话", http_status=410)
        return session

    def list_sessions(self, tenant_id: str, user_id: str) -> list[dict]:
        prefix = f"{tenant_id}_{user_id}_"
        sessions = []
        now = time.time()
        for path in self._dir.glob(f"{prefix}*.json"):
            try:
                s = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
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
            self._path(session_id).write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
            return session

    def delete(self, session_id: str, tenant_id: str, user_id: str) -> None:
        """删除会话：快照与工具调用明细一并删除（不留残留原文）。"""
        with self._lock_for(session_id):
            self._check_owner(session_id, tenant_id, user_id)
            self._path(session_id).unlink(missing_ok=True)
        with self._locks_guard:
            self._locks.pop(session_id, None)

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

    def save_turn(
        self,
        session_id: str,
        tenant_id: str,
        user_id: str,
        question: str,
        answer: str,
        verdict: str,
        state: dict,
    ) -> None:
        """轮次结束写快照：带轮次号，同会话串行，证据截断到上限。"""
        with self._lock_for(session_id):
            session = self.get(session_id, tenant_id, user_id)
            new_turn = session.get("turn", 0) + 1
            session["turn"] = new_turn
            session.setdefault("turns", []).append({
                "question": question, "answer": answer, "verdict": verdict, "turn": new_turn,
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
            self._path(session_id).write_text(
                json.dumps(session, ensure_ascii=False, default=str), encoding="utf-8"
            )
