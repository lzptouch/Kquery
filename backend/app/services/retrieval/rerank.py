"""重排。

- 生产实现：千问平台 gte-rerank-v2（交叉编码器的托管版本，只走接口不部署）；
- 测试实现：MockReranker，按 query 与候选的词重叠度打分（0~1 确定性）；
- 重排是增强能力：接口超时/限流时必须降级为"跳过重排直接用融合结果"，
  绝不能让重排失败阻断整次检索。
"""
import time
from typing import Protocol

from app.services.retrieval.base import FusedCandidate
from app.core.envelope import ErrCode
from app.services.upstream import CircuitOpenError, bulkhead
from app.core.logging_setup import get_logger

logger = get_logger(__name__)


class RerankError(Exception):
    """重排接口不可用（限流/超时/配额），由上层降级处理。

    `code`：**服务没有这个端点**（404/405）时给专用错误码——
    这类故障重试无意义（同一个 URL 再试还是 404），必须立刻说清楚。
    """

    def __init__(self, message: str, *, code: int = 0) -> None:
        super().__init__(message)
        self.code = code


class Reranker(Protocol):
    model: str

    def rerank(
        self,
        query: str,
        candidates: list[FusedCandidate],
        top_n: int,
        model: str | None = None,
    ) -> list[FusedCandidate]:
        """为候选填充 rerank_score 并返回按相关度排序的前 top_n 个。

        `model` 为运行时热配置的重排模型名；为空时用实例默认模型。
        """
        ...


class DashScopeReranker:
    """千问 gte-rerank-v2。候选数 50 个耗时百毫秒量级，单次问答只调用一次。"""

    def __init__(self, api_key: str, model: str = "gte-rerank-v2", max_retries: int = 3) -> None:
        self._api_key = api_key
        self.model = model
        self._max_retries = max_retries

    def rerank(
        self,
        query: str,
        candidates: list[FusedCandidate],
        top_n: int,
        model: str | None = None,
    ) -> list[FusedCandidate]:
        if not candidates:
            return []
        import dashscope

        # 运行时热配置优先：此前 rerank_model 只影响缓存键，实际调用模型不变
        effective_model = model or self.model
        if effective_model != self.model:
            logger.info("重排模型热切换：%s → %s", self.model, effective_model)
        documents = [c.chunk.content for c in candidates]
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                # 批次 2：上游隔离仓。熔断打开抛 RerankError——hybrid 已把它降级为融合序
                with bulkhead("rerank").acquire(timeout_s=30.0):
                    resp = dashscope.TextReRank.call(
                        model=effective_model,
                        query=query,
                        documents=documents,
                        top_n=min(top_n, len(documents)),
                        return_documents=False,
                        api_key=self._api_key,
                    )
                if resp.status_code != 200:
                    raise RerankError(f"rerank API {resp.status_code}: {resp.message}")
                ranked: list[FusedCandidate] = []
                for item in resp.output["results"]:
                    cand = candidates[item["index"]]
                    cand.rerank_score = float(item["relevance_score"])
                    ranked.append(cand)
                return ranked
            except RerankError:
                raise
            except CircuitOpenError as exc:
                raise RerankError(f"重排上游熔断（{exc.upstream}），本次未精排") from exc
            except Exception as exc:  # noqa: BLE001 - 重排调用失败，%ds 后重试
                last_error = exc
                if attempt == self._max_retries - 1:
                    break
                wait = 2**attempt
                logger.warning("重排调用失败，%ds 后重试（%d/%d）: %s", wait, attempt + 1, self._max_retries, exc)
                time.sleep(wait)
        raise RerankError(f"重排接口不可用: {last_error}")


