"""Kquery 智能 RAG 后端（阶段 2：混合检索 + 工程化）。

组装顺序：配置 → Embedding/LLM/Reranker → 向量索引 + BM25 索引 → 混合检索
→ 缓存 / 限流 / 指标 / 运行时配置 → 知识库服务 → 流水线。
索引文件损坏时拒绝启动，不允许以坏索引对外服务。
"""
import logging
import time
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
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
from app.core.envelope import ApiError, ErrCode, error_envelope, validate_code_uniqueness
from app.core.metrics import MetricsStore, PromMetrics
from app.core.otel import setup_otel, shutdown_otel
from app.core.shared_state import SharedState
from app.services.agent.memory import LongTermMemory
from app.core.tasks import BackgroundQueue
from app.core.tracing import get_trace_id, new_trace_id, reset_trace_id, set_trace_id
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
from app.services.agent.checkpointer import build_checkpointer, close_checkpointer
from app.services.agent.graph import build_agent_graph
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
from app.services.graph.builder import GraphBuilder
from app.services.graph.extractor import LLMExtractor, ScriptedExtractor
from app.services.graph.images import ImageIndex, MockCaptioner, QwenVlCaptioner
from app.services.graph.query import GraphQueryService
from app.services.graph.resolver import EntityResolver
from app.services.graph.sqlite_store import build_graph_store
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
from app.services.security.auth import UserDirectory, build_auth_provider
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
from app.services.validation.history import ValidationHistoryStore
from app.services.validation.selfcheck import HallucinationChecker
from app.core.logging_setup import setup_logging
from app.services.agent.tools_extra import (
    DateTimeCalcTool,
    ListDocumentsTool,
    SqlSchemaTool,
)
from app.services.vectorstore import build_vector_store

logger = logging.getLogger(__name__)


def _app_version() -> str:
    """应用版本号：以 pyproject.toml 为唯一来源（TASK-184），未安装为包时兜底。"""
    try:
        from importlib.metadata import version
        return version("kquery-backend")
    except Exception:  # noqa: BLE001 - 含 PackageNotFoundError 与元数据缺失
        return "0.1.0"


def _elapsed(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 2)


