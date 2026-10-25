"""ReAct 单步决策循环。

## 要解决的根本问题

改造前的 Agent 是 **plan-then-execute**：开工前一次性生成 1~6 步清单，
之后 `tool` 节点只是照着清单执行 if-else（选哪个工具、拼什么入参、判定成功与否
全部硬编码）。LLM 只在「开工前规划」与「事后打分」两个位置出现，
**执行过程中没有任何决策权**。后果：

- 第 1 步检索到了新信息，第 2 步无法据此调整（计划不可变）；
- 工具返回"参数不合法"或"实体未命中"，模型看不到、也无法换路；
- 补一步「根据上一步结果再查一次」在架构上做不到。

## ReAct 的做法

每一轮由 LLM 基于**当前全部观察**决定下一步：

```
think   → {thought, tool_calls[], done, answer_hint}
   ↓（done=false）
act     → 执行 tool_calls（结构化入参，逐个独立超时）
   ↓
observe → 归一化结果 + 压缩进上下文 + 累计预算
   ↓
think（下一轮）
```

## 兼容与灰度

`agent_mode` 控制走哪条路径，默认仍是 `plan_execute`：

- **为什么默认不切**：ReAct 每轮多一次 LLM 调用（成本上升），且行为变化大，
  在**没有评测集**（ 未完成）的情况下贸然切换等于"无法验证的改动"。
  文档已把  排在批次 A，但当前批次 A 只完成了 CI 与死断言清理。
- 因此本次实现的是**可运行的 ReAct 引擎 + 图节点**，通过配置开启，
  并用测试覆盖其正确性（含降级到 plan_execute 的路径）。

## 失败语义（与文档 §7 F1 一致）

| 失败 | 行为 |
| --- | --- |
| think 返回非法 JSON | 重试 1 次 → 仍失败则**降级为 plan_execute**并标 degraded |
| think 返回未知工具名 | 交 registry 幻觉兜底，观察里显式告知模型"该工具不存在" |
| think 超时 | 同解析失败 |
| think 说 done 但证据为空 | **忽略 done**，强制至少一轮 act（防"幻觉式跳过检索"） |
"""
import logging
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.core.otel import span
from app.services.llm import LLM, LLMError
from app.services.query.jsonutil import JsonParseError, parse_json_model

logger = logging.getLogger(__name__)

REACT_MAX_TOOL_CALLS_PER_TURN = 3

REACT_PROMPT = """你是一个能调用工具的检索助手。请**基于已有观察**决定下一步。

可用工具：
{tool_catalog}

用户问题：{question}

{budget_hint}
到目前为止你已经看到的信息：
{observations}

要求：
1. 每一步先想清楚"我为什么需要这个工具"，写进 thought（≤100 字）。
2. 如果已有信息足以回答，返回 done=true 并给出 answer_hint。
3. 否则返回 1~{max_tools} 个 tool_calls，每个都要填 arguments。
4. **不要重复调用完全相同的工具与参数**——那不会带来新信息。
5. 若某个工具连续报错，换一个工具或换个查询角度。

只输出 JSON：
{{
  "thought": "我的思考",
  "tool_calls": [
    {{"name": "工具名", "arguments": {{"query": "..."}}, "purpose": "为什么要调"}}
  ],
  "done": false,
  "answer_hint": ""
}}
"""