class MockReranker:
    """词重叠度打分（过滤虚词后）：|Q′∩D| / |Q′|，确定性输出。

    只统计实词（长度 ≥2 的词/二元组与英文数字词），否则"的/是/多少"等
    高频虚词会摊薄相关文档的得分、抬高无关文档的得分。
    """

    model = "mock-rerank"

    _STOP = {"的", "是", "吗", "呢", "了", "什么", "怎么", "怎样", "如何", "哪些", "哪个", "多少", "几", "是否", "这份", "请问"}

    def _content_tokens(self, text: str) -> set[str]:
        """jieba 实词（单字虚词与噪声二元组会摊薄相关度，弃用）。"""
        # 按需载入：仅在特定路径用到
        from app.services.retrieval.bm25 import tokenize

        return {
            t
            for t in tokenize(text)
            if (len(t) >= 2 or t.isascii()) and t not in self._STOP and t.isalnum()
        }

    def rerank(
        self,
        query: str,
        candidates: list[FusedCandidate],
        top_n: int,
        model: str | None = None,
    ) -> list[FusedCandidate]:
        # Mock 不区分模型名（词重叠度打分是纯本地计算）
        q_tokens = self._content_tokens(query)
        for cand in candidates:
            d_tokens = self._content_tokens(cand.chunk.content)
            cand.rerank_score = round(len(q_tokens & d_tokens) / max(len(q_tokens), 1), 4)
        ranked = sorted(candidates, key=lambda c: c.rerank_score or 0.0, reverse=True)
        return ranked[:top_n]


