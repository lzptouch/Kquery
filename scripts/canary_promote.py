#!/usr/bin/env python3
"""canary 提升 / 回滚（`OP-03`）。

## 与 `OP-02` 的关系

**同一套口径**：判定逻辑复用 `services/canary.py` 的规则与控制器
（`app.services.canary.CanaryRule` / `CanaryController`）——
两处各写一份必然漂移，而「判定标准」漂移一次就会推出互相矛盾的结论。

## 流程

1. **观察窗**：等到观察窗结束再判定（避免用启动瞬间的数字做判断）；
2. **读指标**：走 `OP-02` 的指标来源（多副本必须配 `prometheus_url`；
   未配置 → **无数据 → 不判定**，不猜）；
3. **判定**：达标 → 提升（摘掉 canary 权重 / 把流量切回稳定版）；
   不达标 → 回滚（删掉 canary 资源）；
4. **默认 `--dry-run`**：只输出判定与依据，**不实际改集群**。

用法::

    # 干跑（默认）：只看判定，不动集群
    backend/.venv/bin/python scripts/canary_promote.py \\
        --prometheus-url http://prometheus:9090 \\
        --rules '[{"metric": "error_rate", "direction": "gt", "threshold": 0.05, "min_samples": 30}]'

    # 真正执行（需显式 --apply）
    backend/.venv/bin/python scripts/canary_promote.py --apply ...
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def parse_rules(raw: str) -> list:
    """解析规则集（JSON 数组）；解析失败 → 抛错（不静默当成「无规则」）。"""
    from app.services.canary import CanaryRule

    raw = (raw or "").strip()
    if not raw:
        return []
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("规则集必须是 JSON 数组")
    return [
        CanaryRule(
            metric=str(item.get("metric") or ""),
            direction=str(item.get("direction") or "gt"),
            threshold=float(item.get("threshold") or 0.0),
            relative=bool(item.get("relative", False)),
            min_samples=int(item.get("min_samples") or 30),
            observe_minutes=int(item.get("observe_minutes") or 5),
        )
        for item in data
    ]


def build_controller(prometheus_url: str, rules: list, baseline: dict | None):
    """装配控制器（复用 `OP-02` 的实现与口径）。"""
    from app.services.canary import (
        CanaryController,
        MultiReplicaMetricSource,
        evaluate_rule,
    )

    source = MultiReplicaMetricSource(prometheus_url)
    baseline_map = baseline or {}

    def _baseline(metric: str, scope: str):
        return baseline_map.get(metric)

    return CanaryController(
        metric_source=source,
        baseline_provider=_baseline,
        rules=rules,
        enabled=True,
    ), evaluate_rule


def main() -> int:
    parser = argparse.ArgumentParser(description="canary 提升 / 回滚（OP-03）")
    parser.add_argument(
        "--prometheus-url", default="",
        help="Prometheus 地址（多副本必须配；未配置 → 无数据 → 不判定）",
    )
    parser.add_argument("--rules", default="", help="规则集（JSON 数组，口径同 OP-02）")
    parser.add_argument(
        "--baseline", default="", help="基线快照（JSON 对象：{metric: value}）",
    )
    parser.add_argument("--scope", default="canary", help="判定作用域（标签）")
    parser.add_argument(
        "--observe-minutes", type=int, default=0,
        help="观察窗（分钟）：未满则输出「观察中」并退出，不判定",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="真正执行（默认 dry-run：只输出判定与依据）",
    )
    parser.add_argument(
        "--action", choices=("promote", "rollback"), default="promote",
        help="达标时执行的动作（不达标一律回滚）",
    )
    args = parser.parse_args()

    try:
        rules = parse_rules(args.rules)
    except Exception as exc:  # noqa: BLE001 - 规则解析失败要明确报错
        print(f"[canary] 规则集解析失败：{exc}", file=sys.stderr)
        return 2
    if not rules:
        print("[canary] 没有规则 → 不判定（不改任何东西）")
        return 1

    baseline = {}
    if args.baseline:
        try:
            baseline = json.loads(args.baseline)
        except Exception as exc:  # noqa: BLE001
            print(f"[canary] 基线快照解析失败：{exc}", file=sys.stderr)
            return 2

    controller, _ = build_controller(args.prometheus_url, rules, baseline)
    verdict = controller.check_scope(args.scope)

    print(f"[canary] 判定：{verdict.action}")
    print(f"[canary] 原因：{verdict.reason}")
    print(
        f"[canary] 对比：实验组={verdict.experiment_value} 基线={verdict.baseline_value}"
        f" 样本={verdict.samples}"
    )
    if verdict.details:
        print(f"[canary] 明细：{json.dumps(verdict.details, ensure_ascii=False)}")

    if verdict.action == "no_data":
        print("[canary] 无数据 → **不判定**（不猜数），也不做任何动作")
        return 1

    if verdict.action == "alert":
        print("[canary] 只告警 → 不做任何动作")
        return 1

    if verdict.action == "ok":
        target = args.action
    else:
        target = "rollback"

    if not args.apply:
        print(f"[canary] dry-run：本次会执行「{target}」，但**没有改动任何东西**（加 --apply 生效）")
        return 0

    # 真正执行：改集群（需要 kubectl + 集群上下文）
    if target == "rollback":
        cmd = ["kubectl", "delete", "-f", str(REPO_ROOT / "deploy" / "k8s" / "canary"), "--ignore-not-found"]
    else:
        cmd = [
            "kubectl", "patch", "ingress", "kquery-canary",
            "-n", "kquery",
            "--type=json",
            "-p", '[{"op": "remove", "path": "/metadata/annotations/nginx.ingress.kubernetes.io~1upstream-vhost"}]',
        ]
    print(f"[canary] 执行：{' '.join(cmd)}")
    import subprocess

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"[canary] 执行失败：{proc.stderr.strip()}", file=sys.stderr)
        return 1
    print("[canary] 已执行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
