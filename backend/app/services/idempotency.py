"""幂等：在飞去重 + 结果重放。

**为什么需要**：前端在 429/503 后会自动重试一次（`frontend/src/lib/api-client.ts`），
而一次问答要真金白银地调用模型与检索——**同一个问题被算两次钱**，
用户还可能看到两份不一致的答案。这类损耗在监控上完全不可见（两次都是"成功"），
只能靠幂等在源头挡住。

两类幂等，语义不同，别混用：

1. **在飞去重**（按问题内容）：只允许一个在飞，其余立刻返回 409 + Retry-After。
   不排队、不等待——排队会把"重试"变成"再等一轮"，反而更贵更慢。
   执行结束（成功或失败）立即释放占位，所以**不影响用户稍后问同一个问题**。
2. **结果重放**（`Idempotency-Key` 头）：同 key 的成功结果直接回放，零调用零成本，
   保留 24 小时。这是"同一次提交"的语义，调用方自己决定 key。

存储复用 `SharedState`（Redis / 进程内）——它已经在项目里承担限流与缓存的共享语义：
- Redis 可用 → 多副本共享（跨副本也不会重复计费）；
- Redis 不可用 → 降级到进程内，此时**只在单副本内有效**，
  `degraded` 必须能被观测到，否则会给人"已经幂等了"的错觉。
"""
from __future__ import annotations

import hashlib
import json

from app.core.shared_state import SharedState
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

#: 状态常量
NEW = "new"          # 首次执行，由调用方处理
INFLIGHT = "inflight"  # 已有同 key 的请求在飞 → 不要再执行
DONE = "done"        # 已有完成结果 → 直接回放


def content_key(tenant_id: str, question: str, session_id: str = "", doc_id: str = "") -> str:
    """按内容生成在飞去重的键。

    用 sha256 而不是明文拼接：键里含用户问题，明文会进 Redis 的 key 列表与日志。
    """
    raw = f"{tenant_id}|{session_id}|{doc_id}|{question.strip()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


class IdempotencyStore:
    """在飞去重 + 结果重放。

    键空间（都带前缀，避免与限流/缓存的键冲突）：

    - ``kq:idem:inflight:{key}``：在飞计数（`incr` 原子门）
    - ``kq:idem:done:{key}``：完成结果（JSON，24h TTL）
    """

    def __init__(
        self,
        state: SharedState,
        inflight_ttl_s: int = 60,
        result_ttl_s: int = 24 * 3600,
        prefix: str = "kq:idem:",
    ) -> None:
        self._state = state
        self._inflight_ttl_s = inflight_ttl_s
        self._result_ttl_s = result_ttl_s
        self._prefix = prefix

    # ---- 键 ----

    def _k_inflight(self, key: str) -> str:
        return f"{self._prefix}inflight:{key}"

    def _k_done(self, key: str) -> str:
        return f"{self._prefix}done:{key}"

    # ---- 观测 ----

    @property
    def degraded(self) -> bool:
        """共享存储不可用（Redis 掉线）→ 幂等只在单副本内有效。"""
        try:
            return not self._state.healthy()
        except Exception:  # noqa: BLE001 - 观测失败不影响主流程
            return True

    def backend_name(self) -> str:
        try:
            return self._state.backend_name()
        except Exception:  # noqa: BLE001 - 幂等键读取失败：按 unknown 处理
            logger.debug("幂等键读取失败：按 unknown 处理")
            return "unknown"

    # ---- 在飞去重（按内容） ----

    def acquire(self, key: str) -> bool:
        """尝试占位：拿到执行权返回 True；已有在飞返回 False。"""
        try:
            count = self._state.incr(self._k_inflight(key), 1, ttl_s=self._inflight_ttl_s)
        except Exception as exc:  # noqa: BLE001 - 存储不可用不阻断请求
            logger.warning("幂等占位失败（按未占位处理）: %s", exc)
            return True
        return count == 1

    def release(self, key: str) -> None:
        """释放在飞占位（成功或失败都要释放）。"""
        try:
            self._state.delete(self._k_inflight(key))
        except Exception as exc:  # noqa: BLE001 - 幂等占位释放失败
            logger.warning("幂等占位释放失败（TTL 会兜底）: %s", exc)

    # ---- 幂等键（结果重放） ----

    def begin(self, key: str) -> tuple[str, dict | None]:
        """返回 ``(状态, 已完成的结果)``：``new`` / ``inflight`` / ``done``。"""
        try:
            cached = self._state.get(self._k_done(key))
        except Exception as exc:  # noqa: BLE001 - 幂等结果读取失败
            logger.warning("幂等结果读取失败（按首次处理）: %s", exc)
            cached = None
        if cached:
            try:
                return DONE, json.loads(cached)
            except ValueError:
                return DONE, None
        if not self.acquire(key):
            return INFLIGHT, None
        return NEW, None

    def complete(self, key: str, payload: dict) -> None:
        """记录完成结果（可被回放）。"""
        try:
            self._state.set(
                self._k_done(key),
                json.dumps(payload, ensure_ascii=False, default=str),
                ttl_s=self._result_ttl_s,
            )
        except Exception as exc:  # noqa: BLE001 - 幂等结果写入失败
            logger.warning("幂等结果写入失败（本次不重放）: %s", exc)
        self.release(key)

    def fail(self, key: str) -> None:
        """执行失败：释放占位且**不**留下可回放的结果。"""
        self.release(key)
