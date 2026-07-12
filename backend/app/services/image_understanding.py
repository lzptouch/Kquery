"""图像理解服务模块。

使用阿里云通义千问多模态模型进行图像理解，支持：
- 生成图像详细描述
- 识别图像中的文字、图表、图形等元素
- 批量处理和缓存
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from dataclasses import dataclass, field
from enum import Enum

from app.core.settings import get_settings
from app.core.logger import get_logger

settings = get_settings()

logger = get_logger(__name__)


class ImageDescriptionLevel(Enum):
    """图像描述详细程度。"""
    BRIEF = "brief"      # 简洁描述（约50-100字）
    STANDARD = "standard"  # 标准描述（约100-300字）
    DETAILED = "detailed"  # 详细描述（约300-500字）


@dataclass
class ImageUnderstandingResult:
    """图像理解结果。
    
    Attributes:
        image_path: 图像文件路径
        image_hash: 图像哈希值（用于缓存）
        description: 图像描述
        description_level: 描述详细程度
        elements: 识别到的元素列表（文字、图表、图形等）
        success: 是否成功
        error_message: 错误消息（如果有）
    """
    image_path: str
    image_hash: str
    description: str = ""
    description_level: str = "standard"
    elements: List[Dict[str, Any]] = field(default_factory=list)
    success: bool = True
    error_message: Optional[str] = None


class ImageUnderstandingService:
    """图像理解服务类。"""
    
    def __init__(self):
        """初始化图像理解服务。"""
        self.config = settings.image_understanding_config
        self.aliyun_config = settings.aliyun_config
        self._cache_dir: Optional[Path] = None
        self._cache_enabled = self.config["cache_enabled"]
        
        if self._cache_enabled:
            self._init_cache()
    
    def _init_cache(self):
        """初始化缓存目录。"""
        cache_dir = Path(self.config["cache_dir"])
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_dir = cache_dir
        logger.info(f"图像理解缓存目录初始化: {cache_dir}")
    
    def _get_image_hash(self, image_path: Path) -> str:
        """计算图像文件的哈希值。
        
        Args:
            image_path: 图像文件路径
            
        Returns:
            图像哈希值
        """
        try:
            with open(image_path, "rb") as f:
                return hashlib.md5(f.read()).hexdigest()
        except Exception as e:
            logger.warning(f"计算图像哈希失败: {e}")
            return str(hash(str(image_path)))
    
    def _get_cache_path(self, image_hash: str) -> Path:
        """获取缓存文件路径。
        
        Args:
            image_hash: 图像哈希值
            
        Returns:
            缓存文件路径
        """
        if not self._cache_dir:
            raise ValueError("缓存未启用")
        return self._cache_dir / f"{image_hash}.json"
    
    def _load_from_cache(self, image_hash: str) -> Optional[ImageUnderstandingResult]:
        """从缓存加载图像理解结果。
        
        Args:
            image_hash: 图像哈希值
            
        Returns:
            缓存的结果，如果不存在则返回 None
        """
        if not self._cache_enabled or not self._cache_dir:
            return None
        
        try:
            cache_path = self._get_cache_path(image_hash)
            if cache_path.exists():
                with open(cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return ImageUnderstandingResult(**data)
        except Exception as e:
            logger.warning(f"加载缓存失败: {e}")
        
        return None
    
    def _save_to_cache(self, result: ImageUnderstandingResult):
        """保存图像理解结果到缓存。
        
        Args:
            result: 图像理解结果
        """
        if not self._cache_enabled or not self._cache_dir:
            return
        
        try:
            cache_path = self._get_cache_path(result.image_hash)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({
                    "image_path": result.image_path,
                    "image_hash": result.image_hash,
                    "description": result.description,
                    "description_level": result.description_level,
                    "elements": result.elements,
                    "success": result.success,
                    "error_message": result.error_message
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存缓存失败: {e}")
    
    def _get_description_prompt(self, level: ImageDescriptionLevel) -> str:
        """根据描述级别获取提示词。
        
        Args:
            level: 描述级别
            
        Returns:
            提示词
        """
        prompts = {
            ImageDescriptionLevel.BRIEF: """请用50-100字简要描述这张图片的主要内容。""",
            ImageDescriptionLevel.STANDARD: """请详细描述这张图片的内容，包括：
1. 图片的主题和主要内容
2. 图片中的关键元素
3. 图片的整体布局和色调
请用100-300字进行描述。""",
            ImageDescriptionLevel.DETAILED: """请非常详细地描述这张图片的内容，包括：
