"""五角色（阶段 8 §2）：Planner / Researcher / Validator / Writer / Critic。

档位纪律（千问平台）：
- Planner qwen-plus：拆解质量决定任务成败，最不该省成本；
- Researcher 不调生成模型，以工具结果为准；
- Validator 复用 gte-rerank-v2 口径（跨角色分数可比的唯一保证）；
- Writer qwen-max（报告是最终交付物，长文组织对档位最敏感；不可用时降级 qwen-plus）；
- Critic qwen-plus（多轮循环下档位差异被轮次放大，必须平衡成本）。

每个角色独立 System Prompt（短 prompt 会让 LLM 退化为通用助手）。
"""
import json
import logging
import re
import time
from typing import Protocol

from app.services.agent.tools import ToolRegistry
from app.services.llm import LLM, LLMError
from app.services.multiagent.blackboard import Blackboard, Critique, SubTask

logger = logging.getLogger(__name__)

PLANNER_PROMPT = """你是任务规划师（Planner）。把用户的长任务目标拆解为可执行的子任务 DAG。

## 可用工具
{tool_catalog}

## 纪律
1. 每个子任务的 description 必须是可以直接作为检索查询的具体表述；
2. 子任务间不冗余（两个子任务不查同一件事）；
3. 显式标注依赖：只有"必须先拿到 X 的结果才能做 Y"时才设 depends_on；
   能并行的不要串行——但"汇总/对比/结论"类子任务必须依赖数据类子任务；
4. 子任务数 2~{max_tasks} 个；
5. 每个子任务指定一个工具名，必须来自可用工具清单。

## 输出（严格 JSON）
{{"title": "报告标题", "subtasks": [{{"task_id": "t1", "description": "...", "tool": "vector_search", "depends_on": []}}]}}

## 用户目标
{goal}
"""

WRITER_PROMPT = """你是报告撰写人（Writer）。基于已采纳证据撰写结构化中文报告。

## 目标
{goal}

## 已采纳证据（编号即引用编号）
{evidence_block}

## 纪律
1. 每个事实性陈述后标注引用编号 [#n]；
2. 证据不足的部分如实说明"该部分缺少数据支撑"，不得编造；
3. 结构：摘要 → 分主题小节 → 结论；
4. 报告字数 300~800 字。
{feedback_block}

直接输出报告正文。"""

CRITIC_PROMPT = """你是评审人（Critic）。按四个维度评审报告：完整性（目标是否被覆盖）、
忠实性（每个陈述是否有证据支撑）、结构（层次是否清晰）、可读性。

## 目标
{goal}

## 证据（共 {evidence_count} 条已采纳）
{evidence_titles}

## 报告（第 {round} 稿）
{draft}

## 输出（严格 JSON）
- score：0~10 分；
- issues：问题清单，每条必须具体可执行（如"缺少 XX 的对比数据"、"第 2 段无引用编号"），
  禁止"写得不够好"这类无法指导修改的表述；
- resolved：本轮已解决的上一轮问题（原文摘录）。

{{"score": 0, "issues": ["..."], "resolved": ["..."]}}
{history_block}"""


class Planner(Protocol):
    def plan(self, goal: str) -> tuple[str, list[SubTask]]: ...


class Validator(Protocol):
    def score(self, query: str, text: str) -> float: ...


class LLMPlanner:
    def __init__(self, llm: LLM, registry: ToolRegistry, max_tasks: int = 8):
        self._llm = llm
        self._registry = registry
        self._max_tasks = max_tasks

    def plan(self, goal: str) -> tuple[str, list[SubTask]]:
        catalog = "\n".join(f"- {t['name']}: {t['description']}" for t in self._registry.catalog())
        prompt = PLANNER_PROMPT.format(tool_catalog=catalog, goal=goal, max_tasks=self._max_tasks)
        last_error: Exception | None = None
        for _ in range(2):
            try:
                text = self._llm.generate(prompt)
                payload = json.loads(re.search(r"\{[\s\S]*\}", text).group(0))
                title = str(payload.get("title") or goal[:30])
                tasks: list[SubTask] = []
                for raw in (payload.get("subtasks") or [])[: self._max_tasks]:
                    desc = str(raw.get("description", "")).strip()
                    if not desc:
                        continue
                    tool, _ = self._registry.resolve(str(raw.get("tool", "")))
                    tasks.append(SubTask(
                        task_id=f"t{len(tasks) + 1}",
                        description=desc,
                        tool_name=tool.name,
                        depends_on=[str(d) for d in (raw.get("depends_on") or [])],
                    ))
                if not tasks:
                    raise ValueError("规划产出为空")
                return title, tasks
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("报告规划解析失败，重试: %s", exc)
        from app.core.envelope import ApiError, ErrCode

        raise ApiError(
            ErrCode.REPORT_PLAN_FAILED, f"任务拆解失败: {last_error}", http_status=422
        ) from last_error


