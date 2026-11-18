"""Agent 装配：工具目录 / 图谱 / 图像 / 检查点 / 会话 / 运行器（`A-12`）。

## 为什么工具目录先于图谱

`GraphSearchTool` / `ImageSearchTool` 要拿到图查询服务与图像索引，
但**图谱对象本身又依赖工具目录**（规划器按目录规划）。顺序是：
先建目录并注册基础工具 → 再建图谱 → 最后把图谱类工具补进目录。

## 工具「未配置就不注册」

业务库与联网工具在依赖缺失时**不注册**：目录里放一个必然失败的工具，
会诱导 Planner 规划出注定失败的步骤（这比「没有这个能力」更糟）。

## 检查点：可降级但不可无清理

`build_checkpointer` 失败返回 `None`（Agent 仍能跑，只是不能续跑）；
`start_checkpoint_pruner` 必须挂上，否则 LangGraph 的 SqliteSaver 只增不减。
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.assembly.context import Assembly
from app.core.logging_setup import get_logger
from app.services.agent.checkpointer import (
    build_checkpointer,
    prune_checkpoints,
    resolve_checkpoint_db_path,
)
from app.services.agent.graph import build_agent_graph
from app.services.agent.memory import LongTermMemory
from app.services.agent.planner import HeuristicPlanner, LLMPlanner
from app.services.agent.reflector import HeuristicReflector, LLMReflector
from app.services.agent.runner import AgentRunner, agent_prompt_builder
from app.services.agent.sessions import SessionStore
from app.services.agent.tools import (
    CalculatorTool,
    GraphSearchTool,
    ImageSearchTool,
    SqlSearchTool,
    ToolRegistry,
    VectorSearchTool,
    WebSearchTool,
)
from app.services.agent.tools_extra import (
    DateTimeCalcTool,
    ListDocumentsTool,
    SqlSchemaTool,
)
from app.services.graph.build_tasks import GraphBuildTaskRegistry
from app.services.graph.builder import GraphBuilder
from app.services.graph.extractor import LLMExtractor, ScriptedExtractor
from app.services.graph.images import ImageIndex, MockCaptioner, QwenVlCaptioner
from app.services.graph.query import GraphQueryService
from app.services.graph.resolver import EntityResolver
from app.services.graph.sqlite_store import build_graph_store
from app.services.multimodal import build_vlm_answerer
from app.services.vectorstore import build_vector_store

logger = get_logger(__name__)


def _build_tool_registry(asm: Assembly) -> ToolRegistry:
    """建工具目录并注册与图谱无关的工具（检索 / 计算 / 列文档 / 日期 / SQL / 联网）。"""
    settings = asm.settings
    tool_registry = ToolRegistry(
        timeout_s=settings.agent_tool_timeout_s, executor=asm.agent_tool_pool
    )
    tool_registry.register(VectorSearchTool(asm.hybrid, asm.config_store.get_active()))
    tool_registry.register(CalculatorTool())
    # 补"库里有什么"与日期推理两个能力盲区
    tool_registry.register(ListDocumentsTool(asm.knowledge))
    tool_registry.register(DateTimeCalcTool())
    # 业务库 / 联网工具：未配置外部依赖时不注册——
    # 目录里放一个必然失败的工具，会诱导 Planner 规划出注定失败的步骤
    if settings.sql_dsn:
        tool_registry.register(
            SqlSearchTool(
                settings.sql_dsn,
                readonly=settings.sql_readonly,
                max_rows=settings.sql_max_rows,
                tenant_column=settings.sql_tenant_column or None,
            )
        )
        # schema 与查询同源同租户口径：没有 schema 的 sql_search 只能靠模型猜表名
        tool_registry.register(
            SqlSchemaTool(
                settings.sql_dsn,
                tenant_column=settings.sql_tenant_column or None,
            )
        )
    if settings.web_search_endpoint:
        tool_registry.register(
            WebSearchTool(
                settings.web_search_endpoint,
                api_key=settings.web_search_api_key,
                timeout_s=settings.web_search_timeout_s,
            )
        )
    return tool_registry


def _build_graph_parts(
    asm: Assembly,
) -> tuple[object, EntityResolver, GraphBuilder, GraphQueryService, ImageIndex]:
    """装图谱（存储/消歧/构建/查询）与图像索引（多模态统一向量空间）。"""
    settings = asm.settings
    provider = settings.resolved_provider
    embeddings = asm.embeddings
    agent_llm = asm.agent_llm

    # 图存储锁等待 / 持有计时（`C-08`）：判定「图写入是不是瓶颈」的唯一依据。
    # 判据：锁等待 P95 > 锁持有 P95 的 1/3 → 图写入成为瓶颈（触发 `C-09`）。
    def _on_graph_lock_timing(store_type: str, wait_s: float, hold_s: float) -> None:
        try:
            asm.prom.graph_lock_wait_seconds.labels(store=store_type).observe(wait_s)
            asm.prom.graph_lock_hold_seconds.labels(store=store_type).observe(hold_s)
        except Exception:  # noqa: BLE001 - 指标失败不该影响图写入
            pass

    graph_store = build_graph_store(
        settings, settings.data_dir, on_lock_timing=_on_graph_lock_timing
    )
    resolver_path = settings.data_dir / "graph" / "resolver.json"
    if resolver_path.exists():
        entity_resolver = EntityResolver.load(
            json.loads(resolver_path.read_text(encoding="utf-8")),
            embeddings, settings.entity_sim_threshold,
        )
    else:
        entity_resolver = EntityResolver(embeddings, settings.entity_sim_threshold)
    extractor = LLMExtractor(agent_llm) if provider == "dashscope" else ScriptedExtractor()
    graph_builder = GraphBuilder(
        extractor, entity_resolver, graph_store, embeddings, asm.store,
        asm.llm if provider == "dashscope" else None,
        tenant_id=settings.default_tenant_id,
        resolver_path=resolver_path,
        model_version=settings.llm_model,
        max_communities=settings.community_max,
        community_levels=settings.community_levels,
        community_merge_threshold=settings.community_merge_threshold,
    )
    graph_query = GraphQueryService(
        graph_store, entity_resolver, embeddings,
        bfs_depth=settings.graph_bfs_depth,
        seed_top_k=settings.graph_seed_top_k,
    )
    captioner = (
        QwenVlCaptioner(settings.dashscope_api_key, settings.image_caption_model)
        if provider == "dashscope" else MockCaptioner()
    )
    # M1：图像向量路（默认关闭）。图像向量与文本向量**维度不同、空间不同**，
    # 所以必须存**独立**的索引目录（FAISS）/集合（Milvus），混库会污染文本检索。
    image_embedder = None
    image_store = None
    if bool(getattr(settings, "image_vector_enabled", False)):
        if not settings.dashscope_api_key:
            logger.warning("image_vector_enabled=true 但没有 API Key，图像向量路已禁用")
        else:
            # 可选依赖：图像嵌入器仅在启用图像向量时装配
            from app.services.graph.image_embedder import DashScopeImageEmbedder

            image_embedder = DashScopeImageEmbedder(
                settings.dashscope_api_key, settings.image_embedding_model
            )
            # **独立**目录/集合：图像向量不能混进文本库
            image_store = build_vector_store(settings, suffix="_image")
    image_index = ImageIndex(
        captioner, embeddings, asm.store, settings.data_dir / "image-index.json",
        image_embedder=image_embedder, image_store=image_store,
    )
    return graph_store, entity_resolver, graph_builder, graph_query, image_index


def _build_build_tasks(asm: Assembly) -> None:
    """图谱构建任务执行器（`C-09`）：单 worker 串行 + 有界排队。

    **无条件装配**：`graph_enabled=false` 时 `/graph/build` 仍要能回一个
    明确错误（"图谱未启用"），而不是让 `app.state` 缺属性直接 500。
    同步构建会把请求线程按住几十秒——客户端早超时、后台还在跑，用户重试
    一次就再跑一遍（期间 `reject_background_when_busy` 还挡住其它后台操作）。
    """
    settings = asm.settings
    graph_build_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="graph-build")
    graph_build_tasks = GraphBuildTaskRegistry(
        graph_build_pool,
        retention_s=int(getattr(settings, "graph_build_task_retention_s", 1800)),
        max_pending=int(getattr(settings, "graph_build_max_pending", 4)),
    )
    asm.graph_build_pool = graph_build_pool
    asm.app.state.graph_build_tasks = graph_build_tasks


def start_checkpoint_pruner(asm: Assembly) -> None:
    """挂一个检查点 TTL 清理的周期任务（`A-22.6`）。

    `prune_checkpoints()` 此前没有任何调用点——注释说「由 TTL 任务定期清理」，
    但仓里没有这个任务，LangGraph 的 SqliteSaver 只增不减。

    清理策略**保守**：只删明显过期的行（默认留 7 天），
    正在挂起等待人工介入的 run 的 checkpoint 必须留着。

    多副本下每个副本都会跑这个任务：清理是幂等的（按时间戳删），
    重复执行不会互相干扰。
    """
    settings = asm.settings
    # 路径口径收敛到 `resolve_checkpoint_db_path`：原来装配层与 build_checkpointer
    # 各写一遍，API 的按会话删除会是第三处——三处口径必然漂移
    db_path = resolve_checkpoint_db_path(settings)
    if db_path is None:
        return
    keep_days = 7
    interval_s = 3600.0

    def _loop() -> None:
        while True:
            time.sleep(interval_s)
            try:
                deleted = prune_checkpoints(db_path, time.time() - keep_days * 86400)
                if deleted:
                    logger.info("检查点 TTL 清理：删除 %d 行过期检查点", deleted)
            except Exception:  # noqa: BLE001 - 清理失败不该杀死进程
                logger.exception("检查点 TTL 清理失败（忽略）")

    threading.Thread(target=_loop, name="checkpoint-pruner", daemon=True).start()


def build_agent(asm: Assembly) -> None:
    """装工具目录、图谱与图像、规划器/反思器、检查点、会话与 Agent 运行器。"""
    settings = asm.settings
    app = asm.app
    provider = settings.resolved_provider
    llm = asm.llm
    agent_llm = asm.agent_llm

    tool_registry = _build_tool_registry(asm)
    asm.tool_registry = tool_registry
    app.state.tool_registry = tool_registry

    graph_store, entity_resolver, graph_builder, graph_query, image_index = _build_graph_parts(asm)
    _build_build_tasks(asm)
    if settings.graph_enabled:
        tool_registry.register(GraphSearchTool(graph_query))
        tool_registry.register(ImageSearchTool(image_index))

    asm.graph_store = graph_store
    asm.entity_resolver = entity_resolver
    asm.graph_builder = graph_builder
    asm.graph_query = graph_query
    asm.image_index = image_index
    app.state.graph_store = graph_store
    app.state.entity_resolver = entity_resolver
    app.state.graph_builder = graph_builder
    app.state.graph_query = graph_query
    app.state.image_index = image_index

    if provider == "dashscope":
        planner = LLMPlanner(agent_llm, tool_registry, max_steps=settings.agent_max_steps)
        reflector = LLMReflector(agent_llm)
    else:
        planner = HeuristicPlanner(tool_registry)
        reflector = HeuristicReflector(correct_th=settings.crag_correct_th)
    # checkpointer 是断点续跑 / 多轮状态继承 / 完整回放 / HITL 的前提。
    # 不可用时返回 None（降级为不启用续跑），Agent 仍能正常跑。
    agent_checkpointer = build_checkpointer(settings, settings.data_dir)
    agent_graph = build_agent_graph(
        registry=tool_registry,
        planner=planner,
        reflector=reflector,
        answer_llm=llm,
        prompt_builder=agent_prompt_builder,
        no_progress_limit=settings.agent_no_progress_limit,
        checkpointer=agent_checkpointer,
        parametric_fallback_enabled=settings.parametric_fallback_enabled,
        parametric_fallback_score_threshold=settings.parametric_fallback_score_threshold,
        # M5：VLM 直答（默认关闭 → 不注入 = 行为完全不变）
        vlm_answerer=build_vlm_answerer(settings),  # 按 provider 选实现与凭据
    )
    session_store = SessionStore(
        settings.data_dir / "sessions", ttl_s=settings.agent_session_ttl_s,
        docs=asm.session_docs,
    )
    agent_runner = AgentRunner(
        agent_graph, session_store, asm.metrics_store, top_k=settings.top_k,
        registry=tool_registry,  # ReAct 模式的工具目录来源
        crag_router=asm.pipeline._crag,
        history_store=asm.validation_history,
        score_top_n=settings.crag_score_top_n,
        settings=settings,  # 读取三预算配置
        checkpointer=agent_checkpointer,  # HITL 挂起的前提
        prom=asm.prom,  # Agent 路径埋点
        llm=llm,  # 仅用于 token 用量埋点
        # Agent 路径接上与线性路径同一套幻觉自检
        self_checker=asm.pipeline._self_checker,
        executor=asm.agent_run_pool,  # 批次 1：固定池替代每请求新建线程
        run_permits=settings.agent_run_permits,
        docstore=asm.docstore,  # `ST-03`：Agent 路径的父块展开
    )

    # TASK-117：租户级长期记忆（把踩过的坑沉淀下来，注入后续规划）
    long_term_memory = (
        LongTermMemory(settings.data_dir / "memory" / "memory.jsonl",
                       max_per_tenant=settings.memory_max_per_tenant)
        if settings.memory_enabled else None
    )
    # AgentRunner 装配早于记忆创建，此处回填（装配期回填模式）
    agent_runner._memory = long_term_memory

    asm.agent_checkpointer = agent_checkpointer
    asm.agent_graph = agent_graph
    asm.session_store = session_store
    asm.agent_runner = agent_runner
    asm.long_term_memory = long_term_memory
    app.state.agent_checkpointer = agent_checkpointer
    app.state.session_store = session_store
    app.state.agent_runner = agent_runner
    app.state.long_term_memory = long_term_memory

    logger.info(
        "共享状态后端=%s；长期记忆=%s",
        asm.shared_state.backend, "on" if long_term_memory else "off",
    )
    # 检查点 TTL 清理（`A-22.6`）：`prune_checkpoints()` 此前**零调用点**——
    # 注释写着「sqlite 行数由 TTL 任务定期清理」，仓里却没有这个任务，
    # 于是 LangGraph 的 SqliteSaver 只增不减（长跑进程磁盘泄漏）。
    start_checkpoint_pruner(asm)
