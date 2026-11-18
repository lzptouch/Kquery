"""Agentic RAG 测试。

覆盖：状态合并语义 / 规划解析兜底 / 反思三防线 / 三层熔断 /
工具失败不中断 / 会话存档（归属/TTL/乱序/跨版本）/ API 契约 / 端到端。
"""
import json
import time

import pytest

from app.services.agent.graph import build_agent_graph
from app.services.agent.planner import HeuristicPlanner, sanitize_steps
from app.services.agent.reflector import HeuristicReflector
from app.services.agent.runner import agent_prompt_builder
from app.services.agent.sessions import SessionStore
from app.services.agent.state import initial_state, merge_evidence, sanitize_snapshot
from app.services.agent.tools import CalculatorTool, ToolRegistry
from app.services.llm import MockLLM


# ---------- 假工具 ----------

class FakeSearchTool:
    name = "vector_search"
    description = "测试检索工具"

    def __init__(self, items=None, error=""):
        self._items = items or []
        self._error = error
        self.calls: list[str] = []

    def run(self, query: str, tenant_id: str):
        from app.services.agent.tools import ToolResult

        self.calls.append(query)
        if self._error:
            return ToolResult(error=self._error)
        return ToolResult(items=list(self._items), elapsed_ms=5)


def _evidence(chunk_id: str, score: float = 0.5, content: str = "内容") -> dict:
    return {
        "chunk_id": chunk_id, "content": content, "score": score,
        "source": "测试文档", "doc_id": "doc-t", "source_route": "vector",
    }


def make_registry(tool=None) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(tool or FakeSearchTool([_evidence("c#1")]))
    registry.register(CalculatorTool())
    return registry


# ---------- 状态合并语义 ----------

class TestStateMerge:
    def test_evidence_append_and_dedupe(self):
        old = [_evidence("a#1"), _evidence("a#2")]
        new = [_evidence("a#2"), _evidence("a#3")]  # a#2 重复
        merged = merge_evidence(old, new)
        assert [e["chunk_id"] for e in merged] == ["a#1", "a#2", "a#3"]

    def test_snapshot_cross_version(self):
        """旧快照缺字段按默认补齐；类型不符回退默认。"""
        snap = sanitize_snapshot({"question": "q", "iteration": "not-an-int", "future_field": 1})
        assert snap["question"] == "q"
        assert snap["iteration"] == 0  # 类型不符 → 默认
        assert snap["future_field"] == 1  # 多余字段保留（向前兼容）


# ---------- 规划解析 ----------

class TestPlannerSanitize:
    def test_hallucinated_tool_falls_back(self):
        registry = make_registry()
        steps = sanitize_steps([{"description": "查一下", "tool": "oracle_search"}], registry)
        assert steps[0]["tool_name"] == "vector_search"  # 兜底到第一个工具
        assert steps[0]["tool_hallucinated"] is True

    def test_numbering_continuous_and_dag(self):
        registry = make_registry()
        steps = sanitize_steps([
            {"description": "第一步", "tool": "vector_search"},
            {"description": "第二步", "tool": "calculator", "depends_on": [0, 5]},  # 5 越界被剔除
        ], registry)
        assert [s["step_index"] for s in steps] == [0, 1]
        assert steps[1]["depends_on"] == [0]

    def test_empty_description_dropped(self):
        registry = make_registry()
        steps = sanitize_steps([{"description": "  ", "tool": "vector_search"}], registry)
        assert steps == []


# ---------- 图执行 ----------

def run_graph(registry, planner, reflector, question="测试问题", skip=False):
    graph = build_agent_graph(registry, planner, reflector, MockLLM(), agent_prompt_builder)
    events = []
    final = graph.invoke(
        {**initial_state(question, "tenant"), "skip_retrieval": skip},
        config={"configurable": {"emit": lambda e, p: events.append((e, p))}},
    )
    return final, events


