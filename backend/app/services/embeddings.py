"""Embedding 抽象（设计原则：依赖注入 / 模型统一走千问平台）。

- 生产实现：千问平台 text-embedding-v3（阿里云百炼 API，不部署本地向量模型）；
- 测试实现：特征哈希词袋向量，共享词越多的文本余弦相似度越高，便于无凭据
  环境下跑通"上传 → 检索 → 引用"全链路功能测试。

入库前统一做 L2 归一化，使余弦相似度等价于内积，FAISS IndexFlatIP 即可。
"""
import hashlib
import logging
import math
import time
from typing import Protocol

import numpy as np

logger = logging.getLogger(__name__)


class Embeddings(Protocol):
    dim: int

    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def _normalize(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        return vec
    return vec / norm


def _tokenize(text: str) -> list[str]:
    """中英文混合的轻量分词：英文按词、中文按二元字组。"""
    tokens: list[str] = []
    word: list[str] = []
    for ch in text.lower():
        if ch.isascii() and (ch.isalnum()):
            word.append(ch)
        else:
            if word:
                tokens.append("".join(word))
                word = []
            if "一" <= ch <= "鿿":
                tokens.append(ch)
    if word:
        tokens.append("".join(word))
    # 中文单字再组合相邻二元组，提升短文本重叠度
    cjk = [t for t in tokens if len(t) == 1 and "一" <= t <= "鿿"]
    tokens.extend(a + b for a, b in zip(cjk, cjk[1:]))
    return tokens


class MockEmbeddings:
    """特征哈希词袋向量：确定性好、无外部依赖，相似文本相似度高。"""

    def __init__(self, dim: int = 1024):
        self.dim = dim

    def _embed(self, text: str) -> list[float]:
        vec = np.zeros(self.dim, dtype=np.float32)
        for token in _tokenize(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        return _normalize(vec).tolist()

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


class DashScopeEmbeddings:
    """千问平台 text-embedding-v3。

    失败模式（README §3.5）：限流 → 指数退避重试；持续失败 → 抛错由上层整批中止，
    避免半成品索引。
    """

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-v3",
        dim: int = 1024,
        max_retries: int = 3,
        batch_size: int = 10,
    ):
        self._api_key = api_key
        self.model = model
        self.dim = dim
        self._max_retries = max_retries
        self._batch_size = batch_size

    def _call(self, texts: list[str]) -> list[list[float]]:
        import dashscope

        from app.services.security.platform_quota import get_monitor

        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                get_monitor().record_call(self.model)
                resp = dashscope.TextEmbedding.call(
                    model=self.model,
                    input=texts,
                    dimension=self.dim,
                    api_key=self._api_key,
                )
                if resp.status_code == 200:
                    items = sorted(resp.output["embeddings"], key=lambda x: x["text_index"])
                    return [item["embedding"] for item in items]
                last_error = RuntimeError(f"embedding API {resp.status_code}: {resp.message}")
                # 限流 / 超时类错误退避重试；其余直接抛出
                if "Throttl" not in str(resp.message) and resp.status_code not in (429, 500, 503):
                    raise last_error
            except Exception as exc:  # noqa: BLE001 - 统一转为重试或上抛
                last_error = exc
                if attempt == self._max_retries - 1:
                    break
                wait = 2**attempt
                logger.warning("embedding 调用失败，%ds 后重试（%d/%d）: %s", wait, attempt + 1, self._max_retries, exc)
                time.sleep(wait)
        raise RuntimeError(f"千问 Embedding 接口不可用: {last_error}")

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            batch = texts[i : i + self._batch_size]
            vectors.extend(self._call(batch))
        return [_normalize(np.array(v, dtype=np.float32)).tolist() for v in vectors]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]


def build_embeddings(provider: str, api_key: str, model: str, dim: int, max_retries: int) -> Embeddings:
    if provider == "dashscope":
        return DashScopeEmbeddings(api_key=api_key, model=model, dim=dim, max_retries=max_retries)
    logger.info("使用 MockEmbeddings（未配置千问 Key 或显式指定 mock）")
    return MockEmbeddings(dim=dim)
