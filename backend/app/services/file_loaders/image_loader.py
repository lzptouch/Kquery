"""图片文件加载器。

支持常见图片格式，提供OCR文字识别和图片描述功能。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, List, Dict, Any
import os

from app.services.file_loaders.base import BaseLoader, DocumentChunk, ChunkType, FileLoadError


try:
    import cv2
    import easyocr
    from PIL import Image, ImageOps
    ENABLE_IMAGE_PROCESSING = True
except ImportError:
    ENABLE_IMAGE_PROCESSING = False


class ImageLoader(BaseLoader):
    """图片文件加载器。"""
    
    def __init__(self, use_ocr: bool = True, preserve_structure: bool = True):
        """初始化图片加载器。"""
        super().__init__(use_ocr=use_ocr, preserve_structure=preserve_structure)
        self.reader = None
        
        if use_ocr and ENABLE_IMAGE_PROCESSING:
            try:
                # 初始化OCR阅读器（中英文支持）
                self.reader = easyocr.Reader(['ch_sim', 'en'])
            except Exception as e:
                print(f"Warning: Failed to initialize OCR reader: {e}")
    
    @property
    def supported_extensions(self) -> list[str]:
        """支持的文件扩展名列表。"""
        return [".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".gif", ".webp"]
    
    def load(self, file_path: Path) -> str:
        """加载图片文件为纯文本。"""
        self._validate_file(file_path)
        
        try:
            text_content = ""
            
            # 如果启用OCR，尝试提取文字
            if self.use_ocr and self.reader:
                ocr_text = self._extract_text_with_ocr(file_path)
                if ocr_text:
                    text_content += f"OCR识别文字:\n{ocr_text}\n\n"
            
            # 添加图片基本信息
            image_info = self._get_image_info(file_path)
            text_content += f"图片信息: {image_info}"
            
            # 更新元数据
            self.metadata.update(self.get_document_metadata(file_path))
            
            return text_content
            
        except Exception as e:
            raise FileLoadError(f"Failed to load image file: {e}", file_path) from e
    
    def load_stream(self, file_path: Path) -> Iterator[str]:
        """流式加载图片文件。"""
        self._validate_file(file_path)
        
        try:
            # 返回图片基本信息
            image_info = self._get_image_info(file_path)
            yield f"图片信息: {image_info}"
            
            # 如果启用OCR，返回识别的文字
            if self.use_ocr and self.reader:
                ocr_text = self._extract_text_with_ocr(file_path)
                if ocr_text:
                    yield f"OCR识别文字: {ocr_text}"
                    
        except Exception as e:
            raise FileLoadError(f"Failed to stream image file: {e}", file_path) from e
    
    def load_document_chunks(self, file_path: Path) -> List[DocumentChunk]:
        """加载图片文件为结构化文档块列表。"""
        self._validate_file(file_path)
        
        try:
            chunks = []
            
            # 图片基本信息块
            image_info = self._get_image_info(file_path)
            metadata = self.metadata.copy()
            metadata.update(self._get_image_metadata(file_path))
            
            chunks.append(DocumentChunk(
                content=f"图片基本信息: {image_info}",
                metadata=metadata,
                chunk_type=ChunkType.TEXT,
                source_file=str(file_path),
                page_number=None,
                heading_level=None
            ))
            
            # OCR文字识别块
            if self.use_ocr and self.reader:
                ocr_text = self._extract_text_with_ocr(file_path)
                if ocr_text:
                    chunks.append(DocumentChunk(
                        content=f"OCR识别文字:\n{ocr_text}",
                        metadata=metadata.copy(),
                        chunk_type=ChunkType.TEXT,
                        source_file=str(file_path),
                        page_number=None,
                        heading_level=None
                    ))
            
            # 如果没有OCR内容，返回基本信息
            if len(chunks) == 1 and not ocr_text:
                chunks[0].content = f"图片文件: {file_path.name}\n{image_info}"
            
            return chunks
            
        except Exception as e:
            raise FileLoadError(f"Failed to load image chunks: {e}", file_path) from e
    
    def get_document_metadata(self, file_path: Path) -> Dict[str, Any]:
        """获取图片文档元数据。"""
        metadata = super().get_document_metadata(file_path)
        
        try:
            image_metadata = self._get_image_metadata(file_path)
            metadata.update(image_metadata)
            
        except Exception:
            # 如果图片处理失败，只返回基础元数据
            pass
            
        return metadata
    
    def supports_ocr(self) -> bool:
        """图片支持OCR功能。"""
        return ENABLE_IMAGE_PROCESSING
    
    def supports_structured_content(self) -> bool:
        """图片支持结构化内容提取。"""
        return True
    
    def _extract_text_with_ocr(self, file_path: Path) -> str:
        """使用OCR提取图片中的文字。"""
        if not self.reader:
            return ""
        
        try:
            # 使用EasyOCR进行文字识别
            results = self.reader.readtext(str(file_path))
            
            # 提取识别结果
            text_parts = []
            for bbox, text, confidence in results:
                if confidence > 0.3:  # 置信度阈值
                    text_parts.append(text)
            
            return "\n".join(text_parts) if text_parts else "未识别到文字"
            
        except Exception as e:
            print(f"OCR processing failed: {e}")
            return ""
    
    def _get_image_info(self, file_path: Path) -> str:
        """获取图片基本信息。"""
        try:
            with Image.open(file_path) as img:
                width, height = img.size
                mode = img.mode
                format_info = img.format
                
                return f"尺寸: {width}x{height}, 模式: {mode}, 格式: {format_info}"
                
        except Exception:
            return "无法获取图片信息"
    
    def _get_image_metadata(self, file_path: Path) -> Dict[str, Any]:
        """获取图片详细元数据。"""
        metadata = {}
        
        try:
            with Image.open(file_path) as img:
                width, height = img.size
                
                metadata.update({
                    "image_width": width,
                    "image_height": height,
                    "image_mode": img.mode,
                    "image_format": img.format,
                    "image_has_ocr": self.use_ocr and self.reader is not None
                })
                
                # 尝试获取EXIF信息
                try:
                    exif_data = img._getexif()
                    if exif_data:
                        # 提取常见的EXIF标签
                        exif_tags = {}
                        for tag, value in exif_data.items():
                            if hasattr(Image.ExifTags.TAGS, 'get'):
                                tag_name = Image.ExifTags.TAGS.get(tag, tag)
                                exif_tags[str(tag_name)] = str(value)
                        
                        if exif_tags:
                            metadata["exif_data"] = exif_tags
                except Exception:
                    pass
                
        except Exception:
            pass
            
        return metadata
    
    def _preprocess_image(self, file_path: Path) -> Any:
        """图片预处理（可选）。"""
        if not ENABLE_IMAGE_PROCESSING:
            return None
            
        try:
            # 读取图片
            img = cv2.imread(str(file_path))
            if img is None:
                return None
            
            # 转换为灰度图（OCR效果更好）
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            
            # 可选：图像增强
            # 直方图均衡化
            equalized = cv2.equalizeHist(gray)
            
            return equalized
            
        except Exception:
            return None


# 加载器会自动通过模块化系统注册