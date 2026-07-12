"""文件加载器配置管理。

管理文件加载器的按需加载配置，支持选择性启用/禁用特定加载器。
"""

from typing import Dict, List, Set
from pathlib import Path


class LoaderConfig:
    """文件加载器配置类。"""
    
    def __init__(self):
        # 启用的加载器模块
        self.enabled_modules: Set[str] = {
            # 基础文本格式
            "text_loader",
            "markdown_loader",
            
            # 办公文档格式
            "pdf_loader",
            "word_loader", 
            "excel_loader",
            "powerpoint_loader",
            
            # 网页格式
            "html_loader",
            
            # 新增格式
            "xml_loader",
            "json_loader",
            "image_loader",
        }
        
        # 模块依赖关系
        self.module_dependencies: Dict[str, List[str]] = {
            "pdf_loader": ["pypdf", "pdf2image", "pytesseract"],
            "word_loader": ["python-docx"],
            "excel_loader": ["openpyxl"],
            "powerpoint_loader": ["python-pptx"],
            "html_loader": ["beautifulsoup4"],
            "xml_loader": ["lxml", "xmltodict"],
            "image_loader": ["Pillow", "opencv-python", "easyocr"],
        }
        
        # 文件扩展名到模块的映射
        self.extension_to_module: Dict[str, str] = {
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
    
    def enable_module(self, module_name: str) -> bool:
        """启用指定模块。"""
        if module_name in self.extension_to_module.values():
            self.enabled_modules.add(module_name)
            return True
        return False
    
    def disable_module(self, module_name: str) -> bool:
        """禁用指定模块。"""
        if module_name in self.enabled_modules:
            self.enabled_modules.remove(module_name)
            return True
        return False
    
    def is_module_enabled(self, module_name: str) -> bool:
        """检查模块是否启用。"""
        return module_name in self.enabled_modules
    
    def get_module_for_extension(self, extension: str) -> str:
        """获取文件扩展名对应的模块。"""
        return self.extension_to_module.get(extension.lower())
    
    def is_extension_supported(self, extension: str) -> bool:
        """检查文件扩展名是否被支持。"""
        module_name = self.get_module_for_extension(extension)
        return module_name is not None and self.is_module_enabled(module_name)
    
    def get_supported_extensions(self) -> List[str]:
        """获取所有支持的扩展名列表。"""
        return [ext for ext, module in self.extension_to_module.items() 
                if self.is_module_enabled(module)]
    
    def check_dependencies(self, module_name: str) -> Dict[str, bool]:
        """检查模块的依赖是否满足。"""
        dependencies = self.module_dependencies.get(module_name, [])
        result = {}
        
        for dep in dependencies:
            try:
                __import__(dep.replace('-', '_'))
                result[dep] = True
            except ImportError:
                result[dep] = False
        
        return result
    
    def can_load_module(self, module_name: str) -> bool:
        """检查模块是否可以加载（依赖满足）。"""
        if not self.is_module_enabled(module_name):
            return False
        
        dependencies = self.check_dependencies(module_name)
        return all(dependencies.values())


# 全局配置实例
loader_config = LoaderConfig()