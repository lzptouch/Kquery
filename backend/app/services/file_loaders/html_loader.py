"""HTML 文件加载器

支持格式：.html, .htm

功能特点：
- 支持 HTML 文件的文本提取
- 保留文档结构（标题、段落、列表等）
- 支持表格和链接提取
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, List, Dict, Any

from app.services.file_loaders.base import (
    BaseLoader, DocumentChunk, ChunkType, FileLoadError,
    link_document_chunks
)
from app.core.logger import get_logger

logger = get_logger(__name__)


try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False
    logger.warning("BeautifulSoup library not available. HTML files will not be processed.")


class HTMLLoader(BaseLoader):
    """HTML 文件加载器（兼容 BaseLoader）"""

    def __init__(self, use_ocr: bool = False, preserve_structure: bool = True,
                 extract_links: bool = True, extract_tables: bool = True):
        super().__init__(use_ocr=use_ocr, preserve_structure=preserve_structure)
        self.extract_links = extract_links
        self.extract_tables = extract_tables

    @property
    def supported_extensions(self) -> list[str]:
        return ['.html', '.htm']

    def load(self, file_path: Path) -> str:
        """加载 HTML 文件为纯文本。"""
        self._validate_file(file_path)

        if not BS4_AVAILABLE:
            raise FileLoadError("BeautifulSoup library is required for HTML file processing", file_path)

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                html_content = f.read()

            return self._parse_html_text(html_content)

        except Exception as e:
            raise FileLoadError(f"Failed to load HTML: {e}", file_path) from e

    def load_stream(self, file_path: Path) -> Iterator[str]:
        """流式加载 HTML 文件。"""
        content = self.load(file_path)
        for line in content.split('\n'):
            if line.strip():
                yield line.strip()

    def load_document_chunks(self, file_path: Path) -> list[DocumentChunk]:
        """加载 HTML 文件为结构化文档块。"""
        self._validate_file(file_path)

        if not BS4_AVAILABLE:
            raise FileLoadError("BeautifulSoup library is required for HTML file processing", file_path)

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                html_content = f.read()

            soup = BeautifulSoup(html_content, 'html.parser')
            chunks = []

            # 提取标题作为 heading chunks
            for i in range(1, 7):
                headers = soup.find_all(f'h{i}')
                for header in headers:
                    text = header.get_text().strip()
                    if text:
                        chunks.append(DocumentChunk(
                            content=text,
                            metadata=self.metadata.copy(),
                            chunk_type=ChunkType.HEADING,
                            source_file=str(file_path),
                            heading_level=i
                        ))

            # 提取段落作为 text chunks
            paragraphs = soup.find_all('p')
            for p in paragraphs:
                text = p.get_text().strip()
                if text:
                    chunks.append(DocumentChunk(
                        content=text,
                        metadata=self.metadata.copy(),
                        chunk_type=ChunkType.TEXT,
                        source_file=str(file_path)
                    ))

            # 提取列表
            lists = soup.find_all(['ul', 'ol'])
            for lst in lists:
                items = lst.find_all('li')
                item_texts = []
                for item in items:
                    t = item.get_text().strip()
                    if t:
                        prefix = "- " if lst.name == 'ul' else "1. "
                        item_texts.append(f"{prefix}{t}")
                if item_texts:
                    chunks.append(DocumentChunk(
                        content="\n".join(item_texts),
                        metadata=self.metadata.copy(),
                        chunk_type=ChunkType.LIST,
                        source_file=str(file_path)
                    ))

            # 提取表格
            if self.extract_tables:
                tables = soup.find_all('table')
                for table in tables:
                    table_text = self._extract_table_text(table)
                    if table_text:
                        chunks.append(DocumentChunk(
                            content=f"表格:\n{table_text}",
                            metadata=self.metadata.copy(),
                            chunk_type=ChunkType.TABLE,
                            source_file=str(file_path),
                            table_data={"rows": [line.split(" | ") for line in table_text.split("\n") if line.strip()]}
                        ))

            chunks = link_document_chunks(chunks)

            self.metadata.update(self.get_document_metadata(file_path))
            self.metadata.update({
                "title": soup.title.get_text().strip() if soup.title else "",
                "extract_tables": self.extract_tables,
                "extract_links": self.extract_links,
            })

            return chunks

        except Exception as e:
            raise FileLoadError(f"Failed to load HTML chunks: {e}", file_path) from e

    def _parse_html_text(self, html_content: str) -> str:
        """解析 HTML 内容为纯文本。"""
        soup = BeautifulSoup(html_content, 'html.parser')
        parts = []

        if soup.title:
            parts.append(f"标题: {soup.title.get_text().strip()}")

        for i in range(1, 7):
            for header in soup.find_all(f'h{i}'):
                text = header.get_text().strip()
                if text:
                    parts.append(f"{'#' * i} {text}")

        for p in soup.find_all('p'):
            text = p.get_text().strip()
            if text:
                parts.append(text)

        for lst in soup.find_all(['ul', 'ol']):
            for item in lst.find_all('li'):
                text = item.get_text().strip()
                if text:
                    prefix = "- " if lst.name == 'ul' else "1. "
                    parts.append(f"{prefix}{text}")

        if self.extract_tables:
            for table in soup.find_all('table'):
                table_text = self._extract_table_text(table)
                if table_text:
                    parts.append(f"表格:\n{table_text}")

        return "\n\n".join(parts)

    def _extract_table_text(self, table) -> str:
        """提取表格文本内容。"""
        lines = []
        for row in table.find_all('tr'):
            cells = row.find_all(['th', 'td'])
            cell_texts = [cell.get_text().strip() for cell in cells]
            if cell_texts:
                lines.append(" | ".join(cell_texts))
        return "\n".join(lines)
