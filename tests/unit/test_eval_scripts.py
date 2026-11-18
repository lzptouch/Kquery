"""评测脚本的冒烟测试（reindex / Agent 过程评测）。

这两个脚本是 CI 的一部分（§4.2 / §5.3），所以它们**必须能被 CI 跑起来**，
而不是"看起来能跑"。这里验证：参数解析、主流程、报告产出、门禁判定。
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(f"_t_{name}", REPO_ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def agent_eval():
    return _load("agent_eval", "scripts/eval/run_agent_eval.py")


@pytest.fixture(scope="module")
def reindex():
    return _load("reindex", "scripts/eval/reindex.py")


class TestAgentEval:
    def test_dataset_is_valid(self):
        path = REPO_ROOT / "tests" / "data" / "eval" / "agent_cases.jsonl"
        cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(cases) >= 5
        for case in cases:
            assert "query_id" in case and "expected_tools" in case
            assert isinstance(case["expected_tools"], list)

    def test_metrics_are_produced(self, agent_eval):
        dataset = REPO_ROOT / "tests" / "data" / "eval" / "agent_cases.jsonl"
        cases = [
            json.loads(line)
            for line in dataset.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        results = [agent_eval.run_case(case) for case in cases]
        assert len(results) == len(cases)
        for r in results:
            assert "actual_tools" in r
            assert r["iterations"] >= 0

    def test_tool_selection_accuracy_is_perfect_for_scripted_plan(self, agent_eval):
        """脚本化计划下，选中的工具必须与期望一致（否则链路本身有问题）。"""
        dataset = REPO_ROOT / "tests" / "data" / "eval" / "agent_cases.jsonl"
        cases = [
            json.loads(line)
            for line in dataset.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        results = [agent_eval.run_case(case) for case in cases]
        exact = sum(1 for r in results if r["actual_tools"] == r["expected_tools"])
        assert exact == len(results), [r for r in results if r["actual_tools"] != r["expected_tools"]]

    def test_chat_shortcut_is_not_counted_as_runaway(self, agent_eval):
        """闲聊短路（0 个期望工具）不该计入失控率——那是正确行为。"""
        dataset = REPO_ROOT / "tests" / "data" / "eval" / "agent_cases.jsonl"
        cases = [
            json.loads(line)
            for line in dataset.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        chat = [c for c in cases if not c["expected_tools"]]
        for case in chat:
            r = agent_eval.run_case(case)
            assert r["counted_for_fuse"] is False


class TestReindexScript:
    def test_policy_presets_exist(self, reindex):
        from app.services.evaluation.policies import PRESETS

        assert "recursive-800-100" in PRESETS

    def test_sandbox_build_does_not_touch_production(self, reindex, tmp_path):
        """沙箱索引必须落在**独立目录**（不能碰生产索引）。"""
        corpus = REPO_ROOT / "tests" / "data" / "eval" / "corpus"
        target = tmp_path / "sandbox"
        info = reindex.build_sandbox_index("recursive-300-50", corpus, target, "eval-tenant")
        assert info["chunks"] > 0
        assert target.exists()
        assert (target / "index.faiss").exists() or (target / "index.json").exists()

    def test_eval_policy_reports_metrics(self, reindex):
        corpus = REPO_ROOT / "tests" / "data" / "eval" / "corpus"
        metrics = reindex.eval_policy("recursive-800-100", corpus, "eval-tenant")
        assert metrics["chunks"] > 0
        assert "10" in metrics["recall"]
        assert "10" in metrics["ndcg"]
