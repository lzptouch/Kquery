"""Embedding 抽象（设计原则：依赖注入 / 模型统一走千问平台）。

- 生产实现：千问平台 text-embedding-v3（阿里云百炼 API，不部署本地向量模型）；
- 测试实现：特征哈希词袋向量，共享词越多的文本余弦相似度越高，便于无凭据
  环境下跑通"上传 → 检索 → 引用"全链路功能测试。

入库前统一做 L2 归一化，使余弦相似度等价于内积，FAISS IndexFlatIP 即可。
"""
import hashlib
import time
from typing import Protocol

import numpy as np

from app.services.upstream import CircuitOpenError, bulkhead
from app.core.envelope import ErrCode
from app.core.errors import UpstreamUnavailableError
from app.core.logging_setup import get_logger

logger = get_logger(__name__)


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

    def __init__(self, dim: int = 1024) -> None:
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
    ) -> None:
        self._api_key = api_key
        self.model = model
        self.dim = dim
        self._max_retries = max_retries
        self._batch_size = batch_size

    def _call(self, texts: list[str]) -> list[list[float]]:
        import dashscope

        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                # 批次 2：上游隔离仓（熔断打开时 CircuitOpenError 直接上抛，不重试）
                with bulkhead("embedding").acquire(timeout_s=30.0):
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
            except CircuitOpenError:
                raise
            except Exception as exc:  # noqa: BLE001 - 统一转为重试或上抛
                last_error = exc
                if attempt == self._max_retries - 1:
                    break
                wait = 2**attempt
                logger.warning("embedding 调用失败，%ds 后重试（%d/%d）: %s", wait, attempt + 1, self._max_retries, exc)
                time.sleep(wait)
        raise UpstreamUnavailableError(f"千问 Embedding 接口不可用: {last_error}")

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            batch = texts[i : i + self._batch_size]
            vectors.extend(self._call(batch))
        return [_normalize(np.array(v, dtype=np.float32)).tolist() for v in vectors]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]


