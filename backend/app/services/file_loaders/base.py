"""文件加载器抽象基类和注册表。

定义文件加载器的标准接口和注册机制，支持根据文件扩展名自动选择加载器。

模块结构：
- BaseLoader: 所有文件加载器的抽象基类
- LoaderRegistry: 加载器注册表，管理加载器的注册和获取
- FileLoaderError: 文件加载相关异常类

使用示例：
    from app.core.file_loaders import LoaderRegistry
    
    # 根据文件路径自动获取对应的加载器
    loader = LoaderRegistry.get_loader(Path("document.pdf"))
    text = loader.load(Path("document.pdf"))
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Dict, Type, List, Optional, Any
from enum import Enum


class FileLoaderError(Exception):
    """文件加载器相关异常基类。
    
    Attributes:
        message: 错误消息
        file_path: 导致错误的文件路径（可选）
    """
    
    def __init__(self, message: str, file_path: Path | None = None):
        self.message = message
        self.file_path = file_path
        super().__init__(f"{message} (File: {file_path})" if file_path else message)


class UnsupportedFileTypeError(FileLoaderError):
    """不支持的文件类型异常。"""
    pass


class FileLoadError(FileLoaderError):
    """文件加载失败异常。"""
    pass


class ChunkType(Enum):
    """文档块类型枚举。"""
    TEXT = "text"           # 普通文本段落
    HEADING = "heading"     # 标题
    TABLE = "table"         # 表格
    LIST = "list"           # 列表
    CODE = "code"           # 代码块
    IMAGE = "image"         # 图片（仅描述）
    SECTION = "section"     # 章节分隔
    IMAGE_DESCRIPTION = "image_description"  # 图像描述
    TABLE_DESCRIPTION = "table_description"  # 表格描述


@dataclass
class DocumentChunk:
    """文档块数据结构。
    
    Attributes:
        content: 块内容
        metadata: 元数据字典
        chunk_type: 块类型
        source_file: 源文件路径
        page_number: 页码（仅对分页文档有效）
        char_offset: 字符偏移量（在原文档中的起始字符位置）
        heading_level: 标题级别（仅对标题块有效）
        image_path: 图像文件路径（如果是图像相关块）
        image_description: AI生成的图像描述（如果是图像相关块）
        image_hash: 图像哈希值（用于缓存）
        table_data: 表格数据（如果是表格相关块）
        table_description: AI生成的表格描述（如果是表格相关块）
        previous_chunk_id: 前一个文档块的ID（用于上下文关联）
        next_chunk_id: 后一个文档块的ID（用于上下文关联）
        chunk_id: 当前文档块的唯一ID
    """
    content: str
    metadata: Dict[str, Any]
    chunk_type: ChunkType
    source_file: str
    page_number: Optional[int] = None
    char_offset: Optional[int] = None
    heading_level: Optional[int] = None
    image_path: Optional[str] = None
    image_description: Optional[str] = None
    image_hash: Optional[str] = None
    table_data: Optional[Dict[str, Any]] = None
    table_description: Optional[str] = None
    previous_chunk_id: Optional[str] = None
    next_chunk_id: Optional[str] = None
    chunk_id: Optional[str] = None
    
    def __post_init__(self):
        """初始化后自动生成chunk_id（如果未提供）。"""
        if self.chunk_id is None:
            import uuid
            self.chunk_id = str(uuid.uuid4())
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式。"""
        result = {
            "content": self.content,
            "metadata": self.metadata,
            "chunk_type": self.chunk_type.value,
            "source_file": self.source_file,
            "page_number": self.page_number,
            "char_offset": self.char_offset,
            "heading_level": self.heading_level,
            "chunk_id": self.chunk_id
        }
        
        if self.image_path is not None:
            result["image_path"] = self.image_path
        if self.image_description is not None:
            result["image_description"] = self.image_description
        if self.image_hash is not None:
            result["image_hash"] = self.image_hash
        if self.table_data is not None:
            result["table_data"] = self.table_data
        if self.table_description is not None:
            result["table_description"] = self.table_description
        if self.previous_chunk_id is not None:
            result["previous_chunk_id"] = self.previous_chunk_id
        if self.next_chunk_id is not None:
            result["next_chunk_id"] = self.next_chunk_id
        
        return result
    
    def get_full_content(self) -> str:
        """获取包含图像/表格描述的完整内容。"""
        parts = [self.content]
        
        if self.image_description:
            parts.append(f"\n\n【图像描述】：{self.image_description}")
        
        if self.table_description:
            parts.append(f"\n\n【表格描述】：{self.table_description}")
        
        return "\n".join(parts)


