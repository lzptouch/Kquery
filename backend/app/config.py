"""
Kquery项目配置管理（过渡层）
推荐使用 app.core.settings.get_settings() 获取配置。
此模块保留向后兼容，内部委托给 Settings 对象。
"""

import warnings
from app.core.settings import get_settings, Settings as _Settings

# 发出弃用警告（不阻断，但提示开发者）
warnings.filterwarnings("once", message=".*已弃用.*", module="app.config")
warnings.warn(
    "app.config.Config 已弃用，请使用 app.core.settings.get_settings() 替代。",
    DeprecationWarning,
    stacklevel=2,
)

# 获取全局 Settings 实例
_settings = get_settings()


class Config:
    """配置兼容层 - 委托给 Settings 对象"""

    @property
    def API_HOST(self) -> str:
        return _settings.api_host

    @property
    def API_PORT(self) -> int:
        return _settings.api_port

    @property
    def DEBUG(self) -> bool:
        return _settings.debug

    @property
    def ENVIRONMENT(self) -> str:
        return _settings.environment.value

    @property
    def ALLOWED_ORIGINS(self) -> list:
        return _settings.cors_origins

    @property
    def ENABLE_METRICS(self) -> bool:
        return _settings.enable_metrics

    @property
    def USE_JSON_LOGGING(self) -> bool:
        return _settings.environment.value in ("production", "staging")

    @property
    def LOG_LEVEL(self) -> str:
        return "DEBUG" if _settings.is_debug else "INFO"

    @property
    def MILVUS_HOST(self) -> str:
        return _settings.database.milvus_host

    @property
    def MILVUS_PORT(self) -> int:
        return _settings.database.milvus_port

    @property
    def MILVUS_USER(self) -> str:
        return _settings.database.milvus_user or "root"

    @property
    def MILVUS_PASSWORD(self) -> str:
        return _settings.database.milvus_password or ""

    @property
    def EMBEDDING_DIMENSION(self) -> int:
        return _settings.llm.embedding_dimension

    @property
    def EMBEDDING_MODEL_NAME(self) -> str:
        return _settings.llm.embedding_model_name

    @property
    def EMBEDDING_API_BASE(self) -> str:
        return _settings.llm.embedding_api_base

    @property
    def EMBEDDING_API_KEY(self):
        return _settings.llm.embedding_api_key

    @property
    def EMBEDDING_TIMEOUT(self) -> int:
        return _settings.llm.embedding_timeout

    @property
    def OPENAI_TIMEOUT(self) -> int:
        return _settings.llm.embedding_timeout

    @property
    def AGENT_MODEL_NAME(self) -> str:
        return _settings.llm.agent_model_name

    @property
    def AGENT_API_BASE(self) -> str:
        return _settings.llm.agent_api_base

    @property
    def AGENT_API_KEY(self):
        return _settings.llm.agent_api_key

    @property
    def RERANK_MODEL_NAME(self) -> str:
        return _settings.llm.rerank_model_name

    @property
    def RERANK_API_BASE(self) -> str:
        return _settings.llm.rerank_api_base

    @property
    def RERANK_API_KEY(self):
        return _settings.llm.rerank_api_key

    @property
    def RAG_INITIAL_TOP_K(self) -> int:
        return _settings.rag.initial_top_k

    @property
    def RAG_TOP_K(self) -> int:
        return _settings.rag.top_k

    @property
    def RAG_USE_HYDE(self) -> bool:
        return _settings.rag.use_hyde

    @property
    def RAG_USE_RRF(self) -> bool:
        return _settings.rag.use_rrf

    @property
    def RAG_USE_RERANKER(self) -> bool:
        return _settings.rag.use_reranker

    @property
    def RAG_USE_TIME_DECAY(self) -> bool:
        return _settings.rag.use_time_decay

    @property
    def RERANK_RRF_K(self) -> int:
        return _settings.rag.rerank_rrf_k

    @property
    def RAG_TIME_DECAY_HALF_LIFE_DAYS(self) -> float:
        return _settings.rag.time_decay_half_life_days

    @property
    def CHUNK_SIZE(self) -> int:
        return _settings.rag.chunk_size

    @property
    def CHUNK_OVERLAP(self) -> int:
        return _settings.rag.chunk_overlap

    @property
    def REDIS_HOST(self) -> str:
        import urllib.parse
        url = urllib.parse.urlparse(_settings.database.redis_url)
        return url.hostname or "localhost"

    @property
    def REDIS_PORT(self) -> int:
        import urllib.parse
        url = urllib.parse.urlparse(_settings.database.redis_url)
        return url.port or 6379

    @property
    def REDIS_PASSWORD(self):
        import urllib.parse
        url = urllib.parse.urlparse(_settings.database.redis_url)
        return url.password

    @property
    def REDIS_DB(self) -> int:
        import urllib.parse
        url = urllib.parse.urlparse(_settings.database.redis_url)
        path = url.path.lstrip("/")
        return int(path) if path else 0

    @property
    def REDIS_MAX_CONNECTIONS(self) -> int:
        return _settings.database.redis_max_connections

    @property
    def REDIS_DECODE_RESPONSES(self) -> bool:
        return _settings.is_debug

    @property
    def BATCH_MAX_CONCURRENCY(self) -> int:
        return _settings.performance.batch_max_concurrency

    @property
    def EMBEDDING_MAX_CONCURRENCY(self) -> int:
        return _settings.performance.embedding_max_concurrency

    @property
    def EMBEDDING_BATCH_SIZE(self) -> int:
        return _settings.performance.embedding_batch_size

    @property
    def QUERY_TIMEOUT(self) -> int:
        return _settings.performance.query_timeout

    @property
    def PROCESS_TIMEOUT(self) -> int:
        return _settings.performance.process_timeout

    @property
    def ENABLE_IMAGE_UNDERSTANDING(self) -> bool:
        return _settings.image.enable_understanding

    @property
    def ALIYUN_VISION_MODEL(self) -> str:
        return _settings.aliyun.vision_model

    @property
    def IMAGE_DESCRIPTION_LEVEL(self) -> str:
        return _settings.image.description_level

    @property
    def IMAGE_CACHE_ENABLED(self) -> bool:
        return _settings.image.cache_enabled

    @property
    def IMAGE_CACHE_DIR(self) -> str:
        return _settings.image.cache_dir

    @property
    def MIN_IMAGE_WIDTH(self) -> int:
        return _settings.image.min_width

    @property
    def MIN_IMAGE_HEIGHT(self) -> int:
        return _settings.image.min_height

    @property
    def ALIYUN_ACCESS_KEY_ID(self):
        return _settings.aliyun.access_key_id

    @property
    def ALIYUN_ACCESS_KEY_SECRET(self):
        return _settings.aliyun.access_key_secret

    @property
    def ALIYUN_REGION(self) -> str:
        return _settings.aliyun.region

    @property
    def API_KEY_ENABLED(self) -> bool:
        return _settings.security.api_key_enabled

    @property
    def API_KEYS(self) -> list:
        return list(_settings.security.api_keys)

    @property
    def UPLOAD_DIR(self) -> str:
        return _settings.storage.upload_dir

    @property
    def AUTO_CLEANUP(self) -> bool:
        return _settings.storage.auto_cleanup

    @property
    def FILE_RETENTION_DAYS(self) -> int:
        return _settings.storage.file_retention_days

    @property
    def milvus_uri(self) -> str:
        return _settings.milvus_uri

    @property
    def redis_url(self) -> str:
        return _settings.database.redis_url

    @property
    def embedding_config(self) -> dict:
        return _settings.embedding_config

    @property
    def reranker_config(self) -> dict:
        return _settings.reranker_config

    @property
    def aliyun_config(self) -> dict:
        return _settings.aliyun_config

    @property
    def agent_config(self) -> dict:
        return _settings.agent_config

    # 文件处理配置（旧版遗留）
    USE_OCR: bool = False
    PRESERVE_STRUCTURE: bool = True

    @property
    def image_understanding_config(self) -> dict:
        return {
            "enabled": _settings.image.enable_understanding,
            "vision_model": _settings.aliyun.vision_model,
            "description_level": _settings.image.description_level,
            "cache_enabled": _settings.image.cache_enabled,
            "cache_dir": _settings.image.cache_dir,
            "min_image_width": _settings.image.min_width,
            "min_image_height": _settings.image.min_height,
        }


# 全局配置实例（向后兼容）
config = Config()