"""Plan 节点（阶段 6 §4.2）。

- 生产：LLMPlanner（qwen-plus，JSON 输出约束）——档位过低会规划出无效步骤；
- 测试/无 Key：HeuristicPlanner（按问题形态产出确定性计划）；
- 解析纪律：步骤号从 0 连续、描述可直接作为检索查询、工具名幻觉纠偏、依赖构成 DAG；
- Plan 质量三判定：可执行 / 不冗余 / 无环依赖。
"""
import json
import logging
import re
from typing import Protocol

from app.services.agent.tools import ToolRegistry
from app.services.llm import LLM

logger = logging.getLogger(__name__)

PLAN_PROMPT = """你是任务规划器。根据用户问题和可用工具，生成一份有序的检索/计算计划。

## 可用工具
{tool_catalog}

## 要求
1. 每步的描述必须是可以直接作为检索查询的具体表述（禁止"了解一下""分析一下"这类无法落地的步骤）；
2. 步骤之间不冗余（两步不检索同一件事）；
3. 依赖关系构成有向无环图；
4. 简单事实型问题只需 1 步；多实体对比/多跳问题拆成多步；
5. 最多 {max_steps} 步。

## 输出（严格 JSON，不要输出其他内容）
{{"steps": [{{"description": "具体查询", "tool": "工具名", "depends_on": []}}]}}

## 用户问题
{question}
"""


class Planner(Protocol):
    def plan(self, question: str, replan_hint: str = "") -> list[dict]: ...


def sanitize_steps(
    raw_steps: list[dict], registry: ToolRegistry, max_steps: int = 6
) -> list[dict]:
    """规划解析的兜底纪律：编号连续化、工具名纠偏、依赖合法化、环检测。"""
    steps: list[dict] = []
    for i, raw in enumerate(raw_steps[:max_steps]):
        desc = str(raw.get("description", "")).strip()
        if not desc:
            continue
        tool_name = str(raw.get("tool", "")).strip()
        tool, hallucinated = registry.resolve(tool_name)
        depends = [d for d in (raw.get("depends_on") or []) if isinstance(d, int) and 0 <= d < i]
        steps.append({
            "step_index": len(steps),
            "description": desc,
            "tool_name": tool.name,
            "tool_hallucinated": hallucinated,
            "depends_on": depends,
            "status": "pending",  # pending / done / failed
            "result_count": 0,
        })
    # 环依赖检测：依赖只允许指向已出现的步骤（上面已保证 d < i），天然无环
    return steps


class LLMPlanner:
    def __init__(self, llm: LLM, registry: ToolRegistry, max_steps: int = 6, max_retries: int = 1):
        self._llm = llm
        self._registry = registry
        self._max_steps = max_steps
        self._max_retries = max_retries

    def plan(self, question: str, replan_hint: str = "") -> list[dict]:
        catalog = "\n".join(
            f"- {t['name']}: {t['description']}" for t in self._registry.catalog()
        )
        q = question + (f"\n\n（此前检索不充分，请换个角度规划：{replan_hint}）" if replan_hint else "")
        prompt = PLAN_PROMPT.format(tool_catalog=catalog, question=q, max_steps=self._max_steps)
        for _ in range(self._max_retries + 1):
            try:
                text = self._llm.generate(prompt)
                payload = json.loads(self._extract_json(text))
                steps = sanitize_steps(payload.get("steps") or [], self._registry, self._max_steps)
                if steps:
                    return steps
            except Exception as exc:  # noqa: BLE001
                logger.warning("规划解析失败，重试: %s", exc)
        # 兜底：单步向量检索（降级不崩溃）
        logger.warning("规划失败兜底：单步 vector_search")
        return sanitize_steps(
            [{"description": question, "tool": "vector_search"}], self._registry
        )

    @staticmethod
    def _extract_json(text: str) -> str:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            raise ValueError("规划中未找到 JSON")
        return m.group(0)


class HeuristicPlanner:
    """确定性规划（测试与 mock 模式）：按问题形态产出计划。"""

    def __init__(self, registry: ToolRegistry):
        self._registry = registry

    def plan(self, question: str, replan_hint: str = "") -> list[dict]:
        # 表达式形态 → 计算器
        if re.fullmatch(r"[\d\s\.\+\-\*/\(\)%]+", question.strip()):
            steps = [{"description": question, "tool": "calculator"}]
        else:
            steps = [{"description": question, "tool": "vector_search"}]
            if replan_hint:
                steps.append({"description": f"{question}（换角度：{replan_hint[:30]}）", "tool": "vector_search"})
        return sanitize_steps(steps, self._registry)
