"""用**对话模型**做重排（`LLMReranker`）。

## 为什么不是 `/rerank`

DeepSeek 官方 API 没有 `/rerank` 端点（实测 `/v1/rerank` → 404），但它的 chat 模型
能读一批候选并给出顺序——重排本来就是排序任务。于是「重排走 DeepSeek」=
`{base}/chat/completions` + listwise 提示。

代价必须由测试盯住：

- 输出是**自由文本**，不是结构化字段 → 解析必须容错（代码块、前后缀文字、越界编号）；
- 模型**可能漏写**候选 → 漏掉的必须补到尾部，不能凭空消失；
- `rerank_model` 的历史默认是千问重排名（`gte-rerank-v2`）→ 原样发给 DeepSeek 会 400，
  所以必须替换成对话模型名；
- 失败一律抛 `RerankError`（由 `hybrid` 降级为融合序），不能吞掉整次检索。
"""
from __future__ import annotations

import pytest

from app.core.envelope import ErrCode
from app.domain.schemas import Chunk
from app.services.retrieval.base import FusedCandidate
from app.services.retrieval.rerank import LLMReranker, RerankError, build_reranker


def _cand(key: str, content: str) -> FusedCandidate:
    return FusedCandidate(
        chunk=Chunk(
            chunk_id=key, doc_id="d1", filename="f.md", content=content,
            source="s", index=0, seq=0, tenant_id="default",
        ),
        rrf_score=0.5,
    )


