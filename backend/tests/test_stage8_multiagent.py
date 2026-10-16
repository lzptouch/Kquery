"""阶段 8：多智能体协同测试。

覆盖：黑板六操作（原子追加/去重复用/更新不删除/评审追加）/ 拓扑分层（DAG/环防御）/
校验淘汰保留 / Writer↔Critic 循环（反馈可见/达标通过/上限诚实返回/评审异常=未评审）/
全部失败不产出空报告 / API 契约（异步提交/分视图/accepted=false 成功语义/指标分开统计）。
"""
import time


from app.core.envelope import ErrCode
from app.services.agent.tools import CalculatorTool, ToolRegistry
from app.services.llm import MockLLM
from app.services.multiagent.blackboard import Blackboard, SubTask
from app.services.multiagent.orchestrator import Orchestrator
from app.services.multiagent.roles import (
    FixedValidator,
    HeuristicPlanner,
    LLMWriter,
    ScriptedCritic,
)
from app.services.multiagent.store import ReportStore
from app.services.multiagent.topology import topo_layers
from tests.test_stage6_agent import FakeSearchTool, _evidence


def make_registry_with_data() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(FakeSearchTool([_evidence("c#1", 0.9, "机会成本是为了得到某物所放弃的东西。")]))
    registry.register(CalculatorTool())
    return registry


def make_orchestrator(tmp_path, critic_script=None, validator_score=0.5, registry=None):
    registry = registry or make_registry_with_data()
    reports = ReportStore(tmp_path / "reports")
    return Orchestrator(
        planner=HeuristicPlanner(registry),
        validator=FixedValidator(validator_score),
        writer=LLMWriter(MockLLM()),
        critic=ScriptedCritic(critic_script or [{"score": 8.5, "issues": []}]),
        registry=registry, reports=reports,
        pass_score=8.0, max_rounds=3, adopt_threshold=0.14,
    ), reports


class TestBlackboard:
    def test_six_operations(self):
        bb = Blackboard("rpt-t", "目标")
        bb.put_plan([SubTask(task_id="t1", description="查A", tool_name="vector_search")])
        assert len(bb.get_plan()) == 1
        assert len(bb.get_plan(pending_only=True)) == 1
        bb.update_task("t1", "done")
        assert len(bb.get_plan(pending_only=True)) == 0

        # 原子追加返回标识
        e1 = bb.append_evidence("t1", "内容A", "vector_search")
        e2 = bb.append_evidence("t1", "内容B", "vector_search")
        assert e1 != e2
        # 同内容去重 + 跨任务复用记录
        e3 = bb.append_evidence("t2", "内容A", "vector_search")
        assert e3 == e1
        rec = bb.list_evidence()[0]
        assert "t2" in rec.used_by_tasks

        # 更新而非删除
        bb.update_evidence(e1, 0.1, False, "低于阈值")
        rejected = bb.list_evidence(adopted=False)
        assert len(rejected) == 1 and rejected[0].rejected_reason == "低于阈值"
        assert len(bb.list_evidence()) == 2  # 证据仍在黑板上

        from app.services.multiagent.blackboard import Critique
        bb.add_critique(Critique(round=1, score=6.0, issues=["缺对比"]))
        bb.add_critique(Critique(round=2, score=8.5, issues=[], resolved_issues=["缺对比"]))
        assert len(bb.list_critiques()) == 2  # 逐轮追加不可覆盖
        snap = bb.snapshot()
        assert len(snap["plan"]) == 1 and len(snap["evidence"]) == 2 and len(snap["critiques"]) == 2


