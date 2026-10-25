"""F5 端到端：/ask 返回 followups；SSE 在 done 之前发出 followups 事件。"""
import json

from tests.conftest import upload_sample


def _sse_events(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: "):
                data = line[6:]
        if event and data:
            try:
                events.append((event, json.loads(data)))
            except json.JSONDecodeError:
                pass
    return events


class TestFollowups:
    def test_ask_returns_followups(self, client):
        upload_sample(client)
        body = client.post("/ask", json={"question": "Kquery 支持哪些文档格式？"}).json()
        data = body["data"]
        assert "followups" in data, "响应必须带 followups 字段"
        assert isinstance(data["followups"], list)
        assert len(data["followups"]) == 3
        assert all(isinstance(x, str) and x.strip() for x in data["followups"])

    def test_stream_emits_followups_before_done(self, client):
        upload_sample(client)
        with client.stream("POST", "/ask/stream", json={"question": "计费规则是什么？"}) as resp:
            body = "".join(resp.iter_text())
        events = _sse_events(body)
        names = [e for e, _ in events]
        assert "followups" in names, f"SSE 必须发出 followups 事件：{names}"
        assert "done" in names
        # 顺序：followups 必须在 done 之前（前端在 done 时收尾）
        assert names.index("followups") < names.index("done"), names
        items = [p for e, p in events if e == "followups"][0]["items"]
        assert len(items) == 3

    def test_llm_failure_does_not_break_answer(self, client, monkeypatch):
        """追问生成失败 → 主答案照常返回（followups 为空）。"""
        upload_sample(client)

        import app.services.llm as llm_mod

        def _boom(self, answer: str, n: int = 3) -> list[str]:
            raise RuntimeError("追问服务不可用")

        monkeypatch.setattr(llm_mod.MockLLM, "generate_followups", _boom)
        body = client.post("/ask", json={"question": "计费规则是什么？"}).json()
        assert body["status"] == "ok"
        assert body["data"]["answer"], "主答案必须照常返回"
        assert body["data"]["followups"] == []


class TestFollowupsAgentPath:
    def test_agent_stream_emits_followups(self, client):
        """Agent 路径（前端默认 mode=agent）也要发 followups。"""
        upload_sample(client)
        with client.stream(
            "POST", "/ask/stream", json={"question": "计费规则是什么？", "mode": "agent"}
        ) as resp:
            body = "".join(resp.iter_text())
        events = _sse_events(body)
        names = [e for e, _ in events]
        assert "done" in names, names
        assert "followups" in names, f"Agent 路径必须发 followups：{names}"
        assert names.index("followups") < names.index("done"), names