class RerankValidator:
    """Validator：复用 gte-rerank-v2 口径给证据打分（与阶段 5 同标准）。"""

    def __init__(self, reranker, adopt_threshold: float):
        self._reranker = reranker
        self._threshold = adopt_threshold

    def score(self, query: str, text: str) -> float:
        from app.domain.schemas import Chunk
        from app.services.retrieval.fusion import FusedCandidate

        cand = FusedCandidate(
            chunk=Chunk(
                chunk_id="tmp", doc_id="tmp", seq=1, content=text,
                source="", tenant_id="default",
            ),
            rrf_score=0.0,
        )
        ranked = self._reranker.rerank(query, [cand], top_n=1)
        return ranked[0].rerank_score if ranked else 0.0


class LLMWriter:
    def __init__(self, llm: LLM, fallback_llm: LLM | None = None):
        self._llm = llm
        self._fallback = fallback_llm

    def write(self, goal: str, evidence: list[dict], critiques: list[Critique]) -> str:
        evidence_block = "\n\n".join(
            f"[#{i + 1}] {e['content'][:300]}" for i, e in enumerate(evidence)
        )
        feedback_block = ""
        if critiques:
            # 循环有效的唯一关键：书写者必须看到上轮评审意见（§5.2）
            last = critiques[-1]
            feedback_block = (
                "\n## 上轮评审意见（必须逐条落实）\n"
                + "\n".join(f"- {issue}" for issue in last.issues)
            )
        prompt = WRITER_PROMPT.format(
            goal=goal, evidence_block=evidence_block, feedback_block=feedback_block
        )
        try:
            return self._llm.generate(prompt)
        except Exception as exc:  # noqa: BLE001
            if self._fallback is not None:
                logger.warning("Writer 高档位模型不可用，降级: %s", exc)
                return self._fallback.generate(prompt)
            raise


class LLMCritic:
    def __init__(self, llm: LLM, pass_score: float = 8.0):
        self._llm = llm
        self.pass_score = pass_score

    def critique(self, goal: str, draft: str, evidence: list[dict], history: list[Critique]) -> Critique:
        history_block = ""
        if history:
            prev = history[-1]
            history_block = (
                "\n## 上一轮问题清单（请对照检查是否已解决）\n"
                + "\n".join(f"- {i}" for i in prev.issues)
            )
        prompt = CRITIC_PROMPT.format(
            goal=goal, draft=draft, round=len(history) + 1,
            evidence_count=len(evidence),
            evidence_titles="\n".join(f"- {e['content'][:60]}" for e in evidence[:10]),
            history_block=history_block,
        )
        raw = self._llm.generate(prompt)
        try:
            payload = json.loads(re.search(r"\{[\s\S]*\}", raw).group(0))
            score = max(0.0, min(10.0, float(payload.get("score", 0))))
            issues = [str(i)[:200] for i in (payload.get("issues") or [])][:8]
            resolved = [str(i)[:200] for i in (payload.get("resolved") or [])][:8]
        except Exception:  # noqa: BLE001
            # 解析失败：保留原始输出（排查是提示问题还是解析问题的唯一依据）
            score, issues, resolved = 0.0, ["评审输出解析失败"], []
        return Critique(
            round=len(history) + 1, score=score, issues=issues,
            resolved_issues=resolved, raw_output=raw[:2000],
        )


# ---------- Mock 实现（测试 / 无 Key 环境） ----------

class HeuristicPlanner:
    """确定性拆解：按"与/和/对比/以及"切分目标。"""

    def __init__(self, registry: ToolRegistry):
        self._registry = registry

    def plan(self, goal: str) -> tuple[str, list[SubTask]]:
        parts = [p.strip() for p in re.split(r"[与和]|以及|对比", goal) if p.strip()]
        if len(parts) < 2:
            parts = [goal]
        tasks = []
        for i, part in enumerate(parts[:4]):
            tasks.append(SubTask(task_id=f"t{i + 1}", description=part, tool_name="vector_search"))
        if len(tasks) > 1:
            tasks.append(SubTask(
                task_id=f"t{len(tasks) + 1}", description=f"综合汇总：{goal}",
                tool_name="vector_search", depends_on=[t.task_id for t in tasks],
            ))
        return goal[:24], tasks


class FixedValidator:
    def __init__(self, score: float = 0.5):
        self._score = score

    def score(self, query: str, text: str) -> float:
        return self._score


class ScriptedCritic:
    """测试用：按脚本逐轮返回评分。"""

    def __init__(self, script: list[dict], pass_score: float = 8.0):
        self._script = script
        self.pass_score = pass_score
        self.calls = 0

    def critique(self, goal: str, draft: str, evidence: list[dict], history: list[Critique]) -> Critique:
        item = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        return Critique(
            round=len(history) + 1, score=item["score"],
            issues=item.get("issues", []), resolved_issues=item.get("resolved", []),
            raw_output=json.dumps(item, ensure_ascii=False),
        )
