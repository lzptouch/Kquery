#!/usr/bin/env python3
"""容量计算器：给定目标 QPS 与平均耗时，算出各层参数并**校验硬约束**。

**为什么要有它**：扩容参数靠"感觉调"会反复踩坑——线程加了但许可没加（照样排队）、
许可加了但线程没加（死锁风险）。这个脚本把推导过程写死：

1. **在飞需求 = QPS × L**（Little's Law）→ 需要多少并发槽位；
2. **各上游许可**按"该段占平均耗时的比例"折算 → 慢上游不该按满额配；
3. **硬约束**：各上游许可之和 ≤ `pipeline_workers`
   （否则会出现"许可等线程、线程等许可"的嵌套死锁）；
4. **限流面**：单租户令牌桶的回填速率必须 ≥ 目标 QPS，
   否则单租户根本压不到执行器上限（测出来的"容量"是限流器的，不是系统的）；
5. **真实上限是上游模型配额**——本机参数再对，也绕不过平台 QPS。

纯函数实现（不读配置、不连服务），因此**可测**；CLI 只是把参数喂进去。
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field


@dataclass
class CapacityPlan:
    """一次容量测算的结果。"""

    target_qps: float
    avg_latency_s: float
    in_flight: int = 0
    suggested_workers: int = 0
    permits: dict[str, int] = field(default_factory=dict)
    llm_concurrency: float = 0.0
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "target_qps": self.target_qps,
            "avg_latency_s": self.avg_latency_s,
            "in_flight": self.in_flight,
            "suggested_workers": self.suggested_workers,
            "permits": self.permits,
            "llm_concurrency": round(self.llm_concurrency, 2),
            "violations": self.violations,
            "warnings": self.warnings,
            "notes": self.notes,
        }


def compute(
    target_qps: float,
    avg_latency_s: float,
    llm_share: float = 0.7,
    embedding_share: float = 0.05,
    rerank_share: float = 0.05,
    milvus_share: float = 0.05,
    multimodal_share: float = 0.05,
    single_tenant_ratelimit_per_min: int = 60,
    upstream_quota_qps: float | None = None,
    current_workers: int = 32,
) -> CapacityPlan:
    """算一次容量方案并校验硬约束。

    `*_share` 是各上游阶段占平均耗时的比例（加起来不必为 1：
    剩下的归给本地处理与排队）。
    """
    if target_qps <= 0:
        raise ValueError("target_qps 必须为正")
    if avg_latency_s <= 0:
        raise ValueError("avg_latency_s 必须为正")

    plan = CapacityPlan(target_qps=target_qps, avg_latency_s=avg_latency_s, in_flight=0)
    # ① Little's Law
    in_flight = max(1, round(target_qps * avg_latency_s))
    plan.in_flight = in_flight
    # worker 数：在飞需求 + 一点余量（排队缓冲），但不超过现值上限
    plan.suggested_workers = max(1, round(in_flight * 1.2))

    # ② 各上游许可 = 在飞数 × 该段耗时占比（向上取整，至少 1）
    # `LM-03`：多模态那一路也纳入许可和（此前绕过隔离仓 → 许可和口径与现实不符）
    shares = {
        "llm": llm_share,
        "embedding": embedding_share,
        "rerank": rerank_share,
        "milvus": milvus_share,
        "multimodal": multimodal_share,
    }
    for name, share in shares.items():
        plan.permits[name] = max(1, round(in_flight * max(0.0, share)))
    plan.llm_concurrency = in_flight * max(0.0, llm_share)

    # ③ 硬约束：许可之和 ≤ workers
    total_permits = sum(plan.permits.values())
    if total_permits > plan.suggested_workers:
        plan.violations.append(
            f"各上游许可之和 {total_permits} > workers {plan.suggested_workers}："
            "会出现许可等线程、线程等许可的嵌套死锁"
        )
    if plan.suggested_workers > current_workers:
        plan.notes.append(
            f"建议 workers {plan.suggested_workers} 超过当前 {current_workers}，"
            "扩容前先核对上游配额（见 warnings）"
        )

    # ④ 限流面：单租户令牌桶
    tenant_qps = single_tenant_ratelimit_per_min / 60.0
    if tenant_qps < target_qps:
        plan.warnings.append(
            f"单租户令牌桶上限约 {tenant_qps:.2f} QPS < 目标 {target_qps} QPS："
            "单租户压不到执行器上限，容量是多租户合计出来的（压测需多租户）"
        )

    # ⑤ 上游配额是真实上限
    if upstream_quota_qps is not None and upstream_quota_qps < target_qps:
        plan.warnings.append(
            f"上游配额 {upstream_quota_qps} QPS < 目标 {target_qps} QPS："
            "本机参数再对也达不到目标，扩容只是把等待从本机队列搬到上游超时"
        )
    return plan


def render(plan: CapacityPlan) -> str:
    lines = [
        "=== 容量方案 ===",
        f"目标 QPS        : {plan.target_qps}",
        f"平均耗时 L      : {plan.avg_latency_s}s",
        f"在飞需求(QPS×L) : {plan.in_flight}",
        f"建议 workers    : {plan.suggested_workers}",
        f"上游许可        : {plan.permits}（LLM 段均并发 {plan.llm_concurrency:.2f}）",
    ]
    for v in plan.violations:
        lines.append(f"  [硬约束] {v}")
    for w in plan.warnings:
        lines.append(f"  [告警]   {w}")
    for n in plan.notes:
        lines.append(f"  [提示]   {n}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="容量计算器")
    parser.add_argument("--qps", type=float, required=True, help="目标 QPS")
    parser.add_argument("--latency", type=float, required=True, help="平均请求耗时（秒）")
    parser.add_argument("--llm-share", type=float, default=0.7, help="LLM 段耗时占比")
    parser.add_argument("--embedding-share", type=float, default=0.05)
    parser.add_argument("--rerank-share", type=float, default=0.05)
    parser.add_argument("--milvus-share", type=float, default=0.05)
    parser.add_argument("--multimodal-share", type=float, default=0.05,
                        help="多模态段耗时占比（`LM-03`：图像 caption / OCR / 视觉直答 / 图像向量）")
    parser.add_argument("--ratelimit-per-min", type=int, default=60, help="单租户令牌桶回填/分钟")
    parser.add_argument("--upstream-quota-qps", type=float, default=None, help="上游模型配额 QPS")
    parser.add_argument("--current-workers", type=int, default=32)
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    plan = compute(
        target_qps=args.qps,
        avg_latency_s=args.latency,
        llm_share=args.llm_share,
        embedding_share=args.embedding_share,
        rerank_share=args.rerank_share,
        milvus_share=args.milvus_share,
        multimodal_share=args.multimodal_share,
        single_tenant_ratelimit_per_min=args.ratelimit_per_min,
        upstream_quota_qps=args.upstream_quota_qps,
        current_workers=args.current_workers,
    )
    if args.json:
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render(plan))
    # 硬约束违反 → 非零退出（可以直接当 CI 检查）
    return 1 if plan.violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
