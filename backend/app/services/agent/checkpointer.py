"""Checkpointer 装配。

 曾把 "Checkpointer + thread_id" 写进交付承诺，代码里却一直是
`graph.compile()` 裸编译。本次补齐，一次解锁四项能力：

1. **断点续跑**：同一 thread_id 二次调用能取到上一轮 state；
2. **多轮状态继承**：跨轮携带的工具指纹/无进展计数不再靠 snapshots 手搓；
3. **完整回放**：step 级快照让"为什么这么答"可事后追溯。

设计取舍：
- **生产用 SqliteSaver**（单文件、事务安全、跨进程可读），**测试用 MemorySaver**
  （不落盘、避免测试间串状态）；
- **不可用时降级为 None**（等价于不启用续跑），并由调用方在 state 里打 degraded 标记——
  绝不让 checkpointer 故障阻断 Agent 主流程。
- **TTL 清理**：由 `prune_checkpoints()` 执行，**装配期挂一个周期任务**调用它
  （此前注释说「由 TTL 任务定期清理」，但仓里根本没有这个任务——`prune_checkpoints()`
  零调用点，等于 sqlite 行数只增不减，`A-22.6`）。
"""
import sqlite3
import threading
from pathlib import Path
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

_MEMORY_SAVER = None
_MEMORY_LOCK = threading.Lock()


def build_checkpointer(settings, data_dir: Path | None = None):
    """按配置构造 checkpointer；返回 None 表示不启用（降级而非报错）。

    配置项：
    - `agent_checkpointer`：`sqlite`（默认）/ `memory` / `none`
    - `agent_checkpoint_path`：sqlite 文件路径，默认 `{data_dir}/agent_checkpoints.db`
    """
    kind = (getattr(settings, "agent_checkpointer", "sqlite") or "sqlite").lower()
    if kind == "none":
        logger.info("Agent checkpointer 已显式禁用（不启用断点续跑）")
        return None

    if kind == "memory":
        return _memory_saver()

    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError as exc:  # 依赖未安装 → 降级而非启动失败
        logger.warning("SqliteSaver 不可用（%s），降级为 MemorySaver", exc)
        return _memory_saver()

    try:
        base = Path(data_dir) if data_dir else Path(getattr(settings, "data_dir", "."))
        path = getattr(settings, "agent_checkpoint_path", "") or str(
            base / "agent_checkpoints.db"
        )
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：FastAPI 线程池会跨线程复用连接，
        # 而 sqlite3 默认禁止跨线程使用同一连接。这里靠 SqliteSaver 自身的
        # 锁保证串行化，不依赖 check_same_thread 的互斥。
        conn = sqlite3.connect(path, check_same_thread=False)
        saver = SqliteSaver(conn)
        saver.setup()
        logger.info("Agent checkpointer 已启用：sqlite (%s)", path)
        return saver
    except Exception as exc:  # noqa: BLE001 - 磁盘满/权限/锁冲突等一律降级
        logger.warning("SqliteSaver 初始化失败（%s），降级为 MemorySaver", exc)
        return _memory_saver()


def _memory_saver():
    """进程级共享的 MemorySaver。

    **为什么共享单例**：AgentRunner 是 lifespan 单例，若每次 run 新建 saver，
    同一 thread_id 也读不到上一轮 state，断点续跑形同虚设。
    """
    global _MEMORY_SAVER
    with _MEMORY_LOCK:
        if _MEMORY_SAVER is None:
            from langgraph.checkpoint.memory import MemorySaver

            _MEMORY_SAVER = MemorySaver()
    return _MEMORY_SAVER


def thread_id_for(session_id: str, turn: int | str) -> str:
    """checkpointer 的 thread_id 约定：`{session_id}:{turn}`。

    显式带 turn 是为了**可回放**：同一会话的每一轮是独立 checkpoint，
    不会互相覆盖；不带 turn 则第二轮会直接覆盖第一轮的状态。
    """
    return f"{session_id}:{turn}"