def _reply(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def test_orders_candidates_and_assigns_monotonic_scores(monkeypatch):
    """顺序按模型输出重排，`rerank_score` 单调递减（第 1 名 1.0）。"""
    from app.services import openai_compat

    seen: dict = {}

    def _fake_post(url, payload, api_key, timeout_s=30.0, extra_headers=None):
        seen.update(url=url, payload=payload)
        return _reply("[2, 0, 1]")

    monkeypatch.setattr(openai_compat, "post_json", _fake_post)
    rr = LLMReranker(api_key="dk", model="deepseek-chat", base_url="https://api.deepseek.com/")
    cands = [_cand("c0", "甲"), _cand("c1", "乙"), _cand("c2", "丙")]

    ranked = rr.rerank("问题", cands, top_n=3)
    assert seen["url"] == "https://api.deepseek.com/chat/completions"
    assert seen["payload"]["model"] == "deepseek-chat"
    assert [c.chunk.content for c in ranked] == ["丙", "甲", "乙"]
    assert ranked[0].rerank_score == 1.0
    assert ranked[0].rerank_score > ranked[1].rerank_score > ranked[2].rerank_score


def test_parses_json_wrapped_in_prose_or_code_fence(monkeypatch):
    """模型爱输出 ```json ... ``` 或带解释：解析要能扛住。"""
    from app.services import openai_compat

    monkeypatch.setattr(
        openai_compat,
        "post_json",
        lambda *a, **k: _reply("排序如下：\n```json\n[1, 0]\n```\n以上。"),
    )
    rr = LLMReranker(api_key="dk")
    ranked = rr.rerank("q", [_cand("c0", "甲"), _cand("c1", "乙")], top_n=2)
    assert [c.chunk.content for c in ranked] == ["乙", "甲"]


def test_missing_candidates_are_appended_not_dropped(monkeypatch):
    """模型漏写候选时必须补到尾部（重排不能变成「筛选」）。"""
    from app.services import openai_compat

    monkeypatch.setattr(openai_compat, "post_json", lambda *a, **k: _reply("[2]"))
    rr = LLMReranker(api_key="dk")
    ranked = rr.rerank("q", [_cand("c0", "甲"), _cand("c1", "乙"), _cand("c2", "丙")], top_n=3)
    assert [c.chunk.content for c in ranked] == ["丙", "甲", "乙"]
    assert ranked[2].rerank_score == pytest.approx(1 / 3)


def test_out_of_range_index_is_ignored(monkeypatch):
    """越界/重复编号不能让候选重复出现。"""
    from app.services import openai_compat

    monkeypatch.setattr(openai_compat, "post_json", lambda *a, **k: _reply("[9, 0, 0, 1]"))
    rr = LLMReranker(api_key="dk")
    ranked = rr.rerank("q", [_cand("c0", "甲"), _cand("c1", "乙")], top_n=2)
    assert [c.chunk.content for c in ranked] == ["甲", "乙"]


def test_unparseable_output_raises_for_graceful_degrade(monkeypatch):
    """解析不出来 → `RerankError`（上层降级为融合序），绝不能静默返回乱序。"""
    from app.services import openai_compat

    monkeypatch.setattr(openai_compat, "post_json", lambda *a, **k: _reply("我觉得第二条更好"))
    rr = LLMReranker(api_key="dk", max_retries=1)
    with pytest.raises(RerankError):
        rr.rerank("q", [_cand("c0", "甲")], top_n=1)


def test_qwen_rerank_model_name_is_replaced(monkeypatch):
    """`gte-rerank-v2` 是千问重排名 → 换成对话模型名（否则 DeepSeek 400 unknown model）。"""
    from app.services import openai_compat

    seen: dict = {}

    def _fake_post(url, payload, api_key, timeout_s=30.0, extra_headers=None):
        seen["model"] = payload["model"]
        return _reply("[0]")

    monkeypatch.setattr(openai_compat, "post_json", _fake_post)
    rr = LLMReranker(api_key="dk", model="deepseek-chat")
    rr.rerank("q", [_cand("c0", "甲")], top_n=1, model="gte-rerank-v2")
    assert seen["model"] == "deepseek-chat"


def test_explicit_non_qwen_model_is_respected(monkeypatch):
    """用户显式指定别的对话模型 → 原样使用。"""
    from app.services import openai_compat

    seen: dict = {}

    def _fake_post(url, payload, api_key, timeout_s=30.0, extra_headers=None):
        seen["model"] = payload["model"]
        return _reply("[0]")

    monkeypatch.setattr(openai_compat, "post_json", _fake_post)
    rr = LLMReranker(api_key="dk", model="deepseek-chat")
    rr.rerank("q", [_cand("c0", "甲")], top_n=1, model="deepseek-v4-pro")
    assert seen["model"] == "deepseek-v4-pro"


def test_404_maps_to_unsupported_endpoint_code(monkeypatch):
    """base_url 写错（如带了 `/v1`）→ 404：翻译成「端点不存在」而非笼统失败。"""
    from app.services import openai_compat

    def _raise(*a, **k):
        raise openai_compat.OpenAICompatError(404, "not found")

    monkeypatch.setattr(openai_compat, "post_json", _raise)
    rr = LLMReranker(api_key="dk", base_url="https://api.deepseek.com/v1", max_retries=1)
    with pytest.raises(RerankError) as exc:
        rr.rerank("q", [_cand("c0", "甲")], top_n=1)
    assert exc.value.code == ErrCode.RERANK_ENDPOINT_UNSUPPORTED


def test_auth_error_fails_fast(monkeypatch):
    from app.services import openai_compat

    calls = {"n": 0}

    def _raise(*a, **k):
        calls["n"] += 1
        raise openai_compat.OpenAICompatError(401, "unauthorized")

    monkeypatch.setattr(openai_compat, "post_json", _raise)
    rr = LLMReranker(api_key="bad", max_retries=3)
    with pytest.raises(RerankError, match="鉴权"):
        rr.rerank("q", [_cand("c0", "甲")], top_n=1)
    assert calls["n"] == 1


def test_candidates_beyond_max_docs_are_kept_in_tail(monkeypatch):
    """超过 `max_docs` 的候选不送 prompt，但要保留在结果尾部（不丢候选）。"""
    from app.services import openai_compat

    seen: dict = {}

    def _fake_post(url, payload, api_key, timeout_s=30.0, extra_headers=None):
        seen["prompt"] = payload["messages"][-1]["content"]
        return _reply("[0]")

    monkeypatch.setattr(openai_compat, "post_json", _fake_post)
    rr = LLMReranker(api_key="dk", max_docs=2)
    cands = [_cand(f"c{i}", f"第{i}段") for i in range(4)]
    ranked = rr.rerank("q", cands, top_n=4)
    assert "[2]" not in seen["prompt"]  # 只送了前两条
    assert len(ranked) == 4
    assert [c.chunk.content for c in ranked[2:]] == ["第2段", "第3段"]


def test_empty_candidates_short_circuit():
    assert LLMReranker(api_key="dk").rerank("q", [], top_n=5) == []


def test_build_reranker_dispatches_deepseek():
    rr = build_reranker(
        provider="deepseek", api_key="dk", model="deepseek-chat", max_retries=1,
        base_url="https://api.deepseek.com",
    )
    assert isinstance(rr, LLMReranker)
