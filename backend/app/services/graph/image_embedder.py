"""图像向量编码（M1 的跨模态向量路）。

**为什么单独一个模块**：caption 路（以文搜图，靠 VLM 写的描述）与
图像向量路（直接用图像 embedding 检索）是**两套不同的编码**，
模型、维度、失败语义都不同，混在 `images.py` 里会互相污染。

**口径**：图像向量路的向量与文本向量**不同空间**（不同维度），
所以必须存**独立 collection**（或独立目录）——混在一个库里会污染文本检索。
本模块只负责"把一张图编码成向量"，存储与融合由 `ImageIndex` 编排。
"""
from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Protocol
from app.core.errors import UpstreamUnavailableError
from app.core.logging_setup import get_logger
from app.services.upstream import bulkhead

logger = get_logger(__name__)


class ImageEmbedder(Protocol):
    """图像向量编码器。"""

    dim: int

    def embed_images(self, paths: list[Path]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]:
        """把**文本查询**编码到图像向量空间（多模态 embedding 的文本侧）。

        没有它，图像向量路就没法用"文字问题"检索——而这正是检索场景的实际用法。
        """
        ...  # pragma: no cover - 协议声明


class DashScopeImageEmbedder:
    """千问多模态 embedding（`multimodal-embedding-v1`）。

    **没配 Key 就不要构造**：调用方据此回落到 caption 路。
    """

    def __init__(self, api_key: str, model: str = "multimodal-embedding-v1",
                 dim: int = 1024, max_retries: int = 2, timeout_s: float = 60.0) -> None:
        self._api_key = api_key
        self._model = model
        self.dim = dim
        self._max_retries = max_retries
        # `LM-03`：等待许可的上限（不许无限排队——要快速失败）
        self._timeout_s = timeout_s

    def embed_query(self, text: str) -> list[float]:
        import dashscope

        resp = dashscope.MultiModalEmbedding.call(
            model=self._model,
            input=[{"text": text}],
            api_key=self._api_key,
        )
        if resp.status_code != 200:
            raise UpstreamUnavailableError(f"文本编码 {resp.status_code}: {resp.message}")
        return [float(x) for x in resp.output["embeddings"][0]["embedding"]]

    def embed_images(self, paths: list[Path]) -> list[list[float]]:
        import dashscope

        out: list[list[float]] = []
        last_error: Exception | None = None
        for path in paths:
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            data = f"data:image/png;base64,{encoded}"
            got = None
            for attempt in range(self._max_retries):
                try:
                    # `LM-03`：图像向量也纳入隔离仓（此前这条路完全不受限）
                    with bulkhead("multimodal").acquire(timeout_s=self._timeout_s):
                        resp = dashscope.MultiModalEmbedding.call(
                        model=self._model,
                        input=[{"image": data}],
                        api_key=self._api_key,
                    )
                    if resp.status_code == 200:
                        got = resp.output["embeddings"][0]["embedding"]
                        break
                    last_error = RuntimeError(f"embedding {resp.status_code}: {resp.message}")
                    if resp.status_code not in (429, 500, 503):
                        raise last_error
                except Exception as exc:  # noqa: BLE001 - 降级继续（异常已在本处被隔离，不影响对外语义）
                    last_error = exc
                    if attempt == self._max_retries - 1:
                        break
                    time.sleep(2**attempt)
            if got is None:
                raise UpstreamUnavailableError(f"图像向量生成失败: {last_error}")
            out.append([float(x) for x in got])
        return out


class MockImageEmbedder:
    """测试用确定性图像向量（按文件内容哈希派生）。

    **它不是真图像向量**：只能验证"双路的编排与融合"是否正确，
    不能用来评估跨模态召回效果。
    """

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim

    def embed_images(self, paths: list[Path]) -> list[list[float]]:
        import hashlib

        out: list[list[float]] = []
        for path in paths:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            out.append(self._vec_from_digest(digest))
        return out

    def embed_query(self, text: str) -> list[float]:
        import hashlib

        return self._vec_from_digest(hashlib.sha256(text.encode("utf-8")).hexdigest())

    def _vec_from_digest(self, digest: str) -> list[float]:
        """从摘要派生定长向量。

        摘要只有 64 个十六进制字符（32 字节），而 dim=1024 需要 512 字节 →
        **必须链式派生**，否则会越界（IndexError → 图像路静默失效）。
        """
        import hashlib

        out: list[float] = []
        seed = digest
        while len(out) < self.dim:
            seed = hashlib.sha256(seed.encode("utf-8")).hexdigest()
            out.extend(int(seed[i : i + 2], 16) / 255.0 for i in range(0, len(seed), 2))
        return out[: self.dim]
