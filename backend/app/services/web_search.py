"""联网搜索服务。

支持多种搜索引擎 Provider，实现查询分类、网页抓取和结果缓存。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

from app.core.logger import get_logger

logger = get_logger(__name__)


@dataclass
class WebSearchResult:
    """网页搜索结果。"""
    title: str
    url: str
    snippet: str
    content: str = ""
    source: str = "web"
    published_at: str = ""
    relevance_score: float = 0.0


class WebSearchProvider(ABC):
    """搜索引擎 Provider 抽象基类。"""

    @abstractmethod
    async def search(self, query: str, top_n: int = 5) -> List[WebSearchResult]:
        """执行搜索。

        Args:
            query: 搜索查询
            top_n: 返回结果数

        Returns:
            搜索结果列表
        """
        ...


class DuckDuckGoSearchProvider(WebSearchProvider):
    """DuckDuckGo 搜索 Provider（免费，无需 API Key）。

    适合开发测试使用，生产环境建议使用 Tavily 或 Bing。
    """

    async def search(self, query: str, top_n: int = 5) -> List[WebSearchResult]:
        """使用 DuckDuckGo 搜索。"""
        try:
            from duckduckgo_search import DDGS

            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(
                None,
                lambda: list(DDGS().text(query, max_results=top_n)),
            )

            return [
                WebSearchResult(
                    title=r.get("title", ""),
                    url=r.get("href", ""),
                    snippet=r.get("body", ""),
                    source="duckduckgo",
                )
                for r in results
            ]
        except ImportError:
            logger.warning("duckduckgo_search 未安装，请 pip install duckduckgo-search")
            return await self._fallback_search(query, top_n)
        except Exception as e:
            logger.error(f"DuckDuckGo 搜索失败: {e}")
            return []

    async def _fallback_search(self, query: str, top_n: int) -> List[WebSearchResult]:
        """无 SDK 时的 HTTP 降级搜索。"""
        import urllib.parse

        import aiohttp

        try:
            url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
            headers = {"User-Agent": "Mozilla/5.0 (compatible; KQuery/1.0)"}

            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=10) as resp:
                    if resp.status != 200:
                        return []
                    html = await resp.text()

            # 简单 HTML 解析提取结果
            from html.parser import HTMLParser

            class DuckHTMLParser(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.results = []
                    self.current = {}
                    self.in_result = False
                    self.in_link = False
                    self.in_snippet = False

                def handle_starttag(self, tag, attrs):
                    attrs_dict = dict(attrs)
                    if tag == "a" and "result__a" in attrs_dict.get("class", ""):
                        self.in_link = True
                        self.current = {"title": "", "url": attrs_dict.get("href", ""), "snippet": ""}
                    elif tag == "a" and "result__snippet" in attrs_dict.get("class", ""):
                        self.in_snippet = True

                def handle_data(self, data):
                    if self.in_link:
                        self.current["title"] += data
                    elif self.in_snippet:
                        self.current["snippet"] += data

                def handle_endtag(self, tag):
                    if tag == "a" and self.in_link:
                        self.in_link = False
                    elif tag == "a" and self.in_snippet:
                        self.in_snippet = False
                        if self.current.get("title"):
                            self.results.append(self.current.copy())
                        self.current = {}

            parser = DuckHTMLParser()
            parser.feed(html)

            return [
                WebSearchResult(
                    title=r.get("title", ""),
                    url=r.get("url", ""),
                    snippet=r.get("snippet", ""),
                    source="duckduckgo",
                )
                for r in parser.results[:top_n]
            ]
        except Exception as e:
            logger.error(f"DuckDuckGo HTTP 搜索失败: {e}")
            return []


class TavilySearchProvider(WebSearchProvider):
    """Tavily 搜索 Provider（推荐，高质量摘要+URL）。"""

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def search(self, query: str, top_n: int = 5) -> List[WebSearchResult]:
        """使用 Tavily API 搜索。"""
        try:
            from tavily import TavilyClient

            client = TavilyClient(api_key=self.api_key)
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: client.search(query, max_results=top_n, search_depth="basic"),
            )

            results = response.get("results", [])
            return [
                WebSearchResult(
                    title=r.get("title", ""),
                    url=r.get("url", ""),
                    snippet=r.get("content", ""),
                    source="tavily",
                    relevance_score=r.get("score", 0.0),
                )
                for r in results
            ]
        except ImportError:
            logger.error("tavily-python 未安装，请 pip install tavily-python")
            raise
        except Exception as e:
            logger.error(f"Tavily 搜索失败: {e}")
            return []


class WebSearchService:
    """联网搜索服务门面。

    统一管理多个搜索 Provider，支持自动切换和结果缓存。

    使用方式：
        service = WebSearchService()
        results = await service.search("2026 年 AI 最新进展")
    """

    def __init__(self):
        from app.core.settings import get_settings

        settings = get_settings()
        provider_name = getattr(settings, "web_search_provider", "duckduckgo")

        self._providers: dict[str, WebSearchProvider] = {}
        self._cache: dict[str, tuple[float, List[WebSearchResult]]] = {}
        self._cache_ttl = 300  # 缓存 5 分钟

        # 注册 DuckDuckGo（免费，默认）
        self._providers["duckduckgo"] = DuckDuckGoSearchProvider()

        # 注册 Tavily（如有 API Key）
        tavily_key = getattr(settings, "web_search_api_key", None)
        if tavily_key:
            self._providers["tavily"] = TavilySearchProvider(api_key=tavily_key)

        self._default_provider = provider_name

    async def search(
        self,
        query: str,
        top_n: int = 5,
        provider: Optional[str] = None,
        fetch_content: bool = False,
    ) -> List[WebSearchResult]:
        """执行联网搜索。

        Args:
            query: 搜索查询
            top_n: 返回结果数
            provider: 使用的搜索引擎
            fetch_content: 是否抓取网页正文

        Returns:
            搜索结果列表
        """
        # 检查缓存
        cache_key = hashlib.md5(f"{query}:{top_n}:{provider}".encode()).hexdigest()
        if cache_key in self._cache:
            timestamp, cached_results = self._cache[cache_key]
            if time.time() - timestamp < self._cache_ttl:
                logger.info(f"使用缓存搜索结果: {query[:50]}...")
                return cached_results

        provider_name = provider or self._default_provider
        search_provider = self._providers.get(provider_name)

        if search_provider is None:
            logger.warning(f"搜索 Provider '{provider_name}' 不可用，回退到 DuckDuckGo")
            search_provider = self._providers.get("duckduckgo")

        if search_provider is None:
            logger.error("没有可用的搜索 Provider")
            return []

        results = await search_provider.search(query, top_n)

        # 可选：抓取网页正文
        if fetch_content and results:
            results = await self._fetch_contents(results)

        # 缓存结果
        self._cache[cache_key] = (time.time(), results)

        return results

    async def _fetch_contents(
        self, results: List[WebSearchResult]
    ) -> List[WebSearchResult]:
        """并发抓取网页正文。"""
        import aiohttp

        async def fetch_one(result: WebSearchResult) -> WebSearchResult:
            try:
                from app.services.web_fetcher import WebFetcher

                fetcher = WebFetcher()
                content = await fetcher.fetch(result.url)
                if content:
                    result.content = content[:2000]  # 限制长度
            except Exception as e:
                logger.debug(f"抓取 {result.url} 失败: {e}")
            return result

        tasks = [fetch_one(r) for r in results]
        return await asyncio.gather(*tasks)

    def clear_cache(self) -> None:
        """清空搜索缓存。"""
        self._cache.clear()


# 全局单例
_search_service: Optional[WebSearchService] = None


def get_web_search_service() -> WebSearchService:
    """获取全局联网搜索服务单例。"""
    global _search_service
    if _search_service is None:
        _search_service = WebSearchService()
    return _search_service