class ToolCallIntent(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    purpose: str = ""


class ThinkDecision(BaseModel):
    thought: str = ""
    tool_calls: list[ToolCallIntent] = Field(default_factory=list)
    done: bool = False
    answer_hint: str = ""


@dataclass
class Observation:
    """一次工具调用的观察结果（喂回给下一次 think）。"""

    tool: str
    arguments: dict
    summary: str          # 已压缩的摘要（不是原文，避免撑爆上下文）
    hit_count: int = 0
    error: str = ""

    def to_prompt_line(self) -> str:
        head = f"- {self.tool}({_brief_args(self.arguments)})"
        if self.error:
            return f"{head} → 失败：{self.error}"
        if not self.hit_count:
            return f"{head} → 无结果"
        return f"{head} → {self.hit_count} 条：{self.summary}"


@dataclass
class ThinkOutcome:
    """think 的结果 + 诊断信息（degraded 由调用方决定是否切模式）。"""

    decision: ThinkDecision
    degraded: bool = False
    degraded_reason: str = ""


def _brief_args(args: dict) -> str:
    parts = []
    for k, v in list((args or {}).items())[:3]:
        text = str(v)
        parts.append(f"{k}={text[:24]}")
    return ", ".join(parts)


class ReActThinker:
    """单步决策器：调 LLM 产出结构化决策 + 解析容错 + 幻觉兜底。"""

    def __init__(self, llm: LLM, max_retries: int = 1):
        self._llm = llm
        self._max_retries = max(0, max_retries)

    def think(
        self,
        question: str,
        observations: list[Observation],
        tool_catalog: list[dict],
        evidence_count: int = 0,
        budget_hint: str = "",
        max_tools: int = REACT_MAX_TOOL_CALLS_PER_TURN,
    ) -> ThinkOutcome:
        """产出下一步决策。解析失败**不抛异常**，返回 degraded 让上层降级。

        这比抛异常更合适：ReAct 是"增强路径"，解析失败时退回 plan_execute
        仍能给出答案；直接抛错会让整个问答失败。
        """
        catalog_text = _render_catalog(tool_catalog)
        obs_text = "\n".join(o.to_prompt_line() for o in observations) or "（尚未调用任何工具）"
        prompt = REACT_PROMPT.format(
            tool_catalog=catalog_text,
            question=question,
            observations=obs_text,
            budget_hint=budget_hint,
            max_tools=max_tools,
        )
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                with span("agent.think", **{"kquery.attempt": attempt}) as sp:
                    raw = self._llm.generate(prompt)
                    sp.set_attribute("kquery.observations", len(observations))
                decision = parse_json_model(raw, ThinkDecision)
            except (JsonParseError, LLMError, ValidationError) as exc:
                last_error = exc
                logger.warning("ReAct think 第 %d 次失败: %s", attempt + 1, exc)
                continue
            except Exception as exc:  # noqa: BLE001 - 模型层任何异常都走降级
                last_error = exc
                logger.warning("ReAct think 异常: %s", exc)
                continue

            decision.thought = (decision.thought or "").strip()[:200]
            decision.tool_calls = [
                c for c in decision.tool_calls if str(c.name or "").strip()
            ][:max_tools]
            # 防幻觉式跳过检索：自称完成但没有任何证据支撑 → 忽略 done
            if decision.done and evidence_count == 0 and not observations:
                logger.info("ReAct think 声称完成但无证据，忽略 done 强制检索")
                decision.done = False
                decision.answer_hint = decision.answer_hint or ""
                if not decision.tool_calls:
                    # 连工具都没提，给一个兜底检索意图，避免"空转 → 立刻 done"死循环
                    decision.tool_calls = [ToolCallIntent(
                        name=next((t["name"] for t in tool_catalog), "vector_search"),
                        arguments={"query": question},
                        purpose="无证据时至少检索一次",
                    )]
            return ThinkOutcome(decision=decision)

        logger.warning("ReAct think 连续失败，降级为 plan_execute: %s", last_error)
        return ThinkOutcome(
            decision=ThinkDecision(),
            degraded=True,
            degraded_reason=f"react_think_parse: {last_error}" if last_error else "react_think_parse",
        )


def _render_catalog(catalog: list[dict]) -> str:
    lines = []
    for t in catalog:
        props = (t.get("parameters") or {}).get("properties") or {}
        args_desc = ", ".join(props) if props else "query"
        danger = t.get("danger", "")
        suffix = f"（{danger}）" if danger and danger != "read_only" else ""
        lines.append(f"- {t['name']}: {t.get('description', '')}｜参数：{args_desc}{suffix}")
    return "\n".join(lines) or "（无可用工具）"


def observations_from_tool_results(
    calls: list[dict], summarize=None, summary_chars: int = 160
) -> list[Observation]:
    """把工具调用记录转成观察项。

    `summarize` 可选——不传时按内容前 N 字截断（**必须截断**：观察会累积进
    每一轮 prompt，全文塞入会让 token 成本随轮数平方增长）。
    """
    out: list[Observation] = []
    for call in calls or []:
        items = call.get("items") or []
        error = call.get("error") or ""
        if error:
            out.append(Observation(
                tool=call.get("tool_name", "?"), arguments=call.get("arguments") or {},
                summary="", hit_count=0, error=str(error)[:120],
            ))
            continue
        text = ""
        if summarize is not None:
            text = summarize(items)
        elif items:
            text = " / ".join(
                str(i.get("content", ""))[:summary_chars // 2] for i in items[:2]
            )
        out.append(Observation(
            tool=call.get("tool_name", "?"), arguments=call.get("arguments") or {},
            summary=text[:summary_chars], hit_count=len(items),
        ))
    return out
