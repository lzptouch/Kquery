"""数据库配置模块。

提供 SQLAlchemy 异步引擎、会话工厂和声明式基类。
"""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base

from app.core.settings import get_settings
from app.core.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()

# 声明式基类
Base = declarative_base()

# 异步引擎（使用 aiosqlite 作为默认后端）
async_engine = create_async_engine(
    "sqlite+aiosqlite:///./kquery.db",
    echo=settings.is_debug,
    future=True,
)

# 异步会话工厂
async_session_maker = async_sessionmaker(
    async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def init_db():
    """初始化数据库，创建所有表。"""
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("数据库表初始化完成")


async def get_db() -> AsyncSession:
    """获取数据库会话的异步生成器。

    用于 FastAPI Depends，确保每个请求有独立的会话，
    并在请求结束后自动关闭。
    """
    async with async_session_maker() as session:
        yield session


async def close_db():
    """关闭数据库引擎。"""
    await async_engine.dispose()
    logger.info("数据库引擎已关闭")
