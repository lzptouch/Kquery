"""文件加载器模块。

提供多种文件格式的加载器，支持按需加载。
所有加载器都遵循统一的接口（BaseLoader），通过 LoaderRegistry 自动管理。

主要组件：
- BaseLoader: 抽象基类，定义加载器标准接口
- LoaderRegistry: 注册表，根据文件扩展名自动分配加载器
- 各种具体加载器实现（PDFLoader, WordLoader, XMLLoader, JSONLoader, ImageLoader 等）

使用示例：
    from pathlib import Path
    from app.services.file_loaders import LoaderRegistry
    
    # 自动获取并使用的加载器
    loader = LoaderRegistry.get_loader(Path("document.pdf"))
    text = loader.load(Path("document.pdf"))
    
    # 或者直接使用特定加载器
    from app.services.file_loaders.pdf_loader import PDFLoader
    pdf_loader = PDFLoader()
    text = pdf_loader.load(Path("document.pdf"))

支持的格式：
- PDF (.pdf) - 支持扫描版 OCR
- Word (.docx, .doc)
- Excel (.xlsx, .xls) - 按 Sheet 分块，保留表格结构
- PowerPoint (.pptx, .ppt) - 按 Slide 分块
- HTML (.html, .htm) - 结构化分块
- Markdown (.md, .markdown) - 按标题分块
- 文本 (.txt) - 自动编码检测
- XML (.xml, .xsl, .xsd, .dtd) - 结构化XML解析
- JSON (.json, .jsonl) - JSON文档解析
- 图片 (.jpg, .jpeg, .png, .bmp, .tiff, .tif, .gif, .webp) - OCR文字识别

模块化加载：
加载器按需加载，只有在需要时才会导入相应的模块。
"""

from app.services.file_loaders.base import (
    BaseLoader,
    LoaderRegistry,
    FileLoaderError,
    UnsupportedFileTypeError,
    FileLoadError,
    DocumentChunk,
    ChunkType,
)

# 定义加载器模块映射
LOADER_MODULES = {
    # 基础文本格式
    ".txt": "text_loader",
    ".md": "markdown_loader",
    ".markdown": "markdown_loader",
    
    # 办公文档格式
    ".pdf": "pdf_loader",
    ".docx": "word_loader",
    ".doc": "word_loader",
    ".xlsx": "excel_loader",
    ".xls": "excel_loader",
    ".csv": "excel_loader",
    ".tsv": "excel_loader",
    ".pptx": "powerpoint_loader",
    ".ppt": "powerpoint_loader",
    
    # 网页格式
    ".html": "html_loader",
    ".htm": "html_loader",
    
    # 新增格式
    ".xml": "xml_loader",
    ".xsl": "xml_loader",
    ".xsd": "xml_loader",
    ".dtd": "xml_loader",
    ".json": "json_loader",
    ".jsonl": "json_loader",
    ".jpg": "image_loader",
    ".jpeg": "image_loader",
    ".png": "image_loader",
    ".bmp": "image_loader",
    ".tiff": "image_loader",
    ".tif": "image_loader",
    ".gif": "image_loader",
    ".webp": "image_loader",
}

# 按需加载器类映射
_loader_classes = {}


def _load_loader_module(extension: str):
    """按需加载对应的加载器模块。"""
    module_name = LOADER_MODULES.get(extension.lower())
    if not module_name:
        return None
    
    try:
        module = __import__(f"app.services.file_loaders.{module_name}", 
                          fromlist=[module_name])
        
        # 从模块中获取加载器类
        loader_class = None
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (isinstance(attr, type) and 
                issubclass(attr, BaseLoader) and 
                attr != BaseLoader):
                loader_class = attr
                break
        
        if loader_class:
            _loader_classes[extension] = loader_class
            return loader_class
            
    except ImportError as e:
        print(f"Warning: Failed to load loader module for {extension}: {e}")
    
    return None


# 重写LoaderRegistry的get_loader方法以支持按需加载
class LazyLoaderRegistry(LoaderRegistry):
    """支持按需加载的加载器注册表。"""
    
    @classmethod
    def get_loader(cls, file_path, use_ocr=False, preserve_structure=True):
        """根据文件路径获取对应的加载器实例（支持按需加载）。"""
        ext = file_path.suffix.lower()
        
        # 检查是否已注册
        if ext in cls._loaders:
            loader_class = cls._loaders[ext]
        else:
            # 按需加载
            loader_class = _load_loader_module(ext)
            if loader_class:
                # 注册到全局注册表
                cls._loaders[ext] = loader_class
            else:
                supported = sorted(set(cls._loaders.keys()))
                raise UnsupportedFileTypeError(
                    f"Unsupported file type: {ext}. "
                    f"Supported types: {', '.join(supported)}",
                    file_path
                )
        
        return loader_class(use_ocr=use_ocr, preserve_structure=preserve_structure)
    
    @classmethod
    def get_supported_extensions(cls):
        """获取所有支持的扩展名列表（包括按需加载的）。"""
        # 返回所有可能的扩展名，即使对应的模块可能尚未加载
        return sorted(set(LOADER_MODULES.keys()))
    
    @classmethod
    def is_supported(cls, file_path):
        """检查文件类型是否被支持。"""
        ext = file_path.suffix.lower()
        return ext in LOADER_MODULES


# 替换默认的注册表
LoaderRegistry = LazyLoaderRegistry

__all__ = [
    # 基类和注册表
    "BaseLoader",
    "LoaderRegistry",
    "FileLoaderError",
    "UnsupportedFileTypeError",
    "FileLoadError",
    # 数据结构
    "DocumentChunk",
    "ChunkType",
    # 加载器模块映射
    "LOADER_MODULES",
    "_load_loader_module",
]