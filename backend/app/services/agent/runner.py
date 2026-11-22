"""Agent 运行器。

职责：
- 以事件队列驱动图执行（SSE 流式与同步共用一条路径）；
- 会话上下文的载入与快照回写（证据跨轮累积、上限治理）；
- 结果归一为问答契约（answer + evidence + verdict + agent 轨迹）；
- 熔断退出映射为"不完整的成功"（status=circuit_breaker，仍 200 返回）。
"""
import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
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
from app.core.logging_setup import get_logger

logger = get_logger(__name__)
def tool_fallbacks(settings) -> dict[str, str]:
    """解析工具空结果降级映射（`{"原工具": "兜底工具"}`）。

    配错不阻断运行：解析失败按空映射处理（没有降级 = 旧行为）。
    """
    raw = str(getattr(settings, "agent_tool_fallbacks_json", "") or "")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("工具降级映射解析失败（按无降级处理）: %s", raw[:80])
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if str(k) and str(v)}


_DONE = object()  # 事件流终止哨兵

# 参与"检索耗时/召回数"聚合的工具（calculator 不计入，否则统计无意义）
RETRIEVAL_TOOLS = frozenset({"vector_search", "graph_search", "image_search", "sql_search", "web_search"})

# 单次工具调用伴随的 LLM 开销估算（规划/反思各一次量级的保守值）。
# 与 query/orchestrator.py 的估算口径保持一致——
# 预算判定宁可保守（高估），也不要用乐观值把钱烧穿。
ESTIMATED_TOKENS_PER_TOOL_CALL = 800


