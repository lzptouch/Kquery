"""网页内容抓取服务。

负责抓取网页正文并进行清洗，去除广告、导航等噪声。
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

from app.core.logger import get_logger

logger = get_logger(__name__)


class WebFetcher:
    """网页内容抓取器。

    使用 aiohttp 异步抓取网页，BeautifulSoup 进行正文提取和清洗。
    """

    def __init__(self, timeout: int = 10, max_content_length: int = 5000):
        """初始化抓取器。

        Args:
            timeout: 请求超时（秒）
            max_content_length: 最大正文长度
        """
        self.timeout = timeout
        self.max_content_length = max_content_length
        self.user_agent = (
            "Mozilla/5.0 (compatible; KQuery/1.0; +https://kquery.dev)"
        )

    async def fetch(self, url: str) -> Optional[str]:
        """抓取网页正文。

        Args:
            url: 网页 URL

        Returns:
            清洗后的正文文本，失败返回 None
        """
        try:
            import aiohttp
            from bs4 import BeautifulSoup

            headers = {"User-Agent": self.user_agent}

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, headers=headers, timeout=self.timeout
                ) as resp:
                    if resp.status != 200:
                        logger.debug(f"抓取 {url} 返回状态码 {resp.status}")
                        return None

                    content_type = resp.headers.get("Content-Type", "")
                    if "text/html" not in content_type:
                        return None

                    html = await resp.text()

            soup = BeautifulSoup(html, "html.parser")

            # 移除无用元素
            for tag in soup(["script", "style", "nav", "footer", "header", "iframe"]):
                tag.decompose()

            # 提取正文
            text = self._extract_content(soup)

            # 清洗文本
            text = self._clean_text(text)

            # 截断
            if len(text) > self.max_content_length:
                text = text[: self.max_content_length] + "..."

            return text

        except Exception as e:
            logger.debug(f"抓取 {url} 失败: {e}")
            return None

    def _extract_content(self, soup) -> str:
        """从 BeautifulSoup 提取主要正文。

        优先使用 article/main 标签，其次使用 body。
        """
        # 优先 article / main
        for tag_name in ["article", "main", '[role="main"]']:
            element = soup.select_one(tag_name)
            if element:
                return element.get_text(separator="\n", strip=True)

        # 回退到 body
        body = soup.find("body")
        if body:
            return body.get_text(separator="\n", strip=True)

        return soup.get_text(separator="\n", strip=True)

    def _clean_text(self, text: str) -> str:
        """清洗文本：去多余空白、去短行噪声。"""
        # 合并多余换行
        text = re.sub(r"\n{3,}", "\n\n", text)
        # 移除只含空白字符的行
        lines = [line.strip() for line in text.split("\n")]
        lines = [line for line in lines if len(line) > 3]
        return "\n".join(lines)


class QueryClassifier:
    """查询意图分类器。

    判断用户查询是否需要联网搜索。
    """

    # 需要联网搜索的关键词
    NEED_WEB_KEYWORDS = [
        "最新", "今天", "今日",
        "实时", "当前",
        "新闻", "快讯",
        "股价", "股票", "行情",
        "天气", "汇率",
        "2025", "2026", "2027",
        "最近", "近期", "近日",
        "刚刚", "刚才",
        "热搜", "热点",
    ]

    # 不需要联网的关键词（优先本地知识库）
    LOCAL_KEYWORDS = [
        "知识库", "文档",
        "公司内部", "内部资料",
        "根据文档", "根据知识库",
        "合同", "报销", "流程",
        "规章制度",
    ]

    @classmethod
    def needs_web_search(cls, query: str) -> bool:
        """规则判断是否需要联网搜索。

        Args:
            query: 用户查询

        Returns:
            是否需要联网搜索
        """
        # 本地关键词优先
        for kw in cls.LOCAL_KEYWORDS:
            if kw in query:
                return False

        # 联网关键词触发
        for kw in cls.NEED_WEB_KEYWORDS:
            if kw in query:
                return True

        return False

    @classmethod
    async def llm_classify(cls, query: str) -> bool:
        """使用 LLM 判断是否需要联网搜索。

        Args:
            query: 用户查询

        Returns:
            是否需要联网搜索
        """
        try:
            from app.core.llm_client import call_openai_chat
            from app.core.llm.router import resolve_provider_config

            config = resolve_provider_config()

            prompt = f"""请判断以下用户问题是否需要联网搜索最新信息才能准确回答。

判断标准：
- 需要联网：涉及实时数据、最新新闻、当前事件、股票价格、天气等时效性强的信息
- 不需要联网：常识问答、知识库内问题、编程帮助、通用知识等

用户问题：{query}

请只回答 "true" 或 "false"。"""

            result = await call_openai_chat(
                model=config["model"],
                messages=[{"role": "user", "content": prompt}],
                api_key=config["api_key"],
                base_url=config["base_url"],
                temperature=0,
                max_tokens=10,
            )

            return "true" in result.strip().lower()

        except Exception as e:
            logger.warning(f"LLM 分类失败: {e}，回退到规则判断")
            return cls.needs_web_search(query)
