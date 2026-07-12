"""Word文档加载器。

支持 .docx 文件格式，增强功能：
- 提取Word内部图像
- 对图像进行AI理解和描述生成
- 提取和处理表格
- 将图像和表格与周围文本关联
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Iterator, List, Dict, Any

from app.services.file_loaders.base import (
    BaseLoader, DocumentChunk, ChunkType, FileLoadError,
    link_document_chunks
)
from app.core.logger import get_logger
from app.services.image_understanding import image_understanding_service
from app.services.table_understanding import table_understanding_service

logger = get_logger(__name__)


class WordLoader(BaseLoader):
    """Word文档加载器（增强版）。"""
    
    def __init__(
        self,
        use_ocr: bool = False,
        preserve_structure: bool = True,
        extract_images: bool = True,
        understand_images: bool = True,
        extract_tables: bool = True
    ):
        """初始化Word加载器。
        
        Args:
            use_ocr: 是否启用OCR
            preserve_structure: 是否保留文档结构
            extract_images: 是否提取Word内部图像
            understand_images: 是否对提取的图像进行AI理解
            extract_tables: 是否提取和处理表格
        """
        super().__init__(use_ocr=use_ocr, preserve_structure=preserve_structure)
        self.extract_images = extract_images
        self.understand_images = understand_images
        self.extract_tables = extract_tables
        self._temp_dir: Optional[Path] = None
    
    @property
    def supported_extensions(self) -> list[str]:
        """支持的文件扩展名列表。"""
        return [".docx"]
    
    def supports_structured_content(self) -> bool:
        """是否支持结构化内容提取。"""
        return True
    
    def _ensure_temp_dir(self):
        """确保临时目录存在。"""
        if self._temp_dir is None:
            self._temp_dir = Path(tempfile.mkdtemp(prefix="kquery_word_"))
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
        """加载Word文件为纯文本。"""
        self._validate_file(file_path)
        
        try:
            from docx import Document
            
            doc = Document(file_path)
            content_parts = []
            
            for paragraph in doc.paragraphs:
                if paragraph.text.strip():
                    content_parts.append(paragraph.text)
            
            if self.extract_tables:
                for table_idx, table in enumerate(doc.tables):
                    table_content = self._extract_table_text(table)
                    if table_content:
                        content_parts.append(f"\n表格 {table_idx + 1}:\n{table_content}")
            
            full_content = "\n\n".join(content_parts)
            
            self.metadata.update(self.get_document_metadata(file_path))
            self.metadata.update({
                "paragraph_count": len([p for p in doc.paragraphs if p.text.strip()]),
                "table_count": len(doc.tables),
                "extract_tables": self.extract_tables
            })
            
            return full_content
            
        except ImportError:
            raise FileLoadError(
                "Word加载需要安装python-docx: pip install python-docx",
                file_path
            )
        except Exception as e:
            raise FileLoadError(f"Failed to load Word: {e}", file_path) from e
    
    def load_stream(self, file_path: Path) -> Iterator[str]:
        """流式加载Word文件（按段落）。"""
        self._validate_file(file_path)
        
        try:
            from docx import Document
            
            doc = Document(file_path)
            
            for paragraph in doc.paragraphs:
                if paragraph.text.strip():
                    yield paragraph.text
            
            if self.extract_tables:
                for table_idx, table in enumerate(doc.tables):
                    table_content = self._extract_table_text(table)
                    if table_content:
                        yield f"表格 {table_idx + 1}:\n{table_content}"
            
        except ImportError:
            raise FileLoadError(
                "Word加载需要安装python-docx: pip install python-docx",
                file_path
            )
        except Exception as e:
            raise FileLoadError(f"Failed to stream Word: {e}", file_path) from e
    
    def load_document_chunks(self, file_path: Path) -> list[DocumentChunk]:
        """加载Word文件为文档块列表，并提取和处理图像、表格。"""
        self._validate_file(file_path)
        
        try:
            from docx import Document
            
            doc = Document(file_path)
            chunks = []
            
            self._ensure_temp_dir()
            
            paragraph_chunks = self._process_paragraphs(doc.paragraphs, file_path)
            chunks.extend(paragraph_chunks)
            
            if self.extract_images:
                image_chunks = self._extract_and_process_images(doc, file_path)
                chunks.extend(image_chunks)
            
            if self.extract_tables:
                table_chunks = self._extract_and_process_tables(doc.tables, file_path)
                chunks.extend(table_chunks)
            
            chunks = link_document_chunks(chunks)
            
            self.metadata.update(self.get_document_metadata(file_path))
            self.metadata.update({
                "paragraph_count": len([p for p in doc.paragraphs if p.text.strip()]),
                "table_count": len(doc.tables),
                "extract_images": self.extract_images,
                "understand_images": self.understand_images,
                "extract_tables": self.extract_tables
            })
            
            return chunks
            
        except ImportError:
            raise FileLoadError(
                "Word加载需要安装python-docx: pip install python-docx",
                file_path
            )
        except Exception as e:
            raise FileLoadError(f"Failed to load Word chunks: {e}", file_path) from e
        finally:
            self._cleanup_temp_dir()
    
    def _process_paragraphs(
        self,
        paragraphs,
        source_file: Path
    ) -> List[DocumentChunk]:
        """处理Word段落。
        
        Args:
            paragraphs: Word段落列表
            source_file: 源文件路径
            
        Returns:
            文档块列表
        """
        chunks = []
        current_text = []
        
        for paragraph in paragraphs:
            if paragraph.text.strip():
                current_text.append(paragraph.text)
        
        if current_text:
            text_chunk = DocumentChunk(
                content="\n".join(current_text),
                metadata=self.metadata.copy(),
                chunk_type=ChunkType.TEXT,
                source_file=str(source_file),
                page_number=None,  # Word 无明确页码信息
                char_offset=0
            )
            chunks.append(text_chunk)
        
        return chunks
    
    def _extract_and_process_images(
        self,
        doc,
        source_file: Path
    ) -> List[DocumentChunk]:
        """提取并处理Word文档中的图像。
        
        Args:
            doc: Word文档对象
            source_file: 源文件路径
            
        Returns:
            图像相关的文档块列表
        """
        chunks = []
        
        try:
            from docx.oxml.table import CT_Tbl
            from docx.oxml.text.paragraph import CT_P
            from docx.table import Table
            from docx.text.paragraph import Paragraph
            
            img_idx = 0
            
            for element in doc.element.body:
                if isinstance(element, CT_P):
                    paragraph = Paragraph(element, doc)
                    img_idx = self._process_paragraph_images(
                        paragraph, img_idx, source_file, chunks
                    )
                elif isinstance(element, CT_Tbl):
                    table = Table(element, doc)
                    for row in table.rows:
                        for cell in row.cells:
                            for paragraph in cell.paragraphs:
                                img_idx = self._process_paragraph_images(
                                    paragraph, img_idx, source_file, chunks
                                )
            
        except Exception as e:
            logger.warning(f"提取Word图像失败: {e}")
        
        return chunks
    
    def _process_paragraph_images(
        self,
        paragraph,
        img_idx: int,
        source_file: Path,
        chunks: List[DocumentChunk]
    ) -> int:
        """处理单个段落中的图像。
        
        Args:
            paragraph: Word段落对象
            img_idx: 当前图像索引
            source_file: 源文件路径
            chunks: 文档块列表
            
        Returns:
            更新后的图像索引
        """
        try:
            for run in paragraph.runs:
                for inline in run._element.findall('.//pic:pic', namespaces={
                    'pic': 'http://schemas.openxmlformats.org/drawingml/2006/picture'
                }):
                    img_idx += 1
                    
                    blip = inline.find('.//a:blip', namespaces={
                        'a': 'http://schemas.openxmlformats.org/drawingml/2006/main'
                    })
                    
                    if blip is not None:
                        embed = blip.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed')
                        if embed:
                            image_part = None
                            for rel in paragraph.part.rels.values():
                                if rel.rId == embed:
                                    image_part = rel.target_part
                                    break
                            
                            if image_part:
                                image_bytes = image_part.blob
                                image_ext = self._get_image_extension(image_bytes)
                                
                                image_path = self._temp_dir / f"img_{img_idx}.{image_ext}"
                                
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
                                
                                chunk_content = f"Word图像 {img_idx}"
                                if image_description:
                                    chunk_content += f"\n\n图像描述: {image_description}"
                                
                                image_chunk = DocumentChunk(
                                    content=chunk_content,
                                    metadata=self.metadata.copy(),
                                    chunk_type=ChunkType.IMAGE_DESCRIPTION,
                                    source_file=str(source_file),
                                    image_path=str(image_path),
                                    image_description=image_description,
                                    image_hash=image_hash
                                )
                                chunks.append(image_chunk)
            
        except Exception as e:
            logger.warning(f"处理段落图像失败: {e}")
        
        return img_idx
    
    def _get_image_extension(self, image_bytes: bytes) -> str:
        """根据图像字节判断图像格式。
        
        Args:
            image_bytes: 图像字节数据
            
        Returns:
            图像扩展名
        """
        if image_bytes.startswith(b'\xff\xd8\xff'):
            return 'jpg'
        elif image_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
            return 'png'
        elif image_bytes.startswith(b'GIF8'):
            return 'gif'
        elif image_bytes.startswith(b'II*\x00') or image_bytes.startswith(b'MM\x00*'):
            return 'tiff'
        elif image_bytes.startswith(b'BM'):
            return 'bmp'
        else:
            return 'png'
    
    def _extract_and_process_tables(
        self,
        tables,
        source_file: Path
    ) -> List[DocumentChunk]:
        """提取并处理Word文档中的表格。
        
        Args:
            tables: Word表格列表
            source_file: 源文件路径
            
        Returns:
            表格相关的文档块列表
        """
        chunks = []
        
        for table_idx, table in enumerate(tables):
            table_data = self._extract_table_data(table)
            table_content = self._extract_table_text(table)
            
            table_description = None
            try:
                if table_data and "rows" in table_data:
                    result = table_understanding_service.understand_table(table_data)
                    if result.success:
                        table_description = result.description
            except Exception as e:
                logger.warning(f"表格理解失败: {e}")
            
            chunk_content = f"Word表格 {table_idx + 1}\n\n{table_content}"
            if table_description:
                chunk_content += f"\n\n表格描述: {table_description}"
            
            table_chunk = DocumentChunk(
                content=chunk_content,
                metadata=self.metadata.copy(),
                chunk_type=ChunkType.TABLE_DESCRIPTION,
                source_file=str(source_file),
                table_data=table_data,
                table_description=table_description
            )
            chunks.append(table_chunk)
        
        return chunks
    
    def _extract_table_data(self, table) -> Dict[str, Any]:
        """提取表格结构化数据。
        
        Args:
            table: Word表格对象
            
        Returns:
            表格结构化数据
        """
        data = {
            "rows": [],
            "columns": 0
        }
        
        try:
            rows = []
            for row in table.rows:
                cells = []
                for cell in row.cells:
                    cells.append(cell.text.strip())
                rows.append(cells)
            
            data["rows"] = rows
            if rows:
                data["columns"] = len(rows[0])
            
        except Exception as e:
            logger.warning(f"提取表格数据失败: {e}")
        
        return data
    
    def _extract_table_text(self, table) -> str:
        """提取表格文本内容。
        
        Args:
            table: Word表格对象
            
        Returns:
            表格文本内容
        """
        lines = []
        
        try:
            for row in table.rows:
                row_cells = []
                for cell in row.cells:
                    row_cells.append(cell.text.strip())
                lines.append(" | ".join(row_cells))
            
        except Exception as e:
            logger.warning(f"提取表格文本失败: {e}")
        
        return "\n".join(lines)
