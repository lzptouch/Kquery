"""OCR 解析器（阶段 3 §3.3）：图片 / 扫描件 → 结构化文本。

## 架构约束：OCR **一律走云服务 API**，不提供任何本地/自部署模型路径

| 项 | 规定 |
|---|---|
| 生产 | 千问 `qwen-vl-ocr`（阿里云百炼）——**云服务 API** |
| 测试 | `MockOcrEngine`，返回确定性文本（不联网） |
| 本地模型 | **不支持**。`build_ocr_engine` 收到非云 provider 直接报错，不静默降级 |

为什么不留本地 OCR 的口子：

1. **模型权重分发**：本地 OCR（DeepDOC / PaddleOCR / ONNX）需要在**镜像构建期**
   烤入权重，或在运行时从 HuggingFace 下载——内网/离线环境两者都不可行，
   于是"支持本地模型"最终变成"部署后 OCR 静默不可用"。
2. **镜像与 CI 膨胀**：ONNX Runtime + 多个模型权重会让镜像大出一个量级，
   而 CI 每个用例都会背上这份体积。
3. **可复现性**：测试要能用 Mock 跑通全链路；本地模型路径与 Mock 是两套实现，
   测试覆盖到的和线上跑的不是同一段代码。
4. **识别质量**：中文复杂版面（倾斜、多栏、表格线）下，云端视觉语言模型
   通常比开源 OCR 引擎更稳。

因此本模块**只保留云 API 一条路**：想加本地模型，请先推翻这条架构约束，
而不是在这里塞一个开关。
"""
import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from app.core.envelope import ApiError, ErrCode
from app.domain.schemas import MediaAsset
from app.services.parsers.base import ParseResult

logger = logging.getLogger(__name__)

OCR_PROMPT = "请识别图片中的全部文字，按阅读顺序输出，保留段落结构，表格区域用 Markdown 表格表示。"


class OcrEngine(Protocol):
    def recognize(self, image_path: Path) -> str: ...


class QwenVlOcr:
    """千问 qwen-vl-ocr 真实引擎（限流指数退避重试）。"""

    def __init__(self, api_key: str, model: str = "qwen-vl-ocr", max_retries: int = 3):
        self._api_key = api_key
        self._model = model
        self._max_retries = max_retries

    def recognize(self, image_path: Path) -> str:
        import dashscope

        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = dashscope.MultiModalConversation.call(
                    model=self._model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"image": f"file://{quote(str(image_path.resolve()))}"},
                                {"text": OCR_PROMPT},
                            ],
                        }
                    ],
                    api_key=self._api_key,
                )
                if resp.status_code == 200:
                    return resp.output.choices[0].message.content[0]["text"]
                last_error = RuntimeError(f"OCR API {resp.status_code}: {resp.message}")
                if "Throttl" not in str(resp.message) and resp.status_code not in (429, 500, 503):
                    raise last_error
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == self._max_retries - 1:
                    break
                wait = 2**attempt
                logger.warning("OCR 调用失败，%ds 后重试（%d/%d）: %s", wait, attempt + 1, self._max_retries, exc)
                time.sleep(wait)
        raise ApiError(ErrCode.DOCUMENT_PARSE_FAILED, f"OCR 识别失败: {last_error}")


class MockOcrEngine:
    """确定性假引擎：从文件名与大小派生可读文本。"""

    def recognize(self, image_path: Path) -> str:
        return (
            f"[OCR 模拟识别结果]\n"
            f"图像文件 {image_path.name} 中包含一段示例文字。\n"
            f"本文字由 Mock OCR 引擎生成，用于无凭据环境下的全链路测试。\n"
            f"实际使用时由千问 qwen-vl-ocr 返回真实识别内容。"
        )


def _clean_ocr_text(text: str) -> str:
    """二次清洗：剥离代码围栏，丢弃孤立单字符行与过短噪声行（保真动作，§3.3）。"""
    # VL 模型常给整段输出包 ```markdown ... ``` 围栏，入库前剥掉
    text = re.sub(r"^```(?:markdown|md|text)?\s*\n?", "", text.strip())
    text = re.sub(r"\n?```\s*$", "", text).strip()
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        # 孤立单字符（表格线误识别等噪声）
        if len(stripped) == 1 and not stripped.isalnum():
            continue
        if len(re.sub(r"[\W_]+", "", stripped)) == 0:
            continue
        lines.append(line)
    cleaned = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


class OcrParser:
    name = "ocr"

    def __init__(self, engine: OcrEngine, assets_dir: Path):
        self._engine = engine
        self._assets_dir = assets_dir

    def parse(self, path: Path, doc_id: str, tenant_id: str, policy: dict) -> ParseResult:
        if not policy.get("ocr_enabled", True):
            raise ApiError(ErrCode.DOCUMENT_PARSE_FAILED, "未启用 OCR 解析策略，图片文件无法入库")
        raw_text = self._engine.recognize(path)
        text = _clean_ocr_text(raw_text)
        if not text:
            raise ApiError(ErrCode.DOCUMENT_PARSE_FAILED, "OCR 未识别到有效文字")

        # 图像本体进资源目录，元数据进 DocStore（分开存放，§4.5）
        content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        self._assets_dir.mkdir(parents=True, exist_ok=True)
        asset_path = self._assets_dir / f"{doc_id}{path.suffix.lower()}"
        if asset_path.resolve() != path.resolve():
            asset_path.write_bytes(path.read_bytes())
        asset = MediaAsset(
            asset_id=f"{doc_id}#img-1",
            doc_id=doc_id,
            uri=str(asset_path),
            page=1,
            caption=text[:100],
            hash=content_hash,
            tenant_id=tenant_id,
            kind="image",
        )
        return ParseResult(
            segments=[("第1页·OCR", text)],
            assets=[asset],
        )


#: 允许的 OCR provider：云服务 API + 测试用 Mock。
#: **没有本地/自部署模型选项**（见模块 docstring 的架构约束）。
ALLOWED_OCR_PROVIDERS = ("dashscope", "mock")


def build_ocr_engine(provider: str, api_key: str, max_retries: int = 3) -> OcrEngine:
    """按 provider 构造 OCR 引擎。

    非云 provider（local / onnx / self-hosted / paddleocr …）**直接报错**，
    不静默退回 Mock——静默降级会让"配错了"看起来像"跑通了"，
    生产上表现为"图片入库了但没有文字"，极难排查。
    """
    if provider == "dashscope":
        return QwenVlOcr(api_key=api_key, max_retries=max_retries)
    if provider == "mock":
        logger.info("使用 MockOcrEngine（显式指定 mock）")
        return MockOcrEngine()
    raise ApiError(
        ErrCode.INVALID_INPUT,
        f"不支持的 OCR provider={provider!r}：OCR 一律走云服务 API"
        f"（{ '、'.join(ALLOWED_OCR_PROVIDERS) }），不支持本地/自部署模型",
    )
