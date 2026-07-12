"""
速率限制中间件

提供基于Redis的多层速率限制功能
支持IP级别、用户级别的限流
"""

from typing import Optional, Callable, Awaitable
import redis.asyncio as redis
from fastapi import Request, Response
from fastapi.responses import JSONResponse
import time
import json

from app.core.settings import get_settings
from app.core.exceptions import RateLimitError, ErrorCode
from app.core.logger import get_logger


logger = get_logger(__name__)
settings = get_settings()


class RateLimiter:
    """
    速率限制器
    
    使用Redis实现滑动窗口算法进行速率限制
    支持多种限制策略：
    - IP级别限制（防止DDoS）
    - 用户级别限制（防止滥用）
    - API Key级别限制（针对API调用者）
    
    使用示例:
        limiter = RateLimiter()
        
        # 检查IP限制
        await limiter.check_ip_limit(request.client.host)
        
        # 检查用户限制
        await limiter.check_user_limit(user_id)
    """
    
    def __init__(self):
        """初始化速率限制器"""
        self.redis: Optional[redis.Redis] = None
        
        # 默认限制配置（可覆盖）
        self.default_limits = {
            "ip": {  # IP级别限制
                "requests": settings.rate_limit_requests,
                "window_seconds": settings.rate_limit_window_seconds,
            },
            "user": {  # 用户级别限制（保留结构，匿名模式下可复用）
                "requests": settings.rate_limit_requests or 200,
                "window_seconds": settings.rate_limit_window_seconds,
            },
            "api_key": {  # API Key级别限制
                "requests": settings.rate_limit_requests or 500,
                "window_seconds": settings.rate_limit_window_seconds * 10,  # 更宽松
            },
        }
        
        # 特殊端点限制配置
        self.endpoint_limits = {
            "/api/v1/documents/upload": {"requests": 30, "window_seconds": 3600},  # 每小时
        }
    
    async def init_redis(self) -> None:
        """初始化Redis连接"""
        try:
            self.redis = redis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True
            )
            logger.info("RateLimiter: Redis连接初始化成功")
        except Exception as e:
            logger.error(f"RateLimiter: Redis连接失败 - {e}")
            self.redis = None
    
    async def check_rate_limit(
        self,
        key: str,
        limit_type: str = "ip",
        endpoint: Optional[str] = None
    ) -> dict:
        """
        检查是否超过速率限制
        
        Args:
            key: 限制标识符（如IP地址、用户ID、API Key等）
            limit_type: 限制类型 ("ip", "user", "api_key", "sensitive")
            endpoint: API端点路径，用于应用特殊规则
            
        Returns:
            包含状态信息的字典：
            {
                "allowed": bool,       # 是否允许请求
                "remaining": int,      # 剩余请求数
                "reset_time": float,   # 重置时间戳
                "limit": int           # 当前窗口的限制数
            }
            
        Raises:
            RateLimitError: 超过限制时抛出
        """
        if not self.redis:
            return {"allowed": True, "remaining": 999999, "reset_time": time.time() + 3600, "limit": 999999}
        
        try:
            # 确定使用的限制规则
            if endpoint and endpoint in self.endpoint_limits:
                rule = self.endpoint_limits[endpoint]
            else:
                rule = self.default_limits.get(limit_type, self.default_limits["ip"])
            
            requests_limit = rule["requests"]
            window_seconds = rule["window_seconds"]
            
            # 构建Redis键名
            redis_key = f"ratelimit:{limit_type}:{key}:{endpoint or 'global'}"
            
            current_time = time.time()
            
            # 获取当前计数
            pipe = self.redis.pipeline()
            pipe.zrangebyscore(redis_key, current_time - window_seconds, current_time)
            pipe.zcard(redis_key)
            results = await pipe.execute()
            
            existing_requests = results[1]
            
            if existing_requests >= requests_limit:
                # 超过限制
                oldest_request = results[0][0] if results[0] else current_time - window_seconds
                reset_at = float(oldest_request) + window_seconds
                retry_after = int(reset_at - current_time) + 1
                
                logger.warning(
                    f"速率限制触发 - 类型:{limit_type}, "
                    f"Key:{key[:20]}..., 端点:{endpoint}"
                )
                
                raise RateLimitError(
                    message=f"请求过于频繁，请在 {retry_after} 秒后重试",
                    retry_after=retry_after,
                    error_code=ErrorCode.RATE_LIMIT_EXCEEDED
                )
            
            # 记录本次请求
            await self.redis.zadd(redis_key, {str(current_time): current_time})
            await self.redis.expire(redis_key, window_seconds + 1)
            
            remaining = requests_limit - (existing_requests + 1)
            
            return {
                "allowed": True,
                "remaining": remaining,
                "reset_time": current_time + window_seconds,
                "limit": requests_limit
            }
            
        except RateLimitError:
            raise
        except Exception as e:
            logger.error(f"速率限制检查失败: {e}")
            # 出错时不阻止请求
            return {"allowed": True, "remaining": 999999, "reset_time": time.time() + 3600, "limit": 999999}


