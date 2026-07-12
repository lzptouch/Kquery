"""
Hypothetical Document Embeddings (HyDE) 生成模块

职责：
- 根据用户查询生成假设性文档，用于提升检索效果
- 提供可配置的生成策略和降级处理机制
- 支持异步生成和批量处理

主要功能：
1. 假设性文档生成：
   - generate: 生成单个假设性文档
   - batch_generate: 批量生成假设性文档

2. 配置管理：
   - 支持自定义模型和参数
   - 支持温度、最大长度等参数调整
   - 支持知识类型提示

设计原则：
- 遵循服务层模式，提供懒加载机制
- 完善的错误处理和降级策略
- 详细的性能监控和日志记录
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional, Any

from app.core.llm_client import call_openai_chat, is_llm_configured
from app.core.settings import get_settings
from app.core.logger import get_logger
from openai import AsyncOpenAI

settings = get_settings()

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class HyDEGenerationResult:
    """HyDE 生成结果。

    Attributes:
        hypothetical_doc: 假设性文档内容
        original_query: 原始查询
        generation_time_ms: 生成耗时（毫秒）
        success: 是否成功生成
        error_message: 错误信息（如果失败）
    """
    hypothetical_doc: str
    original_query: str
    generation_time_ms: float
    success: bool
    error_message: Optional[str] = None


@dataclass(slots=True)
class HyDEGeneratorConfig:
    """HyDE 生成器配置。

    Attributes:
        model: 使用的模型名称
        temperature: 生成温度（0.0-1.0）
        max_tokens: 最大生成 token 数
        system_prompt: 系统提示词
        enable_hyde: 是否启用 HyDE 功能
    """
    model: str = field(default_factory=lambda: settings.llm.agent_model_name)
    temperature: float = 0.3
    max_tokens: int = 200
    system_prompt: str = "You are a hypothetical document generator for a knowledge retrieval system. Generate concise, information-rich hypothetical ideal answer documents (within 100 characters) that bridge the semantic gap between user queries and relevant knowledge base documents. Include relevant terminology and concepts naturally."
    enable_hyde: bool = True


class HyDEGenerator:
    """假设性文档生成器。

    使用 LLM 根据用户查询生成假设性文档，用于提升向量检索效果。

    设计特点：
    - 懒加载 OpenAI 客户端
    - 完善的错误处理和降级策略
    - 详细的性能监控和日志记录
    - 支持自定义配置
    """

    def __init__(self, generator_config: Optional[HyDEGeneratorConfig] = None):
        """初始化 HyDE 生成器。

        Args:
            generator_config: 生成器配置（可选，默认使用全局配置）
        """
        self._client: Optional[AsyncOpenAI] = None
        self._config: HyDEGeneratorConfig = generator_config or HyDEGeneratorConfig()

    def _get_client(self) -> AsyncOpenAI:
        """获取或创建 OpenAI 客户端（懒加载模式）。"""
        if self._client is None:
            api_key = settings.llm.agent_api_key or "sk-placeholder"
            base_url = settings.llm.agent_api_base
            timeout = settings.llm.embedding_timeout

            self._client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
            )
            logger.info(f"HyDE Generator: OpenAI client initialized with model {self._config.model}")
        return self._client

    async def generate(
        self,
        query: str,
        knowledge_type: Optional[str] = None,
        max_length: Optional[int] = None,
    ) -> HyDEGenerationResult:
        """生成假设性文档。

        Args:
            query: 用户查询
            knowledge_type: 知识类型（可选）
            max_length: 最大长度（可选，默认使用配置值）

        Returns:
            HyDEGenerationResult 格式的生成结果
        """
        start_time = time.time()

        if not self._config.enable_hyde:
            logger.debug(f"HyDE 功能已禁用，返回原始查询: {query[:50]}...")
            return HyDEGenerationResult(
                hypothetical_doc=query,
                original_query=query,
                generation_time_ms=(time.time() - start_time) * 1000,
                success=True,
            )

        if not query or not query.strip():
            logger.warning("查询为空，返回空字符串")
            return HyDEGenerationResult(
                hypothetical_doc="",
                original_query=query,
                generation_time_ms=(time.time() - start_time) * 1000,
                success=False,
                error_message="Query is empty",
            )

        prompt = self._build_prompt(query, knowledge_type)
        max_tokens = max_length or self._config.max_tokens

        try:
            # 如果 LLM 未配置，使用 Mock 数据
            if not is_llm_configured("agent"):
                logger.warning("Agent API 未配置，HyDE 使用 Mock 数据")
                from app.core.mock_data import get_mock_data
                mock_result = get_mock_data("hyde_document", query=query)
                generation_time_ms = (time.time() - start_time) * 1000
                return HyDEGenerationResult(
                    hypothetical_doc=mock_result.get("hypothetical_doc", query),
                    original_query=query,
                    generation_time_ms=generation_time_ms,
                    success=True,
                )

            hypothetical_doc = await call_openai_chat(
                model=self._config.model,
                messages=[
                    {
                        "role": "system",
                        "content": self._config.system_prompt,
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
                max_tokens=max_tokens,
                temperature=self._config.temperature,
                api_key=settings.llm.agent_api_key,
                base_url=settings.llm.agent_api_base,
                timeout=settings.llm.embedding_timeout,
                mock_module="hyde_document",
            )
            generation_time_ms = (time.time() - start_time) * 1000

            logger.info(
                f"HyDE 生成成功，查询: {query[:50]}..., "
                f"文档长度: {len(hypothetical_doc)}, "
                f"耗时: {generation_time_ms:.2f}ms"
            )

            return HyDEGenerationResult(
                hypothetical_doc=hypothetical_doc,
                original_query=query,
                generation_time_ms=generation_time_ms,
                success=True,
            )
        except Exception as e:
            generation_time_ms = (time.time() - start_time) * 1000
            error_message = f"HyDE 生成失败: {e!r}"
            logger.warning(f"{error_message}，使用原始查询，耗时: {generation_time_ms:.2f}ms")

            return HyDEGenerationResult(
                hypothetical_doc=query,
                original_query=query,
                generation_time_ms=generation_time_ms,
                success=False,
                error_message=error_message,
            )

    async def batch_generate(
        self,
        queries: list[str],
        knowledge_type: Optional[str] = None,
        max_length: Optional[int] = None,
    ) -> list[HyDEGenerationResult]:
        """批量生成假设性文档。

        Args:
            queries: 查询列表
            knowledge_type: 知识类型（可选）
            max_length: 最大长度（可选）

        Returns:
            生成结果列表
        """
        results = []
        for query in queries:
            result = await self.generate(query, knowledge_type, max_length)
            results.append(result)
        return results

    def _build_prompt(self, query: str, knowledge_type: Optional[str]) -> str:
        """构建生成提示词。

        Args:
            query: 用户查询
            knowledge_type: 知识类型（可选）

        Returns:
            构建好的提示词
        """
        type_hint = ""
        if knowledge_type:
            type_hint = f" (Type: {knowledge_type})"

        prompt = f"""Generate a hypothetical ideal answer document based on the following user query{type_hint} for a knowledge retrieval system.

Context:
- Knowledge retrieval system for general purpose information
- Supports various document types and formats
- Emphasizes accuracy, relevance, and comprehensiveness

Requirements:
1. Generate a hypothetical ideal answer document that contains all relevant information the user might be seeking
2. The document should be comprehensive but extremely concise
3. Include relevant terminology and concepts naturally
4. The document should be written as if it were a perfect answer to the user's question
5. Keep the document within 100 characters maximum
6. Focus on bridging the semantic gap between the user's query and relevant knowledge documents
7. Use natural language that would appear in knowledge base articles or FAQs

User query: {query}

Hypothetical ideal answer document:"""

        return prompt

    async def close(self) -> None:
        """关闭资源（清理连接等）。"""
        if self._client is not None:
            await self._client.close()
            self._client = None
            logger.info("HyDE Generator: OpenAI client closed")

    async def __aenter__(self) -> HyDEGenerator:
        """异步上下文管理器入口。"""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """异步上下文管理器出口。"""
        await self.close()