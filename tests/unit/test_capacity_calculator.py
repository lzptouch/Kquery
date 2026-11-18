"""容量计算器的测试（批次 6）。

容量参数靠"感觉调"会反复踩坑：线程加了许可没加（照样排队）、
许可加了线程没加（嵌套死锁）。计算器把这三条约束写成可执行的断言。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "capacity_script", REPO_ROOT / "scripts" / "capacity.py"
)
capacity = importlib.util.module_from_spec(_spec)
# dataclass 在装饰时会在 sys.modules 里找自己的模块 —— 必须先登记，
# 否则 `exec_module` 出来的模块一用 @dataclass 就报 AttributeError。
sys.modules["capacity_script"] = capacity
_spec.loader.exec_module(capacity)


class TestLittleLaw:
    def test_in_flight(self):
        plan = capacity.compute(target_qps=4, avg_latency_s=8)
        assert plan.in_flight == 32  # 4 × 8

    def test_fractional_qps(self):
        plan = capacity.compute(target_qps=2.5, avg_latency_s=4)
        assert plan.in_flight == 10

    def test_invalid_input(self):
        with pytest.raises(ValueError):
            capacity.compute(target_qps=0, avg_latency_s=8)
        with pytest.raises(ValueError):
            capacity.compute(target_qps=4, avg_latency_s=0)


class TestPermits:
    def test_permits_scale_with_concurrency(self):
        plan = capacity.compute(target_qps=4, avg_latency_s=8, llm_share=0.7)
        assert plan.permits["llm"] == max(1, round(32 * 0.7))
        assert plan.llm_concurrency == pytest.approx(22.4)

    def test_permits_have_floor_of_one(self):
        plan = capacity.compute(target_qps=0.1, avg_latency_s=1, llm_share=0.0)
        assert plan.permits["llm"] >= 1


class TestHardConstraints:
    def test_permits_sum_exceeding_workers_is_violation(self):
        """许可之和 > workers → 嵌套死锁风险，必须报出来。"""
        plan = capacity.compute(
            target_qps=20, avg_latency_s=8, llm_share=0.9,
            embedding_share=0.2, rerank_share=0.2, milvus_share=0.2,
        )
        assert plan.violations
        assert "许可" in plan.violations[0]

    def test_healthy_plan_has_no_violation(self):
        plan = capacity.compute(target_qps=4, avg_latency_s=8)
        assert not plan.violations


class TestWarnings:
    def test_single_tenant_ratelimit_warning(self):
        """单租户令牌桶 60/min ≈ 1 QPS < 目标 → 压测口径必须多租户。"""
        plan = capacity.compute(target_qps=4, avg_latency_s=8, single_tenant_ratelimit_per_min=60)
        assert any("令牌桶" in w for w in plan.warnings)

    def test_upstream_quota_warning(self):
        plan = capacity.compute(target_qps=10, avg_latency_s=8, upstream_quota_qps=3)
        assert any("上游配额" in w for w in plan.warnings)

    def test_no_warning_when_ratelimit_allows(self):
        plan = capacity.compute(
            target_qps=0.5, avg_latency_s=8, single_tenant_ratelimit_per_min=600
        )
        assert not any("令牌桶" in w for w in plan.warnings)


class TestCli:
    def test_cli_returns_nonzero_on_violation(self, capsys):
        import sys

        argv = sys.argv
        sys.argv = [
            "capacity.py", "--qps", "20", "--latency", "8",
            "--llm-share", "0.9", "--embedding-share", "0.2",
            "--rerank-share", "0.2", "--milvus-share", "0.2",
        ]
        try:
            code = capacity.main()
        finally:
            sys.argv = argv
        assert code == 1
        assert "硬约束" in capsys.readouterr().out
