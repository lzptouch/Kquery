"""OpenAI 兼容通道的**端点能力**判定。

## 场景

用户把 embedding / rerank 指向 **DeepSeek**（它只有 `/chat/completions`），
实测 `/embeddings` → **404 Not Found**。

此前这类失败会走通用重试路径：重试 3 次 → 报一句「连接失败」——
调用方会以为是「服务临时挂了」而一直重试，而实际上**这个服务永远没有这个端点**。

## 判据

- 404 / 405 → **不可重试**，立刻给专用错误码（5211 / 5212）+ 说清楚该怎么改；
- 401 / 403 → 鉴权失败（同样不可重试）；
- 429 → 限流（可重试）。
"""
from __future__ import annotations

import pytest

from app.core.envelope import ErrCode
from app.core.errors import UpstreamUnavailableError
from app.services.openai_compat import OpenAICompatError


class _Resp:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


def _patch_post(monkeypatch, status_code: int, calls: list[int]):
    """把 `post_json` 换成「总是抛给定状态码」的假实现。

    **必须 patch 源模块**（`app.services.openai_compat.post_json`）：
    调用方是在**函数体内**做 `from ... import post_json`，patch 调用方的模块属性无效。
    """
    import app.services.openai_compat as compat

    def _fake_post(url, payload, api_key, timeout_s=30.0, **kwargs):
        calls.append(status_code)
        raise OpenAICompatError(status_code, f"HTTP {status_code}: not found")

    monkeypatch.setattr(compat, "post_json", _fake_post, raising=False)


def test_embedding_404_fails_fast_without_retry(monkeypatch):
    """404 → 不重试，直接失败并给专用错误码。"""
    import app.services.embeddings as emb

    calls: list[int] = []
    _patch_post(monkeypatch, 404, calls)
    client = emb.OpenAICompatibleEmbeddings(
        api_key="k", model="deepseek-chat", base_url="https://api.deepseek.com", dim=8, max_retries=3
    )
    with pytest.raises(UpstreamUnavailableError) as excinfo:
        client.embed_texts(["你好"])
    assert int(excinfo.value.code) == int(ErrCode.EMBEDDING_ENDPOINT_UNSUPPORTED)
    assert excinfo.value.unsupported_endpoint is True
    assert len(calls) == 1, f"404 不该重试，实际调用 {len(calls)} 次"
    assert "不提供 embeddings 端点" in str(excinfo.value)
    assert "DeepSeek" in str(excinfo.value), "报错要说清楚是哪种服务"


def test_rerank_404_fails_fast_without_retry(monkeypatch):
    """404 → 不重试，直接失败并给专用错误码。"""
    import app.services.retrieval.rerank as rr

    calls: list[int] = []
    _patch_post(monkeypatch, 404, calls)
    from app.domain.schemas import Chunk
    from app.services.retrieval.base import FusedCandidate

    def _cand(i: int) -> FusedCandidate:
        chunk = Chunk(
            chunk_id=f"c{i}", doc_id="d1", seq=i, content=f"文档{i}",
            tenant_id="default", source="p1",
        )
        return FusedCandidate(chunk=chunk, rrf_score=0.0)

    client = rr.OpenAICompatibleReranker(
        api_key="k", model="deepseek-chat", base_url="https://api.deepseek.com", max_retries=3
    )
    with pytest.raises(rr.RerankError) as excinfo:
        client.rerank("问题", [_cand(1), _cand(2)], top_n=2)
    assert excinfo.value.code == int(ErrCode.RERANK_ENDPOINT_UNSUPPORTED)
    assert len(calls) == 1, f"404 不该重试，实际调用 {len(calls)} 次"
    assert "不提供 rerank 端点" in str(excinfo.value)


def test_embedding_401_is_auth_failure_not_endpoint_issue(monkeypatch):
    """401 → 鉴权失败（不是「端点不存在」），错误码不同。"""
    import app.services.embeddings as emb

    calls: list[int] = []
    _patch_post(monkeypatch, 401, calls)
    client = emb.OpenAICompatibleEmbeddings(
        api_key="bad", model="m", base_url="https://x.example", dim=8, max_retries=2
    )
    with pytest.raises(UpstreamUnavailableError) as excinfo:
        client.embed_texts(["你好"])
    assert int(excinfo.value.code) == int(ErrCode.DEPENDENCY_UNAVAILABLE)
    assert excinfo.value.unsupported_endpoint is False


def test_provider_notes_explain_deepseek_fallback():
    """启动期提示：生成用 deepseek 时，向量/重排实际回落要说清楚。"""
    from app.core.config import Settings

    s = Settings(_env_file=None)
    s.model_provider = "deepseek"
    s.deepseek_api_key = "x"
    assert s.resolved_embedding_provider == "dashscope"
    assert s.resolved_rerank_provider == "dashscope"
    notes = s.provider_notes()
    assert any("deepseek" in n and "向量" in n for n in notes), notes
    assert any("重排" in n for n in notes), notes


def test_effective_summary_shows_resolved_providers():
    """摘要里要能看到「实际用的」向量/重排 provider。"""
    from app.core.config import Settings

    s = Settings(_env_file=None)
    s.model_provider = "deepseek"
    s.deepseek_api_key = "x"
    summary = s.effective_summary()
    assert summary["resolved_embedding_provider"] == "dashscope"
    assert summary["resolved_rerank_provider"] == "dashscope"
