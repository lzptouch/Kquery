"""真实链路：MiniMax 向量（`embo-01`）+ DeepSeek 对话模型重排。

## 为什么需要这个文件

单元测试证明的是「我们怎么解释响应」，证明不了「上游真的这么返回」。
这里打的是**真接口**，因此：

* **默认不跑**：需要 `KQUERY_VECTOR_LIVE=1` 且对应 provider 的 key 存在；
* **不断言具体措辞**：只断言可观测事实（维度 / 归一化 / 排序相对先后）。

## 覆盖的三个真实风险

| 风险 | 用例 |
|---|---|
| 请求体形状写错（`texts`/`type` 不是 OpenAI 的 `input`）→ 400 | `test_embo01_returns_normalized_1536_vectors` |
| 入库 `db` 与检索 `query` 混用 → 相似度静默失真 | `test_db_and_query_projections_stay_close` |
| **供应商能力边界被当成配置问题排查** | `test_deepseek_has_no_embeddings_or_rerank_endpoint` |

运行：
```bash
KQUERY_VECTOR_LIVE=1 \
  backend/.venv/bin/python -m pytest tests/integration/test_vector_rerank_live.py -q
```
"""
from __future__ import annotations

import math
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KQUERY_VECTOR_LIVE") != "1",
    reason="live 用例：需要显式 KQUERY_VECTOR_LIVE=1（会真实消耗配额）",
)

MINIMAX_KEY = os.environ.get("MINIMAX_API_KEY", "")
MINIMAX_BASE = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.chat/v1")
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

_need_minimax = pytest.mark.skipif(not MINIMAX_KEY, reason="缺 MINIMAX_API_KEY")
_need_deepseek = pytest.mark.skipif(not DEEPSEEK_KEY, reason="缺 DEEPSEEK_API_KEY")


def _emb(dim: int = 1536):
    from app.services.embeddings import MiniMaxEmbeddings

    return MiniMaxEmbeddings(
        api_key=MINIMAX_KEY, model="embo-01", base_url=MINIMAX_BASE, dim=dim, max_retries=2
    )


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb or 1.0)


# ---------- MiniMax embo-01 ----------


@_need_minimax
def test_embo01_returns_normalized_1536_vectors():
    """真实返回 1536 维且单位长度（`embo-01` 的硬约束）。"""
    vec = _emb().embed_query("Milvus 索引别名怎么原子切换？")
    assert len(vec) == 1536
    norm = math.sqrt(sum(x * x for x in vec))
    assert abs(norm - 1.0) < 1e-4, f"未归一化：norm={norm}"


@_need_minimax
def test_batch_keeps_input_order_and_semantics():
    """批量入库保留顺序，且语义相近的两条比无关条目更近。"""
    texts = [
        "索引别名切换通过 POST /admin/index-alias 接口完成，支持原子切换。",
        "可以通过管理接口原子地把索引别名指向新集合。",
        "红烧肉要先用冰糖炒糖色。",
    ]
    vectors = _emb().embed_texts(texts)
    assert len(vectors) == 3
    related = _cos(vectors[0], vectors[1])
    unrelated = _cos(vectors[0], vectors[2])
    assert related > unrelated + 0.05, f"语义区分度不足：related={related:.3f} unrelated={unrelated:.3f}"


@_need_minimax
def test_db_and_query_projections_stay_close():
    """入库 `db` 与检索 `query` 的投影必须高度接近（否则召回静默变差）。"""
    text = "入库与检索必须使用同一模型的不同投影，混用会让相似度失真。"
    emb = _emb()
    doc_vec = emb.embed_texts([text])[0]
    query_vec = emb.embed_query(text)
    assert _cos(doc_vec, query_vec) > 0.95


# ---------- DeepSeek：能力边界 + 对话模型重排 ----------


@_need_deepseek
def test_deepseek_has_no_embeddings_or_rerank_endpoint():
    """**把「DeepSeek 没有向量/重排端点」钉成测试**（实测 404）。

    这条用例的价值是「事实有主」：配置里写 `EMBEDDING_PROVIDER=deepseek` 时
    报的是 404，很容易被当成 URL 写错。若哪天 DeepSeek 真上线了这两个端点，
    这条会红——那时**该改的是代码与文档，而不是把它删掉**。
    """
    import httpx

    headers = {"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"}
    with httpx.Client(timeout=30.0) as client:
        models = client.get(f"{DEEPSEEK_BASE}/v1/models", headers=headers)
        assert models.status_code == 200, models.text[:300]

        emb = client.post(
            f"{DEEPSEEK_BASE}/v1/embeddings",
            headers=headers,
            json={"model": "deepseek-embedding", "input": "hi"},
        )
        assert emb.status_code == 404, f"DeepSeek 竟然有 embeddings 了：{emb.status_code} {emb.text[:200]}"

        rerank = client.post(
            f"{DEEPSEEK_BASE}/v1/rerank",
            headers=headers,
            json={"model": "deepseek-rerank", "query": "q", "documents": ["a"]},
        )
        assert rerank.status_code == 404, f"DeepSeek 竟然有 rerank 了：{rerank.status_code} {rerank.text[:200]}"


@_need_deepseek
def test_llm_reranker_puts_relevant_chunk_first():
    """对话模型 listwise 重排：真实调用能把相关候选排到第一。"""
    from app.domain.schemas import Chunk
    from app.services.retrieval.base import FusedCandidate
    from app.services.retrieval.rerank import LLMReranker

    def cand(key: str, content: str) -> FusedCandidate:
        return FusedCandidate(
            chunk=Chunk(
                chunk_id=key, doc_id="d1", filename="f.md", content=content,
                source="s", index=0, seq=0, tenant_id="default",
            ),
            rrf_score=0.5,
        )

    cands = [
        cand("c0", "红烧肉要先用冰糖炒糖色，再加黄酒小火慢炖。"),
        cand("c1", "Milvus 的索引别名切换通过 POST /admin/index-alias 接口完成，支持原子切换。"),
        cand("c2", "向量维度变更后必须重建集合，否则插入会报 dimension mismatch。"),
    ]
    ranked = LLMReranker(
        api_key=DEEPSEEK_KEY, model="deepseek-chat", base_url=DEEPSEEK_BASE, max_retries=2
    ).rerank("Milvus 索引别名怎么切换？", cands, top_n=3)
    assert len(ranked) == 3
    assert ranked[0].chunk.chunk_id == "c1", [c.chunk.chunk_id for c in ranked]
    # 伪分数单调递减（下游只依赖「可用于排序」这一点）
    assert ranked[0].rerank_score > ranked[1].rerank_score > ranked[2].rerank_score
