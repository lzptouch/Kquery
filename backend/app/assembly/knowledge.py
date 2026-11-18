"""知识服务装配：知识服务 / 任务队列 / 快问 / 校验历史（`A-12`）。

## 一处晚绑定：入库后置钩子

`TaskManager` 需要「入库完成后建图 + 建图像索引」的钩子，但图与图像对象在
Agent 域才装好。这里用**可变容器**晚绑定（`post_index_box["hook"]`）：
`TaskManager` 侧存的是 `lambda` 调用，真正调用发生在文档入库时，
那时钩子已经被 `hooks.install_hooks()` 填进去了。

不用「调整装配顺序」解决，因为图构建依赖工具目录，而工具目录又依赖知识服务
——真按依赖排序会形成环。

## 版本过滤回填

`HybridRetriever` 装配早于知识服务（它是存储域），但召回侧要按**当前索引版本**
过滤历史版本块。这里回填 `hybrid._version_of = knowledge.version_of`；
回填前该字段为 None（不过滤），因此顺序错也只影响过滤、不影响可用性。
"""
from __future__ import annotations

import threading

from app.assembly.context import Assembly
from app.core.logging_setup import get_logger
from app.services.ingest import TaskManager
from app.services.knowledge import KnowledgeService
from app.services.quickask import QuickAskService, RedisQuickAskStore
from app.services.validation.feedback import FeedbackStore
from app.services.validation.history import ValidationHistoryStore

logger = get_logger(__name__)


def build_knowledge(asm: Assembly) -> None:
    """装好知识服务、入库任务队列、校验历史、反馈存储与快问。"""
    settings = asm.settings
    app = asm.app

    knowledge = KnowledgeService(
        settings, asm.embeddings, asm.llm, asm.store, asm.bm25, asm.docstore, asm.parser_router
    )
    # hybrid 装配早于 knowledge，回填版本查询（召回侧按当前版本
    # 过滤历史版本块）；knowledge 存在前 _version_of 为 None，不过滤
    asm.hybrid._version_of = knowledge.version_of

    # 后置钩子先占位：图存储/构建器在 Agent 域才装配，
    # 这里用一个可变容器晚绑定，避免调整装配顺序牵连入库服务
    post_index_box: dict = {}
    task_manager = TaskManager(
        knowledge, settings.data_dir / "tasks.json", settings.data_dir / "ingest",
        post_index=lambda record, parsed: post_index_box["hook"](record, parsed),
        docs=asm.task_docs,
        # `A-06`：终态任务的内存保留时长（默认 30 分钟）
        retention_s=int(getattr(settings, "ingest_task_retention_s", 1800)),
    )

    # 校验历史（CRAG 轮次 / 自检明细）落库
    validation_history = ValidationHistoryStore(settings.data_dir / "validation-history")

    asm.knowledge = knowledge
    asm.task_manager = task_manager
    asm.post_index_box = post_index_box
    asm.validation_history = validation_history
    app.state.knowledge = knowledge
    app.state.task_manager = task_manager
    app.state.feedback_store = FeedbackStore(settings.data_dir / "feedback.json")

    # F4 快问：临时态优先放 Redis（多副本共享），Redis 不可用自动降级为进程内
    shared_state = asm.shared_state
    quickask = QuickAskService(
        settings,
        knowledge,
        store=RedisQuickAskStore(shared_state) if settings.redis_url else None,
    )
    app.state.quickask = quickask
    # 启动清扫：进程崩溃会让临时向量残留（TTL 只管元数据，管不到索引）。
    # **放后台线程**：清扫要打 Milvus（每个文档约 10s），放在 lifespan 里
    # 会把启动拖成分钟级——容器在启动期就被判 unhealthy，
    # `restart: unless-stopped` 还会把它打进 crash loop。
    # 清扫是维护任务，不该挡在服务前面。
    def _sweep_quickask() -> None:
        try:
            quickask.sweep_expired(limit=20, budget_s=10.0)
        except Exception as exc:  # noqa: BLE001 - 清扫失败不影响服务
            logger.warning("快问启动清扫失败（不影响服务）: %s", exc)

    threading.Thread(target=_sweep_quickask, name="quickask-sweep", daemon=True).start()
