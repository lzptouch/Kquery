"""装配期共享上下文（`A-12`）。

## 为什么要有这个对象

装配是**有顺序的**：指标 → 模型 → 运行时 → 存储 → 知识 → 流水线 → Agent → 钩子。
后面的阶段要用前面阶段建好的对象（例：缓存要 embedding，流水线要知识服务，
Agent 要工具目录）。把这些对象挂在一个上下文上，每个 `assembly.*` 模块
只关心「我这一段怎么装」，顺序由 `assembly/lifespan.py` 显式编排。

## 为什么不用依赖注入容器

容器的收益是「自动解析依赖顺序」；而在装配层，**顺序本身就是设计**
（先建别名指针再装存储、先建共享状态再建缓存）。自动解析会把这份顺序藏起来，
出问题时只能读容器内部。显式函数调用 + 一个上下文对象，读起来最短。

## 类型标注为什么走 `TYPE_CHECKING`

这里只为**静态检查**声明字段类型，运行期一个符号都不需要：装配函数自己知道
建的是什么。集中 import 二十多个服务类只会让装配层反向依赖服务层细节，
所以注解全部写在 `if TYPE_CHECKING:` 里（`from __future__ import annotations`
让它们在运行期只是字符串）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # 破环 / 仅类型检查
    from concurrent.futures import ThreadPoolExecutor

    from fastapi import FastAPI

    from app.core.config import Settings
    from app.core.metrics import MetricsStore, PromMetrics
    from app.core.shared_state import SharedState
    from app.services.agent.memory import LongTermMemory
    from app.services.agent.runner import AgentRunner
    from app.services.agent.sessions import SessionStore
    from app.services.agent.tools import ToolRegistry
    from app.services.config_store import RetrievalConfigStore
    from app.services.docstore import DocStore
    from app.services.external_state import ExternalDocStore
    from app.services.graph.builder import GraphBuilder
    from app.services.graph.images import ImageIndex
    from app.services.graph.query import GraphQueryService
    from app.services.graph.resolver import EntityResolver
    from app.services.index_alias import IndexAliasManager
    from app.services.ingest import TaskManager
    from app.services.knowledge import KnowledgeService
    from app.services.parsers.router import ParserRouter
    from app.services.pipeline import RagPipeline
    from app.services.pipeline_executor import PipelineExecutor
    from app.services.query.analysis_store import AnalysisStore
    from app.services.query.orchestrator import QueryPreprocessor
    from app.services.retrieval.bm25 import BM25Retriever
    from app.services.retrieval.hybrid import HybridRetriever
    from app.services.validation.history import ValidationHistoryStore


class Assembly:
    """装配期共享上下文：字段由 `assembly.*` 的装配函数**按序**填充。

    只声明字段与类型，不提供默认值——访问未装配的字段会直接
    `AttributeError`，这是期望行为（装配顺序写错了要立刻炸，而不是拿到 None
    然后在请求路径上以「服务不可用」的面目出现）。
    """

    # —— 基础 ——
    settings: Settings
    app: FastAPI
    # —— 指标（runtime.build_metrics）——
    prom: PromMetrics
    metrics_store: MetricsStore
    # —— 模型（models.build_models）——
    embeddings: Any
    llm: Any
    reranker: Any
    fast_llm: Any
    agent_llm: Any
    ocr_engine: Any
    # —— 运行时（runtime.build_runtime）——
    shared_state: SharedState
    session_docs: ExternalDocStore | None
    config_docs: ExternalDocStore | None
    task_docs: ExternalDocStore | None
    config_store: RetrievalConfigStore
    cache: Any
    limiter: Any
    pipeline_executor: PipelineExecutor
    agent_run_pool: ThreadPoolExecutor
    agent_tool_pool: ThreadPoolExecutor
    # —— 存储（storage.build_storage）——
    alias_manager: IndexAliasManager
    store: Any
    bm25: BM25Retriever
    hybrid: HybridRetriever
    docstore: DocStore
    parser_router: ParserRouter
    # —— 知识（knowledge.build_knowledge）——
    knowledge: KnowledgeService
    task_manager: TaskManager
    post_index_box: dict
    validation_history: ValidationHistoryStore
    # —— 流水线（pipeline.build_pipeline）——
    preprocessor: QueryPreprocessor
    analysis_store: AnalysisStore
    pipeline: RagPipeline
    # —— Agent（agent.build_agent）——
    tool_registry: ToolRegistry
    graph_store: Any
    entity_resolver: EntityResolver
    graph_builder: GraphBuilder
    graph_query: GraphQueryService
    image_index: ImageIndex
    agent_checkpointer: Any
    agent_graph: Any
    session_store: SessionStore
    agent_runner: AgentRunner
    long_term_memory: LongTermMemory | None
    graph_build_pool: ThreadPoolExecutor
    # —— 钩子（hooks.install_hooks）——
    post_index: Callable[..., dict]

    def __init__(self, settings: Settings, app: FastAPI) -> None:
        self.settings = settings
        self.app = app
