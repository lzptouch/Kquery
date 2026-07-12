"""XML文件加载器。

支持 .xml 文件格式，提供结构化XML解析和文本提取。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, List, Dict, Any
import xml.etree.ElementTree as ET
from xml.parsers.expat import ExpatError

from app.services.file_loaders.base import BaseLoader, DocumentChunk, ChunkType, FileLoadError


class XMLLoader(BaseLoader):
    """XML文件加载器。"""
    
    @property
    def supported_extensions(self) -> list[str]:
        """支持的文件扩展名列表。"""
        return [".xml", ".xsl", ".xsd", ".dtd"]
    
    def load(self, file_path: Path) -> str:
        """加载XML文件为纯文本。"""
        self._validate_file(file_path)
        
        try:
            # 解析XML文件
            tree = ET.parse(file_path)
            root = tree.getroot()
            
            # 提取所有文本内容
            text_content = self._extract_text_from_element(root)
            
            # 更新元数据
            self.metadata.update(self.get_document_metadata(file_path))
            self.metadata["xml_root_tag"] = root.tag
            
            return text_content
            
        except ExpatError as e:
            raise FileLoadError(f"XML parsing error: {e}", file_path) from e
        except Exception as e:
            raise FileLoadError(f"Failed to load XML file: {e}", file_path) from e
    
    def load_stream(self, file_path: Path) -> Iterator[str]:
        """流式加载XML文件。"""
        self._validate_file(file_path)
        
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
            
            # 按元素流式返回文本
            for element in root.iter():
                if element.text and element.text.strip():
                    yield element.text.strip()
                    
        except Exception as e:
            raise FileLoadError(f"Failed to stream XML file: {e}", file_path) from e
    
    def load_document_chunks(self, file_path: Path) -> List[DocumentChunk]:
        """加载XML文件为结构化文档块列表。"""
        self._validate_file(file_path)
        
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
            
            chunks = []
            
            # 递归处理XML元素
            def process_element(element: ET.Element, path: str = "", depth: int = 0):
                # 获取元素文本内容
                text = element.text.strip() if element.text and element.text.strip() else ""
                
                # 获取元素属性
                attributes = {f"@{k}": v for k, v in element.attrib.items()}
                
                # 构建完整的元素路径
                element_path = f"{path}/{element.tag}" if path else element.tag
                
                if text:
                    # 创建文本块
                    metadata = self.metadata.copy()
                    metadata.update({
                        "xml_element_path": element_path,
                        "xml_element_tag": element.tag,
                        "xml_attributes": attributes,
                        "xml_depth": depth
                    })
                    
                    chunks.append(DocumentChunk(
                        content=text,
                        metadata=metadata,
                        chunk_type=ChunkType.TEXT,
                        source_file=str(file_path),
                        page_number=None,
                        heading_level=depth
                    ))
                
                # 递归处理子元素
                for child in element:
                    process_element(child, element_path, depth + 1)
            
            # 开始处理根元素
            process_element(root)
            
            # 如果没有提取到内容，返回整个文件内容
            if not chunks:
                return super().load_document_chunks(file_path)
            
            return chunks
            
        except Exception as e:
            raise FileLoadError(f"Failed to load XML chunks: {e}", file_path) from e
    
    def get_document_metadata(self, file_path: Path) -> Dict[str, Any]:
        """获取XML文档元数据。"""
        metadata = super().get_document_metadata(file_path)
        
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
            
            metadata.update({
                "xml_root_tag": root.tag,
                "xml_element_count": len(list(root.iter())),
                "xml_depth": self._get_max_depth(root)
            })
            
        except Exception:
            # 如果XML解析失败，只返回基础元数据
            pass
            
        return metadata
    
    def supports_structured_content(self) -> bool:
        """XML支持结构化内容提取。"""
        return True
    
    def _extract_text_from_element(self, element: ET.Element) -> str:
        """从XML元素中提取文本内容。"""
        # 获取当前元素的文本
        text_parts = []
        
        if element.text and element.text.strip():
            text_parts.append(element.text.strip())
        
        # 递归处理子元素
        for child in element:
            child_text = self._extract_text_from_element(child)
            if child_text:
                text_parts.append(child_text)
        
        # 获取尾部文本
        if element.tail and element.tail.strip():
            text_parts.append(element.tail.strip())
        
        return " ".join(text_parts)
    
    def _get_max_depth(self, element: ET.Element, current_depth: int = 0) -> int:
        """获取XML树的最大深度。"""
        if not list(element):
            return current_depth
        
        max_depth = current_depth
        for child in element:
            child_depth = self._get_max_depth(child, current_depth + 1)
            max_depth = max(max_depth, child_depth)
        
        return max_depth


# 加载器会自动通过模块化系统注册