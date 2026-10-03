"""LangGraph 状态机编排（阶段 6 §2.1 / §4 / §5）。

拓扑：plan → tool → reflect → {continue: tool, replan: replan, answer: answer} → END
节点是纯函数（state in, patch out），依赖经闭包注入，事件经 config.emit 发出。

三层熔断（§5.1）：
1. 总轮数上限（动态：计划步骤数 + 2 轮重规划余量）→ 强制作答；
2. 无效进展熔断（连续 3 轮"继续"但证据未增加）→ 强制作答；
3. 单步超时（工具层 30s）→ 标记该步失败，流程继续。
熔断不是错误：强制作答并标记 incomplete，前端黄色提示。
"""
import logging
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.services.agent.planner import Planner
from app.services.agent.reflector import ANSWER_THRESHOLD, Reflector
from app.services.agent.state import AgentState
from app.services.agent.tools import ToolRegistry
from app.services.llm import LLM

logger = logging.getLogger(__name__)

NO_PROGRESS_LIMIT = 3  # 第二层熔断窗口：连续"继续"但证据未增加的轮数
MAX_REPLANS = 2  # 重规划余量（计入动态上限）
REPLAN_HINT = "此前检索不充分，请换个角度"


def _emit(config: RunnableConfig, event: str, payload: dict) -> None:
    emit = (config.get("configurable") or {}).get("emit")
    if emit:
        emit(event, payload)


