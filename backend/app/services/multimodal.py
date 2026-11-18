"""多模态直答（图文范围，M5）：把**原图**交给 VLM 直接回答。

**为什么需要**：图像路现在只有 caption（VLM 生成的文字描述），
caption 会丢信息——"这张流程图第几步是什么"这类问题，caption 里往往没有答案。
直答 = 把原图喂给 VLM，让它基于图像内容作答。

**降级纪律**（与重排降级同原则）：
VLM 调用失败 / 超时 / 没配 Key → **回落 caption 文本路**，绝不让整个回答失败。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol
from app.core.errors import UpstreamUnavailableError
from app.services.upstream import bulkhead
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

#: 判定"看图型问题"的关键词（中文为主；英文补几个常见词）。
#: 只作为**触发条件**，不作为唯一判据——命中关键词且证据里有图才走直答。
VISUAL_KEYWORDS = (
    "图", "图片", "照片", "截图", "流程图", "示意图", "图表", "插图", "海报", "表格里",
    "picture", "image", "photo", "diagram", "chart", "screenshot",
)

VLM_ANSWER_PROMPT = (
    "请基于这张图片回答用户的问题。只描述你能从图中确认的内容，"
    "不要编造看不清的文字或数字；看不清就明说。回答用中文，简洁。"
)


class VlmAnswerer(Protocol):
    """VLM 直答接口。"""

    def answer(self, image_path: Path, question: str, context: str = "") -> str: ...


class QwenVlAnswerer:
    """千问 qwen-vl-max 直答（限流重试与图像描述同一策略）。"""

    def __init__(self, api_key: str, model: str = "qwen-vl-max", max_retries: int = 2,
                 timeout_s: float = 30.0) -> None:
        self._api_key = api_key
        self._model = model
        self._max_retries = max_retries
        self._timeout_s = timeout_s

    def answer(self, image_path: Path, question: str, context: str = "") -> str:
        import dashscope
        from urllib.parse import quote

        prompt = VLM_ANSWER_PROMPT
        if context:
            prompt = f"{prompt}\n\n相关文本线索：{context[:800]}"
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                # `LM-03`：视觉直答纳入隔离仓（此前只有指数退避，可无限并发压上游）
                with bulkhead("multimodal").acquire(timeout_s=self._timeout_s):
                    resp = dashscope.MultiModalConversation.call(
                        model=self._model,
                        messages=[{
                            "role": "user",
                            "content": [
                                # 文件名含 # 等保留字符会截断 file:// URL，必须百分号编码
                                {"image": f"file://{quote(str(image_path.resolve()))}"},
                                {"text": f"{prompt}\n\n用户问题：{question}"},
                            ],
                        }],
                        api_key=self._api_key,
                        timeout=self._timeout_s,
                    )
                if resp.status_code == 200:
                    return str(resp.output.choices[0].message.content[0]["text"]).strip()
                last_error = RuntimeError(f"VL {resp.status_code}: {resp.message}")
                if "Throttl" not in str(resp.message) and resp.status_code not in (429, 500, 503):
                    raise last_error
            except Exception as exc:  # noqa: BLE001 - 降级继续（异常已在本处被隔离，不影响对外语义）
                last_error = exc
                if attempt == self._max_retries - 1:
                    break
                time.sleep(2**attempt)
        raise UpstreamUnavailableError(f"图像直答失败: {last_error}")


class MiniMaxVlAnswerer:
    """MiniMax M3 系直答（OpenAI 兼容 `/chat/completions` + `image_url` data URL）。

    **为什么单独实现而不是复用 `QwenVlAnswerer`**：两者的传输层不同——
    千问走 DashScope SDK 的 `MultiModalConversation`（`file://` URL），
    MiniMax 走 OpenAI 兼容端点，图片只能以 **base64 `data:` URL** 传入。

    **为什么需要它**：`model_provider=minimax` 时如果直答仍然只认千问 key，
    「图像直答」会静默退化成 caption 文本路——而 M3 系本身就是原生多模态，
    等于把已有的能力关掉。回落纪律不变：失败 / 超时 → 抛 `UpstreamUnavailableError`，
    由调用方回落 caption（不让整个回答失败）。
    """

    #: 输出上限：视觉题的回答通常很短，但 **M3.1-Flash 恒开思考**，
    #: 上限太小会烧在思考上导致空正文（与 `MiniMaxLLM._output_budget` 同一原因）。
    _MAX_TOKENS = 2048

    def __init__(self, api_key: str, model: str = "MiniMax-M3.1-Flash",
                 base_url: str = "https://api.minimax.chat/v1",
                 max_retries: int = 2, timeout_s: float = 30.0) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._timeout_s = timeout_s

    @staticmethod
    def _data_url(image_path: Path) -> str:
        """把本地图片编码成 `data:image/...;base64,...`（MiniMax 只接受这种形式）。"""
        import base64

        suffix = image_path.suffix.lower().lstrip(".") or "png"
        mime = "image/jpeg" if suffix in {"jpg", "jpeg"} else f"image/{suffix}"
        data = base64.b64encode(image_path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{data}"

    def answer(self, image_path: Path, question: str, context: str = "") -> str:
        # 按需载入：兼容端点客户端只在真正调用时才需要（千问路径不必付导入成本）
        from app.services.openai_compat import OpenAICompatError, post_json

        prompt = VLM_ANSWER_PROMPT
        if context:
            prompt = f"{prompt}\n\n相关文本线索：{context[:800]}"
        payload = {
            "model": self._model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": f"{prompt}\n\n用户问题：{question}"},
                    {"type": "image_url", "image_url": {"url": self._data_url(image_path)}},
                ],
            }],
            "max_tokens": self._MAX_TOKENS,
        }
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                with bulkhead("multimodal").acquire(timeout_s=self._timeout_s):
                    body = post_json(
                        f"{self._base_url}/chat/completions",
                        payload, self._api_key, timeout_s=self._timeout_s,
                    )
                # MiniMax 的业务错误藏在 HTTP 200 的 `base_resp` 里，必须先看它
                base = body.get("base_resp") or {}
                if int(base.get("status_code") or 0) != 0:
                    raise OpenAICompatError(
                        200, f"MiniMax 业务错误 {base.get('status_code')}: "
                             f"{base.get('status_msg') or ''}"
                    )
                choice = (body.get("choices") or [{}])[0]
                content = str((choice.get("message") or {}).get("content") or "").strip()
                if not content:
                    # 空正文几乎总是思考烧完上限（finish_reason=length）
                    raise OpenAICompatError(
                        200, f"MiniMax 直答正文为空（finish_reason={choice.get('finish_reason')}）"
                    )
                return content
            except Exception as exc:  # noqa: BLE001 - 降级继续（调用方回落 caption 路）
                last_error = exc
                status = getattr(exc, "status_code", 0)
                if status in (401, 403):
                    break  # 凭据错：重试没有意义
                if attempt == self._max_retries - 1:
                    break
                time.sleep(2**attempt)
        raise UpstreamUnavailableError(f"图像直答失败: {last_error}")


class MockVlmAnswerer:
    """测试用确定性直答（不调网络）。"""

    def answer(self, image_path: Path, question: str, context: str = "") -> str:
        return f"（mock 直答）针对「{question}」，图像 {image_path.name} 的内容说明。"


def is_visual_question(question: str, evidence: list[dict] | None = None) -> bool:
    """是否值得走 VLM 直答。

    判据：**问题像在问图** 且 **证据里确实有图**。
    两者缺一不可 —— 只有关键词没有图 → 无从直答；只有图没有关键词 → 多半是文字问题，
    走 caption 文本路更省一次 VLM 调用。
    """
    text = (question or "").lower()
    if not any(k in text for k in VISUAL_KEYWORDS):
        return False
    for item in evidence or []:
        if item.get("modality") == "image" or item.get("uri"):
            return True
    return False


def image_path_of(evidence: list[dict] | None, assets_dir: Path | None) -> Path | None:
    """从证据里取第一张可用的原图路径。

    证据里的 `uri` 是**资源名**（如 "doc#page-2.png"），不是绝对路径；
    拼上 assets 目录才是真实文件。取不到就返回 None（调用方回落 caption 路）。
    """
    if assets_dir is None:
        return None
    for item in evidence or []:
        uri = str(item.get("uri") or "")
        if not uri:
            continue
        name = uri.split("/")[-1]
        if not name:
            continue
        path = (Path(assets_dir) / name).resolve()
        try:
            if path.is_relative_to(Path(assets_dir).resolve()) and path.exists():
                return path
        except (OSError, ValueError):
            continue
    return None


def build_vlm_answerer(settings, api_key: str = "") -> VlmAnswerer | None:
    """按配置构造 VLM 直答器；没配就返回 None（调用方回落 caption 路）。

    **按生成 provider 选实现**（不是按有没有千问 key）：M3 系原生多模态，
    `model_provider=minimax` 时直答走 MiniMax；其余（含 dashscope）走千问。
    没给 `api_key` 时用对应 provider 的凭据，避免「用错 key 去调另一家的端点」。
    """
    if not bool(getattr(settings, "vlm_answer_enabled", False)):
        return None
    if str(getattr(settings, "resolved_provider", "") or "") == "minimax":
        key = api_key or str(getattr(settings, "minimax_api_key", "") or "")
        if not key:
            logger.warning(
                "vlm_answer_enabled=true 但 model_provider=minimax 且没有 MiniMax Key，图像直答已禁用"
            )
            return None
        return MiniMaxVlAnswerer(
            api_key=key,
            model=str(getattr(settings, "resolved_vlm_model", "MiniMax-M3.1-Flash")),
            base_url=str(getattr(settings, "minimax_base_url", "https://api.minimax.chat/v1")),
        )
    key = api_key or str(getattr(settings, "dashscope_api_key", "") or "")
    if not key:
        logger.warning("vlm_answer_enabled=true 但没有 API Key，图像直答已禁用")
        return None
    return QwenVlAnswerer(
        api_key=key,
        model=str(getattr(settings, "vlm_model", "qwen-vl-max")),
    )