class OpenAICompatibleEmbeddings:
    """OpenAI 兼容的 `/embeddings` 端点。

    覆盖任何提供 OpenAI 兼容 embeddings 的服务（硅基流动 / vLLM / Ollama / TEI…）。

    **DeepSeek 不在此列**：DeepSeek 官方 API 没有 embeddings 端点，
    拿 DeepSeek 的 key 调 `/embeddings` 会得到 404/400——这是**供应商能力边界**，
    不是配置错误（详见 `services/openai_compat.py`）。

    失败模式与 `DashScopeEmbeddings` 一致：退避重试 → 持续失败抛
    `UpstreamUnavailableError`（整批中止，不留半成品索引）。
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        dim: int = 1024,
        max_retries: int = 3,
        batch_size: int = 10,
        timeout_s: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._base_url = base_url.rstrip("/")
        self.dim = dim
        self._max_retries = max(1, int(max_retries))
        self._batch_size = max(1, int(batch_size))
        self._timeout_s = timeout_s

    def _call(self, texts: list[str]) -> list[list[float]]:
        # 按需载入：兼容端点客户端只在真正调用时才需要（不必为未启用的 provider 付导入成本）
        from app.services.openai_compat import OpenAICompatError, post_json

        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                with bulkhead("embedding").acquire(timeout_s=self._timeout_s):
                    resp = post_json(
                        f"{self._base_url}/embeddings",
                        {"model": self.model, "input": texts},
                        self._api_key,
                        timeout_s=self._timeout_s,
                    )
                data = resp.get("data") or []
                # 按 index 排序：响应顺序不保证与输入一致
                items = sorted(data, key=lambda x: x.get("index", 0))
                vectors = [item["embedding"] for item in items]
                if len(vectors) != len(texts):
                    raise UpstreamUnavailableError(
                        f"embedding 返回条数不符：期望 {len(texts)}，实际 {len(vectors)}"
                    )
                return vectors
            except CircuitOpenError:
                raise
            except OpenAICompatError as exc:
                last_error = exc
                if exc.status_code in (401, 403):
                    raise UpstreamUnavailableError(f"embedding 鉴权失败（HTTP {exc.status_code}）") from exc
                if exc.status_code == 429:
                    raise UpstreamUnavailableError("embedding 服务限流，请稍后重试") from exc
                if exc.status_code in (404, 405):
                    # **不重试**：服务没有这个端点，重试多少次都是一样的 404。
                    # 典型场景：把 embedding 指向 DeepSeek（它只有 /chat/completions）。
                    # 立刻失败并说清楚，比「重试 3 次后报一句连接失败」有用得多。
                    raise UpstreamUnavailableError(
                        f"该服务不提供 embeddings 端点（HTTP {exc.status_code}）："
                        f"{self._base_url}/embeddings；请改用支持 embeddings 的服务"
                        "（DeepSeek 只有对话补全，向量服务请用 dashscope 或 openai_compatible）",
                        unsupported_endpoint=True,
                        code=ErrCode.EMBEDDING_ENDPOINT_UNSUPPORTED,
                    ) from exc
            except Exception as exc:  # noqa: BLE001 - 统一转为重试或上抛
                last_error = exc
            if attempt == self._max_retries - 1:
                break
            wait = 2**attempt
            logger.warning(
                "embedding 调用失败，%ds 后重试（%d/%d）: %s", wait, attempt + 1, self._max_retries, last_error
            )
            time.sleep(wait)
        raise UpstreamUnavailableError(f"Embedding 接口不可用: {last_error}")

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            batch = texts[i : i + self._batch_size]
            vectors.extend(self._call(batch))
        return [_normalize(np.array(v, dtype=np.float32)).tolist() for v in vectors]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]


class MiniMaxEmbeddings:
    """MiniMax `embo-01`（固定 **1536 维**）。

    ## 为什么单独一个类

    MiniMax 的 embeddings **不是 OpenAI 形状**（实测）：

    - 请求体：`{"model":"embo-01","texts":[...],"type":"db"|"query"}`
      —— 字段是 `texts` 而不是 `input`，且**必须**给 `type`；
    - 响应体：`{"vectors":[[...]],"total_tokens":N,"base_resp":{...}}`
      —— 没有 `data[].embedding`，也没有 `index`（顺序即输入顺序）；
    - 业务错误放在 **HTTP 200** 的 `base_resp.status_code` 里（与 MiniMax 对话接口同源），
      不检查就会把「鉴权失败」当成「向量全零」。

    ## `type` 必须入库与查询一致

    `db`（入库）与 `query`（检索）是**同一模型的两种投影**；混用会让相似度失真，
    而且是静默失真（不报错、召回只是变差）。所以 `embed_texts` 固定 `db`、
    `embed_query` 固定 `query`，不提供「统一用一种」的开关。

    ## 与千问/兼容端点的一致性

    同样经 `bulkhead("embedding")` 隔离、同样指数退避、同样 L2 归一化
    （MiniMax 返回已是单位向量，这里再归一化一次是幂等的，但**不能省**：
    契约写死「入库向量必须 L2 归一化」，不能依赖上游实现细节）。
    """

    #: MiniMax 业务错误码：鉴权/参数类（重试无意义）
    _AUTH_CODES = frozenset({1004, 1008, 2049})
    #: 可重试：限流 / 内部错误
    _RETRY_CODES = frozenset({1000, 1002, 1013, 1024})

    def __init__(
        self,
        api_key: str,
        model: str = "embo-01",
        base_url: str = "https://api.minimax.chat/v1",
        dim: int = 1536,
        max_retries: int = 3,
        batch_size: int = 10,
        timeout_s: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._base_url = (base_url or "https://api.minimax.chat/v1").rstrip("/")
        self.dim = dim
        self._max_retries = max(1, int(max_retries))
        self._batch_size = max(1, int(batch_size))
        self._timeout_s = timeout_s

    def _call(self, texts: list[str], kind: str) -> list[list[float]]:
        # 按需载入：与 OpenAICompatibleEmbeddings 共用同一个轻量 HTTP 客户端
        from app.services.openai_compat import OpenAICompatError, post_json

        payload = {"model": self.model, "texts": texts, "type": kind}
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                with bulkhead("embedding").acquire(timeout_s=self._timeout_s):
                    resp = post_json(
                        f"{self._base_url}/embeddings", payload, self._api_key, timeout_s=self._timeout_s
                    )
                base = resp.get("base_resp") or {}
                code = int(base.get("status_code") or 0)
                if code != 0:
                    # **HTTP 200 也可能是失败**：MiniMax 把业务错误放在响应体里
                    message = str(base.get("status_msg") or "")
                    if code in self._AUTH_CODES:
                        raise UpstreamUnavailableError(
                            f"MiniMax embedding 鉴权/参数错误（{code}）：{message}；"
                            "请检查 MINIMAX_API_KEY 与 MINIMAX_BASE_URL（国内站 .chat / 国际站 .io 凭据不通用）"
                        )
                    raise _MiniMaxEmbeddingError(code, message)
                vectors = [list(v) for v in (resp.get("vectors") or [])]
                if len(vectors) != len(texts):
                    raise UpstreamUnavailableError(
                        f"MiniMax embedding 返回条数不符：期望 {len(texts)}，实际 {len(vectors)}"
                    )
                actual_dim = len(vectors[0]) if vectors else self.dim
                if actual_dim != self.dim:
                    # 维度不符会写坏向量库（Milvus 集合维度固定），必须在入库前拦住
                    raise UpstreamUnavailableError(
                        f"MiniMax embedding 维度与配置不符：模型 {self.model} 返回 {actual_dim} 维，"
                        f"配置 EMBEDDING_DIM={self.dim}；embo-01 固定 1536 维"
                    )
                return vectors
            except (CircuitOpenError, UpstreamUnavailableError):
                # 契约违约（条数/维度不符、鉴权失败、端点不存在）**不重试**：
                # 同一个请求再试三次也是同样的结果，只会把错误信息盖成「接口不可用」
                raise
            except _MiniMaxEmbeddingError as exc:
                last_error = exc
                if exc.code not in self._RETRY_CODES and attempt == self._max_retries - 1:
                    break
            except OpenAICompatError as exc:
                last_error = exc
                if exc.status_code in (401, 403):
                    raise UpstreamUnavailableError(
                        f"MiniMax embedding 鉴权失败（HTTP {exc.status_code}）"
                    ) from exc
                if exc.status_code in (404, 405):
                    raise UpstreamUnavailableError(
                        f"MiniMax 端点不存在（HTTP {exc.status_code}）：{self._base_url}/embeddings；"
                        "确认 MINIMAX_BASE_URL 含 /v1（例：https://api.minimax.chat/v1）",
                        unsupported_endpoint=True,
                        code=ErrCode.EMBEDDING_ENDPOINT_UNSUPPORTED,
                    ) from exc
            except Exception as exc:  # noqa: BLE001 - 统一转为重试或上抛
                last_error = exc
            if attempt == self._max_retries - 1:
                break
            wait = 2**attempt
            logger.warning(
                "MiniMax embedding 调用失败，%ds 后重试（%d/%d）: %s",
                wait, attempt + 1, self._max_retries, last_error,
            )
            time.sleep(wait)
        raise UpstreamUnavailableError(f"MiniMax Embedding 接口不可用: {last_error}")

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            batch = texts[i : i + self._batch_size]
            vectors.extend(self._call(batch, "db"))
        return [_normalize(np.array(v, dtype=np.float32)).tolist() for v in vectors]

    def embed_query(self, text: str) -> list[float]:
        # 查询侧必须是 type=query（与入库的 db 成对，混用会让相似度静默失真）
        return self._normalize_one(self._call([text], "query")[0])

    @staticmethod
    def _normalize_one(vec: list[float]) -> list[float]:
        return _normalize(np.array(vec, dtype=np.float32)).tolist()


class _MiniMaxEmbeddingError(Exception):
    """MiniMax 响应体里的业务错误（`base_resp.status_code != 0`）。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"MiniMax embedding 业务错误 {code}: {message}")
        self.code = code


