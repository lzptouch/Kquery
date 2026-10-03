"""重排（阶段 2 README §3.4）。

- 生产实现：千问平台 gte-rerank-v2（交叉编码器的托管版本，只走接口不部署）；
- 测试实现：MockReranker，按 query 与候选的词重叠度打分（0~1 确定性）；
- 重排是增强能力：接口超时/限流时必须降级为"跳过重排直接用融合结果"，
  绝不能让重排失败阻断整次检索。
"""
import logging
import time
from typing import Protocol

from app.services.embeddings import _tokenize
from app.services.retrieval.base import FusedCandidate

logger = logging.getLogger(__name__)


class RerankError(Exception):
    """重排接口不可用（限流/超时/配额），由上层降级处理。"""


class Reranker(Protocol):
    model: str

    def rerank(self, query: str, candidates: list[FusedCandidate], top_n: int) -> list[FusedCandidate]:
        """为候选填充 rerank_score 并返回按相关度排序的前 top_n 个。"""
        ...


class DashScopeReranker:
    """千问 gte-rerank-v2。候选数 50 个耗时百毫秒量级，单次问答只调用一次。"""

    def __init__(self, api_key: str, model: str = "gte-rerank-v2", max_retries: int = 3):
        self._api_key = api_key
        self.model = model
        self._max_retries = max_retries

    def rerank(self, query: str, candidates: list[FusedCandidate], top_n: int) -> list[FusedCandidate]:
        if not candidates:
            return []
        import dashscope

        from app.services.security.platform_quota import get_monitor

        documents = [c.chunk.content for c in candidates]
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                get_monitor().record_call(self.model)
                resp = dashscope.TextReRank.call(
                    model=self.model,
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
            except Exception as exc:  # noqa: BLE001
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
        from app.services.retrieval.bm25 import tokenize

        return {
            t
            for t in tokenize(text)
            if (len(t) >= 2 or t.isascii()) and t not in self._STOP and t.isalnum()
        }

    def rerank(self, query: str, candidates: list[FusedCandidate], top_n: int) -> list[FusedCandidate]:
        q_tokens = self._content_tokens(query)
        for cand in candidates:
            d_tokens = self._content_tokens(cand.chunk.content)
            cand.rerank_score = round(len(q_tokens & d_tokens) / max(len(q_tokens), 1), 4)
        ranked = sorted(candidates, key=lambda c: c.rerank_score or 0.0, reverse=True)
        return ranked[:top_n]


def build_reranker(provider: str, api_key: str, model: str, max_retries: int) -> Reranker:
    if provider == "dashscope":
        return DashScopeReranker(api_key=api_key, model=model, max_retries=max_retries)
    logger.info("使用 MockReranker（未配置千问 Key 或显式指定 mock）")
    return MockReranker()
