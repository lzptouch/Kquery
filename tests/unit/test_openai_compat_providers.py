"""OpenAI 兼容的 embeddings / rerank 实现（`LM-01` 相关）。

## 为什么有这两个实现

生成模型可以换供应商（DeepSeek / 千问 / 本地 vLLM），
但**向量与重排**往往还是另一家服务（DeepSeek 就不提供这两个能力）。
把「兼容端点」做成一等公民，换服务时**只改配置不改代码**。
"""
from __future__ import annotations

import pytest

from app.core.errors import UpstreamUnavailableError
from app.services.embeddings import OpenAICompatibleEmbeddings, build_embeddings
from app.services.retrieval.rerank import OpenAICompatibleReranker, build_reranker


def test_embeddings_call_openai_compatible_endpoint(monkeypatch):
    """请求打到 `{base}/embeddings`，响应按 index 排序后返回。"""
    from app.services import openai_compat

    seen: dict = {}

    def _fake_post(url, payload, api_key, timeout_s=30.0, extra_headers=None):
        seen["url"] = url
        seen["payload"] = payload
        seen["api_key"] = api_key
        return {
            "data": [
                {"index": 1, "embedding": [0.0, 3.0, 0.0]},
                {"index": 0, "embedding": [4.0, 0.0, 0.0]},
            ]
        }

    monkeypatch.setattr(openai_compat, "post_json", _fake_post)

    emb = OpenAICompatibleEmbeddings(
        api_key="k", model="bge-m3", base_url="https://gw.example.com/v1/", dim=3
    )
    vectors = emb.embed_texts(["甲", "乙"])
    assert seen["url"] == "https://gw.example.com/v1/embeddings"
    assert seen["payload"] == {"model": "bge-m3", "input": ["甲", "乙"]}
    # 归一化后长度为 1，且顺序按 index（与输入一致）
    assert len(vectors) == 2
    assert abs(vectors[0][0] - 1.0) < 1e-6
    assert abs(vectors[1][1] - 1.0) < 1e-6


def test_embeddings_count_mismatch_is_an_error(monkeypatch):
    """返回条数与输入不符 → 明确报错（不能静默产出错位的向量）。"""
    from app.services import openai_compat

    monkeypatch.setattr(
        openai_compat,
        "post_json",
        lambda *a, **k: {"data": [{"index": 0, "embedding": [1.0, 0.0]}]},
    )
    emb = OpenAICompatibleEmbeddings(
        api_key="k", model="m", base_url="https://gw.example.com/v1", dim=2, max_retries=1
    )
    with pytest.raises(UpstreamUnavailableError):
        emb.embed_texts(["甲", "乙"])


def test_embeddings_auth_error_fails_fast(monkeypatch):
    """401 → 明确报鉴权失败，不重试三次。"""
    from app.services import openai_compat

    calls = {"n": 0}

    def _fake(url, payload, api_key, timeout_s=30.0, extra_headers=None):
        calls["n"] += 1
        raise openai_compat.OpenAICompatError(401, "bad key")

    monkeypatch.setattr(openai_compat, "post_json", _fake)
    emb = OpenAICompatibleEmbeddings(
        api_key="k", model="m", base_url="https://gw.example.com/v1", dim=2, max_retries=3
    )
    with pytest.raises(UpstreamUnavailableError):
        emb.embed_texts(["甲"])
    assert calls["n"] == 1


def test_rerank_call_openai_compatible_endpoint(monkeypatch):
    """请求打到 `{base}/rerank`，结果按返回的 index 还原候选。"""
    from app.services import openai_compat

    seen: dict = {}

    def _fake_post(url, payload, api_key, timeout_s=30.0, extra_headers=None):
        seen["url"] = url
        seen["payload"] = payload
        return {
            "results": [
                {"index": 1, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.5},
            ]
        }

    monkeypatch.setattr(openai_compat, "post_json", _fake_post)

    cands = [_cand("c0", "第一段"), _cand("c1", "第二段")]
    rr = OpenAICompatibleReranker(
        api_key="k", model="bge-reranker", base_url="https://gw.example.com/v1"
    )
    ranked = rr.rerank("问题", cands, top_n=2)
    assert seen["url"] == "https://gw.example.com/v1/rerank"
    assert seen["payload"]["query"] == "问题"
    assert seen["payload"]["documents"] == ["第一段", "第二段"]
    assert [c.chunk.content for c in ranked] == ["第二段", "第一段"]
    assert ranked[0].rerank_score == 0.9


def test_rerank_skips_out_of_range_index(monkeypatch):
    """响应里的越界 index 必须跳过（否则 IndexError 把整次重排打挂）。"""
    from app.services import openai_compat

    monkeypatch.setattr(
        openai_compat,
        "post_json",
        lambda *a, **k: {"results": [{"index": 99, "relevance_score": 1.0}]},
    )
    rr = OpenAICompatibleReranker(
        api_key="k", model="m", base_url="https://gw.example.com/v1"
    )
    assert rr.rerank("问题", [_cand("c0", "段")], top_n=1) == []


def test_build_functions_dispatch():
    """provider 分发：openai_compatible 走兼容实现，dashscope 走千问实现。"""
    from app.services.embeddings import DashScopeEmbeddings
    from app.services.retrieval.rerank import DashScopeReranker

    emb = build_embeddings(
        provider="openai_compatible", api_key="k", model="m", dim=8, max_retries=1,
        base_url="https://gw.example.com/v1",
    )
    assert isinstance(emb, OpenAICompatibleEmbeddings)
    rr = build_reranker(
        provider="openai_compatible", api_key="k", model="m", max_retries=1,
        base_url="https://gw.example.com/v1",
    )
    assert isinstance(rr, OpenAICompatibleReranker)
    assert isinstance(
        build_embeddings(provider="dashscope", api_key="k", model="m", dim=8, max_retries=1),
        DashScopeEmbeddings,
    )
    assert isinstance(
        build_reranker(provider="dashscope", api_key="k", model="m", max_retries=1),
        DashScopeReranker,
    )


def _cand(key: str, content: str):
    from app.domain.schemas import Chunk
    from app.services.retrieval.base import FusedCandidate

    return FusedCandidate(
        chunk=Chunk(
            chunk_id=key, doc_id="d1", filename="f.md", content=content,
            source="s", index=0, seq=0, tenant_id="default",
        ),
        rrf_score=0.5,
    )