1. 图片的主题和整体场景
2. 图片中的所有可见元素（物体、人物、文字、图表等）
3. 每个元素的位置、颜色、形状和相互关系
4. 图片的构图、光线和色彩
5. 图片可能传达的信息或情感
请用300-500字进行详细描述。"""
        }
        return prompts.get(level, prompts[ImageDescriptionLevel.STANDARD])
    
    async def _validate_image(self, image_path: Path) -> bool:
        """验证图像是否符合处理要求。
        
        Args:
            image_path: 图像文件路径
            
        Returns:
            是否符合要求
        """
        try:
            from PIL import Image
            
            with Image.open(image_path) as img:
                width, height = img.size
                
                min_width = self.config["min_image_width"]
                min_height = self.config["min_image_height"]
                
                if width < min_width or height < min_height:
                    logger.warning(f"图像尺寸太小: {width}x{height} (最小要求: {min_width}x{min_height})")
                    return False
                
                return True
        except ImportError:
            logger.warning("Pillow未安装，跳过图像尺寸验证")
            return True
        except Exception as e:
            logger.warning(f"图像验证失败: {e}")
            return False
    
    async def understand_image(
        self,
        image_path: Union[str, Path],
        description_level: Optional[str] = None,
        use_cache: bool = True
    ) -> ImageUnderstandingResult:
        """理解图像内容。
        
        Args:
            image_path: 图像文件路径
            description_level: 描述级别 (brief, standard, detailed)
            use_cache: 是否使用缓存
            
        Returns:
            图像理解结果
        """
        image_path = Path(image_path)
        image_hash = self._get_image_hash(image_path)
        
        if description_level is None:
            description_level = self.config["description_level"]
        
        level_enum = ImageDescriptionLevel(description_level)
        
        result = ImageUnderstandingResult(
            image_path=str(image_path),
            image_hash=image_hash,
            description_level=description_level
        )
        
        if not self.config["enabled"]:
            result.success = False
            result.error_message = "图像理解功能未启用"
            return result
        
        if not image_path.exists():
            result.success = False
            result.error_message = f"图像文件不存在: {image_path}"
            return result
        
        if not await self._validate_image(image_path):
            result.success = False
            result.error_message = "图像不符合处理要求"
            return result
        
        if use_cache:
            cached_result = self._load_from_cache(image_hash)
            if cached_result and cached_result.success:
                logger.info(f"使用缓存的图像理解结果: {image_path}")
                return cached_result
        
        try:
            description = await self._call_vision_model(image_path, level_enum)
            result.description = description
            result.success = True
            
            if use_cache:
                self._save_to_cache(result)
            
            logger.info(f"图像理解成功: {image_path}")
            
        except Exception as e:
            logger.error(f"图像理解失败: {e}")
            result.success = False
            result.error_message = str(e)
        
        return result
    
    async def _call_vision_model(
        self,
        image_path: Path,
        level: ImageDescriptionLevel
    ) -> str:
        """调用视觉理解模型（带重试和 Mock 降级）。
        
        Args:
            image_path: 图像文件路径
            level: 描述级别
            
        Returns:
            图像描述
        """
        from app.core.llm_client import call_dashscope_multimodal, is_llm_configured

        try:
            import dashscope
            
            access_key_secret = self.aliyun_config["access_key_secret"]
            if not access_key_secret:
                raise ValueError("阿里云Access Key Secret未配置")
            
            # 如果 LLM 未配置，使用 Mock 数据
            if not is_llm_configured("aliyun_vision"):
                logger.warning("阿里云视觉 API 未配置，使用 Mock 数据")
                from app.core.mock_data import get_mock_data
                mock_result = get_mock_data("image_description", image_path=str(image_path))
                return mock_result.get("description", "Mock 图像描述")

            prompt = self._get_description_prompt(level)
            
            with open(image_path, "rb") as f:
                image_data = f.read()
            
            return await call_dashscope_multimodal(
                model=self.config["vision_model"],
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"image": image_data},
                            {"text": prompt}
                        ]
                    }
                ],
                api_key=access_key_secret,
                mock_module="image_description",
            )
                
        except ImportError:
            raise Exception("dashscope库未安装，请安装: pip install dashscope")
    
    async def understand_images_batch(
        self,
        image_paths: List[Union[str, Path]],
        description_level: Optional[str] = None,
        use_cache: bool = True,
        max_concurrency: int = 3
    ) -> List[ImageUnderstandingResult]:
        """批量理解图像内容。
        
        Args:
            image_paths: 图像文件路径列表
            description_level: 描述级别
            use_cache: 是否使用缓存
            max_concurrency: 最大并发数
            
        Returns:
            图像理解结果列表
        """
        semaphore = asyncio.Semaphore(max_concurrency)
        
        async def process_image(image_path):
            async with semaphore:
                return await self.understand_image(
                    image_path,
                    description_level=description_level,
                    use_cache=use_cache
                )
        
        tasks = [process_image(path) for path in image_paths]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        processed_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                processed_results.append(ImageUnderstandingResult(
                    image_path=str(image_paths[i]),
                    image_hash="",
                    success=False,
                    error_message=str(result)
                ))
            else:
                processed_results.append(result)
        
        return processed_results
    
    def clear_cache(self, image_hash: Optional[str] = None) -> int:
        """清理缓存。
        
        Args:
            image_hash: 特定图像的哈希值，如果为None则清理所有缓存
            
        Returns:
            清理的缓存文件数量
        """
        if not self._cache_enabled or not self._cache_dir:
            return 0
        
        try:
            if image_hash:
                cache_path = self._get_cache_path(image_hash)
                if cache_path.exists():
                    cache_path.unlink()
                    return 1
                return 0
            else:
                count = 0
                for cache_file in self._cache_dir.glob("*.json"):
                    cache_file.unlink()
                    count += 1
                logger.info(f"清理了 {count} 个缓存文件")
                return count
        except Exception as e:
            logger.error(f"清理缓存失败: {e}")
            return 0


image_understanding_service = ImageUnderstandingService()
