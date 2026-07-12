"""嵌入服务模块。

使用 openai 原生客户端生成 embedding（兼容 OpenAI 风格端点）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, List

from app.core.llm_client import call_openai_embedding, is_llm_configured
from app.core.settings import get_settings
from app.core.logger import get_logger

settings = get_settings()

logger = get_logger(__name__)


@dataclass(slots=True)
class OpenAIEmbeddingService:
    """使用 openai 原生客户端生成 embedding（兼容 OpenAI 风格端点）。"""

    _client: Any | None = field(default=None, init=False, repr=False)

    def _get_client(self):
        """获取或创建 AsyncOpenAI 客户端（懒加载模式）。"""
        if self._client is None:
            from openai import AsyncOpenAI

            api_base = settings.llm.embedding_api_base
            api_key = settings.llm.embedding_api_key

            # 处理空 API key
            safe_key = "sk-placeholder" if (api_key is None or str(api_key).strip() in {"", "None"}) else api_key

            self._client = AsyncOpenAI(
                api_key=safe_key,
                base_url=api_base,
                timeout=settings.llm.embedding_timeout,
            )
        return self._client

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """异步批量生成 embedding 向量。"""
        # 过滤空字符串
        filtered_texts = [t for t in texts if t and isinstance(t, str) and t.strip()]
        if not filtered_texts:
            logger.warning("输入文本列表为空或全部为空字符串")
            return []

        # 如果 LLM 未配置，使用 Mock 数据
        if not is_llm_configured("embedding"):
            logger.warning("嵌入 API 未配置，使用 Mock 数据")
            from app.core.mock_data import get_mock_data
            return get_mock_data("embeddings", texts=filtered_texts)

        try:
            return await call_openai_embedding(
                texts=filtered_texts,
                model=settings.llm.embedding_model_name,
                api_key=settings.llm.embedding_api_key,
                base_url=settings.llm.embedding_api_base,
                timeout=settings.llm.embedding_timeout,
                mock_module="embeddings",
            )
        except Exception as e:
            logger.error(f"嵌入生成失败，使用 Mock 数据: {e!r}")
            from app.core.mock_data import get_mock_data
            return get_mock_data("embeddings", texts=filtered_texts)

    async def embed_parallel(
        self,
        texts: list[str],
        batch_size: int = 10,
        max_concurrency: int = 5,
    ) -> list[list[float]]:
        """
        并行批量生成 embedding 向量，提升大批量文本的嵌入生成速度。

        Args:
            texts: 待生成嵌入的文本列表
            batch_size: 每批处理的文本数量（默认 10）
            max_concurrency: 最大并发批次数（默认 5）

        Returns:
            嵌入向量列表，顺序与输入文本一致
        """
        if not texts:
            return []

        # 过滤并保留原始索引
        indexed_texts = [(i, t) for i, t in enumerate(texts) if t and isinstance(t, str) and t.strip()]
        if not indexed_texts:
            logger.warning("输入文本列表为空或全部为空字符串")
            return []

        # 如果文本数量较少，直接批量处理
        if len(indexed_texts) <= batch_size:
            original_indices, clean_texts = zip(*indexed_texts)
            vectors = await self.embed(list(clean_texts))
            # 构建结果映射
            result_map = dict(zip(original_indices, vectors))
            # 按原始顺序返回，缺失的用零向量填充
            dim = len(vectors[0]) if vectors else settings.llm.embedding_dimension
            return [result_map.get(i, [0.0] * dim) for i in range(len(texts))]

        # 分批次处理
        batches = [indexed_texts[i:i + batch_size] for i in range(0, len(indexed_texts), batch_size)]

        semaphore = asyncio.Semaphore(max_concurrency)

        async def process_batch(batch, batch_index: int):
            async with semaphore:
                logger.debug(f"开始处理批次 {batch_index + 1}/{len(batches)}，文本数量: {len(batch)}")
                indices, clean_texts = zip(*batch)
                vectors = await self.embed(list(clean_texts))
                logger.debug(f"完成批次 {batch_index + 1}/{len(batches)}")
                return list(zip(indices, vectors))

        tasks = [process_batch(batch, i) for i, batch in enumerate(batches)]
        results = await asyncio.gather(*tasks)

        # 合并结果
        result_map = {}
        for batch_results in results:
            for idx, vec in batch_results:
                result_map[idx] = vec

        dim = len(next(iter(result_map.values()))) if result_map else settings.llm.embedding_dimension
        all_vectors = [result_map.get(i, [0.0] * dim) for i in range(len(texts))]

        logger.info(f"并行嵌入生成完成：总文本数 {len(texts)}，有效文本数 {len(indexed_texts)}，批次数 {len(batches)}")
        return all_vectors