class OpenAICompatibleReranker:
    """OpenAI / Jina / Cohere 风格的 `/rerank` 端点。

    请求体 `{"model","query","documents","top_n"}`，响应
    `{"results":[{"index":..,"relevance_score":..}]}`——
    硅基流动 / Jina / Cohere 兼容层都是这个形状。

    **DeepSeek 不在此列**：DeepSeek 官方 API 没有 rerank 端点（实测 `/v1/rerank` → 404）。
    想用 DeepSeek 做重排请用 `provider="deepseek"`（走对话模型的 listwise 重排，见 `LLMReranker`）。
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        max_retries: int = 3,
        timeout_s: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._max_retries = max(1, int(max_retries))
        self._timeout_s = timeout_s

    def rerank(
        self,
        query: str,
        candidates: list[FusedCandidate],
        top_n: int,
        model: str | None = None,
    ) -> list[FusedCandidate]:
        if not candidates:
            return []
        # 按需载入：兼容端点客户端只在真正调用时才需要（千问路径不必付导入成本）
        from app.services.openai_compat import OpenAICompatError, post_json

        effective_model = model or self.model
        if effective_model != self.model:
            logger.info("重排模型热切换：%s → %s", self.model, effective_model)
        documents = [c.chunk.content for c in candidates]
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                with bulkhead("rerank").acquire(timeout_s=self._timeout_s):
                    resp = post_json(
                        f"{self._base_url}/rerank",
                        {
                            "model": effective_model,
                            "query": query,
                            "documents": documents,
                            "top_n": min(top_n, len(documents)),
                        },
                        self._api_key,
                        timeout_s=self._timeout_s,
                    )
                results = resp.get("results") or []
                ranked: list[FusedCandidate] = []
                for item in results:
                    idx = int(item.get("index", 0))
                    if not (0 <= idx < len(candidates)):
                        continue
                    cand = candidates[idx]
                    cand.rerank_score = float(item.get("relevance_score", 0.0))
                    ranked.append(cand)
                return ranked
            except CircuitOpenError as exc:
                raise RerankError(f"重排上游熔断（{exc.upstream}），本次未精排") from exc
            except OpenAICompatError as exc:
                last_error = exc
                if exc.status_code in (401, 403):
                    raise RerankError(f"重排鉴权失败（HTTP {exc.status_code}）") from exc
                if exc.status_code in (404, 405):
                    # **不重试**：服务没有这个端点，重试多少次都是一样的 404。
                    # 典型场景：把 rerank 指向 DeepSeek（它只有 /chat/completions）。
                    raise RerankError(
                        f"该服务不提供 rerank 端点（HTTP {exc.status_code}）："
                        f"{self._base_url}/rerank；请改用支持重排的服务"
                        "（DeepSeek 只有对话补全，重排请用 dashscope 或 openai_compatible）",
                        code=int(ErrCode.RERANK_ENDPOINT_UNSUPPORTED),
                    ) from exc
            except Exception as exc:  # noqa: BLE001 - 重排调用失败，%ds 后重试
                last_error = exc
            if attempt == self._max_retries - 1:
                break
            wait = 2**attempt
            logger.warning(
                "重排调用失败，%ds 后重试（%d/%d）: %s", wait, attempt + 1, self._max_retries, last_error
            )
            time.sleep(wait)
        raise RerankError(f"重排接口不可用: {last_error}")


class LLMReranker:
    """**用对话模型做 listwise 重排**（DeepSeek / 任何 OpenAI 兼容 chat 端点）。

    ## 为什么需要这一条通道

    DeepSeek 官方 API **没有** `/rerank` 端点（实测 `/v1/rerank` → 404），
    但它的对话模型能读一批候选并给出顺序——重排本来就是个排序任务。
    于是「重排走 DeepSeek」= 走 `{base}/chat/completions`，而不是 `/rerank`。

    代价（必须写清楚，否则会被当成免费能力）：

    - **慢**：一次问答多一次 LLM 往返（实测 20 候选 ≈ 0.7s，端到端占比不低）；
    - **贵**：候选数 × 每候选字数都进 prompt，所以要截断（`max_docs` / `doc_chars`）；
    - **不确定**：输出是自由文本，必须容错解析——解析不出来就**降级为融合序**
      （抛 `RerankError`，由 `hybrid` 兜底），绝不能让重排失败吞掉整次检索。

    ## 分数怎么给

    listwise 只给顺序、不给分数。这里把顺序映射成**单调递减的伪分数**
    （第 1 名 1.0，第 n 名 1/n）并写进 `rerank_score`：下游只依赖
    「rerank_score 用于排序与展示」，不依赖它是交叉编码器的真实相关度。
    未出现在输出里的候选保留在尾部（伪分数 0），避免模型漏写导致候选丢失。
    """

    #: 单次送进 prompt 的候选上限（超出部分不参与精排，按融合序排在后面）
    DEFAULT_MAX_DOCS = 20
    #: 每个候选截断的字数（重排只需判断相关性，不需要全文）
    DEFAULT_DOC_CHARS = 300

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-chat",
        base_url: str = "https://api.deepseek.com",
        max_retries: int = 3,
        timeout_s: float = 30.0,
        max_docs: int = DEFAULT_MAX_DOCS,
        doc_chars: int = DEFAULT_DOC_CHARS,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._base_url = (base_url or "https://api.deepseek.com").rstrip("/")
        self._max_retries = max(1, int(max_retries))
        self._timeout_s = timeout_s
        self._max_docs = max(1, int(max_docs))
        self._doc_chars = max(50, int(doc_chars))

    def _effective_model(self, model: str | None) -> str:
        """运行时热配置优先，但**不接千问重排模型名**。

        `RetrievalConfig.rerank_model` 的历史默认是 `gte-rerank-v2`；把它原样发给
        DeepSeek 会得到 `400 unknown model`，而且看起来像 key 的问题。
        """
        candidate = str(model or "").strip()
        if not candidate or candidate.startswith("gte-"):
            if candidate:
                logger.info("重排模型 %s 是千问重排模型，LLM 重排改用 %s", candidate, self.model)
            return self.model
        return candidate

    def _build_prompt(self, query: str, documents: list[str]) -> list[dict]:
        lines = [
            "你是检索重排器。根据用户查询，把候选按相关度从高到低排序。",
            "只输出一个 JSON 数组，元素是候选编号（整数），不要输出解释、不要输出候选原文。",
            "无法判断相关性的候选也要列出，排在后面。",
        ]
        body = "\n".join(
            f"[{i}] {doc[: self._doc_chars]}".replace("\n", " ")
            for i, doc in enumerate(documents)
        )
        return [
            {"role": "system", "content": "\n".join(lines)},
            {"role": "user", "content": f"查询：{query}\n\n候选：\n{body}"},
        ]

    @staticmethod
    def _parse_order(content: str, n: int) -> list[int]:
        """从模型输出里抠出候选顺序（容忍代码块 / 多余文字 / 越界编号）。"""
        import json

        text = content or ""
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end <= start:
            raise RerankError(f"重排输出无法解析（未找到 JSON 数组）：{text[:120]}")
        try:
            raw = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise RerankError(f"重排输出不是合法 JSON：{text[start:end + 1][:120]}") from exc
        order: list[int] = []
        for item in raw if isinstance(raw, list) else []:
            try:
                idx = int(item)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < n and idx not in order:
                order.append(idx)
        if not order:
            raise RerankError(f"重排输出没有有效编号：{text[start:end + 1][:120]}")
        # 模型漏写的候选按原序补到尾部（不能因为漏写就把候选丢掉）
        order.extend(i for i in range(n) if i not in order)
        return order

    def rerank(
        self,
        query: str,
        candidates: list[FusedCandidate],
        top_n: int,
        model: str | None = None,
    ) -> list[FusedCandidate]:
        if not candidates:
            return []
        # 按需载入：兼容端点客户端只在真正调用时才需要
        from app.services.openai_compat import OpenAICompatError, post_json

        effective_model = self._effective_model(model)
        head = candidates[: self._max_docs]
        tail = candidates[self._max_docs :]
        documents = [c.chunk.content for c in head]
        payload = {
            "model": effective_model,
            "messages": self._build_prompt(query, documents),
            "temperature": 0,
            "max_tokens": 120,
        }
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                with bulkhead("rerank").acquire(timeout_s=self._timeout_s):
                    resp = post_json(
                        f"{self._base_url}/chat/completions",
                        payload,
                        self._api_key,
                        timeout_s=self._timeout_s,
                    )
                choices = resp.get("choices") or []
                if not choices:
                    raise RerankError("重排响应没有 choices")
                content = str((choices[0].get("message") or {}).get("content") or "")
                order = self._parse_order(content, len(head))
                n = len(order)
                ranked: list[FusedCandidate] = []
                for position, idx in enumerate(order):
                    cand = head[idx]
                    # 单调递减伪分数：第 1 名 1.0，第 n 名 1/n
                    cand.rerank_score = round(1.0 - position / max(n, 1), 6)
                    ranked.append(cand)
                # 未进 prompt 的候选保留在尾部（伪分数 0），不参与精排
                for cand in tail:
                    cand.rerank_score = 0.0
                    ranked.append(cand)
                return ranked[:top_n]
            except CircuitOpenError as exc:
                raise RerankError(f"重排上游熔断（{exc.upstream}），本次未精排") from exc
            except OpenAICompatError as exc:
                last_error = exc
                if exc.status_code in (401, 403):
                    raise RerankError(f"重排鉴权失败（HTTP {exc.status_code}）") from exc
                if exc.status_code in (404, 405):
                    raise RerankError(
                        f"对话端点不存在（HTTP {exc.status_code}）：{self._base_url}/chat/completions；"
                        "确认 DEEPSEEK_BASE_URL 只写到域名（例：https://api.deepseek.com）",
                        code=int(ErrCode.RERANK_ENDPOINT_UNSUPPORTED),
                    ) from exc
                if exc.status_code in (400, 422):
                    # 模型名/参数错重试无意义
                    raise RerankError(f"重排请求被拒绝（HTTP {exc.status_code}）：{exc.message}") from exc
            except RerankError:
                raise
            except Exception as exc:  # noqa: BLE001 - 重排调用失败，%ds 后重试
                last_error = exc
            if attempt == self._max_retries - 1:
                break
            wait = 2**attempt
            logger.warning(
                "LLM 重排调用失败，%ds 后重试（%d/%d）: %s",
                wait, attempt + 1, self._max_retries, last_error,
            )
            time.sleep(wait)
        raise RerankError(f"LLM 重排接口不可用: {last_error}")


def build_reranker(
    provider: str,
    api_key: str,
    model: str,
    max_retries: int,
    base_url: str = "",
) -> Reranker:
    """按 provider 构造重排实现。

    `provider` 取值：`dashscope`（gte-rerank-v2）/ `deepseek`（**对话模型做 listwise
    重排**，走 `/chat/completions`）/ `zhipu`（`/rerank` 端点）/ `openai_compatible`
    （任何提供 `/rerank` 的服务）/ `mock`。

    `deepseek` 与 `openai_compatible` 的区别：前者是「用对话模型排序」，
    后者要求服务**真的提供 `/rerank` 端点**（DeepSeek 没有，实测 404）。
    智谱属于后者（`POST /api/paas/v4/rerank`，响应 `results[].relevance_score`），
    因此复用兼容实现、零额外成本——比 DeepSeek 的 listwise 重排省一次 LLM 往返。
    """
    if provider == "dashscope":
        return DashScopeReranker(api_key=api_key, model=model, max_retries=max_retries)
    if provider == "deepseek":
        return LLMReranker(
            api_key=api_key, model=model, base_url=base_url, max_retries=max_retries
        )
    if provider == "zhipu":
        return OpenAICompatibleReranker(
            api_key=api_key,
            model=model or "rerank",
            base_url=base_url or "https://open.bigmodel.cn/api/paas/v4",
            max_retries=max_retries,
        )
    if provider == "openai_compatible":
        return OpenAICompatibleReranker(
            api_key=api_key, model=model, base_url=base_url, max_retries=max_retries
        )
    logger.info("使用 MockReranker（未配置 Key 或显式指定 mock）")
    return MockReranker()
