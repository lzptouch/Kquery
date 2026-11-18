"""LangGraph 状态机编排。

拓扑：plan → tool → reflect → {continue: tool, replan: replan, answer: answer} → END
节点是纯函数（state in, patch out），依赖经闭包注入，事件经 config.emit 发出。

三层熔断：
1. 总轮数上限（动态：计划步骤数 + 2 轮重规划余量）→ 强制作答；
2. 无效进展熔断（连续 3 轮"继续"但证据未增加）→ 强制作答；
3. 单步超时（工具层 30s）→ 标记该步失败，流程继续。
熔断不是错误：强制作答并标记 incomplete，前端黄色提示。
"""

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from app.core.envelope import ErrCode
from app.services.agent.planner import Planner
from app.services.agent.react import observations_from_tool_results
from app.services.agent.tool_spec import arguments_from_step
from app.services.agent.reflector import ANSWER_THRESHOLD, Reflector
from app.services.agent.state import AgentState
from app.services.agent.tools import DANGER_WRITE, ToolRegistry
from app.services.llm import LLM
from app.services.pipeline import GENERAL_KNOWLEDGE_PROMPT, NO_INFO_ANSWER
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

NO_PROGRESS_LIMIT = 3  # 第二层熔断窗口：连续"继续"但证据未增加的轮数
MAX_REPLANS = 2  # 重规划余量（计入动态上限）
# 一次答案生成的 token 预留（闸门口径）：上下文 + 输出的保守上界。
# 与 verify_node 的 600 校验预留区分开——生成比校验贵一个量级。
ANSWER_TOKEN_RESERVE = 2000
REPLAN_HINT = "此前检索不充分，请换个角度"


def _emit(config: RunnableConfig, event: str, payload: dict) -> None:
    emit = (config.get("configurable") or {}).get("emit")
    if emit:
        emit(event, payload)


def _emit_usage(config: RunnableConfig, phase: str, owner) -> None:
    """上报一次 LLM 调用的**真实** token 用量。

    此前规划/反思阶段的 token 一律按常量估算（"每工具调用约 N token"），
    与真实用量脱钩 → `agent_token_budget` 只对最后一段（answer）真实生效。
    这里把 `llm.last_usage`（供应商返回值，读不到才按字符估算）透给 runner 记账。

    `owner` 是持有 llm 的对象（planner / reflector）。拿不到就静默跳过——
    启发式规划器本就不调用 LLM，凭空记账反而是错的。
    """
    llm = getattr(owner, "_llm", None)
    usage = getattr(llm, "last_usage", None)
    if usage is None:
        return
    tokens = int(getattr(usage, "total", 0) or 0)
    if tokens <= 0:
        return
    _emit(config, "usage", {
        "phase": phase,
        "tokens": tokens,
        "estimated": bool(getattr(usage, "estimated", True)),
    })


def hitl_trigger(state: AgentState, cfg) -> str:
    """是否应挂起等人工，返回触发原因（空串 = 不挂起）。

    触发条件刻意保守——HITL 挂起对用户是**打断**，滥用会让问答变得难用：

    | 条件 | 判定 |
    | --- | --- |
    | 判定为拒答 | 恒触发（用户最需要介入的场景） |
    | 全部工具失败（有失败记录且零证据） | 恒触发 |
    | 低置信 | `evidence_score < threshold` 且**有证据**（无证据已被拒答覆盖） |
    | 预算耗尽且证据 < 2 条 | 恒触发 |
    | 闲聊短路 | **不触发**（无需介入） |

    配置来自 `agent_hitl_*`。`cfg` 支持传 dict（可含 threshold 覆盖）。
    """
    if state.get("skip_retrieval"):
        return ""  # 闲聊直答，无需人工
    if state.get("hitl_resolved"):
        return ""  # 已介入过，不再二次打断
    threshold = float((cfg or {}).get("score_threshold", 0.5)) if isinstance(cfg, dict) else 0.5
    evidence = state.get("evidence", [])
    if state.get("refusal") and not evidence:
        return "refused"
    if not evidence and state.get("tool_errors"):
        return "all_tools_failed"
    if state.get("status") == "budget_exhausted" and len(evidence) < 2:
        return "budget_exhausted"
    if evidence and not state.get("refusal"):
        score = float(state.get("evidence_score", 1.0) or 0.0)
        if score < threshold:
            return "low_confidence"
    if not evidence:
        return "no_evidence"
    return ""


def _parse_hitl_decision(decision) -> tuple[str, str]:
    """解析 resume 传入的决策，返回 (action, hint)。

    容错优先：非法/缺失决策一律按 `force_answer` 处理——用户点了"继续"却因
    参数格式问题拿不到答案，比"给了个可能不完整的答案"更糟。
    """
    if isinstance(decision, str):
        # 简写：直接传字符串视为 hint
        return ("hint", decision) if decision.strip() else ("force_answer", "")
    if not isinstance(decision, dict):
        return ("force_answer", "")
    action = str(decision.get("action") or "").strip().lower()
    hint = str(decision.get("value") or decision.get("hint") or "").strip()
    if action in ("stop", "cancel", "abort"):
        return ("stop", "")
    if action == "hint":
        return ("hint", hint) if hint else ("force_answer", "")
    return ("force_answer", hint)


