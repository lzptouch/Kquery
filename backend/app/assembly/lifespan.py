"""应用生命周期编排（`A-12`）：装配顺序在这里，且**只在这里**。

## 顺序是有意的

```text
进程参数 → 指标 → 模型 → 运行时 → 存储 → 知识 → 流水线 → Agent → 钩子
```

- 指标最先：模型层（降级计数）、运行时（丢弃计数）、后续所有层都要记数；
- 模型早于运行时：缓存要 embedding 采样；
- 运行时早于存储：索引别名与共享状态先就位；
- 知识早于流水线：流水线要知识服务做版本过滤与文档名解析；
- Agent 最后：它要工具目录、流水线里的 CRAG/自检、以及入库钩子要用的图谱对象。

## 关停是装配的逆序

先停止接受新任务（执行器 shutdown 会等在飞请求），再关连接（checkpointer / 图存储 /
向量库）。顺序反了会看到「关连接时还有人在写」的报错。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from app.assembly.agent import build_agent
from app.assembly.context import Assembly
from app.assembly.hooks import install_hooks
from app.assembly.knowledge import build_knowledge
from app.assembly.models import build_models
from app.assembly.pipeline import build_pipeline
from app.assembly.runtime import build_metrics, build_runtime, prepare_process
from app.assembly.storage import build_storage
from app.core.logging_setup import get_logger
from app.services.agent.checkpointer import close_checkpointer

if TYPE_CHECKING:  # 仅类型检查
    from app.core.config import Settings

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动：按装配域顺序建好运行时；退出：逆序关停。"""
    settings: Settings = app.state.settings
    asm = Assembly(settings, app)
    prepare_process(asm)
    build_metrics(asm)
    build_models(asm)
    build_runtime(asm)
    build_storage(asm)
    build_knowledge(asm)
    build_pipeline(asm)
    build_agent(asm)
    install_hooks(asm)

    logger.info(
        "Kquery 后端已启动（provider=%s, vector_store=%s, llm=%s, rerank=%s, 向量=%d, BM25=%d）",
        settings.resolved_provider, settings.vector_store, settings.llm_model,
        settings.rerank_model, asm.store.total, asm.bm25.total,
    )
    try:
        yield
    finally:
        shutdown(asm)


def shutdown(asm: Assembly) -> None:
    """关停：先停止接受新任务并等在飞问答/Agent 结束，再关存储连接。"""
    # 批次 1：先停止接受新任务并等在飞问答/Agent 结束，再关存储连接
    asm.pipeline_executor.shutdown(timeout_s=30.0)
    asm.agent_run_pool.shutdown(wait=False, cancel_futures=True)
    asm.agent_tool_pool.shutdown(wait=False, cancel_futures=True)
    # 图构建线程不等待：半途退出由 `rebuild_doc_id` 的覆盖语义兜住
    # （下次重建会把该文档的图整段重写），等它反而拖住关停。
    asm.graph_build_pool.shutdown(wait=False, cancel_futures=True)
    asm.task_manager.shutdown(timeout_s=30.0)
    # 释放 checkpointer 持有的 sqlite 连接。
    # 不关闭的话，Windows 上临时目录无法清理（WinError 32 文件占用），
    # 且连接会一直挂在进程里阻止 WAL 落盘。
    close_checkpointer(asm.agent_checkpointer)
    # 图存储的 sqlite 连接同理——不关会导致临时目录无法清理
    for closer in (
        lambda: asm.store.close(),
        # 内存实现无 close（接口未要求）；hasattr 兜一下，避免降级路径报错
        lambda: asm.graph_store.close() if hasattr(asm.graph_store, "close") else None,
    ):
        try:
            closer()
        except Exception:  # noqa: BLE001 - 关闭钩子失败：进程正在退出，不阻断关停
            pass