def build_agent_graph(
    registry: ToolRegistry,
    planner: Planner,
    reflector: Reflector,
    answer_llm: LLM,
    prompt_builder,  # (question, evidence_dicts) -> str
    no_progress_limit: int = NO_PROGRESS_LIMIT,
) -> CompiledStateGraph:
    deps = {"registry": registry, "planner": planner, "reflector": reflector, "llm": answer_llm}

    # ---------- 节点 ----------

    def plan_node(state: AgentState, config: RunnableConfig) -> dict:
        is_replan = state.get("needs_replan", False)
        replan_hint = REPLAN_HINT if is_replan else ""
        steps = planner.plan(state["question"], replan_hint)
        if is_replan:
            # 重规划：新 plan 接到原 plan 尾部（不清空历史），步骤号顺延
            base = len(state.get("plan", []))
            for s in steps:
                s["step_index"] = base + s["step_index"]
            new_plan = state.get("plan", []) + steps
            max_iterations = state.get("max_iterations", 4)  # 上限不因重规划扩大
        else:
            new_plan = steps
            # 动态上限 = 计划步骤数 + 重规划余量（不能写成固定常数）
            max_iterations = max(len(steps) + MAX_REPLANS, 3)
        _emit(config, "plan", {
            "steps": steps, "max_iterations": max_iterations, "replan": is_replan,
        })
        return {
            "plan": new_plan,
            "max_iterations": max_iterations,
            "needs_replan": False,
            "replan_count": state.get("replan_count", 0) + (1 if is_replan else 0),
        }

    def tool_node(state: AgentState, config: RunnableConfig) -> dict:
        plan = [dict(s) for s in state.get("plan", [])]
        executed = set(state.get("executed_steps", []))
        done_set = {s["step_index"] for s in plan if s["status"] == "done"} | executed
        step = next(
            (
                s for s in plan
                if s["status"] == "pending"
                and s["step_index"] not in executed
                and all(d in done_set for d in s["depends_on"])
            ),
            None,
        )
        if step is None:
            # 无可执行步骤 → 交给 reflect 升级 replan
            return {"next_action": "replan"}

        _emit(config, "tool_start", {
            "step_index": step["step_index"], "tool": step["tool_name"],
            "input": step["description"],
        })
        result, hallucinated, actual_tool = registry.run_with_timeout(
            step["tool_name"], step["description"], state["tenant_id"]
        )
        # 检索后 ACL 过滤（阶段 9 §3.3 双保险的应用层一侧）：
        # 向量库 filter 只有"完全等于"语义，deny/多身份 ACL 必须在应用层做
        acl_filter = (config.get("configurable") or {}).get("acl_filter")
        acl_filtered = 0
        if acl_filter and actual_tool == "vector_search" and result.items:
            before = len(result.items)
            result.items = [it for it in result.items if acl_filter(it.get("doc_id", ""))]
            acl_filtered = before - len(result.items)
            if acl_filtered:
                logger.info("ACL 检索后过滤：%d 条不可见", acl_filtered)
        # 工具返回的三种情况（§4.3）：有结果 / 0 条视为失败不重试 / 异常标记失败不中断
        failed = bool(result.error) or not result.items
        step["status"] = "failed" if failed else "done"
        step["result_count"] = len(result.items)
        step["tool_name"] = actual_tool

        new_plan = [
            step if s["step_index"] == step["step_index"] else s for s in plan
        ]
        evidence_patch = [
            {**item, "step_index": step["step_index"]} for item in result.items
        ]
        if acl_filtered:
            evidence_patch.append({
                "chunk_id": f"acl-filtered#{step['step_index']}",
                "content": f"另有 {acl_filtered} 条结果因权限未展示",
                "score": 0.0, "source": "权限过滤", "doc_id": "__acl__",
                "source_route": "acl", "modality": "acl_notice",
                "acl_notice": True,  # 前端只展示数量，绝不展示被过滤文档的元信息
                "acl_filtered_count": acl_filtered,
                "step_index": step["step_index"],
            })
        call_record = {
            "step_index": step["step_index"],
            "tool_name": actual_tool,
            "arguments": {"query": step["description"]},
            "result_count": len(result.items),
            "elapsed_ms": result.elapsed_ms,
            "error": result.error,
            "truncated_from": result.truncated_from,
        }
        error_patch = []
        if result.error:
            error_patch.append({"step_index": step["step_index"], "tool": actual_tool, "error": result.error})
        elif not result.items:
            error_patch.append({"step_index": step["step_index"], "tool": actual_tool, "error": "无结果（0 条）"})

        _emit(config, "tool_end", {
            "step_index": step["step_index"], "tool": actual_tool,
            "elapsed_ms": result.elapsed_ms, "hit_count": len(result.items),
            "error": result.error, "truncated_from": result.truncated_from,
            "tool_hallucinated": hallucinated,
            "evidence": evidence_patch,  # 契约：tool_end 携带该步证据（轨迹↔证据双向定位）
            "acl_filtered": acl_filtered,
        })
        return {
            "plan": new_plan,
            "evidence": evidence_patch,
            "executed_steps": [step["step_index"]],
            "tool_calls": [call_record],
            "tool_errors": error_patch,
            "iteration": state.get("iteration", 0) + 1,
            "current_step": step["step_index"],
            "degraded": state.get("degraded", False) or bool(result.error),
        }

    def reflect_node(state: AgentState, config: RunnableConfig) -> dict:
        iteration = state.get("iteration", 0)
        max_iterations = state.get("max_iterations", 4)
        evidence = state.get("evidence", [])
        prev_count = state.get("last_evidence_count", 0)

        # ---- 第一道防线：总轮数熔断（不调 LLM，省钱）----
        if iteration >= max_iterations:
            _emit(config, "fuse", {"reason": "iteration_limit", "iteration": iteration, "max": max_iterations})
            return {
                "next_action": "answer",
                "status": "circuit_breaker",
                "incomplete": True,
                "fuse_reason": "iteration_limit",
            }

        remaining = sum(1 for s in state.get("plan", []) if s["status"] == "pending"
                        and s["step_index"] not in set(state.get("executed_steps", [])))
        verdict = reflector.reflect(state["question"], evidence, remaining)
        next_action = verdict["next_action"]
        score = verdict["score"]

        # ---- 第二道防线：低分纠正（LLM 自信幻觉拦截）----
        if next_action == "answer" and score < ANSWER_THRESHOLD:
            next_action = "continue"
            verdict["reason"] += "（低分纠正：分数不足不允许作答）"
        logger.info(
            "reflect 判定: action=%s→%s score=%.3f evidence=%d remaining=%d",
            verdict["next_action"], next_action, score, len(state.get("evidence", [])), remaining,
        )
        # ---- 对称纠正：证据已足却想重规划/空转 → 直接作答 ----
        # 覆盖两种保守循环：replan（高分却重规划）与 continue 但无剩余步骤
        # （单步计划跑完后 LLM 仍想 continue → 耗尽升级为 replan → 同查询打转直到熔断）
        if score >= ANSWER_THRESHOLD and state.get("evidence"):
            if next_action == "replan" or (next_action == "continue" and remaining == 0):
                next_action = "answer"
                verdict["reason"] += "（高分纠正：证据已足，直接作答）"

        # ---- 第三道防线：步骤耗尽升级 ----
        if next_action == "continue" and remaining == 0:
            next_action = "replan"

        # ---- 无效进展追踪（第二层熔断判据）----
        no_progress = state.get("no_progress_rounds", 0)
        if next_action in ("continue", "replan") and len(evidence) <= prev_count:
            no_progress += 1
        else:
            no_progress = 0
        if no_progress >= no_progress_limit:
            _emit(config, "fuse", {"reason": "no_progress", "iteration": iteration})
            return {
                "next_action": "answer", "evidence_score": score,
                "status": "circuit_breaker", "incomplete": True,
                "fuse_reason": "no_progress", "no_progress_rounds": no_progress,
                "last_evidence_count": len(evidence),
            }

        _emit(config, "reflect", {
            "evidence_score": score, "next_action": next_action,
            "reason": verdict["reason"],
        })
        _emit(config, "iteration", {"current": iteration, "max": max_iterations})
        return {
            "evidence_score": score,
            "next_action": next_action,
            "no_progress_rounds": no_progress,
            "last_evidence_count": len(evidence),
        }

    def replan_node(state: AgentState, config: RunnableConfig) -> dict:
        _emit(config, "replan", {"iteration": state.get("iteration", 0)})
        return {"needs_replan": True}  # plan 节点消费此标记生成新计划

    def answer_node(state: AgentState, config: RunnableConfig) -> dict:
        emit = (config.get("configurable") or {}).get("emit")
        evidence = state.get("evidence", [])
        if state.get("skip_retrieval") or not evidence:
            if state.get("skip_retrieval"):
                prompt = state["question"]  # 闲聊直答
            else:
                return {"final_answer": "", "refusal": True, "status": "completed"}
        else:
            prompt = prompt_builder(state["question"], evidence)
            if state.get("incomplete"):
                prompt += "\n\n（注意：检索已达执行上限，请基于现有证据尽力回答，并在开头说明答案可能不完整。）"

        chunks: list[str] = []
        for piece in deps["llm"].stream(prompt):
            chunks.append(piece)
            if emit:
                emit("token", {"content": piece})
        return {"final_answer": "".join(chunks), "status": state.get("status") if state.get("status") == "circuit_breaker" else "completed"}

    # ---------- 路由 ----------

    def entry_router(state: AgentState) -> str:
        return "answer" if state.get("skip_retrieval") else "plan"

    def reflect_router(state: AgentState) -> str:
        action = state.get("next_action", "answer")
        # 重规划余量耗尽 → 强制作答（不再放行 replan）
        if action == "replan" and state.get("replan_count", 0) >= MAX_REPLANS:
            return "answer"
        return {"continue": "tool", "replan": "replan", "answer": "answer"}.get(action, "answer")

    graph = StateGraph(AgentState)
    graph.add_node("plan", plan_node)
    graph.add_node("tool", tool_node)
    graph.add_node("reflect", reflect_node)
    graph.add_node("replan", replan_node)
    graph.add_node("answer", answer_node)

    graph.add_conditional_edges(START, entry_router, {"plan": "plan", "answer": "answer"})
    graph.add_edge("plan", "tool")
    graph.add_edge("tool", "reflect")
    graph.add_conditional_edges(
        "reflect", reflect_router, {"tool": "tool", "replan": "replan", "answer": "answer"}
    )
    graph.add_edge("replan", "plan")
    graph.add_edge("answer", END)
    return graph.compile()