def _final_status(state: AgentState) -> str:
    """answer 节点落库时的终态。

    只有"正常跑完"才叫 completed。此前把 circuit_breaker 之外的终态一律写成
    completed —— budget_exhausted / cancelled 这类**降级终态**会被静默吞掉，
    监控与前端都看不到"这次是被预算拦下的"。
    """
    status = state.get("status") or ""
    return status if status and status != "running" else "completed"


def build_agent_graph(
    registry: ToolRegistry,
    planner: Planner,
    reflector: Reflector,
    answer_llm: LLM,
    prompt_builder,  # (question, evidence_dicts, *, parametric=False) -> str
    no_progress_limit: int = NO_PROGRESS_LIMIT,
    checkpointer=None,  # BaseCheckpointSaver | None；None = 不启用续跑
    # 通用知识兜底（doc/analysis/通用知识兜底回答.md §6.4）：闭包注入，测试可独立覆盖
    parametric_fallback_enabled: bool = True,
    parametric_fallback_score_threshold: float = ANSWER_THRESHOLD,
    vlm_answerer=None,  # M5：VLM 直答器（None = 不启用）
) -> CompiledStateGraph:
    deps = {"registry": registry, "planner": planner, "reflector": reflector, "llm": answer_llm}
    # VLM 直答（M5）：注入即启用；不注入 = 只有 caption 文本路（默认行为不变）
    if vlm_answerer is not None:
        deps["vlm"] = vlm_answerer

    # ---------- 节点 ----------

    def plan_node(state: AgentState, config: RunnableConfig) -> dict:
        is_replan = state.get("needs_replan", False)
        replan_hint = REPLAN_HINT if is_replan else ""
        # 把该租户的经验作为额外提示（与 replan 提示同通道）
        memory_hint = str((config.get("configurable") or {}).get("memory_hint") or "")
        combined_hint = "\n".join(x for x in (replan_hint, memory_hint) if x)
        steps = planner.plan(state["question"], combined_hint)
        # 规划阶段的真实 token 用量（此前只有常量估算口径）
        _emit_usage(config, "think", planner)
        if is_replan:
            # 重规划：新 plan 接到原 plan 尾部（不清空历史），步骤号顺延
            base = len(state.get("plan", []))
            for s in steps:
                s["step_index"] = base + s["step_index"]
                # depends_on 也必须同步偏移。新计划里的依赖写的是**局部序号**
                # （0..n-1，planner 只允许指向更小下标），步骤号顺延后若不偏移，
                # 依赖就会指向错误的历史步骤 / 悬空 → 步骤被永久阻塞、打转到熔断。
                s["depends_on"] = [base + d for d in s.get("depends_on", [])]
            # 悬空依赖剔除：依赖了不存在的步骤会让该步永远无法就绪
            valid = {s["step_index"] for s in steps} | {
                s["step_index"] for s in state.get("plan", [])
            }
            for s in steps:
                dropped = [d for d in s["depends_on"] if d not in valid]
                if dropped:
                    logger.warning("重规划剔除悬空依赖: step=%s deps=%s", s["step_index"], dropped)
                    s["depends_on"] = [d for d in s["depends_on"] if d in valid]
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
        #  三预算闸门：在**调用任何工具之前**检查剩余额度。
        # 放在这里而不是调用后，是因为"调用完才发现超限"等于钱已经花了——
        # 预算控制的目的是不花那笔钱。
        budget = (config.get("configurable") or {}).get("budget")
        if budget is not None and budget.exhausted():
            reason = budget.exhausted()
            logger.info("预算耗尽（%s），停止工具调用并降级作答", reason)
            _emit(config, "fuse", {"reason": f"budget_{reason}", "iteration": state.get("iteration", 0)})
            return {
                "next_action": "answer",
                "status": "budget_exhausted",
                "incomplete": True,
                "fuse_reason": f"budget_{reason}",
                "tool_exhausted": False,
            }

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
            # 无可执行步骤：要么 pending 全部耗尽，要么依赖被阻塞。
            # 此前只置 next_action="replan"，但下一步必经 reflect_node 而它会**无条件**
            # 用 reflector 的结论覆盖 next_action → 该信号是死字段；且此分支不递增
            # iteration → 第一层熔断不可达，只能靠 no_progress 兜底。
            # 现在显式打标记 + 计轮次，让 reflect 短路并让熔断真正可达。
            return {
                "next_action": "replan",
                "tool_exhausted": True,
                "iteration": state.get("iteration", 0) + 1,
            }

        #  循环检测：同一工具 + 同一入参重复调用没有信息增益，
        # 直接短路并计入无进展（否则 Agent 会反复检索同一个查询直到熔断）。
        fingerprint = f"{step['tool_name']}::{step['description'].strip().lower()}"
        seen_calls = set(state.get("call_fingerprints", []))
        if fingerprint in seen_calls:
            logger.info("循环检测：跳过重复的工具调用 %s", fingerprint)
            _emit(config, "tool_start", {
                "step_index": step["step_index"], "tool": step["tool_name"],
                "input": step["description"], "skipped": True,
                "reason": "duplicate_call",
            })
            step["status"] = "failed"
            step["result_count"] = 0
            new_plan = [
                step if s["step_index"] == step["step_index"] else s for s in plan
            ]
            return {
                "plan": new_plan,
                "executed_steps": [step["step_index"]],
                "iteration": state.get("iteration", 0) + 1,
                "tool_exhausted": True,  # 本轮无有效产出，直接交给 reflect 收敛
            }

        _emit(config, "tool_start", {
            "step_index": step["step_index"], "tool": step["tool_name"],
            "input": step["description"],
        })
        # ACL 在 ToolRegistry 层对**所有工具**统一执行（原先只有
        # vector_search 走 ACL，graph_search / image_search 证据可绕过）。
        # ---- 写类工具的人工审批（副作用之前）----
        # 位置纪律与 answer_node 的 HITL 一致：**必须**在任何副作用之前挂起。
        # LangGraph 恢复时会从头重跑本节点，若挂起前已执行过工具，恢复后会重复执行。
        approved_map = dict((config.get("configurable") or {}).get("approved_tools") or {})
        approved_names = list(state.get("approved_tools") or [])
        approved_names += [n for n, ok in approved_map.items() if ok]
        tool_name = step["tool_name"]
        needs_approval = (
            registry.danger_of_name(tool_name) == DANGER_WRITE
            and tool_name not in approved_names
        )
        if needs_approval:
            decision = interrupt({
                "run_id": (config.get("configurable") or {}).get("thread_id", ""),
                "reason": "tool_approval_required",
                "tool": tool_name,
                "description": step["description"],
                "step_index": step["step_index"],
                "actions": ["approve", "reject"],
            })
            action = ""
            if isinstance(decision, dict):
                action = str(decision.get("action") or "")
            elif isinstance(decision, str):
                action = decision.strip()
            if action != "approve":
                # 人工拒绝：不执行工具，把该步标记失败并继续（不中断整条链）
                _emit(config, "hitl_resolved", {
                    "action": "reject", "tool": tool_name, "step_index": step["step_index"],
                })
                step["status"] = "failed"
                step["result_count"] = 0
                step["error"] = f"工具 {tool_name} 未获人工批准，已跳过"
                return {
                    "plan": [
                        step if s["step_index"] == step["step_index"] else s for s in plan
                    ],
                    "executed_steps": [step["step_index"]],
                    "tool_errors": [{
                        "step_index": step["step_index"],
                        "tool": tool_name,
                        "error": step["error"],
                        "error_class": "approval_rejected",
                    }],
                    "iteration": state.get("iteration", 0) + 1,
                    "hitl_resolved": "reject",
                }
            approved_names.append(tool_name)
            approved_map[tool_name] = True
            _emit(config, "hitl_resolved", {
                "action": "approve", "tool": tool_name, "step_index": step["step_index"],
            })
        result, hallucinated, actual_tool = registry.run_with_timeout(
            tool_name, step["description"], state["tenant_id"],
            # 取消标志 / 写类工具审批结果
            run_id=str((config.get("configurable") or {}).get("thread_id") or ""),
            approved={**approved_map, **{n: True for n in approved_names}},
            # 结构化入参（计划里显式写了 arguments 就用它，
            # 否则退回 description 作为 query —— 老计划仍可用）
            arguments=arguments_from_step(step),
        )
        # 工具返回的三种情况：有结果 / 0 条视为失败不重试 / 异常标记失败不中断
        failed = bool(result.error) or not result.items
        # ---- 空结果的降级链 ----
        # 0 条 ≠ 工具坏了：图谱库里没有这个实体是**正常业务结果**，
        # 直接判失败会让 Agent 白丢一次机会（下一步只能重规划或拒答）。
        # 降级到语义检索（可配置的映射），命中即算这一步成功。
        fallbacks = dict((config.get("configurable") or {}).get("tool_fallbacks") or {})
        fallback_tool = fallbacks.get(step["tool_name"], "")
        degraded_from = ""
        if failed and not result.error and fallback_tool and fallback_tool != actual_tool:
            _emit(config, "tool_start", {
                "step_index": step["step_index"], "tool": fallback_tool,
                "input": step["description"], "fallback_from": actual_tool,
            })
            fb_result, _fb_hallucinated, fb_actual = registry.run_with_timeout(
                fallback_tool, step["description"], state["tenant_id"],
                run_id=str((config.get("configurable") or {}).get("thread_id") or ""),
                approved={**approved_map, **{n: True for n in approved_names}},
                arguments=arguments_from_step(step),
            )
            _emit(config, "tool_end", {
                "step_index": step["step_index"], "tool": fb_actual,
                "elapsed_ms": fb_result.elapsed_ms, "hit_count": len(fb_result.items),
                "error": fb_result.error, "error_class": fb_result.error_class,
                "truncated_from": fb_result.truncated_from,
                "tool_hallucinated": False,
                "evidence": [{**item, "step_index": step["step_index"]} for item in fb_result.items],
                "fallback_from": actual_tool,
            })
            if fb_result.items:
                degraded_from = actual_tool
                result = fb_result
                actual_tool = fb_actual
                failed = False
        step["status"] = "failed" if failed else "done"
        step["result_count"] = len(result.items)
        step["tool_name"] = actual_tool

        new_plan = [
            step if s["step_index"] == step["step_index"] else s for s in plan
        ]
        evidence_patch = [
            {**item, "step_index": step["step_index"]} for item in result.items
        ]
        call_record = {
            "step_index": step["step_index"],
            "tool_name": actual_tool,
            "arguments": {"query": step["description"]},
            "result_count": len(result.items),
            "elapsed_ms": result.elapsed_ms,
            "error": result.error,
            "error_class": result.error_class,
            # 工具失败的统一错误码（`A-10` 接线 4201）：此前只有自由文本 error，
            # 前端与排障都得靠字符串匹配——「这次是工具失败」还是「超时」分不清。
            "error_code": int(ErrCode.TOOL_EXECUTION_FAILED) if result.error else 0,
            "truncated_from": result.truncated_from,
            **({"fallback_from": degraded_from} if degraded_from else {}),
        }
        error_patch = []
        if result.error:
            error_patch.append({"step_index": step["step_index"], "tool": actual_tool, "error": result.error})
        elif not result.items:
            error_patch.append({"step_index": step["step_index"], "tool": actual_tool, "error": "无结果（0 条）"})

        _emit(config, "tool_end", {
            "step_index": step["step_index"], "tool": actual_tool,
            "elapsed_ms": result.elapsed_ms, "hit_count": len(result.items),
            "error": result.error, "error_class": result.error_class,
            "error_code": int(ErrCode.TOOL_EXECUTION_FAILED) if result.error else 0,
            "truncated_from": result.truncated_from,
            "tool_hallucinated": hallucinated,
            "evidence": evidence_patch,  # 契约：tool_end 携带该步证据（轨迹↔证据双向定位）
        })
        return {
            "plan": new_plan,
            "evidence": evidence_patch,
            "executed_steps": [step["step_index"]],
            "tool_calls": [call_record],
            "tool_errors": error_patch,
            "call_fingerprints": [fingerprint],
            "iteration": state.get("iteration", 0) + 1,
            "current_step": step["step_index"],
            "degraded": state.get("degraded", False) or bool(result.error),
            # 预算计数进 state。HITL 恢复时若不带回计数，用户反复
            # resume 就能无限绕过 token 预算——预算必须跨中断点保持单调。
            **({"budget": budget.snapshot()} if budget is not None else {}),
        }

    def reflect_node(state: AgentState, config: RunnableConfig) -> dict:
        iteration = state.get("iteration", 0)
        max_iterations = state.get("max_iterations", 4)
        evidence = state.get("evidence", [])
        prev_count = state.get("last_evidence_count", 0)

        # 工具层已判定"无可执行步骤"：此时再问 LLM"够不够"没有意义（它答什么都对
        # 收敛没帮助），直接升级重规划并累加无进展计数。
        if state.get("tool_exhausted"):
            if iteration >= max_iterations:
                _emit(config, "fuse", {"reason": "iteration_limit", "iteration": iteration, "max": max_iterations})
                return {
                    "next_action": "answer", "status": "circuit_breaker",
                    "incomplete": True, "fuse_reason": "iteration_limit",
                    "tool_exhausted": False,
                }
            no_progress = state.get("no_progress_rounds", 0) + 1
            if no_progress >= no_progress_limit:
                _emit(config, "fuse", {"reason": "no_progress", "iteration": iteration})
                return {
                    "next_action": "answer", "status": "circuit_breaker",
                    "incomplete": True, "fuse_reason": "no_progress",
                    "no_progress_rounds": no_progress,
                    "last_evidence_count": len(evidence),
                    "tool_exhausted": False,
                }
            _emit(config, "reflect", {
                "evidence_score": state.get("evidence_score", 0.0),
                "next_action": "replan",
                "reason": "无可执行步骤（步骤耗尽或依赖被阻塞），升级重规划",
            })
            return {
                "next_action": "replan",
                "no_progress_rounds": no_progress,
                "last_evidence_count": len(evidence),
                "tool_exhausted": False,
            }

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
        # 反思阶段的真实 token 用量
        _emit_usage(config, "think", reflector)
        next_action = verdict["next_action"]
        score = verdict["score"]

        # 本轮反思结论先落事件：熔断分支会直接 return，
        # 若只在末尾 emit，前端收不到 reflect/iteration，进度条会停在上一轮出现空档。
        _emit(config, "reflect", {
            "evidence_score": score, "next_action": next_action, "reason": verdict["reason"],
        })
        _emit(config, "iteration", {"current": iteration, "max": max_iterations})

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

        return {
            "evidence_score": score,
            "next_action": next_action,
            "reflect_reason": verdict["reason"],  # 决策理由落 state，快照可复盘
            "no_progress_rounds": no_progress,
            "last_evidence_count": len(evidence),
        }

    def think_node(state: AgentState, config: RunnableConfig) -> dict:
        """ReAct 单步决策节点。

        与 `plan` 节点的根本区别：plan 一次性生成全量清单且**不可变**，
        think 每轮基于最新观察重新决策——这是"Agent 自主性"的实质所在。

        配置：
        - `agent_mode=react` 时才进入本节点（由 `entry_router` 决定）
        - `thinker` 由 runner 注入；缺失则降级到 plan_execute 路径
        """
        cfg = config.get("configurable") or {}
        thinker = cfg.get("thinker")
        if thinker is None:
            # 未注入 thinker（未启用 react）：退回计划式路径
            return {"next_action": "plan_fallback", "react_degraded": ["no_thinker"]}

        budget = cfg.get("budget")
        if budget is not None and budget.exhausted():
            _emit(config, "fuse", {"reason": f"budget_{budget.exhausted()}",
                                   "iteration": state.get("iteration", 0)})
            return {
                "next_action": "answer", "status": "budget_exhausted",
                "incomplete": True, "fuse_reason": f"budget_{budget.exhausted()}",
            }

        question = state.get("question", "")
        observations = observations_from_tool_results(state.get("tool_calls", []))
        catalog = (cfg.get("tool_catalog") or [])
        evidence = state.get("evidence", [])

        outcome = thinker.think(
            question, observations, catalog, evidence_count=len(evidence),
            # 把该租户的经验带进思考（如"这类问题库里通常没有"）
            budget_hint=str(cfg.get("memory_hint") or ""),
        )
        if outcome.degraded:
            logger.warning("ReAct 降级为 plan_execute: %s", outcome.degraded_reason)
            degraded = list(state.get("react_degraded", []))
            degraded.append(outcome.degraded_reason)
            return {"next_action": "plan_fallback", "react_degraded": degraded}

        decision = outcome.decision
        _emit(config, "thought", {
            "thought": decision.thought,
            "done": decision.done,
            "iteration": state.get("iteration", 0),
        })

        # 把 LLM 的决策落成"计划步骤"，复用既有的 tool 节点执行（最小改动）
        steps: list[dict] = []
        for idx, call in enumerate(decision.tool_calls):
            steps.append({
                "step_index": idx,
                "description": str(call.arguments.get("query") or call.purpose or question),
                "tool_name": call.name,
                "depends_on": [],
                "status": "pending",
                "result_count": 0,
                "arguments": dict(call.arguments),
                "thought": decision.thought,
            })

        patch: dict = {
            "plan": steps,
            "next_action": "answer" if (decision.done or not steps) else "continue",
            "react_rounds": int(state.get("react_rounds", 0)) + 1,
        }
        if decision.answer_hint:
            patch["answer_hint"] = decision.answer_hint
        if decision.thought:
            patch["last_thought"] = decision.thought
        return patch

    def verify_node(state: AgentState, config: RunnableConfig) -> dict:
        """答案校验节点。

        与 `reflect` 的区别：reflect 评的是**证据够不够**（要不要继续找），
        verify 评的是**答案站不站得住**（每句话是否有据、引用是否有效）。
        两者互补——证据充足但答案胡说是有可能的（生成阶段幻觉）。

        行为：低分 → 回退 `replan` 重来一轮（受 `max_verify_rounds` 与预算约束），
        而不是直接把可疑答案发给用户。默认关闭（`agent_verify_enabled`）：
        它要多花一次 LLM 调用，且会让"先答后改"变成"可能答两次"。
        """
        cfg = config.get("configurable") or {}
        checker = cfg.get("verifier")
        if checker is None:
            return {"verify_passed": True}

        rounds = int(state.get("verify_rounds", 0))
        max_rounds = int(cfg.get("max_verify_rounds", 1))
        budget = cfg.get("budget")
        answer = state.get("final_answer", "")
        evidence = state.get("evidence", [])
        if not answer or not evidence:
            return {"verify_passed": True}
        if rounds >= max_rounds:
            _emit(config, "verify", {"passed": False, "reason": "已达校验次数上限", "rounds": rounds})
            return {"verify_passed": True, "verify_rounds": rounds}  # 上限到了就放行
        if budget is not None and not budget.can_afford(tokens=600):
            _emit(config, "verify", {"passed": False, "reason": "预算不足，跳过校验"})
            return {"verify_passed": True}

        evidence_block = "\n".join(
            f"[{i + 1}]（来源：{e.get('source', '未知')}）\n{e.get('content', '')}"
            for i, e in enumerate(evidence[:4])
        )
        try:
            result = checker.check(answer, evidence_block)
            unsupported = list(getattr(result, "unsupported_claims", []) or [])
        except Exception as exc:  # noqa: BLE001 - 校验失败不阻断出答
            logger.warning("答案校验失败（放行）：%s", exc)
            return {"verify_passed": True}

        passed = not unsupported
        _emit(config, "verify", {
            "passed": passed, "unsupported": len(unsupported), "rounds": rounds + 1,
        })
        if passed:
            return {"verify_passed": True, "verify_rounds": rounds + 1}
        # 不通过 → 回退重规划，并把"哪些话没依据"作为线索带回去
        logger.info("答案校验未通过（%d 条无据陈述），回退重规划", len(unsupported))
        return {
            "verify_passed": False,
            "verify_rounds": rounds + 1,
            "verify_notes": unsupported[:3],
            "next_action": "replan",
            "evidence_score": 0.0,  # 让 reflect 不再认为"证据已足"，避免被对称纠正拦回 answer
        }

    def replan_node(state: AgentState, config: RunnableConfig) -> dict:
        _emit(config, "replan", {"iteration": state.get("iteration", 0)})
        return {"needs_replan": True}  # plan 节点消费此标记生成新计划

    def answer_node(state: AgentState, config: RunnableConfig) -> dict:
        cfg = config.get("configurable") or {}
        emit = cfg.get("emit")
        evidence = list(state.get("evidence", []))
        evidence_score = float(state.get("evidence_score", 0.0) or 0.0)
        # `ST-03`：组装阶段被丢弃的条目（→ 状态 `dropped_evidence`）
        dropped: list[dict] = []

        # 通用知识兜底（doc/analysis/通用知识兜底回答.md §6.4）与 HITL 的边界：
        # - 零证据：直接兜底——没有可审的证据，挂起等人工也等不到更多信息；
        # - 有证据但低分：HITL 优先——开启 HITL 即表明"低置信要人工确认"；
        #   用户 hint / force_answer 授权后按证据作答（此时兜底会违背授权语义）；
        # - HITL 未启用或未介入时，低分证据走兜底。
        if state.get("skip_retrieval"):
            prompt = state["question"]  # 闲聊直答
            answer_source = ""
        elif not evidence:
            if parametric_fallback_enabled:
                prompt = GENERAL_KNOWLEDGE_PROMPT.format(question=state["question"])
                answer_source = "parametric"
            else:
                return {"final_answer": "", "refusal": True, "status": _final_status(state),
                        "answer_source": ""}
        else:
            # ----  人在环（HITL）：仅证据路径可挂起 ----
            # 位置很关键：必须在**任何副作用之前**（这里指 LLM 流式输出与 token 事件）。
            # LangGraph 恢复时会**从头重跑整个节点**，若在 interrupt() 之前已经 emit 过
            # token，恢复后会重复发一遍——前端会看到答案被拼两次。
            force_answered = False
            hint_text = ""
            hitl = (config.get("configurable") or {}).get("hitl")
            if hitl is not None and not state.get("hitl_resolved"):
                reason = hitl_trigger(state, hitl)
                if reason:
                    decision = interrupt({
                        "run_id": (config.get("configurable") or {}).get("thread_id", ""),
                        "reason": reason,
                        "question": state["question"],
                        "evidence_score": round(float(state.get("evidence_score", 0.0)), 4),
                        "evidence_count": len(evidence),
                        "iteration": state.get("iteration", 0),
                        "refusal": bool(state.get("refusal")),
                        "actions": ["hint", "force_answer", "stop"],
                    })
                    action, hint = _parse_hitl_decision(decision)
                    if action == "stop":
                        _emit(config, "hitl_resolved", {"action": "stop", "reason": reason})
                        return {
                            "final_answer": "",
                            "refusal": True,
                            "incomplete": True,
                            "status": "cancelled",
                            "hitl_resolved": "stop",
                            "answer_source": "",
                        }
                    if hint:
                        hint_text = f"\n\n（用户补充线索：{hint}）"
                    if action == "force_answer":
                        force_answered = True
                    _emit(config, "hitl_resolved", {"action": action, "reason": reason})

            if (
                parametric_fallback_enabled
                and not force_answered
                and not hint_text
                and evidence_score < parametric_fallback_score_threshold
            ):
                # 有证据但反思综合分不足，且无人工作答授权 → 通用知识兜底
                prompt = GENERAL_KNOWLEDGE_PROMPT.format(question=state["question"])
                answer_source = "parametric"
            else:
                # `ST-03`：Agent 证据接入**统一上下文组装**——
                # 预算取「上下文预算」，父块查找用可注入的 docstore；
                # 被裁掉的条目回传状态（→ `context_breakdown.dropped`）。
                prompt = prompt_builder(
                    state["question"],
                    state.get("evidence", []),
                    token_budget=int(cfg.get("context_token_budget") or 0),
                    parent_lookup=cfg.get("parent_lookup"),
                    dropped_out=dropped,
                    # `ST-01` 阶段 B：记忆 / 历史进答案提示词（仅 structured 模式消费；
                    # legacy 模式下这些参数完全不被读取，行为逐字不变）。
                    memory=str(cfg.get("memory_hint") or ""),
                    history_summary=str(state.get("history_summary") or ""),
                    history_recent=list(state.get("history_recent") or []),
                    structured=bool(cfg.get("context_partition_render") == "structured"),
                )
                answer_source = "evidence"
                if state.get("incomplete"):
                    prompt += "\n\n（注意：检索已达执行上限，请基于现有证据尽力回答，并在开头说明答案可能不完整。）"
                if hint_text:
                    prompt += hint_text
                if force_answered:
                    prompt += (
                        "\n\n（用户已知悉证据可能不足并授权作答：请基于现有证据尽力回答，"
                        "并在开头明确说明答案可能不完整。）"
                    )

        # 生成前的预算闸门：剩余额度不够"一次完整生成"的最坏开销时就不开始生成。
        # 生成完才发现超限 = 钱已经花了，闸门形同虚设（与 tool_node 的闸门同一原则）。
        # 额度不足时**降级作答**而不是拒绝服务（预算模块的设计：不烧钱，但给用户交代）。
        budget = (config.get("configurable") or {}).get("budget")
        if budget is not None and not budget.can_afford(tokens=ANSWER_TOKEN_RESERVE):
            reason = budget.exhausted() or "token"
            logger.info("预算不足（%s），跳过答案生成并降级作答", reason)
            _emit(config, "fuse", {"reason": f"budget_{reason}", "iteration": state.get("iteration", 0)})
            return {
                "final_answer": "本次预算不足，未生成答案。可缩小问题范围或稍后重试。",
                "refusal": True,
                "incomplete": True,
                "status": "budget_exhausted",
                "fuse_reason": f"budget_{reason}",
                "answer_source": "",
                **({"budget": budget.snapshot()} if budget is not None else {}),
            }

        # ---- VLM 直答（图文，M5）：证据里有原图且问题在问图时，直接看图作答 ----
        # 位置在**生成之前**：直答失败就回落下面的常规文本路径（caption 证据），
        # 与重排降级同一纪律——任何一步都不能让整个回答失败。
        vlm = deps.get("vlm")
        if vlm is not None and not state.get("skip_retrieval"):
            try:
                # 破环：multimodal 依赖 agent 侧工具
                # 破环：multimodal 依赖 agent 侧工具
                from app.services.multimodal import image_path_of, is_visual_question

                assets_dir = (config.get("configurable") or {}).get("assets_dir")
                if is_visual_question(state["question"], evidence):
                    img = image_path_of(evidence, assets_dir)
                    if img is not None:
                        context = "\n".join(str(e.get("content") or "") for e in evidence[:3])
                        direct = vlm.answer(img, state["question"], context)
                        if direct and direct.strip():
                            if emit:
                                emit("token", {"content": direct})
                            return {
                                "final_answer": direct,
                                "status": _final_status(state),
                                "answer_source": "vlm",
                            }
            except Exception as exc:  # noqa: BLE001 - 直答失败必须回落，不能让回答挂掉
                logger.warning("VLM 直答失败，回落 caption 文本路: %s", exc)

        # `ST-04`：输出侧 token 上限——由「剩余预算 − 预留」反推。
        # 预留 `ANSWER_TOKEN_RESERVE` 是给后续步骤留的；本次生成只能用它剩下的部分。
        budget = (config.get("configurable") or {}).get("budget")
        max_output: int | None = None
        if budget is not None:
            max_output = budget.max_output_tokens(reserve=ANSWER_TOKEN_RESERVE)
            if not max_output:
                # 预算不允许生成（额度耗尽 / 预留吃掉剩余）→ 不发起调用
                _emit(config, "fuse", {"reason": "budget_output_exhausted", "incomplete": True})
                return {
                    "final_answer": "",
                    "status": "incomplete",
                    "fuse_reason": "budget_output_exhausted",
                    "answer_source": "",
                }

        chunks: list[str] = []
        try:
            for piece in deps["llm"].stream(prompt, max_tokens=max_output):
                chunks.append(piece)
                if emit:
                    emit("token", {"content": piece})
        except TypeError:
            # 适配器不支持 `max_tokens`（第三方实现 / 测试替身）→ 退回旧调用面
            chunks = []
            for piece in deps["llm"].stream(prompt):
                chunks.append(piece)
                if emit:
                    emit("token", {"content": piece})
        answer = "".join(chunks)

        # 兜底重生成（仅一次）：证据路径下模型仍输出拒答话术 → 切换为通用知识。
        # 覆盖「反思分刚过阈值但证据实际不足」的残余缺口（模型比阈值更诚实）。
        # token 经 runner 缓冲后按终稿重放，中间文本不会泄露给前端。
        if answer.strip().startswith(NO_INFO_ANSWER) and parametric_fallback_enabled:
            answer = deps["llm"].generate(GENERAL_KNOWLEDGE_PROMPT.format(question=state["question"]))
            answer_source = "parametric"
        return {
            "final_answer": answer,
            "status": _final_status(state),
            "answer_source": answer_source,
            # `ST-03`：被裁 / 被去重的证据条目（前端据此解释「为什么没进上下文」）
            "dropped_evidence": dropped,
        }

    # ---------- 路由 ----------

    def entry_router(state: AgentState, config: RunnableConfig) -> str:
        """入口路由。

        注入 `thinker` 时走 ReAct（think → act → observe → think），
        否则维持既有的 plan → tool → reflect 计划式路径。
        **模式由配置决定**，不由请求决定——避免"同一个问题这次走哪条路"不可预测。
        """
        if state.get("skip_retrieval"):
            return "answer"
        if (config.get("configurable") or {}).get("thinker") is not None:
            return "think"
        return "plan"

    def verify_router(state: AgentState, config: RunnableConfig) -> str:
        """是否需要跑校验节点。

        只有配置注入了 `verifier` 才走校验——默认关闭时这一步是**零成本跳过**，
        图拓扑多一个节点但行为与改造前完全一致（不引入额外调用与延迟）。
        """
        if (config.get("configurable") or {}).get("verifier") is None:
            return "end"
        # 已拒答/已取消/预算耗尽的收尾不必再校验：没有答案可校验
        if state.get("refusal") or state.get("status") in ("cancelled", "budget_exhausted"):
            return "end"
        # 通用知识兜底回答本就无据可核，verify 会判幻觉并回退 replan 形成无意义回环
        if state.get("answer_source") == "parametric":
            return "end"
        return "verify"

    def verify_result_router(state: AgentState) -> str:
        """校验不通过 → 回退重规划；重规划余量不足时由 reflect_router 兜底收敛。"""
        if state.get("verify_passed") is False:
            return "replan"
        return "end"

    def react_router(state: AgentState, config: RunnableConfig) -> str:
        """react 模式下 think 的出口。

        - `answer`：LLM 判定信息已足
        - `tool`：执行本轮决策出的工具
        - `plan_fallback`：think 不可用/解析失败 → 退回计划式路径
        """
        action = state.get("next_action", "answer")
        if action == "continue" and state.get("plan"):
            return "tool"
        if action == "plan_fallback":
            return "plan"
        return "answer"

    def reflect_router(state: AgentState, config: RunnableConfig) -> str:
        action = state.get("next_action", "answer")
        # react 模式下不要走 replan/plan 路径——那是计划式的产物。
        # 回到 think 让 LLM 基于最新观察重新决策（这才是 ReAct 的循环）。
        if (config.get("configurable") or {}).get("thinker") is not None:
            if action in ("continue", "replan"):
                return "think"
            return "answer"
        # 重规划余量耗尽 → 强制作答（不再放行 replan）
        if action == "replan" and state.get("replan_count", 0) >= MAX_REPLANS:
            return "answer"
        return {"continue": "tool", "replan": "replan", "answer": "answer"}.get(action, "answer")

    graph = StateGraph(AgentState)
    graph.add_node("plan", plan_node)
    graph.add_node("think", think_node)
    graph.add_node("tool", tool_node)
    graph.add_node("reflect", reflect_node)
    graph.add_node("replan", replan_node)
    graph.add_node("answer", answer_node)
    graph.add_node("verify", verify_node)

    graph.add_conditional_edges(
        START, entry_router, {"plan": "plan", "think": "think", "answer": "answer"}
    )
    graph.add_conditional_edges(
        "think", react_router, {"tool": "tool", "answer": "answer", "plan": "plan"}
    )
    graph.add_edge("plan", "tool")
    graph.add_edge("tool", "reflect")
    graph.add_conditional_edges(
        "reflect", reflect_router,
        {"think": "think", "tool": "tool", "replan": "replan", "answer": "answer"},
    )
    graph.add_edge("replan", "plan")
    # answer 之后接独立校验节点。图里 answer→END 的直连改成条件边，
    # 未启用校验时（verifier=None）verify_node 直接放行，仍等价于原来的一步到底。
    graph.add_conditional_edges(
        "answer", verify_router, {"verify": "verify", "end": END}
    )
    graph.add_conditional_edges(
        "verify", verify_result_router, {"replan": "replan", "end": END}
    )
    # checkpointer 是断点续跑 / 多轮状态继承 / 完整回放 / HITL 的共同前提。
    # 刻意**不加** `interrupt_before`：那是**静态**中断（指定节点每次都挂起），
    # 而 HITL 需要的是「低置信才挂起」的条件性中断——必须由  在
    # answer 节点内部动态调用 `interrupt()`，静态声明会让该节点每次都停。
    return graph.compile(checkpointer=checkpointer)