class BaseLoader(ABC):
    """文件加载器抽象基类。
    
    所有文件加载器都必须继承此类并实现以下抽象方法：
    - load: 加载文件为纯文本
    - load_stream: 流式加载文件
    - supported_extensions: 支持的扩展名列表
    
    可选实现的方法：
    - load_document_chunks: 加载文档块（支持结构化内容）
    - get_document_metadata: 获取文档元数据
    - supports_ocr: 是否支持OCR
    - supports_structured_content: 是否支持结构化内容
    
    Attributes:
        metadata: 元数据字典，用于存储文件相关信息
        use_ocr: 是否启用OCR（默认为False）
        preserve_structure: 是否保留文档结构（默认为True）
    
    Example:
        class PDFLoader(BaseLoader):
            @property
            def supported_extensions(self) -> list[str]:
                return [".pdf"]
            
            def load(self, file_path: Path) -> str:
                # 实现 PDF 加载逻辑
                pass
            
            def load_stream(self, file_path: Path) -> Iterator[str]:
                # 实现流式加载逻辑
                pass
    """
    
    def __init__(self, use_ocr: bool = False, preserve_structure: bool = True):
        """初始化加载器。
        
        Args:
            use_ocr: 是否启用OCR（仅对支持OCR的加载器有效）
            preserve_structure: 是否保留文档结构
        """
        self.metadata: dict = {}
        self.use_ocr = use_ocr
        self.preserve_structure = preserve_structure
    
    @abstractmethod
    def load(self, file_path: Path) -> str:
        """加载文件为纯文本。
        
        Args:
            file_path: 文件路径
        
        Returns:
            文件的纯文本内容
        
        Raises:
            FileNotFoundError: 文件不存在
            FileLoadError: 加载失败
        """
        pass
    
    @abstractmethod
    def load_stream(self, file_path: Path) -> Iterator[str]:
        """流式加载文件。
        
        适用于大文件，避免一次性加载到内存。
        
        Args:
            file_path: 文件路径
        
        Yields:
            文件的文本片段
        
        Raises:
            FileNotFoundError: 文件不存在
            FileLoadError: 加载失败
        """
        pass
    
    @property
    @abstractmethod
    def supported_extensions(self) -> list[str]:
        """支持的文件扩展名列表（包含点号）。
        
        Returns:
            扩展名列表，例如：[".pdf", ".txt"]
        """
        pass
    
    def load_document_chunks(self, file_path: Path) -> List[DocumentChunk]:
        """加载文件为结构化文档块列表。
        
        默认实现将整个文件内容作为一个文本块返回。
        子类可以重写此方法以提供更精细的文档结构分析。
        
        Args:
            file_path: 文件路径
        
        Returns:
            文档块列表
        
        Raises:
            FileNotFoundError: 文件不存在
            FileLoadError: 加载失败
        """
        content = self.load(file_path)
        return [
            DocumentChunk(
                content=content,
                metadata=self.metadata.copy(),
                chunk_type=ChunkType.TEXT,
                source_file=str(file_path)
            )
        ]
    
    def get_document_metadata(self, file_path: Path) -> Dict[str, Any]:
        """获取文档元数据。
        
        默认实现返回文件基础信息。
        子类可以重写此方法以提供特定格式的元数据。
        
        Args:
            file_path: 文件路径
        
        Returns:
            文档元数据字典
        """
        self._validate_file(file_path)
        return {
            "file_path": str(file_path),
            "file_size": file_path.stat().st_size,
            "file_extension": file_path.suffix.lower(),
            "modified_time": file_path.stat().st_mtime,
            "loader_class": self.__class__.__name__
        }
    
    def supports_ocr(self) -> bool:
        """是否支持OCR功能。
        
        Returns:
            True 如果支持OCR，否则 False
        """
        return False
    
    def supports_structured_content(self) -> bool:
        """是否支持结构化内容提取。
        
        Returns:
            True 如果支持结构化内容，否则 False
        """
        return False
    
    def _validate_file(self, file_path: Path) -> None:
        """验证文件是否存在且可读。
        
        Args:
            file_path: 文件路径
        
        Raises:
            FileNotFoundError: 文件不存在
            FileLoadError: 文件不可读
        """
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        
        if not file_path.is_file():
            raise FileLoadError(f"Not a file: {file_path}", file_path)
        
        # 检查文件扩展名是否匹配
        ext = file_path.suffix.lower()
        if ext not in self.supported_extensions:
            raise UnsupportedFileTypeError(
                f"Unsupported file type: {ext}. Supported: {self.supported_extensions}",
                file_path
            )
    
    def _read_text_with_fallback(self, file_path: Path, encodings: list[str] | None = None) -> str:
        """尝试多种编码读取文本文件。
        
        Args:
            file_path: 文件路径
            encodings: 编码列表，默认为 ["utf-8", "gbk", "latin1"]
        
        Returns:
            文件内容
        
        Raises:
            FileLoadError: 所有编码都失败
        """
        if encodings is None:
            encodings = ["utf-8", "gbk", "latin1"]
        
        last_error: Exception | None = None
        
        for encoding in encodings:
            try:
                return file_path.read_text(encoding=encoding)
            except (UnicodeDecodeError, UnicodeError) as e:
                last_error = e
                continue
            except Exception as e:
                last_error = e
                break
        
        raise FileLoadError(
            f"Failed to read file with all encodings. Last error: {last_error}",
            file_path
        ) from last_error


