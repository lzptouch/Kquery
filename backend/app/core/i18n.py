"""国际化（i18n）模块

提供多语言支持，基于轻量级字典映射方案。
支持通过请求头 Accept-Language 切换语言。

v2.1 新增
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional
from contextvars import ContextVar

from app.core.logger import get_logger


logger = get_logger(__name__)

# 当前请求的语言上下文
_locale_ctx: ContextVar[str] = ContextVar("locale", default="zh_CN")

# 内置语言包
BUILT_IN_LOCALES = {
    "zh_CN": {
        "welcome": "欢迎使用 KQuery Pro",
        "document_processed": "文档处理成功",
        "document_failed": "文档处理失败",
        "query_success": "查询成功",
        "query_empty": "未找到相关结果",
        "rate_limit_exceeded": "请求过于频繁，请稍后再试",
        "unauthorized": "未授权访问",
        "forbidden": "无权访问此资源",
        "not_found": "资源不存在",
        "validation_error": "请求参数错误",
        "server_error": "服务器内部错误",
        "file_too_large": "文件过大",
        "unsupported_file_type": "不支持的文件类型",
        "graph_build_success": "知识图谱构建成功",
        "graph_build_failed": "知识图谱构建失败",
        "entity_not_found": "实体不存在",
        "consistency_check_pass": "图谱一致性检查通过",
        "consistency_check_failed": "图谱一致性检查发现问题",
    },
    "en_US": {
        "welcome": "Welcome to KQuery Pro",
        "document_processed": "Document processed successfully",
        "document_failed": "Document processing failed",
        "query_success": "Query successful",
        "query_empty": "No relevant results found",
        "rate_limit_exceeded": "Too many requests, please try again later",
        "unauthorized": "Unauthorized access",
        "forbidden": "Access to this resource is forbidden",
        "not_found": "Resource not found",
        "validation_error": "Invalid request parameters",
        "server_error": "Internal server error",
        "file_too_large": "File too large",
        "unsupported_file_type": "Unsupported file type",
        "graph_build_success": "Knowledge graph built successfully",
        "graph_build_failed": "Knowledge graph build failed",
        "entity_not_found": "Entity not found",
        "consistency_check_pass": "Graph consistency check passed",
        "consistency_check_failed": "Graph consistency check found issues",
    }
}


class I18n:
    """国际化管理器。"""
    
    def __init__(self):
        """初始化国际化管理器。"""
        self._locales: Dict[str, Dict[str, str]] = {}
        self._load_builtin_locales()
        self._load_external_locales()
    
    def _load_builtin_locales(self) -> None:
        """加载内置语言包。"""
        for lang, translations in BUILT_IN_LOCALES.items():
            self._locales[lang] = translations.copy()
    
    def _load_external_locales(self) -> None:
        """从文件系统加载外部语言包。"""
        locales_dir = Path(__file__).parent.parent / "locales"
        if not locales_dir.exists():
            return
        
        for file_path in locales_dir.glob("*.json"):
            try:
                lang = file_path.stem
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                
                if lang not in self._locales:
                    self._locales[lang] = {}
                self._locales[lang].update(data)
                logger.info(f"加载外部语言包: {lang}")
            except Exception as e:
                logger.warning(f"加载语言包失败 {file_path}: {e}")
    
    def set_locale(self, locale: str) -> None:
        """设置当前线程/请求的本地化语言。
        
        Args:
            locale: 语言代码，如 "zh_CN", "en_US"
        """
        if locale not in self._locales:
            # 尝试匹配前缀（如 en_US -> en）
            prefix = locale.split("_")[0]
            for available in self._locales.keys():
                if available.startswith(prefix):
                    locale = available
                    break
            else:
                locale = "zh_CN"  # 默认回退
        
        _locale_ctx.set(locale)
    
    def get_locale(self) -> str:
        """获取当前本地化语言。"""
        return _locale_ctx.get()
    
    def translate(self, key: str, default: Optional[str] = None, **kwargs) -> str:
        """翻译指定键的文本。
        
        Args:
            key: 翻译键
            default: 默认文本（翻译不存在时返回）
            **kwargs: 格式化参数
            
        Returns:
            翻译后的文本
        """
        locale = self.get_locale()
        translations = self._locales.get(locale, self._locales.get("zh_CN", {}))
        
        text = translations.get(key, default or key)
        
        if kwargs:
            try:
                text = text.format(**kwargs)
            except KeyError:
                pass
        
        return text
    
    def add_translations(self, locale: str, translations: Dict[str, str]) -> None:
        """动态添加翻译条目。
        
        Args:
            locale: 语言代码
            translations: 翻译字典
        """
        if locale not in self._locales:
            self._locales[locale] = {}
        self._locales[locale].update(translations)
    
    def list_locales(self) -> list:
        """列出所有可用的语言。"""
        return list(self._locales.keys())


# 全局实例
i18n = I18n()


# 便捷函数
def _(key: str, default: Optional[str] = None, **kwargs) -> str:
    """快捷翻译函数。
    
    使用示例:
        message = _("welcome")
        message = _("document_processed", default="Processed")
        message = _("greeting", name="User")
    """
    return i18n.translate(key, default=default, **kwargs)


def set_locale(locale: str) -> None:
    """设置当前请求语言。"""
    i18n.set_locale(locale)


def get_locale() -> str:
    """获取当前请求语言。"""
    return i18n.get_locale()
