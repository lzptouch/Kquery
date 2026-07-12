"""PowerPoint 演示文稿加载器

支持格式：.pptx, .ppt

功能特点：
- 支持 PowerPoint 演示文稿的文本提取
- 按幻灯片组织内容结构
- 提取幻灯片标题和备注
- 支持表格内容提取
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
    from pptx import Presentation
    PPTX_AVAILABLE = True
except ImportError:
    PPTX_AVAILABLE = False
    logger.warning("python-pptx library not available. PowerPoint files will not be processed.")


class PowerPointLoader(BaseLoader):
    """PowerPoint 演示文稿加载器（兼容 BaseLoader）"""

    def __init__(self, use_ocr: bool = False, preserve_structure: bool = True,
                 extract_notes: bool = True, extract_tables: bool = True):
        super().__init__(use_ocr=use_ocr, preserve_structure=preserve_structure)
        self.extract_notes = extract_notes
        self.extract_tables = extract_tables

    @property
    def supported_extensions(self) -> list[str]:
        return ['.pptx', '.ppt']

    def load(self, file_path: Path) -> str:
        """加载 PowerPoint 文件为纯文本。"""
        self._validate_file(file_path)

        if file_path.suffix.lower() == '.pptx':
            return self._load_pptx_text(file_path)
        else:
            raise FileLoadError("Legacy .ppt format is not fully supported. Please convert to .pptx format.", file_path)

    def load_stream(self, file_path: Path) -> Iterator[str]:
        """流式加载 PowerPoint 文件（按幻灯片）。"""
        content = self.load(file_path)
        for slide_text in content.split('\n\n幻灯片 '):
            if slide_text.strip():
                yield slide_text.strip()

    def load_document_chunks(self, file_path: Path) -> list[DocumentChunk]:
        """加载 PowerPoint 文件为按幻灯片分块的文档块。"""
        self._validate_file(file_path)

        if file_path.suffix.lower() != '.pptx':
            raise FileLoadError("Legacy .ppt format is not fully supported.", file_path)

        if not PPTX_AVAILABLE:
            raise FileLoadError("python-pptx library is required for .pptx file processing", file_path)

        try:
            prs = Presentation(file_path)
            chunks = []

            for i, slide in enumerate(prs.slides, 1):
                slide_chunks = self._extract_slide_chunks(slide, i, file_path)
                chunks.extend(slide_chunks)

            chunks = link_document_chunks(chunks)

            self.metadata.update(self.get_document_metadata(file_path))
            self.metadata.update({
                "slide_count": len(prs.slides),
                "extract_notes": self.extract_notes,
                "extract_tables": self.extract_tables,
            })

            return chunks

        except Exception as e:
            raise FileLoadError(f"Failed to load PowerPoint chunks: {e}", file_path) from e

    def _load_pptx_text(self, file_path: Path) -> str:
        """加载 .pptx 文件为纯文本。"""
        if not PPTX_AVAILABLE:
            raise FileLoadError("python-pptx library is required for .pptx file processing", file_path)

        prs = Presentation(file_path)
        slides_content = []

        for i, slide in enumerate(prs.slides, 1):
            slide_text = self._extract_slide_text(slide, i)
            if slide_text:
                slides_content.append(slide_text)

        return "\n\n".join(slides_content)

    def _extract_slide_text(self, slide, slide_number: int) -> str:
        """提取单张幻灯片文本。"""
        parts = [f"幻灯片 {slide_number}:"]

        if slide.shapes.title and slide.shapes.title.text.strip():
            parts.append(f"标题: {slide.shapes.title.text.strip()}")

        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text.strip() and shape != slide.shapes.title:
                parts.append(shape.text.strip())

        if self.extract_notes and slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame
            if notes and notes.text.strip():
                parts.append(f"备注: {notes.text.strip()}")

        if self.extract_tables:
            for shape in slide.shapes:
                if shape.has_table:
                    table_text = self._extract_table_text(shape.table)
                    if table_text:
                        parts.append(f"表格:\n{table_text}")

        return "\n".join(parts)

    def _extract_slide_chunks(self, slide, slide_number: int, source_file: Path) -> list[DocumentChunk]:
        """提取单张幻灯片为文档块列表。"""
        chunks = []

        # 标题块
        if slide.shapes.title and slide.shapes.title.text.strip():
            chunks.append(DocumentChunk(
                content=f"幻灯片 {slide_number} 标题: {slide.shapes.title.text.strip()}",
                metadata=self.metadata.copy(),
                chunk_type=ChunkType.HEADING,
                source_file=str(source_file)
            ))

        # 内容块
        content_parts = []
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text.strip() and shape != slide.shapes.title:
                content_parts.append(shape.text.strip())

        if content_parts:
            chunks.append(DocumentChunk(
                content="\n".join(content_parts),
                metadata=self.metadata.copy(),
                chunk_type=ChunkType.TEXT,
                source_file=str(source_file)
            ))

        # 表格块
        if self.extract_tables:
            for shape in slide.shapes:
                if shape.has_table:
                    table_text = self._extract_table_text(shape.table)
                    if table_text:
                        rows = [line.split(" | ") for line in table_text.split("\n")]
                        chunks.append(DocumentChunk(
                            content=f"表格:\n{table_text}",
                            metadata=self.metadata.copy(),
                            chunk_type=ChunkType.TABLE,
                            source_file=str(source_file),
                            table_data={"rows": rows}
                        ))

        # 备注块
        if self.extract_notes and slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame
            if notes and notes.text.strip():
                chunks.append(DocumentChunk(
                    content=f"备注: {notes.text.strip()}",
                    metadata=self.metadata.copy(),
                    chunk_type=ChunkType.TEXT,
                    source_file=str(source_file)
                ))

        return chunks

    def _extract_table_text(self, table) -> str:
        """提取表格文本内容。"""
        lines = []
        for row in table.rows:
            cell_texts = []
            for cell in row.cells:
                text = cell.text_frame.text.strip() if cell.text_frame else ""
                cell_texts.append(text)
            lines.append(" | ".join(cell_texts))
        return "\n".join(lines)