def agent_prompt_builder(
    question: str,
    evidence_dicts: list[dict],
    *,
    parametric: bool = False,
    token_budget: int = 0,
    parent_lookup=None,
    dropped_out: list[dict] | None = None,
    memory: str = "",
    history_summary: str = "",
    history_recent: list[dict] | None = None,
    structured: bool = False,
) -> str:
    """answer 节点的提示词：只读最终入选证据，不读 plan/reflect 中间结果。

    图谱/图像证据需要额外指引：关系三元组可以综合推理直接与间接关系；
    "无相关内容"话术要么不出现、要么是答案的全部（不得在分析后追加，
    否则会被拒答归一误判）。

    parametric=True 时切换为通用知识兜底提示词，不注入证据块
    （低分无关片段会干扰回答；doc/analysis/通用知识兜底回答.md §6.2）。

    `structured=True`（`ST-01` 阶段 B，配置 `context_partition_render`）时改走
    分段渲染：记忆 / 历史 / 工具目录各自成段，证据段排最后。默认 False = 逐字不变。
    历史此前是**死字段**（`state.history_recent` 装配到状态但答案提示词零引用），
    structured 模式才真正把它送进答案提示词。
    """
    if parametric:
        return GENERAL_KNOWLEDGE_PROMPT.format(question=question)

    # `ST-03`：Agent 路径接入**统一上下文组装**——
    # ① 父块展开（与线性路径同一套语义：去重键 = 父块 ID）
    # ② 预算裁剪（Agent 证据此前**无上限**：多轮工具调用后提示词无界增长）
    # 被裁掉的条目写进 `dropped_out`（→ 接线到 `context_breakdown.dropped`）。
    if parent_lookup is not None or token_budget:
        # 按需载入：统一组装只在「有预算或父块」时才需要（多数调用不走这条）
        from app.services.context.assembly import ContextItem, assemble_context
        # 按需载入：同上（父块展开只在传了 parent_lookup 时才需要）
        from app.services.context.expand import expand_parents

        expanded, dropped = expand_parents(evidence_dicts, parent_lookup or (lambda _pid: None))
        items = [
            ContextItem(
                key=e.key, doc_id=e.doc_id, content=e.content,
                score=float((src.get("score") or 0.0)),
                source=str(src.get("source") or ""), rank=i,
            )
            for i, (e, src) in enumerate(zip(expanded, evidence_dicts))
        ]
        assembled = assemble_context(items, token_budget=token_budget) if token_budget else None
        if assembled is not None:
            kept = {it.key for it in assembled.items}
            for it in assembled.dropped:
                dropped.append({"key": it.key, "reason": it.reason or "超出上下文预算"})
            evidence_dicts = [
                src for e, src in zip(expanded, evidence_dicts) if e.key in kept
            ]
        if dropped_out is not None:
            dropped_out.extend(dropped)

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
    if structured:
        # 按需载入：默认（legacy）路径不导入渲染器
        from app.services.context.render import render_history_lines, render_prompt

        history_lines: list[str] = []
        if history_summary.strip():
            history_lines.append(f"- 前文摘要：{history_summary.strip()}")
        history_lines.extend(render_history_lines(history_recent or []))
        prompt = render_prompt(
            question,
            evidence,
            structured=True,
            memory=memory,
            history=history_lines,
            no_info=NO_INFO_ANSWER,
        )
    else:
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
        crag_router=None,  # CragRouter：与线性路径同一阈值口径，产 crag_branch
        history_store=None,  # ValidationHistoryStore：CRAG 轮次落库
        score_top_n: int = 2,  # 证据充分度聚合的 Top-N（与 crag_score_top_n 对齐）
        settings=None,  # Settings：读取三预算配置，可缺省（测试用默认预算）
        checkpointer=None,  # BaseCheckpointSaver：断点续跑的前提
        prom=None,  # PromMetrics：Agent 路径埋点，可缺省（测试）
        llm=None,  # LLM：仅用于读取 last_usage 做 token 埋点，不参与生成（图闭包持有）
        self_checker=None,  # HallucinationChecker：逐句自检
        executor=None,  # ThreadPoolExecutor：run/resume 工作线程来源（批次 1）
        run_permits: int = 16,  # Agent 并发闸门：在飞 run 上限，超出 429（批次 1）
        docstore=None,  # DocStore：`ST-03` 父块展开（取父块全文）
    ) -> None:
        self._graph = graph
        self._sessions = sessions
        self._metrics = metrics_store
        self._top_k = top_k
        self._crag = crag_router
        self._history = history_store
        self._score_top_n = max(1, score_top_n)
        self._settings = settings
        self._registry = registry
        self._memory = memory
        self._checkpointer = checkpointer
        self._prom = prom
        self._llm = llm
        self._self_checker = self_checker
        # `ST-03`：父块展开的取块回调（可缺省 → 不展开，行为与改造前一致）
        self._docstore = docstore
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
        trace_id: str = "",
    ) -> Iterator[tuple[str, dict]]:
        """事件流：plan/tool_start/tool_end/reflect/iteration/token/fuse/_final。

         网关语义：token 事件被缓冲——答案定稿后才放行
        （缓冲保证流式吐出的内容与最终答案一致）。
        """
        events: queue.Queue = queue.Queue()
        started = time.monotonic()

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
            state["evidence"] = [{**e, "carried": True} for e in carried]
            state["last_evidence_count"] = len(carried)

        def emit(event: str, payload: dict) -> None:
            events.put((event, payload))

        # 每次 run 新建一份预算账本（预算是一次性的，不是会话级的）
        budget = (
            budget_from_settings(self._settings, tenant_id) if self._settings else AgentBudget()
        )

        # `ST-02`：历史摘要压缩——更早的轮次压成结构化摘要，最近 N 轮保留原文。
        # **必须在预算创建之后**：压缩本身是一次模型调用，预算闸门要靠它判断
        # 「值不值得花这次调用」（预算不够就直接截断回落，不烧预算）。
        compaction = self._compact_history(history, budget=budget)
        # 计量（`ST-02`）：压缩次数与省下的 token。
        # 成本账本的 `compress` 阶段此前**只有枚举没有写入点**——这里补上。
        saved = max(0, compaction.tokens_before - compaction.tokens_after)
        if self._prom is not None:
            try:
                self._prom.record_history_compaction(compaction.method, saved)
            except Exception:  # noqa: BLE001 - 指标失败不得影响问答
                pass
        if compaction.method != "skipped":
            state["history"] = compaction.recent_turns
            state["history_summary"] = compaction.summary
            state["history_recent"] = compaction.recent_turns
            state["history_compaction"] = compaction.as_dict()
        # 是否已拿到真实 LLM 用量（规划/反思阶段）。拿到后不再叠加常量估算口径。
        real_usage_seen = False

        # LangGraph 启用 checkpointer 后**必须**提供 thread_id，
        # 否则 invoke 直接抛 ValueError。这里按 `{session_id}:{turn}` 约定传入：
        # 有 session_id 时可断点续跑/回放，无 session_id（一次性问答）时
        # 回退到 trace_id —— 保证仍然可用，且不同请求不会互相覆盖状态。
        if session_id:
            try:
                session = self._sessions.get(session_id, tenant_id, user_id)
                turn = int(session.get("turn", 0)) + 1
            except Exception:  # noqa: BLE001 - 会话不可用不应阻断问答
                logger.debug("会话不可用不应阻断问答")
                turn = 1
            thread_id = thread_id_for(session_id, turn)
        else:
            thread_id = f"trace:{trace_id}" if trace_id else None

        def work() -> None:
            try:
                config = {
                    "configurable": {
                        "emit": emit,
                        # 预算账本注入图，tool_node 每轮调用前检查
                        "budget": budget,
                        # 长期记忆提示（注入规划/思考的提示词）
                        "memory_hint": self._memory_hint(tenant_id),
                        # `ST-03`：上下文预算（Agent 证据此前**无上限**）与父块查找
                        "context_token_budget": int(
                            getattr(self._settings, "context_token_budget", 0) or 0
                            if self._settings else 0
                        ),
                        "parent_lookup": (
                            self._docstore.get_parent if self._docstore is not None else None
                        ),
                        # `ST-01` 阶段 B：渲染模式（legacy | structured），由 answer 节点透传给提示词构建器
                        "context_partition_render": str(
                            getattr(self._settings, "context_partition_render", "legacy")
                            if self._settings else "legacy"
                        ),
                    }
                }
                if thread_id:
                    config["configurable"]["thread_id"] = thread_id
                # M5：资产目录（VLM 直答据此定位原图文件）
                assets_dir = self._assets_dir()
                if assets_dir:
                    config["configurable"]["assets_dir"] = assets_dir
                # 工具空结果的降级链（graph_search → vector_search 等）
                config["configurable"]["tool_fallbacks"] = tool_fallbacks(self._settings)
                final = self._graph.invoke(state, config=config)
                events.put(("_state", final))
            except Exception as exc:  # noqa: BLE001 - Agent 执行异常
                logger.exception("Agent 执行异常")
                events.put(("_error", exc))
            finally:
                events.put(_DONE)
                # 取消标志用完即清：工具派发只在图执行期间发生，图返回后
                # 标志再无消费者。此前 clear_cancel 零调用点，`_cancel_flags`
                # 集合随 run 数无界增长（内存泄漏 + 误伤同 id 的新 run）。
                if thread_id and self._registry is not None:
                    self._registry.clear_cancel(thread_id)

        self._executor.submit(work)

        final_state: dict | None = None
        buffered_tokens: list[str] = []
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
                continue
            if event == "_error":
                raise payload
            if event == "usage":
                # 真实 LLM 用量（规划/反思阶段）。此前这些阶段一律按常量估算，
                # 与真实用量脱钩 → agent_token_budget 只对 answer 段真实生效。
                # 有真实值就不再叠加常量估算，否则同一阶段被记两次。
                tokens = int(payload.get("tokens") or 0)
                if tokens > 0:
                    phase = str(payload.get("phase") or "think")
                    usage_cost = estimate_cost_yuan(
                        tokens, self._prices, self._model_name
                    )
                    if budget is not None:
                        budget.settle_tokens(tokens, usage_cost)
                    get_ledger().record(
                        trace_id, tenant_id, phase, self._model_name,
                        tokens, usage_cost,
                        priced=self._model_name in self._prices,
                    )
                    real_usage_seen = True
                continue
            if event == "tool_end" and budget is not None and not real_usage_seen:
                # 每次工具调用都伴随至少一次 LLM 调用（规划/反思）。
                # **仅在没有真实 usage 上报时**才按"每工具调用约 N token"估算
                # （启发式规划器/mock 模式没有真实用量可读）。
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
                    # 失败归因：只有能分类的失败才计数（error_class 为空表示
                    # 未分类的旧路径，避免把"未知"当成一个真实原因统计）。
                    error_class = str(payload.get("error_class") or "")
                    if outcome in ("error", "timeout") and error_class:
                        self._prom.agent_tool_failures_total.labels(
                            tool=tool, error_class=error_class
                        ).inc()
                    elapsed_ms = payload.get("elapsed_ms")
                    if isinstance(elapsed_ms, (int, float)):
                        self._prom.agent_tool_duration_seconds.labels(tool=tool).observe(
                            elapsed_ms / 1000.0
                        )
            elif event == "fuse" and self._prom is not None:
                self._prom.agent_fuse_total.labels(
                    reason=str(payload.get("reason", "unknown"))
                ).inc()
            elif event == "reflect" and self._prom is not None:
                self._prom.agent_next_action_total.labels(
                    action=str(payload.get("next_action", "unknown"))
                ).inc()
            yield event, payload

        # ---- 答案定稿（输出侧）----
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

        # 定稿后放行（缓冲 token 以最终文本为准重新发射）
        final_answer = (final_state or {}).get("final_answer", "")
        if final_answer and buffered_tokens:
            chunk = 24
            for i in range(0, len(final_answer), chunk):
                yield "token", {"content": final_answer[i : i + chunk]}


        # ---- 结果归一 ----
        # answer 阶段的真实用量**回填预算**：此前只进指标与账单，
        # 预算账本漏计最贵的一段（生成）→ token 预算形同虚设。
        # 必须在快照生成之前回填，否则前端看到的预算视图少了这段消耗。
        answer_usage = getattr(self._llm, "last_usage", None)
        if answer_usage is not None and getattr(answer_usage, "total", 0):
            ans_tokens = int(answer_usage.total)
            ans_cost = estimate_cost_yuan(ans_tokens, self._prices, self._model_name)
            if budget is not None:
                budget.settle_tokens(ans_tokens, ans_cost)

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
                    trace_id=trace_id,
                )
            except Exception:  # noqa: BLE001 - 会话快照写入失败
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
                # 破环：llm 反向依赖 agent 侧接口
                from app.services.llm import generate_followups as _fallback

                items = _fallback(self._llm, answer, 3)
        except Exception as exc:  # noqa: BLE001 - 追问失败不影响主答案
            logger.warning("生成推荐追问失败（本次不展示追问）: %s", exc)
            return []
        if not isinstance(items, list):
            return []
        return [str(i).strip() for i in items if str(i).strip()][:3]

    # ---- TASK-117：长期记忆 ----

    def _compact_history(self, history: list[dict], budget=None):
        """历史摘要压缩（`ST-02`）。返回 `CompactionResult`（未启用时 method=skipped）。

        **绝不抛错**：压缩是优化路径，失败就回落截断，不能让问答挂掉。
        """
        # 按需载入：历史压缩只在启用且有历史时才需要（热路径不必付导入成本）
        from app.services.context.history import (
            METHOD_SKIPPED,
            CompactionResult,
            compact_history,
        )

        settings = self._settings
        if settings is None or not bool(getattr(settings, "history_compaction_enabled", False)):
            return CompactionResult(method=METHOD_SKIPPED, note="历史压缩未启用")
        if not history:
            return CompactionResult(method=METHOD_SKIPPED, note="没有历史")
        # 预算闸门：压缩是一次模型调用，预算不够就别烧
        budget_ok = True
        if budget is not None:
            try:
                budget_ok = budget.can_afford(tokens=600)
            except Exception:  # noqa: BLE001 - 预算判定失败时保守放行
                budget_ok = True
        try:
            return compact_history(
                history,
                self._llm,
                keep_recent=int(getattr(settings, "history_compaction_keep_recent", 3) or 3),
                trigger_tokens=int(
                    getattr(settings, "history_compaction_trigger_tokens", 4000) or 4000
                ),
                max_output_tokens=int(
                    getattr(settings, "history_compaction_max_output_tokens", 600) or 600
                ),
                budget_ok=budget_ok,
            )
        except Exception as exc:  # noqa: BLE001 - 压缩失败不打断问答
            logger.warning("历史压缩失败（忽略）: %s", exc)
            return CompactionResult(method=METHOD_SKIPPED, note=f"压缩异常：{exc}")

    def _memory_hint(self, tenant_id: str) -> str:
        """取该租户的经验并渲染成提示词片段（无记忆时返回空串）。

        失败一律返回空串：记忆是**增强**，拿不到不该影响问答。
        """
        if self._memory is None:
            return ""
        try:
            # 破环：memory 依赖 agent 状态类型
            from app.services.agent.memory import LongTermMemory

            return LongTermMemory.render_hint(self._memory.recall(tenant_id))
        except Exception as exc:  # noqa: BLE001 - 长期记忆读取失败：按空文本继续
            logger.debug("长期记忆读取失败（忽略）: %s", exc)
            return ""

    def _record_memory(self, tenant_id: str, state: dict) -> None:
        """从本次运行提炼经验（写入侧）。"""
        if self._memory is None:
            return
        try:
            # 破环：memory 依赖 agent 状态类型
            from app.services.agent.memory import record_from_run

            record_from_run(self._memory, tenant_id, state)
        except Exception as exc:  # noqa: BLE001 - 记忆写入绝不阻断主流程
            logger.debug("长期记忆提炼失败（忽略）: %s", exc)

    def _assets_dir(self):
        """资产目录（图像原文件所在处）；没有设置就不启用 VLM 直答。"""
        settings = getattr(self, "_settings", None)
        data_dir = getattr(settings, "data_dir", None)
        if data_dir is None:
            return None
        return Path(data_dir) / "assets"

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
        evidence_dicts = list(all_evidence)
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

        # 拒答归一：无证据 / 提示词级拒答
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

        # 兜底回答不引用证据：前端不展示无关片段
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
            "from_cache": False,
            "retrieval": self._retrieval_stats(state, len(evidence)),
            "crag": self._crag_payload(avg_score, len(evidence), trace_id),
            "evidence_score": round(avg_score, 4),
            "regenerated": False,
            "unsupported_claims_count": 0,
            # `ST-03`：Agent 路径的上下文构成（被裁 / 被去重的条目）
            "context_breakdown": {
                "dropped": list(state.get("dropped_evidence") or []),
                "dropped_count": len(state.get("dropped_evidence") or []),
            },
        }