def resolve_checkpoint_db_path(settings) -> Path | None:
    """checkpointer 的 sqlite 文件路径；非 sqlite 后端返回 `None`。

    **单一真相源**：装配层（周期清理）与 API（按会话删除）必须用同一个口径，
    否则"清理的任务"和"删除的端点"会指向两个不同的库。
    """
    if str(getattr(settings, "agent_checkpointer", "sqlite")).lower() != "sqlite":
        return None
    configured = str(getattr(settings, "agent_checkpoint_path", "") or "")
    return Path(configured) if configured else Path(settings.data_dir) / "agent_checkpoints.db"


def close_checkpointer(saver) -> None:
    """释放 checkpointer 持有的资源（lifespan 收尾调用）。

    SqliteSaver 把 sqlite3.Connection 存在 `.conn` 上；不显式关闭的话，
    Windows 上进程退出会报文件占用（WinError 32），且 WAL 可能未落盘。
    MemorySaver 无需处理；close 失败只记录不抛出（收尾路径不应抛异常）。
    """
    if saver is None:
        return
    conn = getattr(saver, "conn", None)
    if conn is None:
        return
    try:
        conn.close()
        logger.info("Agent checkpointer 已关闭")
    except Exception as exc:  # noqa: BLE001 - Agent checkpointer 关闭失败
        logger.warning("Agent checkpointer 关闭失败（忽略）: %s", exc)


def prune_checkpoints(db_path: Path, before_ts: float) -> int:
    """清理早于 `before_ts` 的检查点行，返回删除条数。

    **为什么需要**：LangGraph 的 SqliteSaver 每次 run 都写一行，
    此前**没有任何清理机制** → 长跑进程的检查点库只增不减（磁盘泄漏），
    而且"完整回放"这一项的代价随时间线性上升。

    清理是**保守**的：只删明显过期的行（默认留 7 天由调用方给的时间戳决定），
    正在挂起等待人工介入的 run 不该被删——它们的 checkpoint 必须留着，
    否则用户回来点"继续"会发现 run 消失了。
    """
    if not db_path.exists():
        return 0
    deleted = 0
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            tables = [
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
            for table in tables:
                cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()]
                for ts_col in ("created_at", "ts", "updated_at"):
                    if ts_col not in cols:
                        continue
                    cur = conn.execute(
                        f"DELETE FROM {table} WHERE {ts_col} < ?", (float(before_ts),)
                    )
                    deleted += cur.rowcount or 0
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - 清理失败不该影响服务
        logger.warning("检查点清理失败（忽略）: %s", exc)
    return deleted


def _like_prefix(session_id: str) -> str:
    """把 `session_id` 转成安全的 `LIKE` 前缀（`{session_id}:%`）。"""
    escaped = session_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}:%"


def delete_session_threads(db_path: Path, session_id: str) -> int:
    """删除某会话的**全部**检查点行，返回删除条数（`KQ-23` 残余）。

    ## 为什么必须按会话删，而不是等 TTL

    `DELETE /sessions/{id}` 此前只删快照文件，LangGraph 的检查点行还留着——
    用户看到的是"会话没了"，但磁盘上仍有该会话每一轮的完整 state（对话原文 +
    证据 + 工具出入参），要等 7 天 TTL 才消失。"删除"承诺的是立刻不留残留。

    thread_id 编码是 `{session_id}:{turn}`（见 `thread_id_for`），所以按前缀删；
    前缀里的 `%` / `_` 必须转义——`LIKE 'a_c:%'` 的 `_` 是通配符，会误删别的会话
    （当前 session_id 是 uuid4 hex，但依赖"恰好没有下划线"太脆）。
    """
    if not db_path.exists():
        return 0
    prefix = _like_prefix(session_id)
    deleted = 0
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            tables = [
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
            for table in tables:
                cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()]
                if "thread_id" not in cols:
                    continue
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE thread_id LIKE ? ESCAPE '\\'", (prefix,)
                )
                deleted += cur.rowcount or 0
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - 删除失败不该阻断会话删除
        logger.warning("按会话删除检查点失败（忽略）: %s", exc)
    return deleted
