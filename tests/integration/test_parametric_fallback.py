"""通用知识兜底（doc/analysis/通用知识兜底回答.md §7.1）。

知识库未收录问题时，由模型自身知识回答并标注来源，替代空拒答。
红线场景（敏感词/权限/deadline）不兜底，保持拒答。

MockLLM 对 GENERAL_KNOWLEDGE_PROMPT 返回确定性兜底应答（llm.py），
parametric 路径无需千问凭据即可全链路验证。
"""
import os
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

from tests.conftest import upload_sample

_ENV_KEYS = (
    "DATA_DIR", "MODEL_PROVIDER", "RETRIEVAL_CONFIG_APPLY_DELAY_S",
    "VECTOR_STORE", "KQUERY_MILVUS_URI", "CRAG_CORRECT_TH", "CRAG_INCORRECT_TH",
    "PARAMETRIC_FALLBACK_ENABLED",
)


def _done_payload(events: list[str]) -> str:
    """取 done 事件的 data 行（confidence 事件也含 verdict 字段，不能按内容猜）。"""
    for i, line in enumerate(events):
        if line == "event: done" and i + 1 < len(events):
            return events[i + 1]
    return ""


def _make_client(tmp: str, fallback_enabled: str = "true") -> TestClient:
    os.environ["DATA_DIR"] = tmp
    os.environ["MODEL_PROVIDER"] = "mock"
    os.environ["RETRIEVAL_CONFIG_APPLY_DELAY_S"] = "0"
    os.environ["PARAMETRIC_FALLBACK_ENABLED"] = fallback_enabled
    os.environ["VECTOR_STORE"] = os.environ.get("VECTOR_STORE") or (
        "faiss" if sys.platform == "win32" else "milvus"
    )
    if os.environ["VECTOR_STORE"] == "milvus":
        os.environ["KQUERY_MILVUS_URI"] = f"{tmp}/milvus_lite.db"
    os.environ["CRAG_CORRECT_TH"] = "0.3"
    os.environ["CRAG_INCORRECT_TH"] = "0.1"
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    return TestClient(create_app())


@pytest.fixture()
def client_fallback_off():
    """兜底关闭的客户端：应恢复纯拒答行为。"""
    with tempfile.TemporaryDirectory() as tmp:
        with _make_client(tmp, fallback_enabled="false") as c:
            yield c
        from app.core.config import get_settings

        get_settings.cache_clear()
        for key in _ENV_KEYS:
            os.environ.pop(key, None)


def test_zero_evidence_falls_back(client):
    """零证据（空知识库）→ 通用知识兜底：answered + parametric + 固定置信度。"""
    resp = client.post("/ask", json={"question": "任意问题"})
    assert resp.status_code == 200
    body = resp.json()
    data = body["data"]
    assert data["verdict"] == "answered"
    assert data["answer_source"] == "parametric"
    assert data["refusal_reason"] == ""
    assert data["confidence"] == pytest.approx(0.3)
    assert data["evidence"] == []
    assert data["answer"].startswith("以下回答基于大模型通用知识")
    # warn 信封：提醒语义在 API 层表达（code 在 error 体内，不在 meta）
    assert body["status"] == "warn"
    assert body["error"]["code"] == 4402


def test_low_score_evidence_falls_back(client):
    """有知识库但召回全不相关 → CRAG incorrect → 同样兜底。"""
    upload_sample(client)
    resp = client.post("/ask", json={"question": "火星的第三颗卫星叫什么名字"})
    data = resp.json()["data"]
    assert data["verdict"] == "answered"
    assert data["answer_source"] == "parametric"
    assert data["answer"].startswith("以下回答基于大模型通用知识")
    assert data["evidence"] == []


def test_relevant_answer_unchanged(client):
    """相关问题的有据回答不受影响：answer_source=evidence，引用保留。"""
    upload_sample(client)
    resp = client.post("/ask", json={"question": "Kquery 支持哪些文档格式"})
    data = resp.json()["data"]
    assert data["verdict"] == "answered"
    assert data["answer_source"] == "evidence"
    assert "[#" in data["answer"]


def test_switch_off_restores_refusal(client_fallback_off):
    """兜底关闭 → 恢复纯拒答行为（回滚开关有效性）。"""
    resp = client_fallback_off.post("/ask", json={"question": "任意问题"})
    data = resp.json()["data"]
    assert data["verdict"] == "refused"
    assert data["refusal_reason"] == "no_content"
    assert data["answer_source"] == ""
    assert data["confidence"] == 0.0


def test_sse_stream_carries_answer_source(client):
    """SSE 流式路径（线性）：done 事件透传 answer_source。"""
    events: list[str] = []
    with client.stream("POST", "/ask/stream", json={"question": "任意问题"}) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line:
                events.append(line)
    done = _done_payload(events)
    assert '"answer_source": "parametric"' in done or '"answer_source":"parametric"' in done
    # 兜底不发 refused 事件
    assert not any(l.startswith("event: refused") for l in events)


def test_agent_path_falls_back(client):
    """Agent 路径（带 session_id）：零证据 → answer_node 兜底而非硬拒答。"""
    resp = client.post("/sessions", json={})
    session_id = resp.json()["data"]["session_id"]
    events: list[str] = []
    with client.stream(
        "POST", "/ask/stream",
        json={"question": "火星的第三颗卫星叫什么名字", "session_id": session_id},
    ) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line:
                events.append(line)
    done = _done_payload(events)
    assert '"verdict": "answered"' in done or '"verdict":"answered"' in done
    assert '"answer_source": "parametric"' in done or '"answer_source":"parametric"' in done
    assert not any(l.startswith("event: refused") for l in events)


def test_sensitive_word_still_refuses(client):
    """红线回归：敏感词整体拒答不兜底。"""
    # 注册一个测试敏感词（最高密级）
    resp = client.post("/admin/sensitive", json={
        "word": "绝密测试词", "action": "refuse", "category": "test",
    })
    if resp.status_code not in (200, 201, 409):
        pytest.skip("敏感词接口不可用，跳过红线回归")
    resp = client.post("/ask", json={"question": "绝密测试词 是什么"})
    data = resp.json()["data"]
    assert data["verdict"] == "refused"
    assert data["answer_source"] == ""
