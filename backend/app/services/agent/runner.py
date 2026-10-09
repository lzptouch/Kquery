"""Agent 运行器（阶段 6 装配层）。

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
from typing import Iterator

from langgraph.graph.state import CompiledStateGraph

from app.domain.schemas import Evidence
from app.services.agent.sessions import SessionStore
from app.services.agent.state import initial_state
from app.services.pipeline import (
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


def agent_prompt_builder(question: str, evidence_dicts: list[dict]) -> str:
    """answer 节点的提示词：只读最终入选证据，不读 plan/reflect 中间结果（§8.3）。

    图谱/图像证据需要额外指引：关系三元组可以综合推理直接与间接关系；
    "无相关内容"话术要么不出现、要么是答案的全部（不得在分析后追加，
    否则会被拒答归一误判）。
    """
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


class AgentRunner:
    def __init__(
        self,
        graph: CompiledStateGraph,
        sessions: SessionStore,
        metrics_store=None,
        top_k: int = 4,
        audit=None,  # AuditStore（阶段 9：三事件审计）
        scanner=None,  # SensitiveScanner（输出侧扫描）
        crag_router=None,  # CragRouter：与线性路径同一阈值口径，产 crag_branch
        history_store=None,  # ValidationHistoryStore：CRAG 轮次落库
        score_top_n: int = 2,  # 证据充分度聚合的 Top-N（与 crag_score_top_n 对齐）
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

    # ---- 对外主入口：事件流 ----

    def run(
        self,
        question: str,
        tenant_id: str,
        user_id: str = "anon",
        session_id: str | None = None,
        skip_retrieval: bool = False,
        doc_name_resolver=None,
        acl_filter=None,  # (doc_id) -> bool（阶段 9：检索后 ACL 过滤）
        trace_id: str = "",
    ) -> Iterator[tuple[str, dict]]:
        """事件流：plan/tool_start/tool_end/reflect/iteration/token/fuse/_final。

        阶段 9 网关语义：token 事件被缓冲——答案经敏感词扫描后才放行
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

        def work() -> None:
            try:
                final = self._graph.invoke(
                    state,
                    config={"configurable": {"emit": emit, "acl_filter": acl_filter}},
                )
                events.put(("_state", final))
            except Exception as exc:  # noqa: BLE001
                logger.exception("Agent 执行异常")
                events.put(("_error", exc))
            finally:
                events.put(_DONE)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()

        final_state: dict | None = None
        buffered_tokens: list[str] = []
        while True:
            item = events.get()
            if item is _DONE:
                break
            event, payload = item
            if event == "_state":
                final_state = payload
                continue
            if event == "_error":
                raise payload
            if event == "token":
                # 缓冲 token：扫描前不放行（输出侧合规红线）
                buffered_tokens.append(payload.get("content", ""))
                continue
            if event == "tool_end" and self._audit and trace_id:
                # 审计：检索事件（召回数 / 可见数，差值即被权限过滤数）
                retrieved = payload.get("hit_count", 0) + payload.get("acl_filtered", 0)
                visible = payload.get("hit_count", 0)
                self._audit.log_retrieve(trace_id, tenant_id, user_id, retrieved, visible)
            yield event, payload

        # ---- 敏感词扫描（输出侧，响应前）----
        raw_answer = (final_state or {}).get("final_answer", "")
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
            "unsupported_claims_count": result["unsupported_claims_count"],
            "regenerated": result["regenerated"],
            # Agent 路径未做逐句自检（幻觉自检只在线性路径执行），如实标未知
            "unvalidated": True,
            "removed_claims": [],
        }
        # 快照回写（同会话串行由 store 保证）
        if session_id and final_state is not None:
            try:
                self._sessions.save_turn(
                    session_id, tenant_id, user_id,
                    question, result["answer"], result["verdict"], final_state,
                )
            except Exception:  # noqa: BLE001
                logger.exception("会话快照写入失败（不影响本次应答）")
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
            )
            self._metrics.record_agent_run(
                iterations=final_state.get("iteration", 0) if final_state else 0,
                fused=(final_state or {}).get("status") == "circuit_breaker",
                fuse_reason=(final_state or {}).get("fuse_reason", ""),
                tool_calls=(final_state or {}).get("tool_calls", []),
                session_id=session_id or "",
            )
        yield "_final", result

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
        refusal = state.get("refusal", False) or answer.strip().startswith(NO_INFO_ANSWER)
        if refusal:
            verdict, answer_out, confidence = "refused", REFUSAL_TEXT, 0.0
            refusal_reason = "no_content"
            evidence = []
        else:
            verdict = "answered"
            answer_out = answer
            confidence = round(min(0.95, state.get("evidence_score", 0.5)), 4)
            refusal_reason = ""

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
