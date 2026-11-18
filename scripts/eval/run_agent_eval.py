#!/usr/bin/env python3
"""Agent 过程评测（§5.3）：工具选择准确率 / 平均迭代 / 失控率。

**为什么需要它**：Agent 的"效果"目前只有"答得对不对"这一个信号。
但答错的原因分三类——**工具选错了**（该查图谱却查了向量）、**检索没召回**、
**反思没停**。只看最终答案分不出来，工具失败率也看不出来（工具调用成功 ≠ 选对了）。

评测集：每条给出 `expected_tools`（期望的工具序列）与 `max_iterations`。
指标：

| 指标 | 含义 | 验收口径 |
|---|---|---|
| `tool_selection_accuracy` | 实际调用的工具序列与期望一致的比例 | ≥ 0.8 |
| `avg_iterations` | 平均迭代轮数 | ≤ 3（沿用 `rag_agent_iterations` 的注释口径） |
| `fuse_rate` | 失控率（触发熔断的运行比例） | ≤ 0.1 |

**口径声明**：本脚本用**脚本化 planner**（从评测集读期望计划），
因此它度量的是**工具链路的正确性**（选择、降级、熔断是否按预期工作），
**不是**模型的规划能力。规划能力要用 `--mode live` + 真实模型跑（见文末）。

用法：
    python scripts/eval/run_agent_eval.py \
        --dataset tests/data/eval/agent_cases.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
# 仓库根也要在路径上：评测器复用 tests/ 里的测试脚手架（prompt builder 等）
for _p in (BACKEND, REPO_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

DEFAULT_DATASET = REPO_ROOT / "tests" / "data" / "eval" / "agent_cases.jsonl"


class ScriptedPlanner:
    """按评测集给出计划（确定性，便于回归）。

    计划要走生产的 `sanitize_steps`：它负责校验工具名、填 `status` 等字段。
    绕过它会让图节点拿到"半成品计划"（缺 status → KeyError），
    测的就不是真实链路了。
    """

    def __init__(self, plans: list[list[dict]], registry=None):
        self._plans = plans
        self._registry = registry
        self.calls = 0

    def plan(self, question: str, replan_hint: str = "") -> list[dict]:
        from app.services.agent.planner import sanitize_steps

        raw = self._plans[min(self.calls, len(self._plans) - 1)]
        self.calls += 1
        return sanitize_steps(raw, self._registry)


class ScriptedReflector:
    """按评测集给出反思结论。"""

    def __init__(self, verdicts: list[dict]):
        self._verdicts = verdicts
        self.calls = 0

    def reflect(self, question, evidence, remaining_steps) -> dict:
        v = self._verdicts[min(self.calls, len(self._verdicts) - 1)]
        self.calls += 1
        return dict(v)


class EchoTool:
    """假工具：返回固定证据（只用来观察"选了哪个工具"）。"""

    def __init__(self, name: str):
        self.name = name
        self.description = f"{name}（评测用假工具）"

    def run(self, query: str, tenant_id: str, **kwargs):
        from app.services.agent.tools import ToolResult

        return ToolResult(
            items=[
                {
                    "chunk_id": f"{self.name}#1",
                    "content": f"{self.name} 的证据",
                    "score": 0.8,
                    "source": "评测",
                    "doc_id": "eval",
                    "source_route": self.name,
                    "modality": "text",
                }
            ]
        )


def run_case(case: dict) -> dict:
    """跑一条用例，返回实际工具序列与终态。"""
    from app.services.agent.graph import build_agent_graph
    from app.services.agent.tools import ToolRegistry
    from app.services.llm import MockLLM

    from tests.integration.test_stage6_agent import agent_prompt_builder

    registry = ToolRegistry(timeout_s=2)
    for tool_name in {*case.get("expected_tools", []), *(case.get("other_tools") or [])}:
        registry.register(EchoTool(tool_name))

    plan = [
        {"description": f"第 {i + 1} 步", "tool": name}
        for i, name in enumerate(case.get("expected_tools", []))
    ]
    planner = ScriptedPlanner([plan], registry)
    reflector = ScriptedReflector(
        [case.get("reflect") or {"score": 0.9, "next_action": "answer", "reason": "证据足够"}]
    )
    graph = build_agent_graph(registry, planner, reflector, MockLLM(), agent_prompt_builder)
    events: list[tuple[str, dict]] = []
    final = graph.invoke(
        {"question": case.get("question", "评测问题"), "tenant_id": "eval-tenant",
         "user_id": "eval", "turn": 0, "turns": [], "evidence": [], "plan": [], "iteration": 0,
         "last_evidence_count": 0, "fused": False, "skip_retrieval": False, "refusal": False,
         "approved_tools": []},
        config={"configurable": {"thread_id": f"agent-eval-{case.get('query_id', 'q')}"}},
    )
    del events
    actual_tools = [
        step.get("tool_name")
        for step in (final.get("plan") or [])
        if step.get("tool_name")
    ]
    fuse_reason = str(final.get("fuse_reason") or "")
    # 失控率的判定：**只统计"本该干活却失控"的用例**。
    # 闲聊短路（期望 0 个工具）会以"无可执行步骤"结束——那是**正确行为**，
    # 把它算成失控会让指标永远不达标（而且掩盖了真正该修的问题）。
    counted = bool(case.get("expected_tools"))
    return {
        "query_id": case.get("query_id", ""),
        "expected_tools": case.get("expected_tools", []),
        "actual_tools": actual_tools,
        "iterations": int(final.get("iteration", 0) or 0),
        "fuse_reason": fuse_reason,
        "counted_for_fuse": counted,
        "fused": bool(counted and (final.get("fused") or fuse_reason)),
        "status": final.get("status") or "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent 过程评测")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--out", default=str(REPO_ROOT / "tests" / "data" / "eval" / "agent_report.json"))
    parser.add_argument("--min-accuracy", type=float, default=0.8)
    parser.add_argument("--max-avg-iterations", type=float, default=3.0)
    parser.add_argument("--max-fuse-rate", type=float, default=0.1)
    args = parser.parse_args()

    dataset = Path(args.dataset)
    if not dataset.exists():
        raise SystemExit(f"[agent-eval] 评测集不存在：{dataset}")
    cases = [
        json.loads(line)
        for line in dataset.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not cases:
        raise SystemExit("[agent-eval] 评测集为空")

    results = [run_case(case) for case in cases]
    total = len(results)
    exact = sum(1 for r in results if r["actual_tools"] == r["expected_tools"])
    avg_iterations = sum(r["iterations"] for r in results) / total
    counted = [r for r in results if r.get("counted_for_fuse")]
    fuse_rate = (sum(1 for r in counted if r["fused"]) / len(counted)) if counted else 0.0
    report = {
        "cases": total,
        "tool_selection_accuracy": round(exact / total, 4),
        "avg_iterations": round(avg_iterations, 2),
        "fuse_rate": round(fuse_rate, 4),
        "results": results,
    }
    print("=== Agent 过程评测 ===")
    print(f"用例数            : {total}")
    print(f"工具选择准确率    : {report['tool_selection_accuracy']}")
    print(f"平均迭代轮数      : {report['avg_iterations']}")
    print(
        f"失控率（熔断）    : {report['fuse_rate']}"
        f"（分母 {len(counted)} 条'本该干活'的用例；闲聊短路不计入）"
    )
    violations = []
    if report["tool_selection_accuracy"] < args.min_accuracy:
        violations.append(
            f"工具选择准确率 {report['tool_selection_accuracy']} < {args.min_accuracy}"
        )
    if report["avg_iterations"] > args.max_avg_iterations:
        violations.append(
            f"平均迭代 {report['avg_iterations']} > {args.max_avg_iterations}"
        )
    if report["fuse_rate"] > args.max_fuse_rate:
        violations.append(f"失控率 {report['fuse_rate']} > {args.max_fuse_rate}")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[agent-eval] 报告已写入 {args.out}")
    if violations:
        for v in violations:
            print(f"[agent-eval] 未达标：{v}")
        return 1
    print("[agent-eval] 全部达标")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