class TestTopology:
    def test_layers_by_dependency(self):
        tasks = [
            SubTask(task_id="t1", description="a", tool_name="vector_search"),
            SubTask(task_id="t2", description="b", tool_name="vector_search"),
            SubTask(task_id="t3", description="c", tool_name="vector_search", depends_on=["t1", "t2"]),
            SubTask(task_id="t4", description="d", tool_name="vector_search", depends_on=["t3"]),
        ]
        layers = topo_layers(tasks)
        assert [[t.task_id for t in layer] for layer in layers] == [["t1", "t2"], ["t3"], ["t4"]]
        assert tasks[2].layer == 1 and tasks[3].layer == 2

    def test_cycle_defense(self):
        """环依赖不中断：剩余任务并入新层并继续。"""
        tasks = [
            SubTask(task_id="t1", description="a", tool_name="x", depends_on=["t2"]),
            SubTask(task_id="t2", description="b", tool_name="x", depends_on=["t1"]),
        ]
        layers = topo_layers(tasks)
        assert sum(len(layer) for layer in layers) == 2


class TestOrchestrator:
    def test_happy_path_one_round_pass(self, tmp_path):
        orch, reports = make_orchestrator(tmp_path)
        report = reports.create("分析机会成本的含义与举例", "default", "u")
        result = orch.run(report["report_id"], "default")
        assert result["status"] == "accepted"
        assert result["accepted"] is True
        assert result["rounds"] == 1
        assert len(result["drafts"]) == 1  # 每轮一稿
        assert result["role_calls"]["writer"] == 1
        assert result["role_calls"]["critic"] == 1
        bb = result["blackboard"]
        assert all(e["adopted"] for e in bb["evidence"])

    def test_critic_loop_converges_round2(self, tmp_path):
        """第一轮不合格（反馈落黑板）→ 第二轮看到意见后通过。"""
        orch, reports = make_orchestrator(tmp_path, critic_script=[
            {"score": 6.0, "issues": ["缺少对比维度"]},
            {"score": 8.5, "issues": [], "resolved": ["缺少对比维度"]},
        ])
        report = reports.create("分析机会成本与稀缺性", "default", "u")
        result = orch.run(report["report_id"], "default")
        assert result["status"] == "accepted" and result["rounds"] == 2
        assert len(result["drafts"]) == 2  # 首稿与终稿都保留
        critiques = result["blackboard"]["critiques"]
        assert critiques[0]["score"] == 6.0 and critiques[1]["resolved_issues"] == ["缺少对比维度"]

    def test_capped_returns_last_draft_honestly(self, tmp_path):
        """达轮次上限：返回最后一稿 + accepted=false + 遗留问题（禁止伪装通过）。"""
        orch, reports = make_orchestrator(tmp_path, critic_script=[
            {"score": 5.0, "issues": ["数据不足"]},
            {"score": 6.0, "issues": ["数据不足", "无引用"]},
            {"score": 6.5, "issues": ["无引用"]},
        ])
        report = reports.create("分析某主题的报告", "default", "u")
        result = orch.run(report["report_id"], "default")
        assert result["status"] == "capped"
        assert result["accepted"] is False
        assert result["critic_score"] == 6.5  # 最后一轮评分
        assert result["unresolved_issues"] == ["无引用"]
        assert len(result["drafts"]) == 3

    def test_critic_failure_marks_unreviewed(self, tmp_path):
        """评审环节异常 → unreviewed（与未通过严格区分）。"""

        class BrokenCritic:
            pass_score = 8.0

            def critique(self, *a, **kw):
                raise RuntimeError("评审服务超时")

        orch, reports = make_orchestrator(tmp_path)
        orch._critic = BrokenCritic()
        report = reports.create("分析机会成本", "default", "u")
        result = orch.run(report["report_id"], "default")
        assert result["status"] == "unreviewed"
        assert result["accepted"] is None
        assert result["critic_score"] is None
        assert "评审环节异常" in result["unresolved_issues"][0]
        assert len(result["drafts"]) == 1  # 报告仍返回

    def test_all_tasks_failed_no_empty_report(self, tmp_path):
        """硬约束：子任务全部失败 → 不产出空报告。"""
        registry = ToolRegistry()
        registry.register(FakeSearchTool(error="向量库连接失败"))
        orch, reports = make_orchestrator(tmp_path, registry=registry)
        report = reports.create("分析某主题", "default", "u")
        result = orch.run(report["report_id"], "default")
        assert result["status"] == "failed"
        assert str(ErrCode.REPORT_TASKS_ALL_FAILED.value) in result["error"]
        assert result["drafts"] == []  # 没有空报告

    def test_evidence_rejection_preserved(self, tmp_path):
        """校验淘汰：低分证据保留并标注原因。"""
        orch, reports = make_orchestrator(tmp_path, validator_score=0.05)
        report = reports.create("分析机会成本", "default", "u")
        result = orch.run(report["report_id"], "default")
        # 全部淘汰 → 无采纳证据 → 失败（不产出空报告）
        assert result["status"] == "failed"
        rejected = [e for e in result["blackboard"]["evidence"] if e["adopted"] is False]
        assert rejected and all("低于阈值" in e["rejected_reason"] for e in rejected)


