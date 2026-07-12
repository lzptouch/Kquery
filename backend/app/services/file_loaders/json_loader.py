"""JSON文件加载器。

支持 .json 文件格式，提供JSON文档解析和文本提取。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, List, Dict, Any
import json
from json import JSONDecodeError

from app.services.file_loaders.base import BaseLoader, DocumentChunk, ChunkType, FileLoadError


class JSONLoader(BaseLoader):
    """JSON文件加载器。"""
    
    @property
    def supported_extensions(self) -> list[str]:
        """支持的文件扩展名列表。"""
        return [".json", ".jsonl"]
    
    def load(self, file_path: Path) -> str:
        """加载JSON文件为纯文本。"""
        self._validate_file(file_path)
        
        try:
            # 读取JSON文件内容
            content = self._read_text_with_fallback(file_path)
            
            # 解析JSON
            json_data = json.loads(content)
            
            # 提取文本内容
            text_content = self._extract_text_from_json(json_data)
            
            # 更新元数据
            self.metadata.update(self.get_document_metadata(file_path))
            self.metadata["json_type"] = type(json_data).__name__
            
            return text_content
            
        except JSONDecodeError as e:
            raise FileLoadError(f"JSON decoding error: {e}", file_path) from e
        except Exception as e:
            raise FileLoadError(f"Failed to load JSON file: {e}", file_path) from e
    
    def load_stream(self, file_path: Path) -> Iterator[str]:
        """流式加载JSON文件。"""
        self._validate_file(file_path)
        
        try:
            content = self._read_text_with_fallback(file_path)
            
            # 检查是否为JSONL格式（每行一个JSON对象）
            if file_path.suffix.lower() == ".jsonl":
                for line_num, line in enumerate(content.splitlines(), 1):
                    line = line.strip()
                    if line:
                        try:
                            json_obj = json.loads(line)
                            yield f"JSONL Line {line_num}: {self._extract_text_from_json(json_obj)}"
                        except JSONDecodeError:
                            yield f"JSONL Line {line_num}: {line}"
            else:
                # 普通JSON文件
                json_data = json.loads(content)
                for text in self._extract_text_chunks(json_data):
                    yield text
                    
        except Exception as e:
            raise FileLoadError(f"Failed to stream JSON file: {e}", file_path) from e
    
    def load_document_chunks(self, file_path: Path) -> List[DocumentChunk]:
        """加载JSON文件为结构化文档块列表。"""
        self._validate_file(file_path)
        
        try:
            content = self._read_text_with_fallback(file_path)
            json_data = json.loads(content)
            
            chunks = []
            
            if isinstance(json_data, dict):
                # 处理字典类型的JSON
                chunks.extend(self._process_json_object(json_data, file_path, ""))
            elif isinstance(json_data, list):
                # 处理数组类型的JSON
                for i, item in enumerate(json_data):
                    if isinstance(item, (dict, list)):
                        chunks.extend(self._process_json_object(item, file_path, f"[{i}]"))
                    else:
                        chunks.append(self._create_text_chunk(
                            str(item), file_path, f"[{i}]", ChunkType.TEXT
                        ))
            else:
                # 基本类型
                chunks.append(self._create_text_chunk(
                    str(json_data), file_path, "", ChunkType.TEXT
                ))
            
            # 如果没有提取到内容，返回整个文件内容
            if not chunks:
                return super().load_document_chunks(file_path)
            
            return chunks
            
        except Exception as e:
            raise FileLoadError(f"Failed to load JSON chunks: {e}", file_path) from e
    
    def get_document_metadata(self, file_path: Path) -> Dict[str, Any]:
        """获取JSON文档元数据。"""
        metadata = super().get_document_metadata(file_path)
        
        try:
            content = self._read_text_with_fallback(file_path)
            json_data = json.loads(content)
            
            metadata.update({
                "json_type": type(json_data).__name__,
                "json_size": len(content),
                "json_structure": self._analyze_json_structure(json_data)
            })
            
        except Exception:
            # 如果JSON解析失败，只返回基础元数据
            pass
            
        return metadata
    
    def supports_structured_content(self) -> bool:
        """JSON支持结构化内容提取。"""
        return True
    
    def _extract_text_from_json(self, data: Any, path: str = "") -> str:
        """从JSON数据中提取文本内容。"""
        if isinstance(data, dict):
            text_parts = []
            for key, value in data.items():
                current_path = f"{path}.{key}" if path else key
                if isinstance(value, (dict, list)):
                    text_parts.append(self._extract_text_from_json(value, current_path))
                else:
                    text_parts.append(f"{current_path}: {value}")
            return " ".join(text_parts)
        elif isinstance(data, list):
            text_parts = []
            for i, item in enumerate(data):
                current_path = f"{path}[{i}]"
                if isinstance(item, (dict, list)):
                    text_parts.append(self._extract_text_from_json(item, current_path))
                else:
                    text_parts.append(f"{current_path}: {item}")
            return " ".join(text_parts)
        else:
            return str(data)
    
    def _extract_text_chunks(self, data: Any, path: str = "") -> Iterator[str]:
        """从JSON数据中提取文本块。"""
        if isinstance(data, dict):
            for key, value in data.items():
                current_path = f"{path}.{key}" if path else key
                if isinstance(value, (dict, list)):
                    yield from self._extract_text_chunks(value, current_path)
                else:
                    yield f"{current_path}: {value}"
        elif isinstance(data, list):
            for i, item in enumerate(data):
                current_path = f"{path}[{i}]"
                if isinstance(item, (dict, list)):
                    yield from self._extract_text_chunks(item, current_path)
                else:
                    yield f"{current_path}: {item}"
        else:
            yield str(data)
    
    def _process_json_object(self, obj: dict, file_path: Path, path: str) -> List[DocumentChunk]:
        """处理JSON对象并创建文档块。"""
        chunks = []
        
        for key, value in obj.items():
            current_path = f"{path}.{key}" if path else key
            
            if isinstance(value, dict):
                # 嵌套对象，创建标题块
                chunks.append(self._create_text_chunk(
                    f"{key} 对象", file_path, current_path, ChunkType.HEADING, 1
                ))
                chunks.extend(self._process_json_object(value, file_path, current_path))
            elif isinstance(value, list):
                # 数组，创建标题块
                chunks.append(self._create_text_chunk(
                    f"{key} 数组 ({len(value)} 项)", file_path, current_path, ChunkType.HEADING, 1
                ))
                
                # 处理数组项
                for i, item in enumerate(value):
                    array_item_path = f"{current_path}[{i}]"
                    if isinstance(item, dict):
                        chunks.extend(self._process_json_object(item, file_path, array_item_path))
                    else:
                        chunks.append(self._create_text_chunk(
                            str(item), file_path, array_item_path, ChunkType.TEXT
                        ))
            else:
                # 基本值，创建文本块
                chunks.append(self._create_text_chunk(
                    f"{key}: {value}", file_path, current_path, ChunkType.TEXT
                ))
        
        return chunks
    
    def _create_text_chunk(self, content: str, file_path: Path, path: str, 
                          chunk_type: ChunkType, heading_level: int = None) -> DocumentChunk:
        """创建文本块。"""
        metadata = self.metadata.copy()
        metadata.update({
            "json_path": path,
            "json_value_type": type(content).__name__
        })
        
        return DocumentChunk(
            content=content,
            metadata=metadata,
            chunk_type=chunk_type,
            source_file=str(file_path),
            page_number=None,
            heading_level=heading_level
        )
    
    def _analyze_json_structure(self, data: Any) -> Dict[str, Any]:
        """分析JSON结构。"""
        if isinstance(data, dict):
            return {
                "type": "object",
                "key_count": len(data),
                "keys": list(data.keys())
            }
        elif isinstance(data, list):
            return {
                "type": "array",
                "length": len(data),
                "item_types": [type(item).__name__ for item in data]
            }
        else:
            return {
                "type": type(data).__name__,
                "value": str(data)
            }


# 加载器会自动通过模块化系统注册