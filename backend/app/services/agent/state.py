"""Agent 状态定义。

合并规则在此集中声明，不允许散落各节点：
- 累积型字段（evidence / executed_steps / tool_calls / tool_errors）：追加合并 + 去重；
- 结论型字段（score / next_action / final_answer 等）：覆盖；
- iteration 单调递增，不因重规划清零（否则可绕过熔断）。

状态必须是纯基础类型结构（dict/list/str/int/float/bool），
保证会话快照跨版本可读（缺字段按默认值处理）。
"""
import operator
from typing import Annotated, Any, TypedDict


def replace_list(old: list, new: list) -> list:
    """覆盖语义 reducer（用于 plan）。

    plan 与 evidence/tool_calls 的语义不同：后三者按轮**追加**，而 plan 是
    "全量快照替换"（plan_node 追加新步骤后返回完整列表，tool_node 改单步状态后
    也返回完整列表）。此前 plan 未声明 reducer、走 TypedDict 默认覆盖，行为正确
    但意图不显式——一旦有人误以为它是追加语义而只返回增量，整张计划会被静默丢失。
    这里显式声明为"替换"，把契约写死在代码里。
    """
    return new


def merge_evidence(old: list[dict], new: list[dict]) -> list[dict]:
    """证据追加合并 + 按 chunk_id 去重（先出现者优先，保留来源步骤号）。"""
    seen = {e.get("chunk_id") for e in old}
    merged = list(old)
    for e in new:
        if e.get("chunk_id") not in seen:
            seen.add(e.get("chunk_id"))
            merged.append(e)
    return merged


def merge_executed(old: list[int], new: list[int]) -> list[int]:
    """已执行步骤号追加去重。"""
    return sorted(set(old) | set(new))


def merge_errors(old: list[dict], new: list[dict]) -> list[dict]:
    """工具失败记录追加（保留失败步骤与原因，区分"没有信息"与"工具坏了"）。"""
    return old + new


class AgentState(TypedDict, total=False):
    """图执行的唯一事实来源。全部字段为纯基础类型。"""

    # ---- 输入 ----
    question: str  # 本轮问题（覆盖）
    tenant_id: str
    history: list[dict]  # 历史轮次 [{question, answer}]（覆盖，由 runner 注入）

    # ---- 规划 ----
    # 全量快照替换语义（节点必须返回完整 plan，不得只返回增量）
    plan: Annotated[list[dict], replace_list]
    current_step: int

    # ---- 累积（追加合并）----
    evidence: Annotated[list[dict], merge_evidence]
    executed_steps: Annotated[list[int], merge_executed]
    tool_calls: Annotated[list[dict], operator.add]  # [{step_index, tool_name, arguments, result_count, elapsed_ms, error}]
    tool_errors: Annotated[list[dict], merge_errors]
    call_fingerprints: Annotated[list[str], operator.add]  # "tool::query" 指纹，循环检测用

    # ---- 控制（覆盖）----
    iteration: int  # 已迭代轮数，单调递增
    max_iterations: int  # 动态上限 = 计划步骤数 + 2 轮重规划余量
    no_progress_rounds: int  # 连续"继续但证据未增加"的轮数（第二层熔断判据）
    last_evidence_count: int  # 上次反思时的证据总量（无效进展判据）
    needs_replan: bool  # 重规划标记（replan 节点置位，plan 节点消费）
    replan_count: int  # 已重规划次数（余量管控）
    evidence_score: float  # 最近一次反思的充分度
    reflect_reason: str  # 最近一次反思的决策理由（可复盘"为什么继续查/为什么作答"）
    next_action: str  # answer / continue / replan
    tool_exhausted: bool  # 工具层判定"无可执行步骤"（reflect 据此短路，避免信号被覆盖）
    status: str  # running / completed / circuit_breaker / failed
    degraded: bool  # 是否发生降级（工具不可用 / 重排降级等）
    incomplete: bool  # 熔断退出后尽力作答 → 结果可能不完整
    fuse_reason: str  # 熔断原因（iteration_limit / no_progress）

    # ---- 输出 ----
    final_answer: str
    answer_source: str  # 答案来源：evidence（有据）/ parametric（通用知识兜底）/ ""（拒答）
    skip_retrieval: bool  # 闲聊短路
    hitl_resolved: str  # 人工介入结果（""/hint/force_answer/stop），防二次打断
    budget: dict  # 预算快照（跨 HITL 中断点保持计数单调，防 resume 绕过）
    verify_passed: bool  # 答案校验是否通过
    verify_rounds: int  # 已校验轮数（受 max_verify_rounds 约束）
    verify_notes: list[str]  # 未通过时带回的"无据陈述"线索
    # ---- ReAct 循环 ----
    # 注意这些字段**必须声明**：LangGraph 只持久化 TypedDict 里声明过的键，
    # 未声明的 key 会被静默丢弃（表现为"节点明明 return 了，读状态时却没有"）。
    react_rounds: int  # 已进行的 ReAct 轮数
    react_degraded: Annotated[list[str], operator.add]  # 降级原因（追加合并）
    last_thought: str  # 最近一次 think 的思考（供轨迹展示与排障）
    answer_hint: str  # LLM 给出的作答指引
    refusal: bool  # 证据不足拒答


def initial_state(question: str, tenant_id: str, history: list[dict] | None = None) -> dict:
    """新一轮执行的初始状态 patch。"""
    return {
        "question": question,
        "tenant_id": tenant_id,
        "history": history or [],
        "plan": [],
        "current_step": 0,
        "evidence": [],
        "executed_steps": [],
        "tool_calls": [],
        "tool_errors": [],
        "iteration": 0,
        "max_iterations": 4,  # 规划后按步骤数重设
        "no_progress_rounds": 0,
        "last_evidence_count": 0,
        "needs_replan": False,
        "replan_count": 0,
        "evidence_score": 0.0,
        "next_action": "",
        "status": "running",
        "degraded": False,
        "incomplete": False,
        "fuse_reason": "",
        "final_answer": "",
        "answer_source": "",
        "skip_retrieval": False,
        "refusal": False,
    }


def sanitize_snapshot(payload: Any) -> dict:
    """跨版本读取：旧快照缺字段按默认补齐，多余字段保留（向前兼容）。"""
    base = initial_state("", "default")
    if not isinstance(payload, dict):
        return base
    merged = {**base, **payload}
    # 类型兜底：关键字段类型不符时回退默认
    for key, default in base.items():
        if not isinstance(merged.get(key), type(default)):
            merged[key] = default
    return merged