class TestReportApi:
    def test_sync_create_and_views(self, client):
        upload = client.post("/knowledge/upload", files={"file": ("测试.md", "# 手册\n\n机会成本是为了得到某物所放弃的东西。曼昆在第二章中详细讨论了这个概念。".encode(), "text/markdown")})
        assert upload.status_code == 200
        # 同步创建（mock 模式：启发式拆解 + 固定校验 + 脚本评审一轮通过）
        resp = client.post("/reports?sync=true", json={"goal": "分析机会成本的含义与曼昆的举例"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "accepted" and data["accepted"] is True
        rid = data["report_id"]
        # 分视图
        detail = client.get(f"/reports/{rid}").json()["data"]
        assert detail["role_calls"]["writer"] == 1
        plan = client.get(f"/reports/{rid}/plan").json()["data"]
        assert plan["layers"] >= 1 and plan["tasks"]
        ev = client.get(f"/reports/{rid}/evidence").json()["data"]
        assert ev["total"] >= 1 and "adopted" in ev
        cr = client.get(f"/reports/{rid}/critique").json()["data"]
        assert cr["final_score"] == 8.5 and cr["accepted"] is True
        # 列表与删除
        listing = client.get("/reports").json()["data"]
        assert any(r["report_id"] == rid for r in listing)
        client.delete(f"/reports/{rid}")
        assert client.get(f"/reports/{rid}").status_code == 404

    def test_async_submit_returns_immediately(self, client):
        client.post("/knowledge/upload", files={"file": ("异步.md", "# 内容\n\n异步测试内容，机会成本的概念。".encode(), "text/markdown")})
        started = time.time()
        resp = client.post("/reports", json={"goal": "分析机会成本的相关内容"})
        assert time.time() - started < 3  # 立即返回不等待执行完成
        rid = resp.json()["data"]["report_id"]
        for _ in range(20):
            detail = client.get(f"/reports/{rid}").json()["data"]
            if detail["status"] in ("accepted", "rejected", "capped", "unreviewed", "failed"):
                break
            time.sleep(0.3)
        assert detail["status"] in ("accepted", "unreviewed")

    def test_multiagent_metrics_separate_counts(self, client):
        """统计红线：评审失败（unreviewed）与未通过（capped）分开计数。"""
        store = client.app.state.report_store
        # 造三种终态，验证它们被分开计数而非混为一谈
        for status in ("accepted", "capped", "unreviewed"):
            r = store.create(f"goal-{status}", "default", "admin")
            store.update(r["report_id"], status=status, accepted=(status == "accepted"))
        resp = client.get("/admin/metrics/multiagent")
        assert resp.status_code == 200
        by_status = resp.json()["data"]["by_status"]
        assert by_status.get("accepted", 0) >= 1, "accepted 应单独计数"
        assert by_status.get("capped", 0) >= 1, "capped（改满仍不过）应单独计数"
        assert by_status.get("unreviewed", 0) >= 1, "unreviewed（评审异常）应单独计数"

    def test_report_not_found(self, client):
        assert client.get("/reports/not-exist").json()["error"]["code"] == 8004
