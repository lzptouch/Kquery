"""MiniMax **真实链路**测试（live）：文本 / 流式 / 截断 / 视觉直答 / 错误翻译。

## 为什么单独一个文件

单元测试把 `stream_lines` 换成了假数据，只能证明「我们怎么解释响应」，
证明不了「上游真的这么返回」。这里打的是**真接口**，因此：

* **默认不跑**：需要 `MINIMAX_API_KEY` + `KQUERY_MINIMAX_LIVE=1` 两个都设了才执行。
  没设就 skip——CI 不该因为缺凭据变红，也不该偷偷烧钱。
* **每条用例都断言「可观测的事实」**（非空正文 / 片段可达 / finish_reason /
  错误码翻译），不断言具体措辞——模型输出本来就不该被钉死。

## 覆盖的四个真实风险

| 风险 | 用例 |
|---|---|
| 端点 / 凭据 / 模型名错（最贵的错：看起来像网络问题） | `test_models_endpoint_lists_known_m3_models`、`test_generate_returns_nonempty_answer` |
| 流式解析（SSE 分片边界、`reasoning_content` 混入） | `test_stream_yields_pieces` |
| **推理模型烧穿输出上限**（M3.1-Flash 恒开思考） | `test_thinking_exhausts_small_budget_visibly` |
| 业务错误藏在 HTTP 200 里 | `test_bad_key_is_translated_to_domain_error` |
| 多模态（图片直答走 `image_url` + data URL） | `test_vision_direct_answer_reads_image` |

运行：
```bash
MINIMAX_API_KEY=... KQUERY_MINIMAX_LIVE=1 \
  backend/.venv/bin/python -m pytest tests/integration/test_minimax_live.py -q
```
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.envelope import ErrCode
from app.services.llm import LLMError, build_llm

pytestmark = pytest.mark.skipif(
    not (os.environ.get("MINIMAX_API_KEY") and os.environ.get("KQUERY_MINIMAX_LIVE") == "1"),
    reason="live 用例：需要 MINIMAX_API_KEY 且显式 KQUERY_MINIMAX_LIVE=1",
)

BASE_URL = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.chat/v1")
MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-M3.1-Flash")
KEY = os.environ.get("MINIMAX_API_KEY", "")


def _llm(max_retries: int = 2):
    return build_llm(
        provider="minimax", api_key=KEY, model=MODEL, temperature=0.0,
        max_retries=max_retries, timeout_s=90.0, base_url=BASE_URL,
    )


# ---------- 端点 / 模型清单 ----------


def test_models_endpoint_lists_known_m3_models():
    """`/v1/models` 能列出 M3 系（证明 key 与域名匹配，而不是 401）。"""
    from app.services.openai_compat import post_json  # noqa: F401  (仅确保模块可导入)
    import httpx

    resp = httpx.get(
        f"{BASE_URL}/models", headers={"Authorization": f"Bearer {KEY}"}, timeout=30.0
    )
    assert resp.status_code == 200, resp.text[:300]
    ids = {m.get("id") for m in (resp.json().get("data") or [])}
    assert ids, "模型清单为空"
    assert any(str(i).startswith("MiniMax-M3") for i in ids), f"未见 M3 系：{sorted(ids)}"


# ---------- 文本 / 流式 ----------


def test_generate_returns_nonempty_answer():
    """真调用：答案非空，且 usage 有计费口径。"""
    llm = _llm()
    text = llm.generate("用一句话说明：向量检索里的 rerank 解决什么问题？不要解释。")
    assert text.strip(), "正文为空——若上游是推理模型，检查 max_tokens 是否被思考烧完"
    assert llm.last_usage.output_tokens > 0


def test_stream_yields_pieces():
    """流式：至少一个片段，拼起来等于非流式语义（都非空）。"""
    pieces = list(_llm().stream("只回答一个词：中国的首都。"))
    assert pieces, "流式没有产出任何片段"
    assert "".join(pieces).strip()


def test_thinking_exhausts_small_budget_visibly():
    """把输出上限压到极小：**要么被截断、要么被判失败，但不能静默返回空串**。

    这是 `MiniMax-M3.1-Flash`（恒开思考）最容易踩的坑：
    思考 token 计入 `max_tokens`，上限小的时候正文为空。项目里的约定是
    「空正文 = 失败」，所以这里允许两种可观测结果，但**不允许空串无声通过**。
    """
    llm = _llm()
    try:
        text = llm.generate("请详细解释 HNSW 索引的构建过程，越详细越好。", max_tokens=16)
    except LLMError as exc:
        assert exc.code == ErrCode.GENERATION_FAILED
        assert exc.retryable is False
        return
    assert text.strip(), "返回了空正文却没有报错：空答案必须能被上游失败判据兜住"
    assert llm.last_finish_reason in {"length", "stop"}
    if llm.last_finish_reason == "length":
        # 有正文但被截断：只记录事实，不要求非空之外的行为
        assert text.strip()


# ---------- 错误翻译 ----------


def test_bad_key_is_translated_to_domain_error():
    """坏 key → 领域错误（不可重试、不可降级）。

    **实测补充**（2026-11-14）：鉴权失败走的是 **HTTP 401**，不是
    HTTP 200 + `base_resp=1004`——`base_resp` 那条路是给限流 / 余额等
    「请求合法但业务拒绝」的错误用的。两条路都必须归一成同一个领域错误，
    否则上层熔断会按「供应商方言」分裂。
    """
    llm = build_llm(
        provider="minimax", api_key="sk-invalid-key-for-test", model=MODEL,
        temperature=0.0, max_retries=1, timeout_s=30.0, base_url=BASE_URL,
    )
    with pytest.raises(LLMError) as exc:
        llm.generate("ping")
    assert exc.value.code == ErrCode.GENERATION_FAILED
    assert exc.value.retryable is False
    assert exc.value.degradable is False
    assert "鉴权" in str(exc.value) or "401" in str(exc.value) or "1004" in str(exc.value)


# ---------- 多模态 ----------


def _make_image(path: Path) -> Path:
    """造一张带文字与彩色边框的图（不依赖外部素材）。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (320, 120), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([8, 8, 312, 112], outline=(200, 0, 0), width=4)
    draw.text((40, 50), "KQUERY 2026", fill=(0, 0, 0))
    img.save(path, format="PNG")
    return path


def test_vision_direct_answer_reads_image(tmp_path):
    """图像直答：模型能从图里读出「边框是红色」这类只有看图才知道的事实。"""
    pytest.importorskip("PIL.Image")
    from app.services.multimodal import MiniMaxVlAnswerer

    image = _make_image(tmp_path / "probe.png")
    answerer = MiniMaxVlAnswerer(api_key=KEY, model=MODEL, base_url=BASE_URL, timeout_s=90.0)
    text = answerer.answer(image, "这张图的外边框是什么颜色？只回答颜色。")
    assert text.strip()
    assert "红" in text or "red" in text.lower(), f"未读出边框颜色：{text!r}"
