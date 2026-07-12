"""Markdown文件加载器。

支持 .md, .markdown 文件格式，支持按标题结构化分块。
增强功能：
- 识别和处理Markdown中的图像链接
- 对本地和远程图像进行理解
"""

from __future__ import annotations

import re
import tempfile
import urllib.request
from pathlib import Path
from typing import Iterator, List, Dict, Any, Tuple
from urllib.parse import urlparse

from app.services.file_loaders.base import (
    BaseLoader, DocumentChunk, ChunkType, FileLoadError,
    link_document_chunks
)
from app.core.logger import get_logger
from app.services.image_understanding import image_understanding_service

logger = get_logger(__name__)


class MarkdownLoader(BaseLoader):
    """Markdown文件加载器（增强版）。"""
    
    def __init__(
        self,
        use_ocr: bool = False,
        preserve_structure: bool = True,
        extract_images: bool = True,
        understand_images: bool = True
    ):
        """初始化Markdown加载器。
        
        Args:
            use_ocr: 是否启用OCR
            preserve_structure: 是否保留文档结构
            extract_images: 是否提取Markdown中的图像
            understand_images: 是否对提取的图像进行进行理解
        """
        super().__init__(use_ocr=use_ocr, preserve_structure=preserve_structure)
        self.extract_images = extract_images
        self.understand_images = understand_images
        self._temp_dir: Optional[Path] = None
        self._image_pattern = re.compile(
            r'!\[(.*?)\]\((.*?)\)',
            re.DOTALL
        )
    
    @property
    def supported_extensions(self) -> list[str]:
        """支持的文件扩展名列表。"""
        return [".md", ".markdown"]
    
    def supports_structured_content(self) -> bool:
        """是否支持结构化内容提取。"""
        return True
    
    def _ensure_temp_dir(self):
        """确保临时目录存在。"""
        if self._temp_dir is None:
            self._temp_dir = Path(tempfile.mkdtemp(prefix="kquery_md_"))
            logger.debug(f"创建临时目录: {self._temp_dir}")
    
    def _cleanup_temp_dir(self):
        """清理临时目录。"""
        if self._temp_dir and self._temp_dir.exists():
            try:
                import shutil
                shutil.rmtree(self._temp_dir)
                logger.debug(f"清理临时目录: {self._temp_dir}")
            except Exception as e:
                logger.warning(f"清理临时目录失败: {e}")
            self._temp_dir = None
    
    def _is_url(self, path: str) -> bool:
        """判断是否是URL。
        
        Args:
            path: 路径字符串
            
        Returns:
            是否是URL
        """
        try:
            result = urlparse(path)
            return all([result.scheme, result.netloc])
        except Exception:
            return False
    
    def _download_image(self, url: str, target_path: Path) -> bool:
        """下载远程图像。
        
        Args:
            url: 图像URL
            target_path: 目标保存路径
            
        Returns:
            是否下载成功
        """
        try:
            urllib.request.urlretrieve(url, target_path)
            return True
        except Exception as e:
            logger.warning(f"下载图像失败 {url}: {e}")
            return False
    
    def _get_image_extension(self, url: str) -> str:
        """从URL或路径获取图像扩展名。
        
        Args:
            url: URL或路径
            
        Returns:
            图像扩展名
        """
        url_lower = url.lower()
        if url_lower.endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.tiff')):
            return Path(url).suffix[1:]
        return 'png'
    
    def _extract_images_from_content(
        self,
        content: str,
        source_file: Path
    ) -> Tuple[str, List[DocumentChunk]]:
        """从内容中提取图像并处理。
        
        Args:
            content: Markdown内容
            source_file: 源文件路径
            
        Returns:
            (处理后的内容, 图像文档块列表)
        """
        image_chunks = []
        modified_content = content
        img_idx = 0
        
        matches = list(self._image_pattern.finditer(content))
        
        for match in reversed(matches):
            img_idx += 1
            alt_text = match.group(1)
            image_url = match.group(2)
            
            image_path = None
            image_description = None
            image_hash = None
            
            if self.extract_images:
                self._ensure_temp_dir()
                
                if self._is_url(image_url):
                    image_ext = self._get_image_extension(image_url)
                    temp_image_path = self._temp_dir / f"md_img_{img_idx}.{image_ext}"
                    if self._download_image(image_url, temp_image_path):
                        image_path = temp_image_path
                else:
                    base_dir = source_file.parent
                    local_image_path = base_dir / image_url
                    if local_image_path.exists():
                        image_path = local_image_path
                
                if image_path and self.understand_images:
                    try:
                        import asyncio
                        result = asyncio.run(
                            image_understanding_service.understand_image(image_path)
                        )
                        if result.success:
                            image_description = result.description
                            image_hash = result.image_hash
                    except Exception as e:
                        logger.warning(f"图像理解失败 {image_path}: {e}")
            
            if image_path or image_description:
                chunk_content = f"Markdown图像 {img_idx}"
                if alt_text:
                    chunk_content += f"\n替代文本: {alt_text}"
                if image_description:
                    chunk_content += f"\n\n图像描述: {image_description}"
                
                image_chunk = DocumentChunk(
                    content=chunk_content,
                    metadata=self.metadata.copy(),
                    chunk_type=ChunkType.IMAGE_DESCRIPTION,
                    source_file=str(source_file),
                    image_path=str(image_path) if image_path else None,
                    image_description=image_description,
                    image_hash=image_hash
                )
                image_chunks.insert(0, image_chunk)
        
        return modified_content, image_chunks
    
    def load(self, file_path: Path) -> str:
        """加载Markdown文件为纯文本。"""
        self._validate_file(file_path)
        
        try:
            content = self._read_text_with_fallback(file_path)
            
            self.metadata.update(self.get_document_metadata(file_path))
            
            return content
            
        except Exception as e:
            raise FileLoadError(f"Failed to load Markdown file: {e}", file_path) from e
    
    def load_stream(self, file_path: Path) -> Iterator[str]:
        """流式加载Markdown文件（按行）。"""
        self._validate_file(file_path)
        
        try:
            content = self._read_text_with_fallback(file_path)
            
            for line in content.splitlines():
                if line.strip():
                    yield line
                    
        except Exception as e:
            raise FileLoadError(f"Failed to stream Markdown file: {e}", file_path) from e
    
    def load_document_chunks(self, file_path: Path) -> list[DocumentChunk]:
        """加载Markdown文件为文档块列表（按标题分块），并处理图像。"""
        content = self.load(file_path)
        
        if not self.preserve_structure:
            return super().load_document_chunks(file_path)
        
        try:
            chunks = []
            
            if self.extract_images:
                content, image_chunks = self._extract_images_from_content(content, file_path)
            else:
                image_chunks = []
            
            lines = content.splitlines()
            current_chunk = []
            current_heading_level = None
            
            for line in lines:
                line = line.rstrip()
                
                if line.startswith('#'):
                    if current_chunk:
                        chunk_content = '\n'.join(current_chunk).strip()
                        if chunk_content:
                            chunks.append(DocumentChunk(
                                content=chunk_content,
                                metadata=self.metadata.copy(),
                                chunk_type=ChunkType.TEXT,
                                source_file=str(file_path),
                                page_number=None,
                                heading_level=current_heading_level
                            ))
                    
                    current_chunk = [line]
                    
                    heading_level = 0
                    for char in line:
                        if char == '#':
                            heading_level += 1
                        else:
                            break
                    current_heading_level = heading_level
                    
                else:
                    current_chunk.append(line)
            
            if current_chunk:
                chunk_content = '\n'.join(current_chunk).strip()
                if chunk_content:
                    chunks.append(DocumentChunk(
                        content=chunk_content,
                        metadata=self.metadata.copy(),
                        chunk_type=ChunkType.TEXT,
                        source_file=str(file_path),
                        page_number=None,
                        heading_level=current_heading_level
                    ))
            
            chunks.extend(image_chunks)
            chunks = link_document_chunks(chunks)
            
            self.metadata.update(self.get_document_metadata(file_path))
            self.metadata.update({
                "extract_images": self.extract_images,
                "understand_images": self.understand_images,
                "image_count": len(image_chunks)
            })
            
            return chunks
            
        except Exception as e:
            raise FileLoadError(f"Failed to load Markdown chunks: {e}", file_path) from e
        finally:
            self._cleanup_temp_dir()
