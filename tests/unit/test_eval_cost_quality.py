"""检索指标补全 + 成本-质量曲线（`EV-06`）。

## 四条验收

1. `precision@k` / `hit_rate@k` 口径正确（precision 分母 = min(k, 实际候选数)）
2. 两个指标进入逐条指标与聚合报告
3. **帕累托前沿**：识别出「既更便宜又更准」的策略
4. `cost_quality.json` 产出且字段齐全
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_precision_uses_actual_candidate_count():
    """① precision 的分母是 min(k, 实际候选数)——不是固定 k。"""
    from app.services.evaluation.metrics import precision_at_k

    rel = {"a", "b"}
    # 召 3 条命中 2 条 → 2/3（不是 2/5）
    assert precision_at_k(["a", "x", "b"], rel, 5) == 2 / 3
    # 召 1 条命中 1 条 → 1.0（不是 1/5）
    assert precision_at_k(["a"], rel, 5) == 1.0
    # 没有候选 → 0.0（不返回 1.0：那会把「没召回」算成「全对」）
    assert precision_at_k([], rel, 5) == 0.0


def test_hit_rate_is_any_hit():
    """① hit_rate = 至少命中一个金标的占比。"""
    from app.services.evaluation.metrics import hit_rate_at_k

    rel = {"a", "b"}
    assert hit_rate_at_k(["x", "a"], rel, 2) == 1.0
    assert hit_rate_at_k(["x", "y"], rel, 2) == 0.0
    # @1 只看第一条
    assert hit_rate_at_k(["x", "a"], rel, 1) == 0.0


def test_metrics_enter_aggregate():
    """② 两个指标进入逐条指标与聚合报告。"""
    from app.services.evaluation.metrics import aggregate, evaluate_query

    res = evaluate_query(["a", "x"], {"a": 2, "b": 2}, k_values=(2,))
    assert "precision" in res and "hit_rate" in res
    agg = aggregate([res])
    assert "precision" in agg and "hit_rate" in agg
    assert agg["precision"]["2"] == 0.5  # 1/2 命中
    assert agg["hit_rate"]["2"] == 1.0


def test_pareto_front_picks_dominant_strategy():
    """③ 帕累托前沿：既更便宜又更准的策略入选，被支配的淘汰。"""
    cost_quality = _load(
        "cost_quality_script", REPO_ROOT / "scripts" / "eval" / "cost_quality.py"
    )
    points = [
        {"policy": "A", "cost": 100, "ndcg10": 0.8},
        {"policy": "B", "cost": 50, "ndcg10": 0.9},   # 支配 A
        {"policy": "C", "cost": 80, "ndcg10": 0.85},  # 支配 A，但被 B 支配
    ]
    front = [p["policy"] for p in cost_quality.pareto_front(points)]
    assert front == ["B"]


def test_cost_quality_artifact_shape():
    """④ `cost_quality.json` 产出且字段齐全。"""
    path = REPO_ROOT / "tests" / "data" / "eval" / "cost_quality.json"
    assert path.exists(), "成本-质量对照还没产出（跑 scripts/eval/cost_quality.py）"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["points"], "没有策略点"
    for point in payload["points"]:
        for key in ("policy", "cost", "recall10", "ndcg10", "precision10", "hit_rate10"):
            assert key in point, f"{point.get('policy')} 缺字段 {key}"
    assert "pareto_front" in payload
    assert "代理量" in payload["cost_basis"], "必须写明成本是代理量而非真实账单"


def test_cost_quality_json_is_regenerated_in_sync():
    """产出的 JSON 与当前策略集一致（策略集变了要重跑）。"""
    path = REPO_ROOT / "tests" / "data" / "eval" / "cost_quality.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    policies = {p["policy"] for p in payload["points"]}
    assert "recursive-300-50" in policies, "默认策略必须在对照表里"