class ScriptedPlanner:
    """返回固定计划的规划器。"""

    def __init__(self, plans: list[list[dict]]):
        self._plans = plans
        self.calls = 0

    def plan(self, question: str, replan_hint: str = "") -> list[dict]:
        registry_plan = self._plans[min(self.calls, len(self._plans) - 1)]
        self.calls += 1
        return sanitize_steps(registry_plan, _registry_holder)


class ScriptedReflector:
    """按脚本返回反思结论。"""

    def __init__(self, verdicts: list[dict]):
        self._verdicts = verdicts
        self.calls = 0

    def reflect(self, question: str, evidence: list[dict], remaining_steps: int) -> dict:
        v = self._verdicts[min(self.calls, len(self._verdicts) - 1)]
        self.calls += 1
        return dict(v)


_registry_holder: ToolRegistry = None  # ScriptedPlanner 用


@pytest.fixture(autouse=True)
def _set_registry():
    global _registry_holder
    _registry_holder = make_registry()


class TestGraphExecution:
    def test_happy_path_plan_tool_reflect_answer(self):
        registry = make_registry(FakeSearchTool([_evidence("c#1", 0.9, "证据内容")]))
        planner = HeuristicPlanner(registry)
        reflector = HeuristicReflector()
        final, events = run_graph(registry, planner, reflector)
        assert final["status"] == "completed"
        assert final["iteration"] == 1
        assert final["evidence"][0]["chunk_id"] == "c#1"
        kinds = [e for e, _ in events]
        assert kinds[0] == "plan"
        assert "tool_start" in kinds and "tool_end" in kinds and "reflect" in kinds
        assert "token" in kinds  # MockLLM 流式生成

    def test_iteration_limit_fuse(self):
        """第一层熔断：低分 continue 且证据持续增长（不触发无效进展熔断）→ 达到总轮数上限强制作答。

        高分 continue/replan 会被"证据已足"纠正为直接作答（对称纠正防线），
        因此走到总轮数熔断的路径必须是低分 + 每轮有新证据。
        """

        class GrowingTool:
            name = "vector_search"
            description = "每次返回新证据"

            def __init__(self):
                self.n = 0

            def run(self, query: str, tenant_id: str):
                from app.services.agent.tools import ToolResult

                self.n += 1
                return ToolResult(items=[_evidence(f"c#{self.n}", 0.1)], elapsed_ms=1)

        registry = ToolRegistry()
        registry.register(GrowingTool())
        global _registry_holder
        _registry_holder = registry
        planner = ScriptedPlanner([[{"description": "q", "tool": "vector_search"}]] * 5)
        reflector = ScriptedReflector([{"score": 0.3, "next_action": "continue", "reason": ""}] * 10)
        final, events = run_graph(registry, planner, reflector)
        assert final["status"] == "circuit_breaker"
        assert final["fuse_reason"] == "iteration_limit"
        assert final["incomplete"] is True
        assert any(e == "fuse" for e, _ in events)

    def test_no_progress_fuse(self):
        """第二层熔断：连续"继续"但证据不增加 → 强制作答。"""
        registry = make_registry(FakeSearchTool([_evidence("c#1", 0.9)]))  # 同一证据，去重后不增长
        planner = ScriptedPlanner([
            [{"description": "q1", "tool": "vector_search"}, {"description": "q2", "tool": "vector_search"},
             {"description": "q3", "tool": "vector_search"}, {"description": "q4", "tool": "vector_search"}],
        ])
        reflector = ScriptedReflector([{"score": 0.3, "next_action": "continue", "reason": ""}] * 10)
        final, events = run_graph(registry, planner, reflector)
        assert final["fuse_reason"] == "no_progress"
        assert final["status"] == "circuit_breaker"

    def test_tool_failure_not_fatal(self):
        """工具异常：该步标记失败，流程继续到作答。"""
        registry = make_registry(FakeSearchTool(error="连接超时"))
        planner = HeuristicPlanner(registry)
        reflector = ScriptedReflector([{"score": 0.8, "next_action": "answer", "reason": ""}])
        final, events = run_graph(registry, planner, reflector)
        assert final["plan"][0]["status"] == "failed"
        assert final["tool_errors"][0]["error"] == "连接超时"
        assert final["degraded"] is True
        assert final["status"] == "completed"  # 不中断

    def test_low_score_correction(self):
        """反思第二道防线：低分 answer 被纠正为 continue。

        （循环检测）落地后行为有一处**预期内的变化**：本测试的 planner 永远
        返回同一计划，重规划产出的查询与首次完全相同 —— 这类无信息增益的重复调用
        现在会被指纹检测短路并收敛，而不是继续"重规划→再检索→再重规划"。
        熔断不是错误：仍会基于已有证据尽力作答，只是标记 incomplete。
        """
        registry = make_registry(FakeSearchTool([_evidence("c#1", 0.9)]))
        planner = ScriptedPlanner([[{"description": "q", "tool": "vector_search"}]])
        # 低分却想作答 → 纠正为 continue → 无剩余步骤 → 升级 replan → 重复查询被循环检测短路
        reflector = ScriptedReflector([
            {"score": 0.2, "next_action": "answer", "reason": ""},
            {"score": 0.9, "next_action": "answer", "reason": ""},
        ])
        final, events = run_graph(registry, planner, reflector)
        kinds = [e for e, _ in events]
        assert "replan" in kinds  # 经历了重规划
        assert final["replan_count"] <= 2, "重规划必须有上限"
        assert final["status"] in ("completed", "circuit_breaker")
        # 无论走哪条路径，都必须给出可用产出（答案或明确拒答），不能空返回
        assert final.get("final_answer") or final.get("refusal") is True

    def test_calculator_tool(self):
        registry = make_registry()
        tool = registry._tools["calculator"]
        result = tool.run("(1 + 2) * 3", "t")
        assert "9" in result.items[0]["content"]
        bad = tool.run("import os; os.system('ls')", "t")
        assert bad.error  # 白名单拒绝任意代码

    def test_chitchat_skip_retrieval(self):
        registry = make_registry()
        final, events = run_graph(registry, HeuristicPlanner(registry), HeuristicReflector(),
                                  question="你好", skip=True)
        assert final["status"] == "completed"
        assert not any(e == "tool_start" for e, _ in events)  # 未走检索


