"""文本文件加载器。

支持 .txt 文件格式，自动检测编码。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from app.services.file_loaders.base import BaseLoader, DocumentChunk, ChunkType


class TextLoader(BaseLoader):
    """文本文件加载器。"""
    
    @property
    def supported_extensions(self) -> list[str]:
        """支持的文件扩展名列表。"""
        return [".txt"]
    
    def load(self, file_path: Path) -> str:
        """加载文本文件为纯文本。"""
        self._validate_file(file_path)
        
        try:
            # 使用多种编码尝试读取文件
            content = self._read_text_with_fallback(file_path)
            
            # 更新元数据
            self.metadata.update(self.get_document_metadata(file_path))
            
            return content
            
        except Exception as e:
            raise FileLoadError(f"Failed to load text file: {e}", file_path) from e
    
    def load_stream(self, file_path: Path) -> Iterator[str]:
        """流式加载文本文件。"""
        self._validate_file(file_path)
        
        try:
            # 使用多种编码尝试读取文件
            content = self._read_text_with_fallback(file_path)
            
            # 按行流式返回
            for line in content.splitlines():
                if line.strip():  # 跳过空行
                    yield line
                    
        except Exception as e:
            raise FileLoadError(f"Failed to stream text file: {e}", file_path) from e
    
    def load_document_chunks(self, file_path: Path) -> list[DocumentChunk]:
        """加载文本文件为文档块列表。"""
        content = self.load(file_path)
        
        # 如果启用结构化处理，按段落分块
        if self.preserve_structure:
            # 按空行分割段落
            paragraphs = [p.strip() for p in content.split('\n\n') if p.strip()]
            chunks = []
            
            for i, paragraph in enumerate(paragraphs):
                chunks.append(DocumentChunk(
                    content=paragraph,
                    metadata=self.metadata.copy(),
                    chunk_type=ChunkType.TEXT,
                    source_file=str(file_path),
                    page_number=None,
                    heading_level=None
                ))
            
            return chunks
        else:
            # 否则返回整个内容作为一个块
            return super().load_document_chunks(file_path)