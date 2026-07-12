"""PDF文件加载器。

支持 .pdf 文件格式，可选OCR功能。
增强功能：
- 提取PDF内部图像
- 对图像进行AI理解和描述生成
- 将图像描述与周围文本关联
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Iterator, List, Dict, Any
import io

from app.services.file_loaders.base import (
    BaseLoader, DocumentChunk, ChunkType, FileLoadError,
    link_document_chunks
)
from app.core.logger import get_logger
from app.services.image_understanding import image_understanding_service

logger = get_logger(__name__)


class PDFLoader(BaseLoader):
    """PDF文件加载器（增强版）。"""
    
    def __init__(
        self,
        use_ocr: bool = False,
        preserve_structure: bool = True,
        extract_images: bool = True,
        understand_images: bool = True
    ):
        """初始化PDF加载器。
        
        Args:
            use_ocr: 是否启用OCR
            preserve_structure: 是否保留文档结构
            extract_images: 是否提取PDF内部图像
            understand_images: 是否对提取的图像进行AI理解
        """
        super().__init__(use_ocr=use_ocr, preserve_structure=preserve_structure)
        self.extract_images = extract_images
        self.understand_images = understand_images
        self._temp_dir: Optional[Path] = None
    
    @property
    def supported_extensions(self) -> list[str]:
        """支持的文件扩展名列表。"""
        return [".pdf"]
    
    def supports_ocr(self) -> bool:
        """是否支持OCR功能。"""
        return True
    
    def supports_structured_content(self) -> bool:
        """是否支持结构化内容提取。"""
        return True
    
    def _ensure_temp_dir(self):
        """确保临时目录存在。"""
        if self._temp_dir is None:
            self._temp_dir = Path(tempfile.mkdtemp(prefix="kquery_pdf_"))
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
    
    def load(self, file_path: Path) -> str:
        """加载PDF文件为纯文本。"""
        self._validate_file(file_path)
        
        try:
            import fitz  # PyMuPDF
            
            doc = fitz.open(file_path)
            content_parts = []
            
            for page_num in range(len(doc)):
                page = doc[page_num]
                
                text = page.get_text()
                
                if self.use_ocr and len(text.strip()) < 100:
                    try:
                        import pytesseract
                        from PIL import Image
                        
                        pix = page.get_pixmap()
                        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                        
                        ocr_text = pytesseract.image_to_string(img, lang='chi_sim+eng')
                        if ocr_text.strip():
                            text = ocr_text
                    except ImportError:
                        pass
                
                if text.strip():
                    content_parts.append(f"页面 {page_num + 1}:\n{text}")
            
            doc.close()
            
            full_content = "\n\n".join(content_parts)
            
            self.metadata.update(self.get_document_metadata(file_path))
            self.metadata.update({
                "page_count": len(doc),
                "ocr_used": self.use_ocr
            })
            
            return full_content
            
        except ImportError:
            raise FileLoadError(
                "PDF加载需要安装PyMuPDF: pip install PyMuPDF",
                file_path
            )
        except Exception as e:
            raise FileLoadError(f"Failed to load PDF: {e}", file_path) from e
    
    def load_stream(self, file_path: Path) -> Iterator[str]:
        """流式加载PDF文件（按页面）。"""
        self._validate_file(file_path)
        
        try:
            import fitz  # PyMuPDF
            
            doc = fitz.open(file_path)
            
            for page_num in range(len(doc)):
                page = doc[page_num]
                text = page.get_text()
                
                if text.strip():
                    yield f"页面 {page_num + 1}:\n{text}"
            
            doc.close()
            
        except ImportError:
            raise FileLoadError(
                "PDF加载需要安装PyMuPDF: pip install PyMuPDF",
                file_path
            )
        except Exception as e:
            raise FileLoadError(f"Failed to stream PDF: {e}", file_path) from e
    
    def load_document_chunks(self, file_path: Path) -> list[DocumentChunk]:
        """加载PDF文件为文档块列表（按页面），并提取和理解图像。"""
        self._validate_file(file_path)
        
        try:
            import fitz  # PyMuPDF
            
            doc = fitz.open(file_path)
            chunks = []
            
            self._ensure_temp_dir()
            
            page_count = len(doc)
            char_offset = 0
            
            for page_num in range(page_count):
                page = doc[page_num]
                
                page_chunks = self._process_page(page, page_num, file_path, char_offset)
                chunks.extend(page_chunks)
                
                # 更新下一页的字符偏移量
                for chunk in page_chunks:
                    char_offset += len(chunk.content)
            
            chunks = link_document_chunks(chunks)
            
            self.metadata.update(self.get_document_metadata(file_path))
            self.metadata.update({
                "page_count": page_count,
                "extract_images": self.extract_images,
                "understand_images": self.understand_images
            })
            
            doc.close()
            
            return chunks
            
        except ImportError:
            raise FileLoadError(
                "PDF加载需要安装PyMuPDF: pip install PyMuPDF",
                file_path
            )
        except Exception as e:
            raise FileLoadError(f"Failed to load PDF chunks: {e}", file_path) from e
        finally:
            self._cleanup_temp_dir()
    
    def _process_page(
        self,
        page,
        page_num: int,
        source_file: Path,
        char_offset: int = 0
    ) -> List[DocumentChunk]:
        """处理单页PDF内容。
        
        Args:
            page: PyMuPDF页面对象
            page_num: 页码（0-based）
            source_file: 源文件路径
            char_offset: 字符偏移量
            
        Returns:
            文档块列表
        """
        chunks = []
        
        text = page.get_text()
        
        if text.strip():
            text_chunk = DocumentChunk(
                content=text,
                metadata=self.metadata.copy(),
                chunk_type=ChunkType.TEXT,
                source_file=str(source_file),
                page_number=page_num + 1,
                char_offset=char_offset
            )
            chunks.append(text_chunk)
        
        if self.extract_images:
            image_chunks = self._extract_and_process_images(page, page_num, source_file)
            chunks.extend(image_chunks)
        
        return chunks
    
    def _extract_and_process_images(
        self,
        page,
        page_num: int,
        source_file: Path
    ) -> List[DocumentChunk]:
        """提取并处理页面中的图像。
        
        Args:
            page: PyMuPDF页面对象
            page_num: 页码（0-based）
            source_file: 源文件路径
            
        Returns:
            图像相关的文档块列表
        """
        chunks = []
        
        try:
            image_list = page.get_images()
            
            for img_idx, img in enumerate(image_list):
                xref = img[0]
                
                base_image = page.parent.extract_image(xref)
                image_bytes = base_image["image"]
                image_ext = base_image["ext"]
                
                image_path = self._temp_dir / f"page_{page_num + 1}_img_{img_idx + 1}.{image_ext}"
                
                with open(image_path, "wb") as f:
                    f.write(image_bytes)
                
                image_description = None
                image_hash = None
                
                if self.understand_images:
                    try:
                        import asyncio
                        result = asyncio.run(
                            image_understanding_service.understand_image(image_path)
                        )
                        if result.success:
                            image_description = result.description
                            image_hash = result.image_hash
                    except Exception as e:
                        logger.warning(f"图像理解失败: {e}")
                
                chunk_content = f"PDF页面 {page_num + 1} 图像 {img_idx + 1}"
                if image_description:
                    chunk_content += f"\n\n图像描述: {image_description}"
                
                image_chunk = DocumentChunk(
                    content=chunk_content,
                    metadata=self.metadata.copy(),
                    chunk_type=ChunkType.IMAGE_DESCRIPTION,
                    source_file=str(source_file),
                    page_number=page_num + 1,
                    image_path=str(image_path),
                    image_description=image_description,
                    image_hash=image_hash
                )
                chunks.append(image_chunk)
                
        except Exception as e:
            logger.warning(f"提取页面 {page_num + 1} 图像失败: {e}")
        
        return chunks