class LoaderRegistry:
    """文件加载器注册表。
    
    单例模式，管理所有已注册的加载器，根据文件扩展名自动分配合适的加载器。
    
    使用示例：
        # 注册加载器
        @LoaderRegistry.register
        class PDFLoader(BaseLoader):
            ...
        
        # 获取加载器
        loader = LoaderRegistry.get_loader(Path("test.pdf"))
    """
    
    _instance: "LoaderRegistry" | None = None
    _loaders: Dict[str, Type[BaseLoader]] = {}
    
    def __new__(cls) -> "LoaderRegistry":
        """单例模式实现。"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    @classmethod
    def register(cls, loader_class: Type[BaseLoader]) -> Type[BaseLoader]:
        """注册文件加载器。
        
        可作为装饰器使用，自动将加载器注册到注册表中。
        
        Args:
            loader_class: 加载器类
        
        Returns:
            返回原类（便于作为装饰器使用）
        
        Example:
            @LoaderRegistry.register
            class PDFLoader(BaseLoader):
                @property
                def supported_extensions(self) -> list[str]:
                    return [".pdf"]
                
                def load(self, file_path: Path) -> str:
                    # PDF 加载逻辑
                    pass
        """
        # 实例化以获取 supported_extensions 的值（因为它是 property）
        temp_instance = loader_class()
        extensions = temp_instance.supported_extensions
        for ext in extensions:
            ext_lower = ext.lower()
            if ext_lower in cls._loaders:
                # 如果已存在相同扩展名的加载器，发出警告
                import warnings
                warnings.warn(
                    f"Loader for extension '{ext_lower}' already registered. "
                    f"Replacing {cls._loaders[ext_lower].__name__} with {loader_class.__name__}"
                )
            cls._loaders[ext_lower] = loader_class
        
        return loader_class
    
    @classmethod
    def get_loader(cls, file_path: Path, use_ocr: bool = False, preserve_structure: bool = True) -> BaseLoader:
        """根据文件路径获取对应的加载器实例。
        
        Args:
            file_path: 文件路径
            use_ocr: 是否启用OCR（仅对支持OCR的加载器有效）
            preserve_structure: 是否保留文档结构
        
        Returns:
            对应的加载器实例
        
        Raises:
            UnsupportedFileTypeError: 不支持的文件类型
        
        Example:
            loader = LoaderRegistry.get_loader(Path("document.pdf"))
            text = loader.load(Path("document.pdf"))
        """
        ext = file_path.suffix.lower()
        loader_class = cls._loaders.get(ext)
        
        if loader_class is None:
            supported = sorted(set(cls._loaders.keys()))
            raise UnsupportedFileTypeError(
                f"Unsupported file type: {ext}. "
                f"Supported types: {', '.join(supported)}",
                file_path
            )
        
        return loader_class(use_ocr=use_ocr, preserve_structure=preserve_structure)
    
    @classmethod
    def get_supported_extensions(cls) -> list[str]:
        """获取所有支持的扩展名列表。
        
        Returns:
            排序后的扩展名列表
        """
        return sorted(set(cls._loaders.keys()))
    
    @classmethod
    def is_supported(cls, file_path: Path) -> bool:
        """检查文件类型是否被支持。
        
        Args:
            file_path: 文件路径
        
        Returns:
            True 如果文件类型被支持，否则 False
        """
        ext = file_path.suffix.lower()
        return ext in cls._loaders
    
    @classmethod
    def clear_registry(cls) -> None:
        """清空所有注册的加载器（主要用于测试）。"""
        cls._loaders.clear()


def link_document_chunks(chunks: List[DocumentChunk]) -> List[DocumentChunk]:
    """关联文档块之间的上下文关系。
    
    为每个文档块设置前一个和后一个块的ID，
    以便在检索时可以获取上下文信息。
    
    Args:
        chunks: 文档块列表
        
    Returns:
        关联后的文档块列表
    """
    if not chunks:
        return chunks
    
    for i in range(len(chunks)):
        if i > 0:
            chunks[i].previous_chunk_id = chunks[i-1].chunk_id
        if i < len(chunks) - 1:
            chunks[i].next_chunk_id = chunks[i+1].chunk_id
    
    return chunks


def get_context_chunks(
    chunks: List[DocumentChunk],
    target_chunk: DocumentChunk,
    context_size: int = 1
) -> List[DocumentChunk]:
    """获取目标文档块的上下文文档块。
    
    Args:
        chunks: 所有文档块列表
        target_chunk: 目标文档块
        context_size: 上下文大小（前N个和后N个块）
        
    Returns:
        上下文文档块列表
    """
    chunk_dict = {chunk.chunk_id: chunk for chunk in chunks}
    context_chunks = []
    
    current = target_chunk
    for _ in range(context_size):
        if current.previous_chunk_id and current.previous_chunk_id in chunk_dict:
            prev_chunk = chunk_dict[current.previous_chunk_id]
            context_chunks.insert(0, prev_chunk)
            current = prev_chunk
        else:
            break
    
    current = target_chunk
    for _ in range(context_size):
        if current.next_chunk_id and current.next_chunk_id in chunk_dict:
            next_chunk = chunk_dict[current.next_chunk_id]
            context_chunks.append(next_chunk)
            current = next_chunk
        else:
            break
    
    return context_chunks