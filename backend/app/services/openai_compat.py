"""OpenAI 兼容端点的通用 HTTP 客户端（DeepSeek 等）。

## 为什么自己写而不用 openai SDK

`openai` SDK 是**重依赖**（还带自己的重试 / 连接池语义），而本项目只需要三件事：
发一个 JSON POST、读一个 JSON 响应、把 HTTP 错误码翻译成既有的错误码。
用 `httpx` 直接调更可控——尤其是**超时必须由本项目的 `Deadline` 说了算**
（SDK 自己的重试会把预算烧穿）。

## 覆盖哪些端点

| 端点 | 用途 | 供应商 |
|---|---|---|
| `POST {base}/chat/completions` | 生成（流式 / 非流式） | DeepSeek、vLLM、Ollama、硅基流动… |
| `POST {base}/embeddings` | 向量化 | 任何 OpenAI 兼容的 embeddings 服务 |
| `POST {base}/rerank` | 重排 | Jina / Cohere / 硅基流动风格 |

## **DeepSeek 不提供 embeddings / rerank**

DeepSeek 官方 API 只有**对话补全**（`deepseek-chat` / `deepseek-reasoner`），
**没有** embeddings 端点，也**没有** rerank 端点。
所以「用 DeepSeek 的 key 做向量 / 重排」在直连 DeepSeek 时**不可行**——
本模块提供的是**通用 OpenAI 兼容通道**：把 `base_url` 指向真正提供这两个能力的
服务（硅基流动 / 本地 vLLM / Ollama 等）即可，key 用对应服务的。
把 DeepSeek 的 key 填进 embeddings 配置会得到 404 / 400，这是**供应商能力边界**，
不是配置错误。
"""
from __future__ import annotations

import json
from typing import Any

from app.core.logging_setup import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT_S = 30.0


class OpenAICompatError(Exception):
    """OpenAI 兼容端点调用失败（HTTP 层）。"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.message = message


def post_json(
    url: str,
    payload: dict[str, Any],
    api_key: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """POST 一个 JSON，返回解析后的 JSON。

    - **不重试**：重试策略由调用方决定（LLM 侧有 deadline + 退避，
      embeddings / rerank 侧各有自己的批量与失败语义）；
    - **错误不泄露凭据**：异常里只带状态码与服务端消息。
    """
    import httpx

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    if extra_headers:
        headers.update(extra_headers)
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout_s)
    except httpx.TimeoutException as exc:
        raise OpenAICompatError(504, f"请求超时（{timeout_s}s）") from exc
    except httpx.HTTPError as exc:
        raise OpenAICompatError(502, f"连接失败：{type(exc).__name__}") from exc
    if resp.status_code >= 400:
        # 服务端可能回 HTML（网关错误页），只取前 200 字符做线索
        detail = (resp.text or "")[:200]
        raise OpenAICompatError(resp.status_code, detail)
    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        raise OpenAICompatError(502, "响应不是合法 JSON") from exc


def stream_lines(
    url: str,
    payload: dict[str, Any],
    api_key: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Any:
    """SSE 流式调用，逐行产出 `data: {...}` 的 JSON 文本。

    产出**原始行**（不含 `data: ` 前缀），由调用方决定怎么拼——
    不同供应商的增量字段（`choices[].delta.content`）在调用方更清楚。
    """
    import httpx

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Accept": "text/event-stream",
    }
    with httpx.stream(
        "POST", url, json=payload, headers=headers, timeout=timeout_s
    ) as resp:
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001 - 读错误体失败不该盖掉主错误
                pass
            raise OpenAICompatError(resp.status_code, detail)
        for line in resp.iter_lines():
            if not line:
                continue
            if line.startswith("data:"):
                yield line[len("data:"):].strip()
            elif line.startswith(":"):
                continue  # SSE 注释（心跳）
