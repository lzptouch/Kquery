"""SSE 断点续传的**端点**行为（`Last-Event-ID`）。

要钉住四件事：

1. 开启续传时响应带 `X-SSE-Resume: enabled`——前端**只凭这个头**决定敢不敢自动重连
   （没有它就重连 = 重发一个问题，重复计费）；
2. 每一帧都带 `id: {trace_id}:{seq}`，且 seq 连续；
3. 带 `Last-Event-ID` 续传时**只补断点之后**的帧，不重放已收到的一半；
4. 拿不到缓冲时给 `STREAM_NOT_RESUMABLE`（4304），而不是假装从头发一遍。
"""
import json

import pytest


def _frames(text: str) -> list[dict]:
    """把 SSE 文本切成帧（忽略 `: ping` 注释行）。"""
    out: list[dict] = []
    for block in text.split("\n\n"):
        if not block.strip() or block.lstrip().startswith(":"):
            continue
        frame: dict = {"event": "message", "data": None, "id": None}
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("id:"):
                frame["id"] = line[3:].strip()
            elif line.startswith("event:"):
                frame["event"] = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            frame["data"] = json.loads("\n".join(data_lines))
            out.append(frame)
    return out


def _seq_of(frame_id: str) -> int:
    return int(frame_id.rsplit(":", 1)[1])


@pytest.fixture()
def resume_client(client_with_env):
    return client_with_env(SSE_RESUME_ENABLED="true", SSE_RESUME_GRACE_S="1")


def test_resume_enabled_stream_advertises_and_numbers_frames(resume_client):
    resp = resume_client.post("/ask/stream", json={"question": "产品手册支持哪些格式？"})
    assert resp.status_code == 200
    assert resp.headers.get("x-sse-resume") == "enabled"

    frames = _frames(resp.text)
    assert frames, "流式响应里没有可解析的帧"
    ids = [f["id"] for f in frames if f["id"]]
    assert ids, "帧没有 id，客户端无从续传"
    seqs = [_seq_of(i) for i in ids]
    assert seqs == list(range(1, len(seqs) + 1))  # 连续且从 1 开始
    assert all(i.startswith(f"{resp.headers['x-trace-id']}:") for i in ids)


def test_resume_returns_only_the_tail(resume_client):
    resp = resume_client.post("/ask/stream", json={"question": "产品手册支持哪些格式？"})
    trace_id = resp.headers["x-trace-id"]
    frames = _frames(resp.text)
    ids = [f["id"] for f in frames if f["id"]]
    assert len(ids) >= 3, "样本太短，无法验证断点续传"

    cut = len(ids) // 2
    resumed = resume_client.get(
        f"/ask/stream/{trace_id}", headers={"Last-Event-ID": ids[cut - 1]},
    )
    assert resumed.status_code == 200
    tail = [f["id"] for f in _frames(resumed.text) if f["id"]]
    # 断点之后的部分，一个不多一个不少
    assert tail == ids[cut:]


def test_resume_from_zero_replays_everything(resume_client):
    resp = resume_client.post("/ask/stream", json={"question": "计费规则是什么？"})
    trace_id = resp.headers["x-trace-id"]
    ids = [f["id"] for f in _frames(resp.text) if f["id"]]

    resumed = resume_client.get(f"/ask/stream/{trace_id}")
    assert [f["id"] for f in _frames(resumed.text) if f["id"]] == ids


def test_unknown_trace_is_not_resumable(resume_client):
    resp = resume_client.get("/ask/stream/does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == 4304


def test_resume_disabled_by_default(client):
    """默认关闭：不声明能力，续传端点也不存在（404），行为与引入前完全一致。"""
    resp = client.post("/ask/stream", json={"question": "计费规则是什么？"})
    assert resp.status_code == 200
    assert "x-sse-resume" not in {k.lower() for k in resp.headers}
    # 帧上不该有 id（没开续传就不该给客户端任何"能续"的暗示）
    assert all(f["id"] is None for f in _frames(resp.text))

    trace_id = resp.headers["x-trace-id"]
    assert client.get(f"/ask/stream/{trace_id}").status_code == 404
