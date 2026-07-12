"""文档分类器 (Document Classifier)

根据文件扩展名、MIME 类型和内容特征，自动将文档划分为：
- structured: Excel/CSV/数据库导出表
- semi_structured: Word/HTML/JSON/XML/可复制 PDF
- unstructured: 纯文本/Markdown
- multimodal: 扫描 PDF/图片/PPT

分类结果用于路由到对应的解析与抽取分支，实现差异化知识图谱构建。

作者：KQuery Pro Team
版本：1.0.0
更新：2026-07-05
"""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from app.services.document_models import DocumentCategory
from app.core.logger import get_logger

logger = get_logger(__name__)


try:
    import fitz  # PyMuPDF
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False


try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False


class DocumentClassifier:
    """文档分类器
    
    按结构化程度对文档进行分类，支持：
    1. 基于扩展名快速分类
    2. 基于 MIME 类型辅助分类
    3. PDF 扫描件检测（文本量 < 50 字符/页判定为扫描件）
    4. 自定义覆盖规则
    """
    
    # 扩展名 -> 文档类别映射
    EXTENSION_CATEGORY_MAP: Dict[str, str] = {
        # 结构化文档
        ".xlsx": DocumentCategory.STRUCTURED.value,
        ".xls": DocumentCategory.STRUCTURED.value,
        ".csv": DocumentCategory.STRUCTURED.value,
        ".tsv": DocumentCategory.STRUCTURED.value,
        ".db": DocumentCategory.STRUCTURED.value,
        ".sql": DocumentCategory.STRUCTURED.value,
        
        # 半结构化文档
        ".docx": DocumentCategory.SEMI_STRUCTURED.value,
        ".doc": DocumentCategory.SEMI_STRUCTURED.value,
        ".pdf": DocumentCategory.SEMI_STRUCTURED.value,  # 默认可复制 PDF，检测后可能转为 multimodal
        ".html": DocumentCategory.SEMI_STRUCTURED.value,
        ".htm": DocumentCategory.SEMI_STRUCTURED.value,
        ".xml": DocumentCategory.SEMI_STRUCTURED.value,
        ".json": DocumentCategory.SEMI_STRUCTURED.value,
        ".yaml": DocumentCategory.SEMI_STRUCTURED.value,
        ".yml": DocumentCategory.SEMI_STRUCTURED.value,
        
        # 非结构化文档
        ".txt": DocumentCategory.UNSTRUCTURED.value,
        ".md": DocumentCategory.UNSTRUCTURED.value,
        ".markdown": DocumentCategory.UNSTRUCTURED.value,
        ".rtf": DocumentCategory.UNSTRUCTURED.value,
        
        # 多模态文档
        ".pptx": DocumentCategory.MULTIMODAL.value,
        ".ppt": DocumentCategory.MULTIMODAL.value,
        ".png": DocumentCategory.MULTIMODAL.value,
        ".jpg": DocumentCategory.MULTIMODAL.value,
        ".jpeg": DocumentCategory.MULTIMODAL.value,
        ".tiff": DocumentCategory.MULTIMODAL.value,
        ".tif": DocumentCategory.MULTIMODAL.value,
        ".bmp": DocumentCategory.MULTIMODAL.value,
        ".gif": DocumentCategory.MULTIMODAL.value,
        ".webp": DocumentCategory.MULTIMODAL.value,
    }
    
    # 扫描 PDF 判定阈值：每页平均可提取字符数低于此值视为扫描件
    SCANNED_PDF_TEXT_THRESHOLD = 50
    
    def __init__(self, scanned_pdf_threshold: int = 50):
        self.scanned_pdf_threshold = scanned_pdf_threshold
    
    def classify(self, file_path: Path | str) -> Dict[str, Any]:
        """对文件进行分类，返回分类结果字典
        
        Returns:
            {
                "category": str,       # structured/semi_structured/unstructured/multimodal
                "confidence": float,   # 分类置信度（0.0-1.0）
                "method": str,         # extension / mime / content / fallback
                "mime_type": str,
                "is_scanned_pdf": bool,
                "file_hash": str,      # MD5 哈希
                "reason": str          # 分类原因说明
            }
        """
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")
        
        suffix = file_path.suffix.lower()
        mime_type, _ = mimetypes.guess_type(str(file_path))
        mime_type = mime_type or "application/octet-stream"
        file_hash = self._compute_md5(file_path)
        
        # 1. 基于扩展名分类
        if suffix in self.EXTENSION_CATEGORY_MAP:
            category = self.EXTENSION_CATEGORY_MAP[suffix]
            method = "extension"
            confidence = 0.9
            reason = f"文件扩展名 {suffix} 映射为 {category}"
        else:
            # 2. 基于 MIME 类型分类
            category = self._classify_by_mime(mime_type)
            method = "mime"
            confidence = 0.7
            reason = f"MIME 类型 {mime_type} 推断为 {category}"
        
        is_scanned_pdf = False
        
        # 3. PDF 内容检测：扫描件判定
        if suffix == ".pdf":
            is_scanned_pdf = self._is_scanned_pdf(file_path)
            if is_scanned_pdf:
                category = DocumentCategory.MULTIMODAL.value
                method = "content"
                reason = "PDF 每页可提取文本量低于阈值，判定为扫描件"
            else:
                reason = "PDF 可提取文本量充足，判定为可复制 PDF"
        
        # 4. 未知类型降级为 unstructured 走 LLM 抽取
        if category is None:
            category = DocumentCategory.UNSTRUCTURED.value
            method = "fallback"
            confidence = 0.5
            reason = "无法识别文件类型，默认走非结构化 LLM 抽取"
        
        return {
            "category": category,
            "confidence": confidence,
            "method": method,
            "mime_type": mime_type,
            "is_scanned_pdf": is_scanned_pdf,
            "file_hash": file_hash,
            "reason": reason
        }
    
    def _classify_by_mime(self, mime_type: str) -> Optional[str]:
        """基于 MIME 类型推断文档类别"""
        if mime_type in (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
            "text/csv",
            "text/tab-separated-values",
        ):
            return DocumentCategory.STRUCTURED.value
        
        if mime_type in (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/msword",
            "application/pdf",
            "text/html",
            "application/xhtml+xml",
            "application/xml",
            "application/json",
        ):
            return DocumentCategory.SEMI_STRUCTURED.value
        
        if mime_type in (
            "text/plain",
            "text/markdown",
        ):
            return DocumentCategory.UNSTRUCTURED.value
        
        if mime_type in (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.ms-powerpoint",
            "image/png",
            "image/jpeg",
            "image/tiff",
            "image/bmp",
            "image/gif",
            "image/webp",
        ):
            return DocumentCategory.MULTIMODAL.value
        
        return None
    
    def _is_scanned_pdf(self, file_path: Path) -> bool:
        """检测 PDF 是否为扫描件
        
        使用 PyMuPDF 提取每页文本，若平均每页文本量低于阈值则判定为扫描件。
        """
        if not PYMUPDF_AVAILABLE:
            logger.warning("PyMuPDF 未安装，无法检测 PDF 是否为扫描件，默认按可复制 PDF 处理")
            return False
        
        try:
            doc = fitz.open(str(file_path))
            total_text_len = 0
            page_count = doc.page_count
            
            if page_count == 0:
                return False
            
            for page in doc:
                text = page.get_text().strip()
                total_text_len += len(text)
            
            doc.close()
            
            avg_text_len = total_text_len / page_count
            return avg_text_len < self.scanned_pdf_threshold
            
        except Exception as e:
            logger.warning(f"PDF 扫描件检测失败: {e}")
            return False
    
    @staticmethod
    def _compute_md5(file_path: Path) -> str:
        """计算文件 MD5 哈希"""
        hash_md5 = hashlib.md5()
        try:
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    hash_md5.update(chunk)
        except Exception as e:
            logger.warning(f"计算文件 MD5 失败: {e}")
        return hash_md5.hexdigest()
    
    def classify_batch(self, file_paths: List[Path | str]) -> List[Dict[str, Any]]:
        """批量分类文件"""
        return [self.classify(fp) for fp in file_paths]


# 全局默认分类器实例
document_classifier = DocumentClassifier()


def classify_document(file_path: Path | str) -> Dict[str, Any]:
    """便捷函数：单文件分类"""
    return document_classifier.classify(file_path)


def classify_documents(file_paths: List[Path | str]) -> List[Dict[str, Any]]:
    """便捷函数：批量文件分类"""
    return document_classifier.classify_batch(file_paths)
