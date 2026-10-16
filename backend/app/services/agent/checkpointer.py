"""Checkpointer 装配（TASK-115）。

stage-6 §1.2 曾把 "Checkpointer + thread_id" 写进交付承诺，代码里却一直是
`graph.compile()` 裸编译。本次补齐，一次解锁四项能力：

1. **断点续跑**：同一 thread_id 二次调用能取到上一轮 state；
2. **多轮状态继承**：跨轮携带的工具指纹/无进展计数不再靠 snapshots 手搓；
3. **完整回放**：step 级快照让"为什么这么答"可事后追溯；
4. **HITL 前提**：LangGraph 的 `interrupt()` 必须依赖 checkpointer 才能挂起与恢复
   （见 TASK-124）。

设计取舍：
- **生产用 SqliteSaver**（单文件、事务安全、跨进程可读），**测试用 MemorySaver**
  （不落盘、避免测试间串状态）；
- **不可用时降级为 None**（等价于不启用续跑），并由调用方在 state 里打 degraded 标记——
  绝不让 checkpointer 故障阻断 Agent 主流程。
- 刻意**不设置 TTL 清理**：sqlite 行数由 TTL 任务定期清理，避免在装配期引入定时器
  （进程内线程定时器在多副本下不可靠，见 TASK-181）。
"""
import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

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
        logger.info("Agent checkpointer 已显式禁用（不启用断点续跑与 HITL）")
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("Agent checkpointer 关闭失败（忽略）: %s", exc)