# ---------- 会话存档 ----------

class TestSessionStore:
    def test_crud_and_ownership(self, tmp_path):
        store = SessionStore(tmp_path / "s")
        s = store.create("tenantA", "u1", "测试")
        sid = s["session_id"]
        assert sid.startswith("tenantA:u1:")
        # 归属校验：他人不可读
        from app.core.envelope import ApiError
        with pytest.raises(ApiError):
            store.get(sid, "tenantA", "u2")
        with pytest.raises(ApiError):
            store.get(sid, "tenantB", "u1")
        # 重命名 + 列表
        store.rename(sid, "tenantA", "u1", " renamed ")
        sessions = store.list_sessions("tenantA", "u1")
        assert sessions[0]["title"] == "renamed"
        # 删除后快照文件不存在
        store.delete(sid, "tenantA", "u1")
        assert not store._path(sid).exists()

    def test_ttl_expiry(self, tmp_path):
        store = SessionStore(tmp_path / "s", ttl_s=1)
        s = store.create("t", "u")
        path = store._path(s["session_id"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["expires_at"] = time.time() - 1  # 伪造过期
        path.write_text(json.dumps(payload))
        from app.core.envelope import ApiError
        with pytest.raises(ApiError, match="已过期"):
            store.get(s["session_id"], "t", "u")
        assert not path.exists()  # 惰性清理

    def test_turn_evidence_carry(self, tmp_path):
        store = SessionStore(tmp_path / "s")
        s = store.create("t", "u")
        sid = s["session_id"]
        state = {**initial_state("q1", "t"), "evidence": [_evidence(f"c#{i}") for i in range(12)]}
        store.save_turn(sid, "t", "u", "q1", "a1", "answered", state)
        ctx = store.load_context(sid, "t", "u")
        assert ctx["turn"] == 1
        assert len(ctx["carried_evidence"]) == 8  # 上限截断
        assert ctx["history"][0]["question"] == "q1"


# ---------- API 契约 ----------

def upload_sample(client) -> None:
    md = (
        "# 产品手册\n\nKquery 是智能知识库问答系统。\n\n"
        "## 规格\nXB-2024 电池的标称电压为 24V，容量 5000mAh。\n"
        "XB-2025 电池的标称电压为 36V，容量 6000mAh。\n"
    )
    resp = client.post("/knowledge/upload", files={"file": ("手册.md", md.encode(), "text/markdown")})
    assert resp.status_code == 200


class TestSessionApi:
    def test_sessions_crud(self, client):
        # 创建
        resp = client.post("/sessions", json={"title": "调研"})
        assert resp.status_code == 200
        sid = resp.json()["data"]["session_id"]
        # 列表
        sessions = client.get("/sessions").json()["data"]
        assert any(s["session_id"] == sid for s in sessions)
        # 详情
        detail = client.get(f"/sessions/{sid}").json()["data"]
        assert detail["title"] == "调研"
        # 重命名
        client.patch(f"/sessions/{sid}", json={"title": "新名"})
        assert client.get(f"/sessions/{sid}").json()["data"]["title"] == "新名"
        # 删除
        client.delete(f"/sessions/{sid}")
        assert client.get(f"/sessions/{sid}").status_code == 404

    def test_session_ownership_prefix_still_enforced(self, client):
        """会话归属仍按前缀判定；请求头无法切换主体。

        会话 ID 形如 `{tenant}:{user}:{token}`。身份固定后前缀恒为 `default:local:`，
        因此**伪造他人前缀的会话 ID** 必须 404（归属校验没被顺手删掉）。
        """
        sid = client.post("/sessions", json={}).json()["data"]["session_id"]
        assert sid.startswith("default:local:"), sid
        # 自称他人仍是同一主体 → 能取回
        assert client.get(f"/sessions/{sid}", headers={"X-User-Id": "someone-else"}).status_code == 200
        # 但前缀对不上的会话 ID 一律 404（不泄露存在性）
        resp = client.get("/sessions/other:other:s1")
        assert resp.status_code == 404


class TestAgentAsk:
    def test_agent_mode_sync(self, client):
        upload_sample(client)
        resp = client.post("/ask", json={"question": "XB-2024 的电压是多少？", "mode": "agent"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["verdict"] in ("answered", "refused")
        assert "agent" in data
        assert data["agent"]["iterations"] >= 1
        assert data["agent"]["plan"][0]["tool_name"] == "vector_search"

    def test_agent_sse_event_sequence(self, client):
        upload_sample(client)
        resp = client.post("/ask/stream", json={"question": "XB-2024 电压？", "mode": "agent"})
        assert resp.status_code == 200
        events = [l[7:] for l in resp.text.splitlines() if l.startswith("event: ")]
        assert "plan" in events
        assert "tool_start" in events and "tool_end" in events
        assert "reflect" in events
        assert events[-1] in ("done", "error")

    def test_session_context_accumulates(self, client):
        """多轮：第二轮携带 session_id，历史轮次写入会话。"""
        upload_sample(client)
        sid = client.post("/sessions", json={}).json()["data"]["session_id"]
        client.post("/ask", json={"question": "XB-2024 电压？", "session_id": sid})
        detail = client.get(f"/sessions/{sid}").json()["data"]
        assert len(detail["turns"]) == 1
        assert detail["turns"][0]["question"] == "XB-2024 电压？"

    def test_invalid_session_returns_404(self, client):
        resp = client.post("/ask", json={"question": "q", "session_id": "default:anon:notexist"})
        # 会话不存在 → 4301
        assert resp.json()["error"]["code"] == 4301

    def test_agent_metrics_endpoint(self, client):
        upload_sample(client)
        client.post("/ask", json={"question": "XB-2024 电压？", "mode": "agent"})
        data = client.get("/admin/metrics/agent").json()["data"]
        assert data["runs"] >= 1
        assert "vector_search" in data["tool_distribution"]
        assert data["avg_iterations"] >= 1