def create_app() -> FastAPI:
    # 错误码编号必须全局唯一（IntEnum 重复值会产生别名，按码分流失效）
    validate_code_uniqueness()
    settings = get_settings()
    # TASK-133：结构化日志。文本形态保留给本地开发（看 JSON 太痛苦），
    # 生产设 LOG_FORMAT=json 后逐行 JSON + DEBUG 采样 + PII 脱敏。
    setup_logging(
        level=getattr(settings, "log_level", "INFO"),
        log_format=getattr(settings, "log_format", "text"),
        debug_sample_rate=float(getattr(settings, "log_debug_sample_rate", 0.1)),
    )

    # TASK-132：OTel（未启用/未配置 endpoint 时静默降级为 NoOp）
    setup_otel(settings)

    # 启动期配置校验：缺项直接拒绝启动，而不是让故障延后到运行期
    issues = settings.validate_startup()
    if issues:
        detail = "\n".join(
            f"  - {i['field']}: {i['reason']}（{i['hint']}）" for i in issues
        )
        raise RuntimeError(f"启动期配置校验未通过，共 {len(issues)} 项：\n{detail}")

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
            settings=settings,
        )

        # 工程设施
        config_store = RetrievalConfigStore(
            settings.data_dir / "retrieval-config.json",
            apply_delay_s=settings.retrieval_config_apply_delay_s,
            settings=settings,
        )
        cache = build_cache(
            settings=settings,
            embeddings=embeddings,
            tenant_id=settings.default_tenant_id,
        )
        limiter = build_limiter()
        prom = PromMetrics()
        metrics_store = MetricsStore()

        # 阶段 3：DocStore（父块/表格/媒体资源）+ 解析路由 + 任务队列
        docstore = DocStore(settings.data_dir / "docstore.json")
        ocr_engine = build_ocr_engine(provider, settings.dashscope_api_key, settings.llm_max_retries)
        transcriber = build_transcriber(
            provider, settings.dashscope_api_key,
            model=settings.transcribe_model, max_retries=settings.llm_max_retries,
            max_chunk_s=settings.audio_max_chunk_s,
            min_sample_rate=settings.audio_min_sample_rate,
            split_on_silence=settings.audio_split_on_silence,
            max_workers=settings.audio_max_workers,
        )
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
        # DEF-R-2 / P0-7：hybrid 装配早于 knowledge，回填版本查询（召回侧按当前版本
        # 过滤历史版本块）；knowledge 存在前 _version_of 为 None，不过滤
        hybrid._version_of = knowledge.version_of
        # 后置钩子先占位：图存储/构建器在下方（阶段 7 段）才装配，
        # 这里用一个可变容器晚绑定，避免调整装配顺序牵连入库服务
        post_index_box: dict = {}
        task_manager = TaskManager(
            knowledge, settings.data_dir / "tasks.json", settings.data_dir / "ingest",
            post_index=lambda record, parsed: post_index_box["hook"](record, parsed),
        )

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
            self_checker=HallucinationChecker(
                llm, model_version=f"{settings.llm_model}"
            ) if settings.selfcheck_enabled else None,
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

        user_directory = UserDirectory(
            settings.users_file,
            initial=_json9.loads(settings.auth_users_json) if settings.auth_users_json else None,
        )
        auth_provider = build_auth_provider(
            settings.auth_enabled,
            _json9.loads(settings.auth_tokens_json) if settings.auth_tokens_json else {},
            jwt_settings=settings,
        )
        audit_store = AuditStore(settings.data_dir / "audit" / "audit.jsonl")
        acl_service = ACLService(settings.data_dir / "acl.json", user_directory)
        # DEF-R-1：pipeline 装配早于 acl_service，回填版本提供者（bind_principal 的
        # clone 会复制 __dict__，故请求级副本也能拿到最新版本）
        pipeline._acl_version = lambda: acl_service.version
        # TASK-142：ACL 下推所需的依赖（bind_principal 会据此按主体构造可见清单）
        pipeline._acl_service = acl_service
        pipeline._knowledge = knowledge
        quota_service = SlidingWindowQuota()
        quota_service.set_policy(
            settings.default_tenant_id,
            settings.quota_per_minute,
            settings.quota_per_day,
            token_per_day=settings.quota_token_per_day,
        )
        # TASK-120：token 用量结算。quota_service 晚于 pipeline 构造，此处回填
        # （与 _acl_version 同一模式；bind_principal 的 clone 会复制 __dict__）
        pipeline._quota = quota_service
        # 平台侧配额（账号级上游约束）：模型客户端上报，网关读取
        platform_monitor = PlatformQuotaMonitor(
            per_day=settings.platform_quota_per_day,
            warn_ratio=settings.platform_quota_warn_ratio,
        )
        install_monitor(platform_monitor)
        sensitive_scanner = SensitiveScanner(settings.data_dir / "sensitive-words.json")
        eval_store = EvalStore(settings.data_dir / "eval")
        # 阶段 5 校验历史（CRAG 轮次 / 自检明细）落库
        validation_history = ValidationHistoryStore(settings.data_dir / "validation-history")
        # 护栏回填到**线性路径**：此前审计与敏感词只挂 Agent 路径，默认问答不产生任何痕迹
        pipeline.attach_security(
            audit=audit_store,
            scanner=sensitive_scanner if settings.sensitive_scan_enabled else None,
            history_store=validation_history,
        )

        def _eval_ask(question: str, tenant_id: str | None = None) -> dict:
            """评测走**线性路径**：阶段 5 的 faithfulness 自检与 CRAG 只在线性路径产出，
            走 Agent 路径只拿得到最终答案，两种口径的 faithfulness 不可比（§7.3）。

            带 tenant_id 参数是为了按调用主体的租户检索——默认租户之外的文档
            不该被计入评测（EvalRunner 用签名探测兼容既有单参注入）。
            """
            result = pipeline.ask(
                question, tenant_id or settings.default_tenant_id, get_trace_id()
            )
            return {
                "answer": result.answer,
                "evidence": [e.model_dump() for e in result.evidence],
                "verdict": result.verdict,
                "self_check": result.self_check.model_dump() if result.self_check else None,
                "crag": result.crag,
            }

        eval_runner = EvalRunner(
            eval_store, _eval_ask,
            model_version=settings.llm_model,
            code_version=settings.code_version or None,  # 留空则读 KQUERY_CODE_VERSION → git → unknown
            path="linear",
            default_tenant_id=settings.default_tenant_id,
        )

        # 阶段 6：Agentic RAG（LangGraph 状态机 + 工具注册 + 会话存档）
        agent_llm = build_llm(
            provider=provider,
            api_key=settings.dashscope_api_key,
            model=settings.agent_planner_model,
            temperature=0,
            max_retries=settings.llm_max_retries,
            timeout_s=settings.llm_timeout_s,
        )
        tool_registry = ToolRegistry(timeout_s=settings.agent_tool_timeout_s)
        tool_registry.register(VectorSearchTool(hybrid, config_store.get_active()))
        tool_registry.register(CalculatorTool())
        # TASK-108：补"库里有什么"与日期推理两个能力盲区
        tool_registry.register(ListDocumentsTool(knowledge))
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

        # 阶段 7：GraphRAG（图存储 + 消歧 + 构建 + 查询）与多模态（图像描述向量统一空间）
        # TASK-180：默认 SQLite 增量持久化（旧 JSON 会自动迁移并备份）
        graph_store = build_graph_store(settings, settings.data_dir)
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
        image_index = ImageIndex(
            captioner, embeddings, store, settings.data_dir / "image-index.json"
        )
        if settings.graph_enabled:
            tool_registry.register(GraphSearchTool(graph_query))
            tool_registry.register(ImageSearchTool(image_index))

        # 图谱构建与图像索引改为**入库联动**（stage-7 §5.1）：此前两者只能靠
        # 手动打 /graph/build 与 /media/index，文档入库后图谱长期是空的，
        # "图谱检索"这个能力等于形同虚设。
        def _post_index(record, parsed) -> dict:
            stats: dict = {"graph": {}, "images": {}}
            if not settings.graph_enabled:
                return stats
            chunks = [c for c in store.all_chunks() if c.doc_id == record.doc_id]
            if chunks:
                stats["graph"] = graph_builder.build_from_chunks(
                    chunks, rebuild_doc_id=record.doc_id
                )
            assets = getattr(parsed, "assets", []) or []
            indexed, skipped = 0, 0
            for asset in assets:
                image_path = getattr(asset, "path", "") or ""
                if not image_path or not Path(image_path).exists():
                    skipped += 1
                    continue
                if image_index.index_image(
                    Path(image_path), asset.asset_id,
                    doc_id=record.doc_id, page=getattr(asset, "page", 0),
                    tenant_id=record.tenant_id,
                ) is not None:
                    indexed += 1
                else:
                    skipped += 1
            stats["images"] = {"indexed": indexed, "skipped": skipped, "total": len(assets)}
            return stats

        post_index_box["hook"] = _post_index
        # 同步上传路径同样要触发（否则只有异步任务能建出图谱）
        knowledge.set_post_index(_post_index)

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
        # TASK-115：checkpointer 是断点续跑 / 多轮状态继承 / 完整回放 / HITL 的前提。
        # 不可用时返回 None（降级为不启用续跑），Agent 仍能正常跑。
        agent_checkpointer = build_checkpointer(settings, settings.data_dir)
        app.state.agent_checkpointer = agent_checkpointer
        agent_graph = build_agent_graph(
            registry=tool_registry,
            planner=planner,
            reflector=reflector,
            answer_llm=llm,
            prompt_builder=agent_prompt_builder,
            no_progress_limit=settings.agent_no_progress_limit,
            checkpointer=agent_checkpointer,
        )
        session_store = SessionStore(
            settings.data_dir / "sessions", ttl_s=settings.agent_session_ttl_s
        )
        app.state.agent_runner = AgentRunner(
            agent_graph, session_store, metrics_store, top_k=settings.top_k,
            registry=tool_registry,  # TASK-101：ReAct 模式的工具目录来源
            # 审计是合规底线，不应由 acl_enabled 决定（DEF-S-18）：无条件挂载
            audit=audit_store,
            scanner=sensitive_scanner if settings.sensitive_scan_enabled else None,
            crag_router=pipeline._crag,
            history_store=validation_history,
            score_top_n=settings.crag_score_top_n,
            settings=settings,  # TASK-121：读取三预算配置
            checkpointer=agent_checkpointer,  # TASK-124：HITL 挂起的前提
            prom=prom,  # TASK-131：Agent 路径埋点
            llm=llm,  # 仅用于 token 用量埋点
            # TASK-112：Agent 路径接上与线性路径同一套幻觉自检
            self_checker=pipeline._self_checker,
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
        # TASK-181：报告生成走有界后台队列（此前每次提交裸起一个 daemon 线程：
        # 无上限、无背压、进程退出时被掐断导致报告写一半）
        report_queue = BackgroundQueue(
            name="reports",
            workers=settings.report_workers,
            queue_size=settings.report_queue_size,
        )
        orchestrator = Orchestrator(
            planner=ma_planner, validator=ma_validator, writer=ma_writer, critic=ma_critic,
            registry=tool_registry, reports=report_store,
            pass_score=settings.critic_pass_score, max_rounds=settings.critic_max_rounds,
            adopt_threshold=settings.crag_correct_th,
            background=report_queue,
        )
        app.state.orchestrator = orchestrator
        app.state.report_store = report_store
        app.state.report_queue = report_queue

        # TASK-165：共享状态门面（Redis 不可用时自动降级为进程内）
        shared_state = SharedState(settings.redis_url, settings.redis_key_prefix)
        app.state.shared_state = shared_state
        # TASK-117：租户级长期记忆（把踩过的坑沉淀下来，注入后续规划）
        long_term_memory = (
            LongTermMemory(settings.data_dir / "memory" / "memory.jsonl",
                           max_per_tenant=settings.memory_max_per_tenant)
            if settings.memory_enabled else None
        )
        app.state.long_term_memory = long_term_memory
        # TASK-117：AgentRunner 装配早于记忆创建，此处回填（与 _acl_version 同模式）
        app.state.agent_runner._memory = long_term_memory
        logger.info("共享状态后端=%s；长期记忆=%s",
                    shared_state.backend, "on" if long_term_memory else "off")


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
        try:
            yield
        finally:
            # TASK-115：释放 checkpointer 持有的 sqlite 连接。
            # 不关闭的话，Windows 上临时目录无法清理（WinError 32 文件占用），
            # 且连接会一直挂在进程里阻止 WAL 落盘。
            close_checkpointer(agent_checkpointer)
            # TASK-180：图存储的 sqlite 连接同理——不关会导致临时目录无法清理
            # TASK-181：优雅停机——让 worker 跑完**当前**任务再退出，
            # 而不是像 daemon 那样被直接掐断（报告/入库都会落盘，半截文件比慢几秒糟得多）
            report_queue.shutdown(wait=True, timeout=5.0)
            # TASK-132：flush 未上报的 span（BatchSpanProcessor 有缓冲）
            shutdown_otel()
            for closer in (
                lambda: store.close(),
                # 内存实现无 close（接口未要求）；hasattr 兜一下，避免降级路径报错
                lambda: graph_store.close() if hasattr(graph_store, "close") else None,
            ):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass

    app = FastAPI(title="Kquery 智能 RAG", version=_app_version(), lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        # 复用请求入口的 trace_id：用户从前端复制的追踪号必须能反查到本次请求日志
        trace_id = get_trace_id()
        envelope = error_envelope(exc.code, exc.message, trace_id, 0)
        headers = {}
        if exc.retry_after is not None:
            headers["Retry-After"] = str(exc.retry_after)
        return JSONResponse(status_code=exc.http_status, content=envelope.model_dump(), headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        """把 FastAPI 的校验错误翻译成统一信封。

        不加这个处理器时，参数缺失会返回 FastAPI 默认的 `{"detail": [...]}`，
        与其余端点的 `{"status","data","error"}` 结构不一致。后果很实际：
        前端 `request()` 统一读 `body.error`，遇到这种响应会拿到 undefined，
        于是页面上显示成 "undefined" 或者干脆空白——而用户填错一个字段
        本该看到"缺少 question"这种能照着改的提示。
        """
        trace_id = get_trace_id()
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        field = loc.split(".")[-1] if loc else "请求"
        message = f"{field} {first.get('msg', '参数不合法')}".strip()
        logger.info("请求参数校验失败 trace_id=%s path=%s detail=%s",
                    trace_id, request.url.path, first.get("msg"))
        envelope = error_envelope(ErrCode.INVALID_INPUT, message, trace_id, 0)
        return JSONResponse(status_code=400, content=envelope.model_dump())

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """404/405 等也走统一信封，否则前端同样读不到 error.message。"""
        trace_id = get_trace_id()
        code = {
            404: ErrCode.DOCUMENT_NOT_FOUND,
            405: ErrCode.INVALID_REQUEST,
            413: ErrCode.FILE_TOO_LARGE,
        }.get(exc.status_code, ErrCode.INVALID_REQUEST)
        envelope = error_envelope(code, str(exc.detail), trace_id, 0)
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope.model_dump(),
            headers=getattr(exc, "headers", None) or {},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        trace_id = get_trace_id()
        logger.exception("未处理异常 trace_id=%s", trace_id)
        envelope = error_envelope(ErrCode.INTERNAL_ERROR, "服务暂时不可用，请稍后再试", trace_id, 0)
        return JSONResponse(status_code=500, content=envelope.model_dump())

    @app.middleware("http")
    async def trace_middleware(request: Request, call_next):
        """每个请求一个 trace_id（入站优先复用网关传来的，便于跨服务串联）。"""
        incoming = request.headers.get("X-Trace-Id", "").strip() or new_trace_id()
        token = set_trace_id(incoming)
        request.state.trace_id = incoming
        request.state.started_ms = None
        try:
            response = await call_next(request)
        finally:
            response_trace = getattr(request.state, "trace_id", incoming)
            reset_trace_id(token)
        response.headers["X-Trace-Id"] = response_trace
        return response

    @app.get("/health")
    def health() -> dict:
        return {"status": "up"}

    @app.get("/livez")
    def livez() -> PlainTextResponse:
        """存活探针：只证明进程在跑，**绝不查依赖**（依赖抖动会误杀 Pod）。"""
        return PlainTextResponse("ok", media_type="text/plain; charset=utf-8")

    @app.get("/readyz")
    def readyz(request: Request, response: Response) -> JSONResponse:
        """就绪探针：判断能否接流量。失败 → 摘流，不抛错（由 K8s 读状态码）。

        语义边界：**"索引为空"不等于"未就绪"**。全新部署没有任何文档时索引
        本来就是 0，若据此返回 503，Service 会永远没有 endpoints，
        请求根本进不来，也就永远没人能上传第一份文档——死锁。
        真正该摘流的是"依赖不可达/集合没加载起来"，那种情况 store 为 None。
        """
        store = getattr(request.app.state, "store", None)
        total = int(getattr(store, "total", 0) or 0)
        ready = store is not None
        body = {
            "status": "ready" if ready else "not_ready",
            "index_chunks": total,
            # 空索引单独标注：可服务但答不出东西，与"未就绪"是两回事，
            # 值班同学看这一行就知道该不该去查数据而不是查服务
            "empty_index": ready and total == 0,
        }
        if ready and total == 0:
            logger.warning("就绪但索引为空：服务可接流量，检索会返回空结果")
        return JSONResponse(
            status_code=200 if ready else 503,
            content=body,
        )

    @app.get("/healthz")
    def healthz(request: Request) -> JSONResponse:
        """完整健康报告：逐个依赖的状态、耗时、错误摘要。不参与流量路由。"""
        state = request.app.state
        settings = state.settings
        deps: list[dict] = []

        def probe(name: str, fn) -> None:
            t0 = time.perf_counter()
            try:
                detail = fn() or {}
                deps.append({"name": name, "status": "up", "latency_ms": _elapsed(t0), **detail})
            except Exception as exc:  # noqa: BLE001
                deps.append(
                    {
                        "name": name,
                        "status": "down",
                        "latency_ms": _elapsed(t0),
                        "error": str(exc)[:200],
                    }
                )

        probe("vector_store", lambda: {"chunks": int(getattr(state.store, "total", 0) or 0)})
        probe("bm25_index", lambda: {"chunks": int(getattr(state.bm25, "total", 0) or 0)})
        probe("graph_store", lambda: state.graph_store.stats())
        # TASK-165/117：探针暴露当前后端，避免"以为在用 Redis 其实已降级"
        probe("shared_state", lambda: state.shared_state.snapshot())
        probe("long_term_memory", lambda: state.long_term_memory.stats()
              if state.long_term_memory else {"enabled": False})
        probe(
            "parser_router",
            lambda: {"supported": sorted(state.parser_router.supported_extensions())},
        )
        probe("provider", lambda: {"provider": settings.resolved_provider, "llm": settings.llm_model})
        probe(
            "platform_quota",
            lambda: {"exhausted": state.platform_monitor.exhausted()},
        )

        down = [d["name"] for d in deps if d["status"] == "down"]
        return JSONResponse(
            status_code=200 if not down else 503,
            content={
                "status": "healthy" if not down else "degraded",
                "down": down,
                "dependencies": deps,
                "data_version": int(getattr(state.knowledge, "data_version", 0) or 0),
            },
        )

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
