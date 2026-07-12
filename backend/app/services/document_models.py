"""多类别异构文档统一知识图谱构建 - 规范化文档数据模型

定义所有文档解析后的统一输出结构 NormalizedDocument，
作为结构化文档（Excel/CSV）、半结构化文档（Word/PDF/HTML）和非结构化/多模态文档
的通用中间表示，供后续差异化抽取使用。

数据类：
- NormalizedDocument: 文档级统一输出
- NormalizedTable: 结构化表格
- NormalizedSection: 半结构化/非结构化内容块（标题、段落、表格、图片）
- NormalizedImage: 图片及其描述

作者：KQuery Pro Team
版本：1.0.0
更新：2026-07-05
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from enum import Enum


class DocumentCategory(str, Enum):
    """文档结构化类别枚举"""
    STRUCTURED = "structured"              # 高结构化：Excel/CSV/数据库导出表
    SEMI_STRUCTURED = "semi_structured" # 半结构化：Word/可复制 PDF/HTML/JSON/XML
    UNSTRUCTURED = "unstructured"         # 非结构化：纯文本/Markdown
    MULTIMODAL = "multimodal"             # 多模态：扫描 PDF/图片/PPT


class ContentType(str, Enum):
    """段落内容类型枚举"""
    HEADING = "heading"       # 标题
    PARAGRAPH = "paragraph"   # 段落
    TABLE = "table"           # 表格
    IMAGE = "image"           # 图片


@dataclass
class NormalizedTable:
    """结构化表格数据
    
    Attributes:
        table_id: 表格唯一标识
        sheet_name: Sheet/工作表名称
        headers: 表头列表
        rows: 数据行（字典列表，key 为表头）
        position_meta: 位置元信息（页码、行范围、列范围等）
    """
    table_id: str
    sheet_name: str
    headers: List[str] = field(default_factory=list)
    rows: List[Dict[str, Any]] = field(default_factory=list)
    position_meta: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        if not self.table_id:
            self.table_id = f"table-{uuid.uuid4().hex[:12]}"
    
    @property
    def row_count(self) -> int:
        return len(self.rows)
    
    @property
    def col_count(self) -> int:
        return len(self.headers)


@dataclass
class NormalizedSection:
    """半结构化/非结构化文档内容块
    
    Attributes:
        section_id: 内容块唯一标识
        content_type: 内容类型：heading/paragraph/table/image
        heading_level: 标题级别 H1-H6（仅标题有效）
        raw_text: 归一化 Markdown 文本
        position_meta: 位置元信息（页码、行范围等）
        parent_section_id: 父章节 ID（用于层级关联）
    """
    section_id: str
    content_type: str = "paragraph"
    heading_level: Optional[int] = None
    raw_text: str = ""
    position_meta: Dict[str, Any] = field(default_factory=dict)
    parent_section_id: Optional[str] = None
    
    def __post_init__(self):
        if not self.section_id:
            self.section_id = f"sec-{uuid.uuid4().hex[:12]}"


@dataclass
class NormalizedImage:
    """文档内嵌图片数据
    
    Attributes:
        image_id: 图片唯一标识
        image_path: 图片本地存储路径
        caption: 图注/标题
        ocr_text: OCR 识别文字
        description: 多模态模型生成的描述
        related_section_id: 关联段落 ID
    """
    image_id: str
    image_path: str
    caption: Optional[str] = None
    ocr_text: Optional[str] = None
    description: Optional[str] = None
    related_section_id: Optional[str] = None
    
    def __post_init__(self):
        if not self.image_id:
            self.image_id = f"img-{uuid.uuid4().hex[:12]}"


@dataclass
class NormalizedDocument:
    """统一归一化文档输出
    
    所有文档加载器解析后统一输出此结构，供后续 DocumentClassifier 路由和
    差异化抽取引擎使用。
    
    Attributes:
        doc_id: 文档唯一标识
        doc_type: 文档结构化类别
        source_file: 原始文件路径
        metadata: 文件元信息（大小、时间、MD5、MIME 等）
        tables: 结构化表格数据（结构化文档）
        sections: 分层内容块（半结构化/非结构化文档）
        images: 图片及其描述（多模态文档）
    """
    doc_id: str
    doc_type: str = DocumentCategory.UNSTRUCTURED.value
    source_file: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    tables: List[NormalizedTable] = field(default_factory=list)
    sections: List[NormalizedSection] = field(default_factory=list)
    images: List[NormalizedImage] = field(default_factory=list)
    
    def __post_init__(self):
        if not self.doc_id:
            self.doc_id = f"doc-{uuid.uuid4().hex[:12]}"
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典，便于 MongoDB 缓存和 JSON 序列化"""
        return {
            "doc_id": self.doc_id,
            "doc_type": self.doc_type,
            "source_file": self.source_file,
            "metadata": self.metadata,
            "tables": [
                {
                    "table_id": t.table_id,
                    "sheet_name": t.sheet_name,
                    "headers": t.headers,
                    "rows": t.rows,
                    "position_meta": t.position_meta
                }
                for t in self.tables
            ],
            "sections": [
                {
                    "section_id": s.section_id,
                    "content_type": s.content_type,
                    "heading_level": s.heading_level,
                    "raw_text": s.raw_text,
                    "position_meta": s.position_meta,
                    "parent_section_id": s.parent_section_id
                }
                for s in self.sections
            ],
            "images": [
                {
                    "image_id": i.image_id,
                    "image_path": i.image_path,
                    "caption": i.caption,
                    "ocr_text": i.ocr_text,
                    "description": i.description,
                    "related_section_id": i.related_section_id
                }
                for i in self.images
            ]
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NormalizedDocument":
        """从字典重建 NormalizedDocument"""
        return cls(
            doc_id=data.get("doc_id", ""),
            doc_type=data.get("doc_type", DocumentCategory.UNSTRUCTURED.value),
            source_file=data.get("source_file", ""),
            metadata=data.get("metadata", {}),
            tables=[NormalizedTable(**t) for t in data.get("tables", [])],
            sections=[NormalizedSection(**s) for s in data.get("sections", [])],
            images=[NormalizedImage(**i) for i in data.get("images", [])]
        )
