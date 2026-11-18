"""生成侧判定的接线（`EV-02` / `EV-03` / `EV-05` 的脚本层）。

## 三条验收

1. **judge 跳过时写明原因**（不是「质量为 0」）
2. 报告带 **rubric 版本**
3. `heuristic` 判定能产出维度分 + 归因桶，且**标清身份**（非模型判定）
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "run_eval_script", REPO_ROOT / "scripts" / "eval" / "run_eval.py"
)
run_eval = importlib.util.module_from_spec(_spec)
sys.modules["run_eval_script"] = run_eval
_spec.loader.exec_module(run_eval)


def _args(**overrides):
    base = {
        "rubric": None,
        "judge": "none",
        "judge_report": "/tmp/judge_report_test.json",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_judge_skipped_states_the_reason():
    """① judge 跳过 → 写明原因（不是「质量为 0」）。"""
    report = run_eval.judge_generation(_args(), {"q1": {"ranked": [], "answer": "答案 [#1]"}})
    assert report["skipped"] is True
    assert report["judge_kind"] == "none"
    assert "未判定" in report["reason"] or "没量" in report["reason"]
    assert report["avg_weighted"] is None


def test_report_carries_rubric_version():
    """② 报告带 rubric 版本（不同版本的分数不可比）。"""
    report = run_eval.judge_generation(_args(), {})
    assert report["rubric_version"] == "v1"


def test_heuristic_judge_produces_scores_and_buckets():
    """③ heuristic 判定 → 维度分 + 归因桶 + 标清身份。"""
    runs = {
        "e001": {"ranked": ["c1"], "answer": "单笔报销上限为 5000 元 [#1]"},
    }
    report = run_eval.judge_generation(_args(judge="heuristic"), runs)
    assert report["skipped"] is False
    assert report["judge_kind"] == "heuristic", "启发式判定必须标清身份（不是模型判定）"
    assert report["judged"] == 1
    assert report["avg_weighted"] is not None
    item = report["items"][0]
    assert item["bucket"] in ("correct", "misquoted", "not_cited", "answered_wrong", "unjudged")
    assert item["conclusion"]


def test_missing_rubric_reports_error_without_raising():
    """rubric 路径不存在 → 报告里写明原因，不崩。"""
    report = run_eval.judge_generation(_args(rubric="/nonexistent/rubric.v9.json"), {})
    assert report["skipped"] is True
    assert "rubric 加载失败" in report["reason"]