def build_embeddings(
    provider: str,
    api_key: str,
    model: str,
    dim: int,
    max_retries: int,
    base_url: str = "",
) -> Embeddings:
    """按 provider 构造向量化实现。

    `provider` 取值：`dashscope`（千问 text-embedding-v3）/ `minimax`（`embo-01`，
    非 OpenAI 形状）/ `zhipu`（`embedding-3`，**OpenAI 形状**，2048 维）/
    `openai_compatible`（任何 OpenAI 兼容的 embeddings 服务）/ `mock`。

    **DeepSeek 没有 embeddings 端点**（实测 `/v1/embeddings` → 404），所以这里没有
    `deepseek` 分支：把它指向 `openai_compatible` 只会在首次入库时报 404。

    智谱不需要新类：`/api/paas/v4/embeddings` 的请求体（`input`）与响应
    （`data[].embedding` / `usage`）与 OpenAI 完全一致，直接复用兼容实现。
    """
    if provider == "dashscope":
        return DashScopeEmbeddings(api_key=api_key, model=model, dim=dim, max_retries=max_retries)
    if provider == "minimax":
        return MiniMaxEmbeddings(
            api_key=api_key, model=model, base_url=base_url, dim=dim, max_retries=max_retries
        )
    if provider == "zhipu":
        return OpenAICompatibleEmbeddings(
            api_key=api_key,
            model=model or "embedding-3",
            base_url=base_url or "https://open.bigmodel.cn/api/paas/v4",
            dim=dim,
            max_retries=max_retries,
        )
    if provider == "openai_compatible":
        return OpenAICompatibleEmbeddings(
            api_key=api_key, model=model, base_url=base_url, dim=dim, max_retries=max_retries
        )
    logger.info("使用 MockEmbeddings（未配置 Key 或显式指定 mock）")
    return MockEmbeddings(dim=dim)
