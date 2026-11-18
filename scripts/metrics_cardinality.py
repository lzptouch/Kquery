#!/usr/bin/env python
"""`C-05` 压测判据：指标标签组合数在真实流量下不膨胀。

## 判据

不是「序列数 < N」——那是拍脑袋的数。判据是两条可证伪的性质：

1. **不随量增长**：对着同一个实例打 `--rounds` 轮、每轮换新的随机路径/文档 ID，
   结束时序列集合必须与预热后**完全相同**；
2. **取值有界**：`route` 只允许路由模板与 `<unmatched>`；任何 label 都不得
   出现 tenant / user / doc_id / query / trace_id 这类由请求方决定的字段。

进程内版本由 `tests/integration/test_metrics_cardinality.py` 守卫（CI 必跑）。
本脚本是同一判据在**压测实例**上的版本，用来抓「只有并发/长稳才出现」的路径，
以及上线前对真机做一次基数体检。

## 用法

```bash
# 体检（默认 3 轮 × 每轮 50 个随机形状）
backend/.venv/bin/python scripts/metrics_cardinality.py

# 压测中途采样：配合 k6/locust 打流量，脚本只负责前后对比
backend/.venv/bin/python scripts/metrics_cardinality.py --base-url http://127.0.0.1:8100 \
    --rounds 5 --per-round 200

# 只看当前基数排行（不制造流量）
backend/.venv/bin/python scripts/metrics_cardinality.py --snapshot-only
```

退出码：0 = 基数有界；1 = 发现膨胀（打印新增序列与来源指标）。
"""
from __future__ import annotations

import argparse
import re
import sys
import uuid
from collections import Counter

import httpx

SAMPLE = re.compile(r'^(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)\{(?P<labels>[^}]*)\}\s')
LABEL_PAIR = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')
FORBIDDEN_LABELS = {"tenant", "tenant_id", "user", "user_id", "doc_id", "query", "trace_id", "path"}


def parse_series(text: str) -> dict[str, set[tuple[str, ...]]]:
    out: dict[str, set[tuple[str, ...]]] = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        m = SAMPLE.match(line)
        if not m:
            continue
        labels = tuple(sorted(f"{k}={v}" for k, v in LABEL_PAIR.findall(m.group("labels"))))
        out.setdefault(m.group("name"), set()).add(labels)
    return out


def scrape(base_url: str, token: str) -> dict[str, set[tuple[str, ...]]]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = httpx.get(f"{base_url.rstrip('/')}/metrics", headers=headers, timeout=30)
    resp.raise_for_status()
    return parse_series(resp.text)


def _shapes(per_round: int) -> list[str]:
    """每轮都**换新值**：膨胀如果存在，只会在这里暴露。"""
    tag = uuid.uuid4().hex[:6]
    shapes = ["/healthz", "/readyz", "/metrics", "/knowledge?limit=1000", "/api/unknown-endpoint"]
    shapes += [f"/scan-{tag}-{i}" for i in range(per_round)]
    shapes += [f"/knowledge/doc-{tag}-{i}" for i in range(max(1, per_round // 2))]
    return shapes


def main() -> int:
    p = argparse.ArgumentParser(description="指标标签组合数膨胀体检（C-05）")
    p.add_argument("--base-url", default="http://127.0.0.1:8100")
    p.add_argument("--token", default="", help="鉴权令牌（AUTH_ENABLED=true 时需要）")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--per-round", type=int, default=50)
    p.add_argument("--snapshot-only", action="store_true", help="只打印当前基数，不制造流量")
    p.add_argument("--top", type=int, default=10, help="基数排行显示前 N 个指标")
    args = p.parse_args()

    if args.snapshot_only:
        series = scrape(args.base_url, args.token)
        print(f"[card] {args.base_url} 当前时间序列：{sum(len(v) for v in series.values())} 条")
        for name, count in Counter({k: len(v) for k, v in series.items()}).most_common(args.top):
            print(f"  {count:6d}  {name}")
        return 0

    baseline = scrape(args.base_url, args.token)
    print(f"[card] 基线：{sum(len(v) for v in baseline.values())} 条序列")

    client = httpx.Client(
        base_url=args.base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {args.token}"} if args.token else {},
        timeout=60,
    )
    with client:
        for round_no in range(args.rounds):
            for path in _shapes(args.per_round):
                try:
                    client.get(path)
                except httpx.HTTPError as exc:  # 单请求失败不该毁掉体检
                    print(f"[card] 请求失败 {path}: {exc}", file=sys.stderr)
            print(f"[card] 第 {round_no + 1}/{args.rounds} 轮完成", flush=True)

    after = scrape(args.base_url, args.token)
    total_before = sum(len(v) for v in baseline.values())
    total_after = sum(len(v) for v in after.values())

    leaks: list[str] = []
    for name, series in after.items():
        new = series - baseline.get(name, set())
        if new:
            leaks.append(f"{name} 新增 {len(new)} 条，例如 {sorted(new)[:3]}")

    forbidden = sorted({
        combo.split("=", 1)[0]
        for series in after.values()
        for combo in series
        if combo.split("=", 1)[0] in FORBIDDEN_LABELS
    })
    if forbidden:
        leaks.append(f"出现高基数/敏感 label：{forbidden}")

    print(f"[card] 结束：{total_after} 条序列（基线 {total_before}，净增 {total_after - total_before}）")
    if leaks:
        print("[card] 判据不通过：", file=sys.stderr)
        for leak in leaks:
            print(f"  - {leak}", file=sys.stderr)
        return 1
    print("[card] 判据通过：轮次与随机路径全部换新后，序列集合完全不变")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
