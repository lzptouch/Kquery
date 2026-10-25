"""Agent 运行器。

职责：
- 以事件队列驱动图执行（SSE 流式与同步共用一条路径）；
- 会话上下文的载入与快照回写（证据跨轮累积、上限治理）；
- 结果归一为问答契约（answer + evidence + verdict + agent 轨迹）；
- 熔断退出映射为"不完整的成功"（status=circuit_breaker，仍 200 返回）。
"""
import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterator

from langgraph.graph.state import CompiledStateGraph

from app.core.envelope import ApiError, ErrCode
from app.domain.schemas import Evidence
from app.services.agent.budget import (
    AgentBudget,
    budget_from_settings,
    estimate_cost_yuan,
    price_table,
)
from app.services.agent.checkpointer import thread_id_for
from app.services.agent.cost import get_ledger
from app.services.agent.react import ReActThinker
from app.services.agent.sessions import SessionStore
from app.services.agent.state import initial_state
from app.services.pipeline import (
    GENERAL_KNOWLEDGE_PROMPT,
    NO_INFO_ANSWER,
    PROMPT_TEMPLATE,
    REFUSAL_TEXT,
    build_evidence_block,
)
from app.services.validation.history import CRAG_KIND

logger = logging.getLogger(__name__)

_DONE = object()  # 事件流终止哨兵

# 参与"检索耗时/召回数"聚合的工具（calculator 不计入，否则统计无意义）
RETRIEVAL_TOOLS = frozenset({"vector_search", "graph_search", "image_search", "sql_search", "web_search"})

# 单次工具调用伴随的 LLM 开销估算（规划/反思各一次量级的保守值）。
# 与 query/orchestrator.py 的估算口径保持一致——
# 预算判定宁可保守（高估），也不要用乐观值把钱烧穿。
ESTIMATED_TOKENS_PER_TOOL_CALL = 800


def agent_prompt_builder(question: str, evidence_dicts: list[dict], *, parametric: bool = False) -> str:
    """answer 节点的提示词：只读最终入选证据，不读 plan/reflect 中间结果。

    图谱/图像证据需要额外指引：关系三元组可以综合推理直接与间接关系；
    "无相关内容"话术要么不出现、要么是答案的全部（不得在分析后追加，
    否则会被拒答归一误判）。

    parametric=True 时切换为通用知识兜底提示词，不注入证据块
    （低分无关片段会干扰回答；doc/analysis/通用知识兜底回答.md §6.2）。
    """
    if parametric:
        return GENERAL_KNOWLEDGE_PROMPT.format(question=question)
    # ACL 提示条目（"另有 N 条因权限未展示"）只是 UI 提示，不进提示词与证据编号
    evidence_dicts = [e for e in evidence_dicts if e.get("modality") != "acl_notice"]
    evidence = [
        Evidence(
            rank=i + 1,
            chunk_id=e.get("chunk_id", ""),
            score=e.get("score"),
            content=e.get("content", ""),
            source=e.get("source", ""),
            doc_id=e.get("doc_id", ""),
            filename=e.get("filename", e.get("doc_id", "")),
            source_route=e.get("source_route", "vector"),
        )
        for i, e in enumerate(evidence_dicts)
    ]
    prompt = PROMPT_TEMPLATE.format(
        no_info=NO_INFO_ANSWER,
        evidence_block=build_evidence_block(evidence),
        question=question,
    )
    modalities = {e.get("modality") for e in evidence_dicts}
    extras = []
    if "graph" in modalities:
        extras.append(
            '图谱证据以"起点 —关系→ 终点（支撑原文）"形式给出，'
            "你可以据此综合推理实体间的直接与间接（多跳链条）关系。"
        )
    if "image" in modalities:
        extras.append("图像证据的内容是图片的结构化描述，可据此回答图文相关问题。")
    if extras:
        extras.append(
            f'如果证据确实无法回答，则答案只能且只需是"{NO_INFO_ANSWER}"这一句话，'
            "不得在有效分析之后追加这句话。"
        )
        prompt += "\n\n补充说明：\n" + "\n".join(f"- {e}" for e in extras)
    return prompt


def _deadline_fallback_state(question: str, tenant_id: str, budget: AgentBudget) -> dict:
    """deadline 超时时的降级状态。

    刻意返回**拒答**而不是空答案：超时意味着"没拿到足够证据"，此时给出
    "知识库中没有足够证据回答该问题"是诚实的；返回空串会被前端当成渲染异常。
    """
    state = initial_state(question, tenant_id, [])
    state.update({
        "status": "budget_exhausted",
        "incomplete": True,
        "fuse_reason": "budget_deadline",
        "final_answer": REFUSAL_TEXT,
        "refusal": True,
        "budget": budget.snapshot(),
    })
    return state


