"""MiniMax `embo-01` 向量实现（`MiniMaxEmbeddings`）。

## 为什么单独测一个类

MiniMax 的 embeddings **不是 OpenAI 形状**，三个差异每一个都会静默出错：

1. 请求字段是 `texts` 而不是 `input`，且**必须**带 `type`（`db` 入库 / `query` 检索）；
2. 响应是 `{"vectors":[[...]],"base_resp":{...}}`，没有 `data[].embedding`、
   也没有 `index`（顺序即输入顺序）；
3. **业务错误放在 HTTP 200 里**（`base_resp.status_code != 0`）。
   不检查就会把「鉴权失败 / 限流」当成「拿到了一堆空向量」，然后写进向量库。

维度也不是可选项：`embo-01` 固定 1536，与千问的 1024 不同。
维度不符时**必须在入库前**报错——Milvus 集合维度是 schema 级硬约束，
写进去才发现就只能重建集合重新灌数据。
"""
from __future__ import annotations

import pytest

from app.core.envelope import ErrCode
from app.core.errors import UpstreamUnavailableError
from app.services.embeddings import MiniMaxEmbeddings, build_embeddings


def _fake_resp(vectors: list[list[float]], status_code: int = 0, msg: str = "success") -> dict:
    return {"vectors": vectors, "total_tokens": 8, "base_resp": {"status_code": status_code, "status_msg": msg}}


def test_texts_use_minimax_request_shape(monkeypatch):
    """入库走 `type=db`，字段名是 `texts`（不是 OpenAI 的 `input`）。"""
    from app.services import openai_compat

    seen: dict = {}

    def _fake_post(url, payload, api_key, timeout_s=30.0, extra_headers=None):
        seen.update(url=url, payload=payload, api_key=api_key)
        return _fake_resp([[3.0, 4.0], [1.0, 0.0]])

    monkeypatch.setattr(openai_compat, "post_json", _fake_post)
    emb = MiniMaxEmbeddings(api_key="mk", base_url="https://api.minimax.chat/v1", dim=2)

    vectors = emb.embed_texts(["甲", "乙"])
    assert seen["url"] == "https://api.minimax.chat/v1/embeddings"
    assert seen["payload"] == {"model": "embo-01", "texts": ["甲", "乙"], "type": "db"}
    assert seen["api_key"] == "mk"
    # L2 归一化：3-4-5 直角三角形 → (0.6, 0.8)
    assert vectors[0] == pytest.approx([0.6, 0.8], abs=1e-6)


def test_query_uses_type_query(monkeypatch):
    """查询侧必须是 `type=query`：与入库的 `db` 配对，混用会让相似度静默失真。"""
    from app.services import openai_compat

    seen: dict = {}

    def _fake_post(url, payload, api_key, timeout_s=30.0, extra_headers=None):
        seen["payload"] = payload
        return _fake_resp([[1.0, 0.0]])

    monkeypatch.setattr(openai_compat, "post_json", _fake_post)
    emb = MiniMaxEmbeddings(api_key="mk", dim=2)
    emb.embed_query("问题")
    assert seen["payload"]["type"] == "query"
    assert seen["payload"]["texts"] == ["问题"]


def test_business_error_in_http_200_is_not_swallowed(monkeypatch):
    """`base_resp.status_code != 0` 必须报错（HTTP 200 也可能是失败）。"""
    from app.services import openai_compat

    monkeypatch.setattr(
        openai_compat, "post_json", lambda *a, **k: _fake_resp([], 1002, "rate limit")
    )
    emb = MiniMaxEmbeddings(api_key="mk", dim=2, max_retries=2)
    with pytest.raises(UpstreamUnavailableError):
        emb.embed_texts(["甲"])


def test_auth_code_fails_fast_without_retry(monkeypatch):
    """鉴权类业务错误（1004）不重试：同一个 key 再试三次也一样。"""
    from app.services import openai_compat

    calls = {"n": 0}

    def _fake_post(url, payload, api_key, timeout_s=30.0, extra_headers=None):
        calls["n"] += 1
        return _fake_resp([], 1004, "invalid api key")

    monkeypatch.setattr(openai_compat, "post_json", _fake_post)
    emb = MiniMaxEmbeddings(api_key="bad", dim=2, max_retries=3)
    with pytest.raises(UpstreamUnavailableError, match="鉴权"):
        emb.embed_texts(["甲"])
    assert calls["n"] == 1


def test_dimension_mismatch_is_rejected_before_writing(monkeypatch):
    """返回维度与配置不符 → 拒绝（否则会把错的维度灌进 Milvus 集合）。"""
    from app.services import openai_compat

    monkeypatch.setattr(openai_compat, "post_json", lambda *a, **k: _fake_resp([[1.0, 0.0]]))
    emb = MiniMaxEmbeddings(api_key="mk", dim=1536)
    with pytest.raises(UpstreamUnavailableError, match="维度"):
        emb.embed_texts(["甲"])


def test_count_mismatch_is_rejected(monkeypatch):
    """返回条数与输入不符 → 拒绝（错位向量比报错更危险）。"""
    from app.services import openai_compat

    monkeypatch.setattr(openai_compat, "post_json", lambda *a, **k: _fake_resp([[1.0, 0.0]]))
    emb = MiniMaxEmbeddings(api_key="mk", dim=2)
    with pytest.raises(UpstreamUnavailableError, match="条数"):
        emb.embed_texts(["甲", "乙"])


def test_batches_are_chunked(monkeypatch):
    """超过 batch_size 时分批调用，拼接顺序与输入一致。"""
    from app.services import openai_compat

    calls: list[list[str]] = []

    def _fake_post(url, payload, api_key, timeout_s=30.0, extra_headers=None):
        calls.append(list(payload["texts"]))
        return _fake_resp([[1.0, 0.0] for _ in payload["texts"]])

    monkeypatch.setattr(openai_compat, "post_json", _fake_post)
    emb = MiniMaxEmbeddings(api_key="mk", dim=2, batch_size=2)
    vectors = emb.embed_texts(["1", "2", "3", "4", "5"])
    assert [len(c) for c in calls] == [2, 2, 1]
    assert len(vectors) == 5


def test_404_maps_to_unsupported_endpoint_code(monkeypatch):
    """base_url 少写 `/v1` 时是 404：必须翻译成「端点不存在」而不是笼统的上游不可用。"""
    from app.services import openai_compat

    def _raise(*a, **k):
        raise openai_compat.OpenAICompatError(404, "not found")

    monkeypatch.setattr(openai_compat, "post_json", _raise)
    emb = MiniMaxEmbeddings(api_key="mk", base_url="https://api.minimax.chat", dim=2, max_retries=1)
    with pytest.raises(UpstreamUnavailableError) as exc:
        emb.embed_texts(["甲"])
    assert exc.value.code == ErrCode.EMBEDDING_ENDPOINT_UNSUPPORTED


def test_build_embeddings_dispatches_minimax():
    emb = build_embeddings(
        provider="minimax", api_key="mk", model="embo-01", dim=1536, max_retries=1,
        base_url="https://api.minimax.chat/v1",
    )
    assert isinstance(emb, MiniMaxEmbeddings)
