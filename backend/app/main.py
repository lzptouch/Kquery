"""Kquery 智能 RAG 后端（阶段 2：混合检索 + 工程化）。

组装顺序：配置 → Embedding/LLM/Reranker → 向量索引 + BM25 索引 → 混合检索
→ 缓存 / 限流 / 指标 / 运行时配置 → 知识库服务 → 流水线。
索引文件损坏时拒绝启动，不允许以坏索引对外服务。
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from app.api import admin as admin_api
from app.api import analysis as analysis_api
from app.api import ask as ask_api
from app.api import feedback as feedback_api
from app.api import ingest as ingest_api
from app.api import graph as graph_api
from app.api import knowledge as knowledge_api
from app.api import reports as reports_api
from app.api import security as security_api
from app.api import sessions as sessions_api
from app.core.config import get_settings
from app.core.envelope import ApiError, ErrCode, error_envelope
from app.core.metrics import MetricsStore, PromMetrics
from app.core.tracing import new_trace_id
from app.services.cache import build_cache
from app.services.config_store import RetrievalConfigStore
from app.services.docstore import DocStore
from app.services.embeddings import build_embeddings
from app.services.ingest import TaskManager
from app.services.knowledge import KnowledgeService
from app.services.llm import build_llm
from app.services.parsers.audio import AudioParser, build_transcriber
from app.services.parsers.docx import DocxParser
from app.services.parsers.ocr import OcrParser, build_ocr_engine
from app.services.parsers.pdf import PdfParser
from app.services.parsers.router import ParserRouter
from app.services.pipeline import RagPipeline
from app.services.agent.graph import build_agent_graph
from app.services.agent.planner import HeuristicPlanner, LLMPlanner
from app.services.agent.reflector import HeuristicReflector, LLMReflector
from app.services.agent.runner import AgentRunner, agent_prompt_builder
from app.services.agent.sessions import SessionStore
from app.services.agent.tools import (
    CalculatorTool,
    GraphSearchTool,
    ImageSearchTool,
    ToolRegistry,
    VectorSearchTool,
)
from app.services.graph.builder import GraphBuilder
from app.services.graph.extractor import LLMExtractor, ScriptedExtractor
from app.services.graph.images import ImageIndex, MockCaptioner, QwenVlCaptioner
from app.services.graph.query import GraphQueryService
from app.services.graph.resolver import EntityResolver
from app.services.graph.store import InMemoryGraphStore
from app.services.multiagent.orchestrator import Orchestrator
from app.services.multiagent.roles import (
    FixedValidator,
    LLMCritic,
    LLMWriter,
    RerankValidator,
    ScriptedCritic,
)
from app.services.multiagent.roles import (
    HeuristicPlanner as MAHeuristicPlanner,
)
from app.services.multiagent.roles import (
    LLMPlanner as MALLMPlanner,
)
from app.services.multiagent.store import ReportStore
from app.services.security.acl import ACLService
from app.services.security.audit import AuditStore
from app.services.security.auth import build_auth_provider
from app.services.security.evalrun import EvalRunner, EvalStore
from app.services.security.platform_quota import PlatformQuotaMonitor, install_monitor
from app.services.security.quota import SlidingWindowQuota
from app.services.security.sensitive import SensitiveScanner
from app.services.query.analysis_store import AnalysisStore
from app.services.query.decomposer import Decomposer, SourceRouter
from app.services.query.expanders import FusionExpander, HydeExpander, StepBackExpander
from app.services.query.intent import IntentClassifier
from app.services.query.orchestrator import QueryPreprocessor
from app.services.ratelimit import build_limiter
from app.services.retrieval.bm25 import BM25Retriever
from app.services.retrieval.hybrid import HybridRetriever
from app.services.retrieval.rerank import build_reranker
from app.services.retrieval.vector import VectorRetriever
from app.services.validation.crag import CragRouter
from app.services.validation.feedback import FeedbackStore
from app.services.validation.selfcheck import HallucinationChecker
from app.services.vectorstore import build_vector_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        provider = settings.resolved_provider
        # 模型层（全部千问平台接口，无本地模型；无凭据时注入 Mock）
        embeddings = build_embeddings(
            provider=provider,
            api_key=settings.dashscope_api_key,
            model=settings.embedding_model,
            dim=settings.embedding_dim,
            max_retries=settings.llm_max_retries,
        )
        llm = build_llm(
            provider=provider,
            api_key=settings.dashscope_api_key,
            model=settings.llm_model,
            temperature=settings.temperature,
            max_retries=settings.llm_max_retries,
            timeout_s=settings.llm_timeout_s,
        )
        reranker = build_reranker(
            provider=provider,
            api_key=settings.dashscope_api_key,
            model=settings.rerank_model,
            max_retries=settings.llm_max_retries,
        )

        # 存储与索引（不可用/损坏 → 拒绝启动）
        store = build_vector_store(settings)
        store.load()
        bm25 = BM25Retriever(k1=settings.bm25_k1, b=settings.bm25_b)
        bm25.rebuild(store.all_chunks())

        # 混合检索编排
        hybrid = HybridRetriever(
            retrievers={
                "keyword": bm25,
                "vector": VectorRetriever(embeddings, store),
            },
            reranker=reranker,
            per_route_top_n=settings.retrieval_per_route_top_n,
            fused_top_n=settings.retrieval_fused_top_n,
            rrf_k=settings.rrf_k,
        )

        # 工程设施
        cache = build_cache()
        limiter = build_limiter()
        prom = PromMetrics()
        metrics_store = MetricsStore()
        config_store = RetrievalConfigStore(
            settings.data_dir / "retrieval-config.json",
            apply_delay_s=settings.retrieval_config_apply_delay_s,
        )

        # 阶段 3：DocStore（父块/表格/媒体资源）+ 解析路由 + 任务队列
        docstore = DocStore(settings.data_dir / "docstore.json")
        ocr_engine = build_ocr_engine(provider, settings.dashscope_api_key, settings.llm_max_retries)
        transcriber = build_transcriber(provider, settings.dashscope_api_key)
        parser_router = ParserRouter(
            ocr_parser=OcrParser(ocr_engine, settings.data_dir / "assets"),
            audio_parser=AudioParser(transcriber, settings.data_dir / "assets"),
            docx_parser=DocxParser(settings.data_dir / "assets"),
            pdf_parser=PdfParser(
                ocr_engine=ocr_engine,
                assets_dir=settings.data_dir / "assets",
                ocr_enabled=settings.pdf_ocr_enabled,
                ocr_dpi=settings.pdf_ocr_dpi,
                ocr_max_pages=settings.pdf_ocr_max_pages,
                scan_min_chars=settings.pdf_scan_min_chars,
            ),
        )
        knowledge = KnowledgeService(settings, embeddings, llm, store, bm25, docstore, parser_router)
        task_manager = TaskManager(knowledge, settings.data_dir / "tasks.json", settings.data_dir / "ingest")

        # 阶段 4：Query 预处理（意图识别压到 qwen-turbo 最低档，HyDE 与 Fusion 互斥）
        fast_llm = build_llm(
            provider=provider,
            api_key=settings.dashscope_api_key,
            model=settings.intent_model,
            temperature=settings.temperature,
            max_retries=settings.llm_max_retries,
            timeout_s=settings.llm_timeout_s,
        )
        expander_names = {n.strip() for n in settings.query_expanders.split(",") if n.strip()}
        expanders: list = []
        if "fusion" in expander_names:
            expanders.append(FusionExpander(fast_llm, n=settings.fusion_n))
        elif "hyde" in expander_names:  # HyDE 与 Fusion 不同时启用（互相稀释）
            expanders.append(HydeExpander(llm))
        if "stepback" in expander_names:
            expanders.append(StepBackExpander(llm))
        preprocessor = QueryPreprocessor(
            intent_classifier=IntentClassifier(fast_llm),
            expanders=expanders,
            decomposer=Decomposer(llm),
            router=SourceRouter(),
            query_budget=settings.query_budget,
            enabled=settings.preprocess_enabled,
        )
        analysis_store = AnalysisStore(settings.data_dir / "query_analysis.json")

        pipeline = RagPipeline(
            settings,
            llm,
            hybrid,
            cache,
            config_store,
            prom,
            metrics_store,
            doc_name_resolver=knowledge.filename_of,
            docstore=docstore,
            index_version_provider=lambda: knowledge.data_version,
            preprocessor=preprocessor,
            analysis_store=analysis_store,
            crag_router=CragRouter(
                correct_th=settings.crag_correct_th,
                incorrect_th=settings.crag_incorrect_th,
                max_iterations=settings.crag_max_iterations,
            )
            if settings.crag_enabled
            else None,
            self_checker=HallucinationChecker(llm) if settings.selfcheck_enabled else None,
        )

        app.state.settings = settings
        app.state.embeddings = embeddings
        app.state.llm = llm
        app.state.reranker = reranker
        app.state.store = store
        app.state.bm25 = bm25
        app.state.hybrid = hybrid
        app.state.cache = cache
        app.state.limiter = limiter
        app.state.prom = prom
        app.state.metrics_store = metrics_store
        app.state.config_store = config_store
        app.state.docstore = docstore
        app.state.parser_router = parser_router
        app.state.task_manager = task_manager
        app.state.knowledge = knowledge
        app.state.pipeline = pipeline
        app.state.analysis_store = analysis_store
        app.state.feedback_store = FeedbackStore(settings.data_dir / "feedback.json")

        # 阶段 9：企业护栏（认证 → 配额 → ACL → 敏感词 → 审计 → 评测）
        import json as _json9

        user_directory = _json9.loads(settings.auth_users_json) if settings.auth_users_json else [
            # 默认演示用户目录（生产应接统一认证中心）
            {"user_id": "admin", "tenant_id": "default", "roles": ["admin"], "groups": [], "enabled": True},
            {"user_id": "zhangsan", "tenant_id": "default", "roles": ["hr"], "groups": [], "enabled": True},
            {"user_id": "lisi", "tenant_id": "default", "roles": ["sales"], "groups": [], "enabled": True},
            {"user_id": "wangwu", "tenant_id": "tenant-b", "roles": ["finance"], "groups": ["exec"], "enabled": True},
        ]
        auth_provider = build_auth_provider(
            settings.auth_enabled,
            _json9.loads(settings.auth_tokens_json) if settings.auth_tokens_json else {},
        )
        audit_store = AuditStore(settings.data_dir / "audit" / "audit.jsonl")
        acl_service = ACLService(settings.data_dir / "acl.json", user_directory)
        quota_service = SlidingWindowQuota()
        quota_service.set_policy("default", settings.quota_per_minute, settings.quota_per_day)
        # 平台侧配额（账号级上游约束）：模型客户端上报，网关读取
        platform_monitor = PlatformQuotaMonitor(
            per_day=settings.platform_quota_per_day,
            warn_ratio=settings.platform_quota_warn_ratio,
        )
        install_monitor(platform_monitor)
        sensitive_scanner = SensitiveScanner(settings.data_dir / "sensitive-words.json")
        eval_store = EvalStore(settings.data_dir / "eval")

        def _eval_ask(question: str) -> dict:
            """评测执行入口：走 Agent 同步路径（与生产口径一致）。"""
            final: dict | None = None
            for event, payload in app.state.agent_runner.run(
                question, settings.default_tenant_id
            ):
                if event == "_final":
                    final = payload
            return final or {"answer": "", "evidence": [], "verdict": "refused"}

        eval_runner = EvalRunner(eval_store, _eval_ask, model_version=settings.llm_model)

        # 阶段 6：Agentic RAG（LangGraph 状态机 + 工具注册 + 会话存档）
        agent_llm = build_llm(
            provider=provider,
            api_key=settings.dashscope_api_key,
            model=settings.agent_planner_model,
            temperature=0,
            max_retries=settings.llm_max_retries,
            timeout_s=settings.llm_timeout_s,
        )
        tool_registry = ToolRegistry()
        tool_registry.register(VectorSearchTool(hybrid, config_store.get_active()))
        tool_registry.register(CalculatorTool())

        # 阶段 7：GraphRAG（图存储 + 消歧 + 构建 + 查询）与多模态（图像描述向量统一空间）
        graph_store = InMemoryGraphStore(settings.data_dir / "graph" / "graph.json")
        resolver_path = settings.data_dir / "graph" / "resolver.json"
        if resolver_path.exists():
            import json as _json

            entity_resolver = EntityResolver.load(
                _json.loads(resolver_path.read_text(encoding="utf-8")),
                embeddings, settings.entity_sim_threshold,
            )
        else:
            entity_resolver = EntityResolver(embeddings, settings.entity_sim_threshold)
        extractor = LLMExtractor(agent_llm) if provider == "dashscope" else ScriptedExtractor()
        graph_builder = GraphBuilder(
            extractor, entity_resolver, graph_store, embeddings, store,
            llm if provider == "dashscope" else None,
            tenant_id=settings.default_tenant_id,
            resolver_path=resolver_path,
            model_version=settings.llm_model,
        )
        graph_query = GraphQueryService(
            graph_store, entity_resolver, embeddings, bfs_depth=settings.graph_bfs_depth
        )
        captioner = (
            QwenVlCaptioner(settings.dashscope_api_key, settings.image_caption_model)
            if provider == "dashscope" else MockCaptioner()
        )
        image_index = ImageIndex(
            captioner, embeddings, store, settings.data_dir / "image-index.json"
        )
        if settings.graph_enabled:
            tool_registry.register(GraphSearchTool(graph_query))
            tool_registry.register(ImageSearchTool(image_index))
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
        agent_graph = build_agent_graph(
            registry=tool_registry,
            planner=planner,
            reflector=reflector,
            answer_llm=llm,
            prompt_builder=agent_prompt_builder,
            no_progress_limit=settings.agent_no_progress_limit,
        )
        session_store = SessionStore(
            settings.data_dir / "sessions", ttl_s=settings.agent_session_ttl_s
        )
        app.state.agent_runner = AgentRunner(
            agent_graph, session_store, metrics_store, top_k=settings.top_k,
            audit=audit_store if settings.acl_enabled else None,
            scanner=sensitive_scanner if settings.sensitive_scan_enabled else None,
        )
        app.state.session_store = session_store
        app.state.tool_registry = tool_registry

        # 阶段 8：多智能体报告（五角色协同 + 黑板 + 拓扑并行 + Writer↔Critic 循环）
        report_store = ReportStore(settings.data_dir / "reports")
        if provider == "dashscope":
            ma_planner = MALLMPlanner(agent_llm, tool_registry, max_tasks=settings.report_max_subtasks)
            ma_validator = RerankValidator(reranker, adopt_threshold=settings.crag_correct_th)
            writer_llm = build_llm(
                provider=provider, api_key=settings.dashscope_api_key,
                model=settings.writer_model, temperature=0.3,
                max_retries=settings.llm_max_retries, timeout_s=settings.llm_timeout_s,
            )
            critic_llm = build_llm(
                provider=provider, api_key=settings.dashscope_api_key,
                model=settings.critic_model, temperature=0,
                max_retries=settings.llm_max_retries, timeout_s=settings.llm_timeout_s,
            )
            ma_writer = LLMWriter(writer_llm, fallback_llm=agent_llm)
            ma_critic = LLMCritic(critic_llm, pass_score=settings.critic_pass_score)
        else:
            ma_planner = MAHeuristicPlanner(tool_registry)
            ma_validator = FixedValidator(0.5)
            ma_writer = LLMWriter(llm)
            ma_critic = ScriptedCritic([{"score": 8.5, "issues": [], "resolved": []}],
                                       pass_score=settings.critic_pass_score)
        orchestrator = Orchestrator(
            planner=ma_planner, validator=ma_validator, writer=ma_writer, critic=ma_critic,
            registry=tool_registry, reports=report_store,
            pass_score=settings.critic_pass_score, max_rounds=settings.critic_max_rounds,
            adopt_threshold=settings.crag_correct_th,
        )
        app.state.orchestrator = orchestrator
        app.state.report_store = report_store


        app.state.auth_provider = auth_provider
        app.state.user_directory = user_directory
        app.state.audit_store = audit_store
        app.state.acl_service = acl_service
        app.state.quota_service = quota_service
        app.state.platform_monitor = platform_monitor
        app.state.sensitive_scanner = sensitive_scanner
        app.state.eval_store = eval_store
        app.state.eval_runner = eval_runner
        logger.info(
            "Kquery 后端已启动（provider=%s, vector_store=%s, llm=%s, rerank=%s, 向量=%d, BM25=%d）",
            provider, settings.vector_store, settings.llm_model, settings.rerank_model, store.total, bm25.total,
        )
        yield

    app = FastAPI(title="Kquery 智能 RAG", version="0.2.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        trace_id = new_trace_id()
        envelope = error_envelope(exc.code, exc.message, trace_id, 0)
        headers = {}
        if exc.retry_after is not None:
            headers["Retry-After"] = str(exc.retry_after)
        return JSONResponse(status_code=exc.http_status, content=envelope.model_dump(), headers=headers)

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        trace_id = new_trace_id()
        logger.exception("未处理异常 trace_id=%s", trace_id)
        envelope = error_envelope(ErrCode.INTERNAL_ERROR, "服务暂时不可用，请稍后再试", trace_id, 0)
        return JSONResponse(status_code=500, content=envelope.model_dump())

    @app.get("/health")
    def health() -> dict:
        return {"status": "up"}

    @app.get("/metrics")
    def metrics(request: Request) -> PlainTextResponse:
        """Prometheus 抓取端点（运营数据，不进用户响应）。"""
        return PlainTextResponse(
            request.app.state.prom.render(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    app.include_router(ask_api.router)
    app.include_router(knowledge_api.router)
    app.include_router(ingest_api.router)
    app.include_router(analysis_api.router)
    app.include_router(feedback_api.router)
    app.include_router(admin_api.router)
    app.include_router(sessions_api.router)
    app.include_router(graph_api.router)
    app.include_router(reports_api.router)
    app.include_router(security_api.router)
    return app


app = create_app()