# 全局实例
rate_limiter = RateLimiter()


async def rate_limit_middleware(request: Request, call_next):
    """
    FastAPI中间件：自动对所有请求执行速率限制
    
    处理流程：
    1. 提取客户端标识（优先使用API Key > 用户ID > IP地址）
    2. 根据端点和标识检查速率限制
    3. 在响应头中添加速率限制信息
    4. 如果超限返回429错误
    """
    # 跳过健康检查端点
    if request.url.path in ["/health", "/health/live", "/health/ready"]:
        response = await call_next(request)
        return response
    
    # 确定限制标识符
    identifier = None
    limit_type = "ip"
    
    # 尝试从请求头提取API Key
    api_key = request.headers.get("X-API-Key")
    if api_key:
        identifier = api_key[:32]  # 截断以保护隐私
        limit_type = "api_key"
    
    # 回退到IP地址
    if not identifier and request.client:
        identifier = request.client.host
        limit_type = "ip"
    
    # 执行速率限制检查
    try:
        result = await rate_limiter.check_rate_limit(
            key=identifier,
            limit_type=limit_type,
            endpoint=request.url.path
        )
        
        # 继续处理请求
        response = await call_next(request)
        
        # 添加速率限制相关响应头
        response.headers["X-RateLimit-Limit"] = str(result["limit"])
        response.headers["X-RateLimit-Remaining"] = str(result["remaining"])
        response.headers["X-RateLimit-Reset"] = str(int(result["reset_time"]))
        
        return response
        
    except RateLimitError as e:
        # 返回429 Too Many Requests
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "code": "RATE_LIMIT_EXCEEDED",
                    "message": e.message,
                    "details": {}
                }
            },
            headers={"Retry-After": str(e.retry_after)}
        )


# ==================== 端点级限流依赖（v2.1 新增）====================

class RateLimitDependency:
    """FastAPI Depends 可调用限流依赖。
    
    用于对特定端点应用独立的速率限制策略，
    与全局中间件并存，提供更精细的控制。
    
    使用示例:
        @router.post("/search", dependencies=[
            Depends(RateLimitDependency(requests=100, window_seconds=60))
        ])
        async def search(...):
            ...
    """
    
    def __init__(
        self,
        requests: int = 60,
        window_seconds: int = 60,
        key_prefix: str = "endpoint",
    ):
        """初始化端点限流依赖。
        
        Args:
            requests: 时间窗口内允许的最大请求数
            window_seconds: 时间窗口（秒）
            key_prefix: Redis键前缀，用于区分不同端点
        """
        self.requests = requests
        self.window_seconds = window_seconds
        self.key_prefix = key_prefix
    
    async def __call__(self, request: Request) -> None:
        """执行限流检查。
        
        Args:
            request: FastAPI Request 对象
            
        Raises:
            RateLimitError: 超过限流阈值时抛出
        """
        if not rate_limiter.redis:
            return
        
        # 提取标识符（仅 IP）
        identifier = "anonymous"
        if request.client:
            identifier = request.client.host
        
        # 构建独立的 Redis key（与中间件区分命名空间）
        redis_key = f"ratelimit:dep:{self.key_prefix}:{identifier}"
        
        current_time = time.time()
        
        try:
            pipe = rate_limiter.redis.pipeline()
            pipe.zrangebyscore(redis_key, current_time - self.window_seconds, current_time)
            pipe.zcard(redis_key)
            results = await pipe.execute()
            
            existing = results[1]
            
            if existing >= self.requests:
                oldest = results[0][0] if results[0] else current_time - self.window_seconds
                reset_at = float(oldest) + self.window_seconds
                retry_after = int(reset_at - current_time) + 1
                
                raise RateLimitError(
                    message=f"请求过于频繁，请在 {retry_after} 秒后重试",
                    retry_after=retry_after,
                    error_code=ErrorCode.RATE_LIMIT_EXCEEDED
                )
            
            # 记录请求
            await rate_limiter.redis.zadd(redis_key, {str(current_time): current_time})
            await rate_limiter.redis.expire(redis_key, self.window_seconds + 1)
            
        except RateLimitError:
            raise
        except Exception as e:
            logger.warning(f"端点限流检查失败（将允许请求）: {e}")
            return


def get_rate_limit_dependency(
    requests: int = 60,
    window_seconds: int = 60,
    key_prefix: str = "endpoint",
) -> RateLimitDependency:
    """工厂函数：创建限流依赖实例。
    
    便于在路由中直接使用：
        dependencies=[Depends(get_rate_limit_dependency(requests=100))]
    
    Args:
        requests: 时间窗口内允许的最大请求数
        window_seconds: 时间窗口（秒）
        key_prefix: Redis键前缀
        
    Returns:
        RateLimitDependency 实例
    """
    return RateLimitDependency(
        requests=requests,
        window_seconds=window_seconds,
        key_prefix=key_prefix,
    )