class AgentRunner:
    def __init__(
        self,
        graph: CompiledStateGraph,
        sessions: SessionStore,
        metrics_store=None,
        top_k: int = 4,
        registry=None,  # ToolRegistry：ReAct 模式需要工具目录
        memory=None,  # LongTermMemory：租户级长期记忆
        audit=None,  # AuditStore
        scanner=None,  # SensitiveScanner（输出侧扫描）
        crag_router=None,  # CragRouter：与线性路径同一阈值口径，产 crag_branch
        history_store=None,  # ValidationHistoryStore：CRAG 轮次落库
        score_top_n: int = 2,  # 证据充分度聚合的 Top-N（与 crag_score_top_n 对齐）
        settings=None,  # Settings：读取三预算配置，可缺省（测试用默认预算）
        checkpointer=None,  # BaseCheckpointSaver：HITL 挂起与续跑的前提
        prom=None,  # PromMetrics：Agent 路径埋点，可缺省（测试）
        llm=None,  # LLM：仅用于读取 last_usage 做 token 埋点，不参与生成（图闭包持有）
        self_checker=None,  # HallucinationChecker：逐句自检
        executor=None,  # ThreadPoolExecutor：run/resume 工作线程来源（批次 1）
        run_permits: int = 16,  # Agent 并发闸门：在飞 run 上限，超出 429（批次 1）
    ):
        self._graph = graph
        self._sessions = sessions
        self._metrics = metrics_store
        self._top_k = top_k
        self._audit = audit
        self._scanner = scanner
        self._crag = crag_router
        self._history = history_store
        self._score_top_n = max(1, score_top_n)
        self._settings = settings
        # 按 agent_mode 决定是否启用 ReAct（None = 走既有计划式路径）
        mode = str(getattr(settings, "agent_mode", "plan_execute") or "plan_execute").lower()
        self._thinker = ReActThinker(llm) if (mode == "react" and llm is not None) else None
        self._registry = registry
        self._memory = memory
        self._checkpointer = checkpointer
        self._prom = prom
        self._llm = llm
        self._self_checker = self_checker
        # 成本折算所需的价格表与模型名（从 settings 一次性解析）
        self._prices = price_table(settings) if settings else {}
        self._model_name = str(getattr(settings, "llm_model", "") or "")
        # 批次 1：此前每次 run 新建 daemon 线程（无上限），高并发下线程数线性增长。
        # 改为固定池 + 许可闸门；池必须与工具池分离（图线程等待工具 future，同池死锁）。
        self._executor = executor or ThreadPoolExecutor(max_workers=8, thread_name_prefix="agent-run")
        self._permits = threading.Semaphore(max(1, int(run_permits)))

    # ---- 对外主入口：事件流 ----

    def run(self, *args, **kwargs) -> Iterator[tuple[str, dict]]:
        """带并发闸门的 run：在飞 run 超限时 429（快速失败，不排队）。"""
        if not self._permits.acquire(blocking=False):
            raise ApiError(
                ErrCode.RATE_LIMITED, "Agent 并发已达上限，请稍后重试",
                http_status=429, retry_after=5,
            )
        try:
            yield from self._run_events(*args, **kwargs)
        finally:
            self._permits.release()

    def _run_events(
        self,
        question: str,
        tenant_id: str,
        user_id: str = "anon",
        session_id: str | None = None,
        skip_retrieval: bool = False,
        doc_name_resolver=None,
        acl_filter=None,  # (doc_id) -> bool
        trace_id: str = "",
    ) -> Iterator[tuple[str, dict]]:
        """事件流：plan/tool_start/tool_end/reflect/iteration/token/fuse/_final。

         网关语义：token 事件被缓冲——答案经敏感词扫描后才放行
        （输出侧扫描必须在响应返回前完成，不能"先漏后遮"）。
        """
        events: queue.Queue = queue.Queue()
        started = time.monotonic()

        # 审计：请求事件（问题只存哈希）
        if self._audit and trace_id:
            self._audit.log_request(trace_id, tenant_id, user_id, question)

        # 会话上下文（跨轮证据累积）
        history: list[dict] = []
        carried: list[dict] = []
        if session_id:
            ctx = self._sessions.load_context(session_id, tenant_id, user_id)
            history = ctx["history"]
            carried = ctx["carried_evidence"]

        state = initial_state(question, tenant_id, history)
        state["skip_retrieval"] = skip_retrieval
        if carried:
            # 跨轮证据同样过 ACL（权限被收回后旧证据不可再用）
            if acl_filter:
                carried = [e for e in carried if acl_filter(e.get("doc_id", ""))]
            state["evidence"] = [{**e, "carried": True} for e in carried]
            state["last_evidence_count"] = len(carried)

        def emit(event: str, payload: dict) -> None:
            events.put((event, payload))

        # 每次 run 新建一份预算账本（预算是一次性的，不是会话级的）
        budget = (
            budget_from_settings(self._settings, tenant_id) if self._settings else AgentBudget()
        )

        # LangGraph 启用 checkpointer 后**必须**提供 thread_id，
        # 否则 invoke 直接抛 ValueError。这里按 `{session_id}:{turn}` 约定传入：
        # 有 session_id 时可断点续跑/回放，无 session_id（一次性问答）时
        # 回退到 trace_id —— 保证仍然可用，且不同请求不会互相覆盖状态。
        if session_id:
            try:
                session = self._sessions.get(session_id, tenant_id, user_id)
                turn = int(session.get("turn", 0)) + 1
            except Exception:  # noqa: BLE001 - 会话不可用不应阻断问答
                turn = 1
            thread_id = thread_id_for(session_id, turn)
        else:
            thread_id = f"trace:{trace_id}" if trace_id else None

        # 只有同时具备 checkpointer 与 thread_id 时才允许挂起——interrupt()
        # 没有 checkpointer 会直接抛错。条件性判定（低置信/拒答/全工具失败）在
        # answer 节点内做，这里只负责"是否具备挂起能力 + 阈值"。
        hitl_cfg = None
        # 仅在**有 session_id** 时启用：run_id 由 session_id 派生，才带得动
        # `{tenant}:{user}:` 前缀做归属校验；无 session 的 run 无法被安全地 resume。
        if (
            self._checkpointer is not None
            and thread_id
            and session_id
            and self._settings is not None
        ):
            if bool(getattr(self._settings, "agent_hitl_enabled", False)):
                hitl_cfg = {
                    "score_threshold": float(
                        getattr(self._settings, "agent_hitl_score_threshold", 0.5)
                    )
                }

        def work() -> None:
            try:
                config = {
                    "configurable": {
                        "emit": emit,
                        "acl_filter": acl_filter,
                        # 预算账本注入图，tool_node 每轮调用前检查
                        "budget": budget,
                        # HITL 配置（None = 不挂起）
                        "hitl": hitl_cfg,
                        # 独立校验器（None = 跳过校验，行为同改造前）
                        "verifier": (
                            self._self_checker
                            if (self._settings is not None
                                and getattr(self._settings, "agent_verify_enabled", False))
                            else None
                        ),
                        # 长期记忆提示（注入规划/思考的提示词）
                        "memory_hint": self._memory_hint(tenant_id),
                        # react 模式注入 thinker（None = 走 plan_execute）
                        "thinker": self._thinker,
                        "tool_catalog": (
                            self._registry.catalog() if self._registry is not None else []
                        ),
                        "max_verify_rounds": int(
                            getattr(self._settings, "agent_max_verify_rounds", 1)
                            if self._settings else 1
                        ),
                    }
                }
                if thread_id:
                    config["configurable"]["thread_id"] = thread_id
                final = self._graph.invoke(state, config=config)
                events.put(("_state", final))
            except Exception as exc:  # noqa: BLE001
                logger.exception("Agent 执行异常")
                events.put(("_error", exc))
            finally:
                events.put(_DONE)

        self._executor.submit(work)

        final_state: dict | None = None
        buffered_tokens: list[str] = []
        interrupted = False  # 图是否停在 interrupt() 上等人
        hitl_payload: dict = {}
        # deadline 兜底。此前 events.get() 无 timeout，上游 LLM 卡死
        # 会让请求线程永久挂起。这里给一个硬上限：到期即放弃等待，
        # 走降级作答——预算控制的目的是保护资源，不是无限期占着连接。
        wait_slice = 0.5
        while True:
            try:
                item = events.get(timeout=wait_slice)
            except queue.Empty:
                if budget is not None and budget.deadline_ms and budget.elapsed_ms >= budget.deadline_ms:
                    logger.warning("Agent deadline 超时（%dms），降级作答", budget.deadline_ms)
                    emit("fuse", {"reason": "budget_deadline", "iteration": 0})
                    final_state = _deadline_fallback_state(question, tenant_id, budget)
                    break
                continue
            if item is _DONE:
                break
            event, payload = item
            if event == "_state":
                final_state = payload
                # 图在 answer 节点内 interrupt() → invoke 返回的 dict 带
                # `__interrupt__`。此时**不能**当正常结果处理：没有 final_answer，
                # 必须让调用方知道"这是在等人"，否则前端只会看到一个空答案。
                pending = payload.get("__interrupt__") if isinstance(payload, dict) else None
                if pending:
                    interrupt_payload = (
                        pending[0].value if hasattr(pending[0], "value") else dict(pending[0])
                    )
                    interrupted = True
                    hitl_payload = interrupt_payload
                    yield "awaiting_human", {**interrupt_payload, "run_id": thread_id or ""}
                    continue
                continue
            if event == "_error":
                raise payload
            if event == "tool_end" and budget is not None:
                # 每次工具调用都伴随至少一次 LLM 调用（规划/反思），
                # 这里按"每工具调用约 N token"的既有口径估算（与
                # query/orchestrator.py 的 ESTIMATED_TOKENS_PER_CALL 同源）。
                phase_cost = estimate_cost_yuan(
                    ESTIMATED_TOKENS_PER_TOOL_CALL, self._prices, self._model_name
                )
                budget.settle_tokens(ESTIMATED_TOKENS_PER_TOOL_CALL, phase_cost)
                # 成本账单按阶段归集（think 阶段含规划+反思）
                get_ledger().record(
                    trace_id, tenant_id, "think", self._model_name,
                    ESTIMATED_TOKENS_PER_TOOL_CALL, phase_cost,
                    priced=self._model_name in self._prices,
                )
            if event == "token":
                # 缓冲 token：扫描前不放行（输出侧合规红线）
                buffered_tokens.append(payload.get("content", ""))
                continue
            if event == "tool_end":
                # Agent 路径埋点。工具结果与耗时是判断"Agent 好不好用"的
                # 第一手信号——此前这条路径一个指标都没有（大盘只有线性路径数据）。
                if self._prom is not None:
                    tool = str(payload.get("tool", "unknown"))
                    if payload.get("skipped"):
                        outcome = "skipped"
                    elif payload.get("error"):
                        outcome = "timeout" if "超时" in str(payload["error"]) else "error"
                    elif not payload.get("hit_count", 0):
                        outcome = "empty"
                    else:
                        outcome = "ok"
                    self._prom.agent_tool_calls_total.labels(
                        tool=tool, result=outcome
                    ).inc()
                    elapsed_ms = payload.get("elapsed_ms")
                    if isinstance(elapsed_ms, (int, float)):
                        self._prom.agent_tool_duration_seconds.labels(tool=tool).observe(
                            elapsed_ms / 1000.0
                        )
                if self._audit and trace_id:
                    # 审计：检索事件（召回数 / 可见数，差值即被权限过滤数）
                    retrieved = payload.get("hit_count", 0) + payload.get("acl_filtered", 0)
                    visible = payload.get("hit_count", 0)
                    self._audit.log_retrieve(trace_id, tenant_id, user_id, retrieved, visible)
            elif event == "fuse" and self._prom is not None:
                self._prom.agent_fuse_total.labels(
                    reason=str(payload.get("reason", "unknown"))
                ).inc()
            elif event == "reflect" and self._prom is not None:
                self._prom.agent_next_action_total.labels(
                    action=str(payload.get("next_action", "unknown"))
                ).inc()
            yield event, payload

        # 中断时短路——没有答案可扫描/自检/落快照，
        # 直接把"待人工"作为终态返回（快照留到 resume 后的那一轮再写）。
        if interrupted:
            result = {
                "answer": "",
                "evidence": [],
                "verdict": "awaiting_human",
                "trace_id": trace_id,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "awaiting_human": True,
                "run_id": thread_id or "",
                "hitl": hitl_payload,
                "budget": budget.snapshot(),
            }
            yield "confidence", {
                "verdict": "awaiting_human", "score": None,
                "unsupported_claims_count": 0, "regenerated": False,
                "unvalidated": True, "removed_claims": [],
                "confidence": None, "self_check": None,
                "budget": budget.snapshot(),
            }
            yield "_final", result
            return

        # ---- 敏感词扫描（输出侧，响应前）----
        raw_answer = (final_state or {}).get("final_answer", "")
        answer_source = (final_state or {}).get("answer_source", "")
        # ---- 幻觉自检----
        # Agent 路径此前完全不逐句校验，答案里出现无据陈述也照原样返回，
        # 只是把 confidence 硬编码为 unvalidated=True（对外等于"没检过"）。
        # 这里接上与线性路径同一套 HallucinationChecker：只**下调置信度**并留痕，
        # 不改写答案（Agent 的 answer_node 已流式发过 token，重写会前后不一致）。
        self_check_record = None
        confidence: float | None = None
        unsupported_count = 0
        # 通用知识兜底回答无据可核：自检会把全部陈述判为无据并把 confidence 压到 0
        if self._self_checker is not None and raw_answer and answer_source != "parametric":
            evidence_list = (final_state or {}).get("evidence", []) or []
            evidence_block = "\n".join(
                f"[{i + 1}]（来源：{e.get('source', '未知')}）\n{e.get('content', '')}"
                for i, e in enumerate(evidence_list[: self._top_k])
            )
            if evidence_block.strip():
                try:
                    check = self._self_checker.check(raw_answer, evidence_block)
                    unsupported_count = len(check.unsupported_claims)
                    confidence = max(0.0, 1.0 - 0.2 * unsupported_count)
                    self_check_record = self._self_checker.to_record(
                        check, raw_answer, unsupported_count, passed=(unsupported_count == 0)
                    )
                except Exception as exc:  # noqa: BLE001 - 自检失败不阻断答案
                    logger.warning("Agent 幻觉自检失败（标注未校验后继续）: %s", exc)
                    confidence = None
            else:
                confidence = None

        sensitive = {"safe": True, "categories": [], "action": "none"}
        if self._scanner and raw_answer:
            sensitive = self._scanner.scan(raw_answer, tenant_id)
            if sensitive["action"] == "refuse":
                # 命中最高密级：整体拒答（缓冲的 token 作废）
                if final_state is not None:
                    final_state["final_answer"] = ""
                    final_state["refusal"] = True
            elif sensitive["action"] == "mask":
                if final_state is not None:
                    final_state["final_answer"] = sensitive["masked_text"]

        # 扫描后放行（缓冲 token 以最终安全文本为准重新发射）
        final_answer = (final_state or {}).get("final_answer", "")
        if final_answer and buffered_tokens:
            chunk = 24
            for i in range(0, len(final_answer), chunk):
                yield "token", {"content": final_answer[i : i + chunk]}

        if self._audit and trace_id:
            self._audit.log_answer(
                trace_id, tenant_id, user_id, final_answer, len(sensitive["categories"])
            )

        # ---- 结果归一 ----
        result = self._build_result(
            final_state or {}, question, doc_name_resolver,
            int((time.monotonic() - started) * 1000), trace_id,
        )

        # ---- 可观测事件（此前 Agent 路径不发这三个，前端面板拿不到任何数据）----
        yield "retrieval_stats", result["retrieval"]
        if result.get("crag"):
            yield "crag_branch", {
                "branch": result["crag"]["branch"],
                "avg_score": result["evidence_score"],
                "round": 1,
            }
        yield "confidence", {
            "verdict": result["verdict"],
            "score": result["confidence"],
            "answer_source": result.get("answer_source", ""),
            "unsupported_claims_count": result["unsupported_claims_count"],
            "regenerated": result["regenerated"],
            # Agent 路径已接上同一套幻觉自检，不再硬编码 unvalidated
            "unvalidated": self_check_record is None,
            "removed_claims": [],
            "confidence": confidence,
            "self_check": (
                self_check_record.model_dump() if self_check_record is not None else None
            ),
            # 预算视图进响应，前端据此解释"为什么答得这么短/这么快"
            "budget": budget.snapshot(),
        }
        # 预算快照同时并入最终结果（/ask 聚合路径与落库快照都要能读到）
        result["budget"] = budget.snapshot()
        # 快照回写（同会话串行由 store 保证）
        if session_id and final_state is not None:
            try:
                self._sessions.save_turn(
                    session_id, tenant_id, user_id,
                    question, result["answer"], result["verdict"], final_state,
                    answer_source=result.get("answer_source", ""),
                )
            except Exception:  # noqa: BLE001
                logger.exception("会话快照写入失败（不影响本次应答）")
        # 提炼本次经验（写入侧）
        self._record_memory(tenant_id, final_state or {})
        # Agent 执行指标（熔断率是先于质量下降的预警信号，必须计入）
        if self._metrics is not None:
            # 请求级指标：Agent 路径与线性路径同口径计入 QPS/延迟/质量
            self._metrics.record_request(
                query=question,
                ok=result["verdict"] == "answered",
                latency_ms=float(result.get("elapsed_ms", 0) or 0),
                from_cache=False,
                llm_called=True,
            )
            self._metrics.record_quality(
                verdict=result["verdict"],
                confidence=result["confidence"],
                branch=None,
                regenerated=False,
                answer_source=result.get("answer_source", ""),
            )
            self._metrics.record_agent_run(
                iterations=final_state.get("iteration", 0) if final_state else 0,
                fused=(final_state or {}).get("status") == "circuit_breaker",
                fuse_reason=(final_state or {}).get("fuse_reason", ""),
                tool_calls=(final_state or {}).get("tool_calls", []),
                session_id=session_id or "",
            )
        # 终态与迭代轮数进 Prometheus（此前 Agent 路径零埋点）
        if self._prom is not None:
            state = final_state or {}
            mode = "agent"
            status = state.get("status") or ("refused" if result.get("refusal") else "completed")
            self._prom.agent_runs_total.labels(status=status, mode=mode).inc()
            self._prom.agent_iterations.labels(mode=mode).observe(
                float(state.get("iteration", 0))
            )
            usage = getattr(self._llm, "last_usage", None)
            if usage is not None and getattr(usage, "total", 0):
                self._prom.agent_tokens_total.labels(phase="answer").inc(int(usage.total))
                # answer 阶段真实用量入账单（这里能拿到供应商返回的 usage）
                answer_cost = estimate_cost_yuan(
                    int(usage.total), self._prices, self._model_name
                )
                get_ledger().record(
                    trace_id, tenant_id, "answer", self._model_name,
                    int(usage.total), answer_cost,
                    priced=(self._model_name in self._prices) and not getattr(usage, "estimated", True),
                )
        # F5：推荐追问（失败不伤主答案；必须在 done 之前发出）
        followups = self._generate_followups(result.get("answer", ""))
        if followups:
            result["followups"] = followups
            yield "followups", {"items": followups}
        yield "_final", result

    def _generate_followups(self, answer: str) -> list[str]:
        """生成推荐追问（**任何失败都吞掉**）。"""
        if not answer or not answer.strip():
            return []
        try:
            generator = getattr(self._llm, "generate_followups", None)
            if callable(generator):
                items = generator(answer, 3)
            else:
                from app.services.llm import generate_followups as _fallback

                items = _fallback(self._llm, answer, 3)
        except Exception as exc:  # noqa: BLE001 - 追问失败不影响主答案
            logger.warning("生成推荐追问失败（本次不展示追问）: %s", exc)
            return []
        if not isinstance(items, list):
            return []
        return [str(i).strip() for i in items if str(i).strip()][:3]

    # ---- TASK-117：长期记忆 ----

    def _memory_hint(self, tenant_id: str) -> str:
        """取该租户的经验并渲染成提示词片段（无记忆时返回空串）。

        失败一律返回空串：记忆是**增强**，拿不到不该影响问答。
        """
        if self._memory is None:
            return ""
        try:
            from app.services.agent.memory import LongTermMemory

            return LongTermMemory.render_hint(self._memory.recall(tenant_id))
        except Exception as exc:  # noqa: BLE001
            logger.debug("长期记忆读取失败（忽略）: %s", exc)
            return ""

    def _record_memory(self, tenant_id: str, state: dict) -> None:
        """从本次运行提炼经验（写入侧）。"""
        if self._memory is None:
            return
        try:
            from app.services.agent.memory import record_from_run

            record_from_run(self._memory, tenant_id, state)
        except Exception as exc:  # noqa: BLE001 - 记忆写入绝不阻断主流程
            logger.debug("长期记忆提炼失败（忽略）: %s", exc)

    # ---- 人在环 ----

    def interrupt_state(self, run_id: str) -> dict:
        """查询某个 run 是否停在人工介入点（供前端刷新后恢复现场）。

        checkpointer 不可用时一律返回未挂起——不假装有能力。
        """
        if self._checkpointer is None or not run_id:
            return {"awaiting": False, "next": [], "payload": {}}
        try:
            snapshot = self._graph.get_state({"configurable": {"thread_id": run_id}})
        except Exception as exc:  # noqa: BLE001
            logger.warning("查询中断状态失败 run_id=%s: %s", run_id, exc)
            return {"awaiting": False, "next": [], "payload": {}}
        pending = getattr(snapshot, "next", ()) or ()
        payload: dict = {}
        for task in getattr(snapshot, "tasks", ()) or ():
            itrs = getattr(task, "interrupts", ()) or ()
            if itrs:
                payload = itrs[0].value if hasattr(itrs[0], "value") else dict(itrs[0])
                break
        return {"awaiting": bool(pending), "next": list(pending), "payload": payload}

    def resume(self, *args, **kwargs) -> Iterator[tuple[str, dict]]:
        """带并发闸门的 resume：与 run 共用许可池（同上限口径）。"""
        if not self._permits.acquire(blocking=False):
            raise ApiError(
                ErrCode.RATE_LIMITED, "Agent 并发已达上限，请稍后重试",
                http_status=429, retry_after=5,
            )
        try:
            yield from self._resume_events(*args, **kwargs)
        finally:
            self._permits.release()

    def _resume_events(
        self,
        run_id: str,
        decision,
        doc_name_resolver=None,
        acl_filter=None,
        trace_id: str = "",
    ) -> Iterator[tuple[str, dict]]:
        """用 `Command(resume=decision)` 唤醒挂起的 run，并产出后续事件流。

        与 `run()` 同为事件生成器，调用方（`/ask/runs/{id}/resume`）可复用同一套
        消费代码。刻意不重跑会话/审计/敏感词那套前置逻辑——那些在第一段已经做过，
        重复执行会写重复的审计记录。
        """
        from langgraph.types import Command

        if self._checkpointer is None or not run_id:
            raise ValueError("未启用 checkpointer，无法恢复运行")

        events: queue.Queue = queue.Queue()
        started = time.monotonic()
        doc_name_resolver = doc_name_resolver or (lambda doc_id: doc_id)

        # 从 checkpoint 恢复预算计数：不恢复的话用户反复 resume 即可绕过 token 预算
        budget = self._restore_budget(run_id)
        hitl_cfg = None
        if self._settings is not None and bool(getattr(self._settings, "agent_hitl_enabled", False)):
            hitl_cfg = {
                "score_threshold": float(
                    getattr(self._settings, "agent_hitl_score_threshold", 0.5)
                )
            }

        def emit(event: str, payload: dict) -> None:
            events.put((event, payload))

        def work() -> None:
            try:
                config = {
                    "configurable": {
                        "emit": emit,
                        "acl_filter": acl_filter,
                        "budget": budget,
                        "hitl": hitl_cfg,
                        "thread_id": run_id,
                    }
                }
                events.put(("_state", self._graph.invoke(Command(resume=decision), config=config)))
            except Exception as exc:  # noqa: BLE001
                logger.exception("Agent 恢复执行异常 run_id=%s", run_id)
                events.put(("_error", exc))
            finally:
                events.put(_DONE)

        self._executor.submit(work)

        final_state: dict | None = None
        buffered: list[str] = []
        hitl_payload: dict = {}
        interrupted_again = False
        while True:
            try:
                item = events.get(timeout=0.5)
            except queue.Empty:
                if budget.deadline_ms and budget.elapsed_ms >= budget.deadline_ms:
                    final_state = _deadline_fallback_state("", "", budget)
                    break
                continue
            if item is _DONE:
                break
            event, payload = item
            if event == "_state":
                final_state = payload
                pending = payload.get("__interrupt__") if isinstance(payload, dict) else None
                if pending:
                    interrupted_again = True
                    hitl_payload = (
                        pending[0].value if hasattr(pending[0], "value") else dict(pending[0])
                    )
                    yield "awaiting_human", {**hitl_payload, "run_id": run_id}
                continue
            if event == "_error":
                raise payload
            if event == "tool_end" and budget is not None:
                budget.settle_tokens(
                    ESTIMATED_TOKENS_PER_TOOL_CALL,
                    estimate_cost_yuan(
                        ESTIMATED_TOKENS_PER_TOOL_CALL, self._prices, self._model_name
                    ),
                )
            if event == "token":
                buffered.append(payload.get("content", ""))
                continue
            yield event, payload

        if interrupted_again:
            yield "_final", {
                "answer": "",
                "evidence": [],
                "verdict": "awaiting_human",
                "trace_id": trace_id,
                "awaiting_human": True,
                "run_id": run_id,
                "hitl": hitl_payload,
                "budget": budget.snapshot(),
            }
            return

        state = final_state or {}
        answer = state.get("final_answer", "")
        if self._scanner and answer:
            scanned = self._scanner.scan(answer, state.get("tenant_id", ""))
            if scanned["action"] == "refuse":
                state["final_answer"] = ""
                state["refusal"] = True
            elif scanned["action"] == "mask":
                state["final_answer"] = scanned["masked_text"]
            answer = state.get("final_answer", "")
        if answer and buffered:
            for i in range(0, len(answer), 24):
                yield "token", {"content": answer[i : i + 24]}

        result = self._build_result(
            state, state.get("question", ""), doc_name_resolver,
            int((time.monotonic() - started) * 1000), trace_id,
        )
        result["budget"] = budget.snapshot()
        yield "retrieval_stats", result["retrieval"]
        yield "confidence", {
            "verdict": result["verdict"], "score": result["confidence"],
            "unsupported_claims_count": result["unsupported_claims_count"],
            "regenerated": result["regenerated"], "unvalidated": True,
            "removed_claims": [], "confidence": None, "self_check": None,
            "budget": budget.snapshot(),
        }
        result["resumed"] = True
        yield "_final", result

    def _restore_budget(self, run_id: str):
        """从 checkpoint 恢复预算计数（缺失时退化为新账本）。"""
        budget = (
            budget_from_settings(self._settings, "") if self._settings else AgentBudget()
        )
        try:
            snapshot = self._graph.get_state({"configurable": {"thread_id": run_id}})
            saved = (getattr(snapshot, "values", None) or {}).get("budget") or {}
        except Exception:  # noqa: BLE001
            saved = {}
        if saved:
            budget.tokens_used = int(saved.get("tokens_used", 0) or 0)
            budget.cost_used = float(saved.get("cost_used_yuan", 0.0) or 0.0)
        return budget

    # ---- 结果归一 ----

    def _retrieval_stats(self, state: dict, evidence_count: int) -> dict:
        """由工具调用记录聚合检索统计。

        Agent 路径此前完全不产 `retrieval`，导致前端 `RetrievalTracePanel`
        拿不到数据、指标 `rag_retrieval_stage_ms` 恒无数据。这里用 tool_calls
        的耗时与命中数聚合出与线性路径同构的形状，前端无需分支处理。
        """
        calls = state.get("tool_calls", []) or []
        retrieval_calls = [c for c in calls if c.get("tool_name") in RETRIEVAL_TOOLS]
        keyword = [c for c in retrieval_calls if c.get("tool_name") == "graph_search"]
        vector = [c for c in retrieval_calls if c.get("tool_name") != "graph_search"]
        return {
            "from_cache": False,
            "reranked": True,  # Agent 路径的精排在工具内部完成，不额外降级标记
            "keyword_candidates": sum(int(c.get("result_count", 0) or 0) for c in keyword),
            "vector_candidates": sum(int(c.get("result_count", 0) or 0) for c in vector),
            "fused_count": evidence_count,
            "reranked_count": evidence_count,
            "into_context_count": evidence_count,
            "keyword_ms": sum(int(c.get("elapsed_ms", 0) or 0) for c in keyword),
            "vector_ms": sum(int(c.get("elapsed_ms", 0) or 0) for c in vector),
            "fusion_ms": 0,  # 融合在工具内部完成，无独立耗时口径
            "rerank_ms": 0,
            "latency_ms": {
                "keyword": sum(int(c.get("elapsed_ms", 0) or 0) for c in keyword),
                "vector": sum(int(c.get("elapsed_ms", 0) or 0) for c in vector),
                "fusion": 0,
                "rerank": 0,
            },
            "candidates": {
                "keyword": sum(int(c.get("result_count", 0) or 0) for c in keyword),
                "vector": sum(int(c.get("result_count", 0) or 0) for c in vector),
                "fused": evidence_count,
                "reranked": evidence_count,
                "into_context": evidence_count,
            },
            "tool_calls": len(retrieval_calls),
            "tool_errors": sum(1 for c in retrieval_calls if c.get("error")),
            "acl_filtered": sum(
                int(e.get("acl_filtered_count", 0) or 0)
                for e in state.get("evidence", [])
                if e.get("modality") == "acl_notice"
            ),
        }

    def _crag_payload(self, score: float, evidence_count: int, trace_id: str) -> dict | None:
        """用与线性路径**同一** CRAG 阈值口径给 Agent 路径的证据定级。

        分支只用于前端检索面板高亮，不改判答/拒答——拒答判定在 answer 节点
        已由 evidence 是否为空完成，这里再拒一次会变成两套拒答口径。
        """
        if self._crag is None:
            return None
        branch = self._crag.decide(score) if evidence_count else "incorrect"
        round_data = {
            "round": 1,
            "branch": branch,
            "avg_score": round(score, 4),
            "evidence_count": evidence_count,
            "refined_query": "",
        }
        if self._history is not None and trace_id:
            self._history.append(
                CRAG_KIND,
                {"trace_id": trace_id, "round": 1, "branch": branch,
                 "avg_score": round(score, 4), "evidence_count": evidence_count},
            )
        return {"branch": branch, "rounds": [round_data], "source": "agent"}

    def _build_result(
        self, state: dict, question: str, doc_name_resolver, elapsed_ms: int, trace_id: str = ""
    ) -> dict:
        answer = state.get("final_answer", "")
        answer_source = state.get("answer_source", "")
        all_evidence = state.get("evidence", [])
        # ACL 提示条目只用于 UI 提示，不进证据编号（安全红线：只报数量）
        acl_filtered = sum(
            e.get("acl_filtered_count", 0)
            for e in all_evidence
            if e.get("modality") == "acl_notice"
        )
        evidence_dicts = [e for e in all_evidence if e.get("modality") != "acl_notice"]
        status = state.get("status", "completed")
        incomplete = state.get("incomplete", False)

        # 证据排序编号：按分数降序取 top_k，rank 从 1 连续
        ranked = sorted(evidence_dicts, key=lambda e: e.get("score", 0.0), reverse=True)
        if doc_name_resolver:
            for e in ranked:
                if e.get("doc_id") and not e.get("filename"):
                    e["filename"] = doc_name_resolver(e["doc_id"])
        top = ranked[: self._top_k]
        evidence = [
            Evidence(
                rank=i + 1,
                chunk_id=e.get("chunk_id", ""),
                score=e.get("score"),
                content=e.get("content", ""),
                source=e.get("source", ""),
                doc_id=e.get("doc_id", ""),
                filename=e.get("filename", e.get("doc_id", "")),
                source_route=e.get("source_route", "vector"),
                modality=e.get("modality", "text"),
                uri=e.get("uri", ""),
                source_chunks=e.get("source_chunks", []),
            ).model_dump()  # SSE 路径走 json.dumps，必须是纯 dict
            for i, e in enumerate(top)
        ]

        # 拒答归一：无证据 / 提示词级拒答 / 敏感词整体拒答
        # 通用知识兜底豁免：parametric 答案不以知识库证据为依据，不参与拒答归一
        refusal = (
            state.get("refusal", False) or answer.strip().startswith(NO_INFO_ANSWER)
        ) and answer_source != "parametric"
        if refusal:
            # ⚠️ 必须回写 state["refusal"]。此前只在"证据为空"时由 answer_node 置位，
            # 而**更常见的**拒答形态是"检索到了候选但反思判定不相关"——那种情况下
            # 模型会输出 NO_INFO_ANSWER 开头的话，verdict 是 refused，但 state 里的
            # refusal 仍是 False。后果有两处：
            #   1. 会话快照存了自相矛盾的数据（verdict=refused + refusal=False）；
            #   2. 长期记忆提炼读 refusal 判断"知识库答不上来"，永远读到 False
            #      → 记忆条目恒为 0（由线上探针发现）。
            state["refusal"] = True
            verdict, answer_out, confidence = "refused", REFUSAL_TEXT, 0.0
            refusal_reason = "no_content"
            evidence = []
        else:
            verdict = "answered"
            answer_out = answer
            if answer_source == "parametric":
                # 兜底回答固定置信度（明确低于有据回答的不确定性），不经自检评分
                confidence = round(
                    float(getattr(self._settings, "parametric_fallback_confidence", 0.3)
                          if self._settings is not None else 0.3),
                    4,
                )
            else:
                confidence = round(min(0.95, state.get("evidence_score", 0.5)), 4)
            refusal_reason = ""

        # 兜底回答不引用证据：前端不展示无关片段（acl_filtered 计数保留）
        if answer_source == "parametric":
            evidence = []

        # 证据充分度：Top-N 均值（与线性路径 crag_score_top_n 同口径）
        top_scores = sorted(
            (e.get("score") for e in top if e.get("score") is not None), reverse=True
        )[: self._score_top_n]
        avg_score = sum(top_scores) / len(top_scores) if top_scores else 0.0

        agent_payload = {
            "plan": state.get("plan", []),
            "tool_calls": state.get("tool_calls", []),
            "tool_errors": state.get("tool_errors", []),
            "iterations": state.get("iteration", 0),
            "max_iterations": state.get("max_iterations", 0),
            "status": status,
            "incomplete": incomplete,
            "fuse_reason": state.get("fuse_reason", ""),
            "degraded": state.get("degraded", False),
        }
        return {
            "answer": answer_out,
            "evidence": evidence,
            "verdict": verdict,
            "refusal_reason": refusal_reason,
            "answer_source": answer_source,
            "confidence": confidence,
            "incomplete": incomplete,
            "fuse_reason": state.get("fuse_reason", ""),
            "agent": agent_payload,
            "elapsed_ms": elapsed_ms,
            "acl_filtered": acl_filtered,
            "from_cache": False,
            "retrieval": self._retrieval_stats(state, len(evidence)),
            "crag": self._crag_payload(avg_score, len(evidence), trace_id),
            "evidence_score": round(avg_score, 4),
            "regenerated": False,
            "unsupported_claims_count": 0,
        }
