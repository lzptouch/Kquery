"""依赖探针的公共实现（`A-05` / `X-18`）。

## 为什么在 `core/` 而不是 `api/`

探针是**跨层基础设施**：`api/probes.py` 的 `/healthz` 要用它，
`bootstrap.py` 的错误指标与 `/readyz` 也要用。若放在 `api/`，
`bootstrap.py` 就得反向依赖端点层，`api` 内部也会互相依赖。

## 三条不变的口径

- **并行 + 单项超时**：总耗时 = 最慢的一项，而不是各项之和；
- **错误只回类型名**：`str(exc)` 可能含连接串、内部路径与库表名；
- **分项错误码**：调用方按码判定「哪一类依赖坏了」，据此差异化处置。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from app.core.envelope import ErrCode
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

# 探针依赖 → 失败时的统一错误码（`A-05`）。
# 为什么**分项**而不是共用一个码：调用方要按「哪一类依赖坏了」差异化处置
# （向量库不可用 → 重建索引；共享状态不可用 → 摘流；模型不可用 → 降级）。
# 依据是 Google SRE 的「告警症状而非原因」原则。
PROBE_ERROR_CODES: dict[str, ErrCode] = {
    "vector_store": ErrCode.VECTOR_STORE_UNAVAILABLE,
    "bm25_index": ErrCode.VECTOR_STORE_UNAVAILABLE,
    "graph_store": ErrCode.GRAPH_STORE_UNAVAILABLE,
    "shared_state": ErrCode.SHARED_STATE_UNAVAILABLE,
    "long_term_memory": ErrCode.MEMORY_UNAVAILABLE,
    "parser_router": ErrCode.PARSER_UNAVAILABLE,
    "provider": ErrCode.PROVIDER_UNAVAILABLE,
    "upstream": ErrCode.UPSTREAM_UNAVAILABLE,
}

# 探针状态 → HTTP 状态码（成文，避免「谁拍脑袋谁说了算」）。
# up = 200；down = 503（依赖不可用 → 摘流，不该继续接流量）。
PROBE_STATUS_HTTP: dict[str, int] = {"up": 200, "down": 503}

# `/metrics` 允许「无令牌裸抓」的环境档位（`X-18` fail-closed 的判断依据）。
# 只有本地开发/测试档才放行：生产漏配 `METRICS_TOKEN` 时必须 401，
# 而不是「读一个已删除的配置项拿到恒假 → 静默放行」。
METRICS_OPEN_ENVS = frozenset({"dev", "development", "local", "test"})


def elapsed(t0: float) -> float:
    """从 `t0` 到现在的毫秒数（保留两位小数）。"""
    return round((time.perf_counter() - t0) * 1000, 2)


def env_of(settings: object) -> str:
    """当前环境档位（小写）；未配置时按 `dev` 处理。"""
    return str(getattr(settings, "app_env", "dev") or "dev").strip().lower()


def probe_all(probes: list[tuple[str, object]], timeout_s: float = 1.0) -> list[dict]:
    """并行执行依赖探针，单项超时上限（X-18）。

    - **并行**：总耗时 = 最慢的一项，而不是各项之和。串行 9 个依赖时，
      任一依赖慢 5s 整个探针就 5s，K8s 的 probe timeout 会误杀 Pod；
    - **单项超时**：慢依赖不会把整个探针拖长（超时按 down 记）；
    - **错误只回类型**：`str(exc)` 可能含连接串（redis://:pwd@host）、
      内部路径与库表名——这些不该出现在探针响应里。排障靠 trace_id 反查日志。

    超时后**不**等待线程结束（`shutdown(wait=False)`）：探针不能被慢依赖拖住，
    残留线程在池回收后自然退出。
    """

    def _one(item: tuple[str, object]) -> dict:
        name, fn = item
        t0 = time.perf_counter()
        try:
            detail = fn() or {}
            return {"name": name, "status": "up", "latency_ms": elapsed(t0), **detail}
        except Exception as exc:  # noqa: BLE001 - 探针单项失败：失败本身体现在响应的 status=down 与 error 上
            return {
                "name": name,
                "status": "down",
                "latency_ms": elapsed(t0),
                # 只回**异常类型名**：原文可能含连接串（redis://:pwd@host）、
                # 内部路径与库表名——这些不该出现在探针响应里。排障靠 trace_id 反查日志。
                "error": type(exc).__name__,
                # 统一错误码：调用方按码判定「哪一类依赖坏了」，据此差异化处置。
                "code": int(PROBE_ERROR_CODES.get(name, ErrCode.DEPENDENCY_UNAVAILABLE)),
            }

    results: list[dict] = []
    pool = ThreadPoolExecutor(max_workers=max(1, len(probes)), thread_name_prefix="probe")
    try:
        futures = {pool.submit(_one, item): item[0] for item in probes}
        for fut, name in futures.items():
            try:
                results.append(fut.result(timeout=max(0.01, float(timeout_s))))
            except FutureTimeout:
                results.append({
                    "name": name,
                    "status": "down",
                    "latency_ms": int(float(timeout_s) * 1000),
                    "error": "Timeout",
                    "code": int(PROBE_ERROR_CODES.get(name, ErrCode.DEPENDENCY_UNAVAILABLE)),
                })
            except Exception as exc:  # noqa: BLE001 - 探针单项超时：失败本身体现在响应的 status=down 与 error 上
                results.append({
                    "name": name,
                    "status": "down",
                    "latency_ms": 0,
                    "error": type(exc).__name__,
                    "code": int(PROBE_ERROR_CODES.get(name, ErrCode.DEPENDENCY_UNAVAILABLE)),
                })
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results


def alias_version(state: object) -> int:
    """索引别名版本（探针用；不可用时降级为 0，不让探针因它失败）。"""
    try:
        alias = getattr(state, "index_alias", None)
        return int(alias.version()) if alias is not None else 0
    except Exception:  # noqa: BLE001 - 索引别名版本读取失败：按 0 处理（前端据此失效缓存）
        logger.debug("索引别名版本读取失败：按 0 处理（前端据此失效缓存）")
        return 0
