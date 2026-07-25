"""KQuery Pro 增强配置管理。

基于Pydantic Settings的现代化配置管理，
支持环境变量、.env文件、多环境部署。
遵循CODE_STANDARDS.md 1.4节（配置管理规范）。

主要特性：
- 类型安全的配置项
- 环境变量自动映射
- 配置验证
- 多环境支持
- 敏感配置保护
- 配置分组管理（v2.1 新增）

使用示例：
    from app.core.settings import get_settings, Settings
    
    settings = get_settings()
    
    # 访问配置
    print(settings.app_name)
    print(settings.milvus_uri)
    
    # 分组访问（v2.1 新增）
    print(settings.database.milvus_host)
    print(settings.llm.embedding_model_name)
"""

from __future__ import annotations

import os
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    """运行环境枚举。
    
    Attributes:
        DEVELOPMENT: 开发环境
        TESTING: 测试环境
        STAGING: 预发布环境
        PRODUCTION: 生产环境
    """
    DEVELOPMENT = "development"
    TESTING = "testing"
    STAGING = "staging"
    PRODUCTION = "production"


# ========== 配置分组类（v2.1 新增）==========

class AppSettings(BaseSettings):
    """应用基础配置。"""
    model_config = SettingsConfigDict(env_prefix="APP_", env_file=".env", env_file_encoding="utf-8", extra="ignore")
    
    name: str = Field(default="KQuery Pro", description="应用名称")
    debug: bool = Field(default=False, description="调试模式")
    environment: Environment = Field(
        default=Environment.DEVELOPMENT, description="运行环境"
    )
    api_host: str = Field(default="0.0.0.0", description="API监听地址")
    api_port: int = Field(default=8000, description="API监听端口")
    cors_origins: List[str] = Field(
        default=["*"], description="允许的CORS来源"
    )
    enable_metrics: bool = Field(default=False, description="启用Prometheus指标")


class DatabaseSettings(BaseSettings):
    """数据库相关配置。"""
    model_config = SettingsConfigDict(env_prefix="DB_", env_file=".env", env_file_encoding="utf-8", extra="ignore")
    
    milvus_host: str = Field(default="localhost", description="Milvus主机")
    milvus_port: int = Field(default=19530, description="Milvus端口")
    milvus_user: Optional[str] = Field(default=None, description="Milvus用户名")
    milvus_password: Optional[str] = Field(default=None, description="Milvus密码")
    embedding_dimension: int = Field(default=4096, description="向量维度")
    
    neo4j_uri: str = Field(
        default="bolt://localhost:7687", description="Neo4j连接URI"
    )
    neo4j_user: str = Field(default="neo4j", description="Neo4j用户名")
    neo4j_password: str = Field(default="password", description="Neo4j密码")
    neo4j_database: str = Field(default="neo4j", description="Neo4j数据库名称")
    
    redis_url: str = Field(
        default="redis://localhost:6379/0", description="Redis连接URL"
    )
    redis_max_connections: int = Field(default=100, description="Redis最大连接数")
    
    mongodb_url: Optional[str] = Field(default=None, description="MongoDB连接URL")
    mongodb_db_name: str = Field(default="kquery_logs", description="MongoDB数据库名称")


class LLMSettings(BaseSettings):
    """LLM相关配置。"""
    model_config = SettingsConfigDict(env_prefix="LLM_", env_file=".env", env_file_encoding="utf-8", extra="ignore")
    
    embedding_model_name: str = Field(default="Qwen3-Embedding-8B", description="嵌入模型名称")
    embedding_api_base: str = Field(default="http://localhost:7928/v1", description="嵌入模型API基础URL")
    embedding_api_key: Optional[str] = Field(default=None, description="嵌入模型API密钥")
    embedding_timeout: int = Field(default=120, description="嵌入生成超时(秒)")
    embedding_dimension: int = Field(default=4096, description="向量维度")
    
    rerank_model_name: str = Field(default="Qwen3-Reranker-8B", description="重排序模型名称")
    rerank_api_base: str = Field(default="http://localhost:7929/v1", description="重排序API基础URL")
    rerank_api_key: Optional[str] = Field(default=None, description="重排序API密钥")
    
    agent_model_name: str = Field(default="qwen-turbo", description="Agent模型名称")
    agent_api_base: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        description="Agent API基础URL"
    )
    agent_api_key: Optional[str] = Field(default=None, description="Agent API密钥")

    # 多模态模型（v2.2 新增）
    multimodal_model_name: str = Field(default="qwen-vl-plus", description="多模态模型名称")
    multimodal_api_base: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        description="多模态 API 基础 URL"
    )
    multimodal_api_key: Optional[str] = Field(default=None, description="多模态 API 密钥")
    multimodal_provider: str = Field(default="dashscope", description="多模态服务提供商：dashscope/openai")



class RAGSettings(BaseSettings):
    """RAG检索配置。"""
    model_config = SettingsConfigDict(env_prefix="RAG_", env_file=".env", env_file_encoding="utf-8", extra="ignore")
    
    initial_top_k: int = Field(default=15, description="初始召回数量")
    top_k: int = Field(default=5, description="最终返回数量")
    use_hyde: bool = Field(default=True, description="启用HyDE增强")
    use_rrf: bool = Field(default=True, description="启用RRF融合")
    use_reranker: bool = Field(default=True, description="启用Reranker排序")
    use_time_decay: bool = Field(default=True, description="启用时间衰减")
    rerank_rrf_k: int = Field(default=60, description="RRF参数k")
    time_decay_half_life_days: float = Field(default=30.0, description="时间衰减半衰期（天）")
    chunk_size: int = Field(default=500, description="文本块大小")
    chunk_overlap: int = Field(default=50, description="块重叠大小")


class SecuritySettings(BaseSettings):
    """安全配置。"""
    model_config = SettingsConfigDict(env_prefix="SEC_", env_file=".env", env_file_encoding="utf-8", extra="ignore")
    
    rate_limit_requests: int = Field(default=100, description="每IP每分钟请求限制")
    rate_limit_window_seconds: int = Field(default=60, description="限流时间窗口(秒)")
    max_file_size_mb: int = Field(default=100, description="最大上传文件大小(MB)")
    allowed_extensions: Set[str] = Field(
        default={
            ".pdf", ".docx", ".doc", ".txt", ".md", ".html",
            ".xlsx", ".xls", ".pptx", ".ppt", ".csv", ".xml",
            ".json", ".jpg", ".jpeg", ".png", ".gif", ".bmp",
        },
        description="允许的文件扩展名",
    )
    api_key_enabled: bool = Field(default=False, description="是否启用API Key验证")
    api_keys: Set[str] = Field(default=set(), description="允许的API Key列表")
    jwt_secret_key: str = Field(default="dev-secret-key", description="JWT密钥")
    jwt_algorithm: str = Field(default="HS256", description="JWT算法")
    jwt_expiration_hours: int = Field(default=24, description="JWT访问令牌过期时间(小时)")
    jwt_refresh_expiration_days: int = Field(default=7, description="JWT刷新令牌过期时间(天)")


class StorageSettings(BaseSettings):
    """文件存储配置。"""
    model_config = SettingsConfigDict(env_prefix="STORAGE_", env_file=".env", env_file_encoding="utf-8", extra="ignore")
    
    upload_dir: str = Field(default="./documents", description="上传文件存储目录")
    auto_cleanup: bool = Field(default=True, description="自动清理过期文件")
    file_retention_days: int = Field(default=30, description="文件保留天数")
    
    # 存储类型：local（本地文件系统）或 minio（MinIO 对象存储）
    type: str = Field(default="local", description="存储类型 (local/minio)")
    
    # MinIO 对象存储配置
    minio_endpoint: str = Field(default="localhost:9000", description="MinIO 服务端点")
    minio_access_key: str = Field(default="minioadmin", description="MinIO 访问密钥")
    minio_secret_key: str = Field(default="minioadmin", description="MinIO 秘密密钥")
    minio_bucket: str = Field(default="kquery-documents", description="MinIO 存储桶名称")
    minio_secure: bool = Field(default=False, description="MinIO 是否使用 HTTPS")
    minio_region: str = Field(default="us-east-1", description="MinIO 区域")


class PerformanceSettings(BaseSettings):
    """性能配置。"""
    model_config = SettingsConfigDict(env_prefix="PERF_", env_file=".env", env_file_encoding="utf-8", extra="ignore")
    
    batch_max_concurrency: int = Field(default=3, description="批量处理最大并发数")
    embedding_max_concurrency: int = Field(default=5, description="嵌入生成并发数")
    embedding_batch_size: int = Field(default=10, description="嵌入批处理大小")
    query_timeout: int = Field(default=30, description="查询超时(秒)")
    process_timeout: int = Field(default=300, description="文档处理超时(秒)")


class ImageSettings(BaseSettings):
    """图像理解配置。"""
    model_config = SettingsConfigDict(env_prefix="IMG_", env_file=".env", env_file_encoding="utf-8", extra="ignore")
    
    enable_understanding: bool = Field(default=True, description="启用图像理解")
    min_width: int = Field(default=100, description="最小图像宽度(px)")
    min_height: int = Field(default=100, description="最小图像高度(px)")
    description_level: str = Field(default="standard", description="图像描述详细程度")
    cache_enabled: bool = Field(default=True, description="图像理解缓存")
    cache_dir: str = Field(default="./cache/images", description="图像缓存目录")


class AliyunSettings(BaseSettings):
    """阿里云服务配置。"""
    model_config = SettingsConfigDict(env_prefix="ALIYUN_", env_file=".env", env_file_encoding="utf-8", extra="ignore")
    
    access_key_id: Optional[str] = Field(default=None, description="阿里云AccessKey ID")
    access_key_secret: Optional[str] = Field(default=None, description="阿里云AccessKey Secret")
    region: str = Field(default="cn-hangzhou", description="阿里云区域")
    vision_model: str = Field(default="qwen-vl-plus", description="阿里云视觉模型")


class ELKSettings(BaseSettings):
    """ELK 日志服务配置。"""
    model_config = SettingsConfigDict(env_prefix="ELK_", env_file=".env", env_file_encoding="utf-8", extra="ignore")

    enabled: bool = Field(default=False, description="是否启用 ELK 日志推送")
    logstash_host: str = Field(default="localhost", description="Logstash 主机地址")
    logstash_port: int = Field(default=5045, description="Logstash TCP 端口")
    logstash_protocol: str = Field(default="tcp", description="Logstash 传输协议 (tcp/udp)")
    logstash_timeout: float = Field(default=5.0, description="Logstash 连接超时(秒)")
    logstash_ssl_verify: bool = Field(default=False, description="是否验证 SSL 证书")
    index_name: str = Field(default="kquery-logs", description="Elasticsearch 索引名前缀")
    environment: str = Field(default="development", description="日志标记的环境标识")
    batch_size: int = Field(default=100, description="异步批量发送大小")
    flush_interval: float = Field(default=2.0, description="异步刷新间隔(秒)")


class OpsSettings(BaseSettings):
    """运维(可观测性)能力配置。

    对应《运维能力缺失功能实现方案》中提出的：
    指标采集、告警、日志聚合、故障诊断、健康巡检、SLO追踪、运维智能体。
    """

    model_config = SettingsConfigDict(env_prefix="OPS_", env_file=".env", env_file_encoding="utf-8", extra="ignore")

    enabled: bool = Field(default=True, description="是否启用运维能力模块")
    metrics_enabled: bool = Field(default=True, description="是否采集/暴露 Prometheus 指标")
    metrics_namespace: str = Field(default="kquery", description="指标命名空间前缀")

    elasticsearch_enabled: bool = Field(default=False, description="是否对接 Elasticsearch 做日志聚合")
    elasticsearch_url: str = Field(default="http://localhost:9200", description="Elasticsearch 地址")
    elasticsearch_index: str = Field(default="kquery-ops-logs", description="Elasticsearch 索引名")

    prometheus_enabled: bool = Field(default=False, description="是否对接独立 Prometheus 服务")
    prometheus_url: str = Field(default="http://localhost:9090", description="Prometheus 地址")
    grafana_enabled: bool = Field(default=False, description="是否启用 Grafana")
    grafana_url: str = Field(default="http://localhost:3000", description="Grafana 地址")

    slo_default_objective: float = Field(default=99.9, description="默认 SLO 目标百分比")
    slo_window_days: int = Field(default=28, description="SLO 统计窗口(天)")
    alert_default_cooldown_seconds: int = Field(default=300, description="告警默认静默冷却(秒)")
    health_check_timeout_seconds: float = Field(default=5.0, description="健康巡检超时(秒)")

    incident_auto_diagnose: bool = Field(default=True, description="故障创建后是否自动执行根因诊断")
    incident_use_llm: bool = Field(default=True, description="根因诊断是否使用 LLM")
    retention_days: int = Field(default=30, description="运维数据默认保留天数")


class FeatureFlagsSettings(BaseSettings):
    """v3.0 功能开关配置。

    用于灰度发布和快速回滚，每个新功能默认关闭，
    可在环境变量或 .env 中按需开启。
    """
    model_config = SettingsConfigDict(env_prefix="FEATURE_", env_file=".env", env_file_encoding="utf-8", extra="ignore")

    web_search_enabled: bool = Field(default=False, description="P0-4 联网搜索功能")
    speech_enabled: bool = Field(default=False, description="P1-1 语音功能")
    memory_enabled: bool = Field(default=False, description="P1-2 长期记忆功能")
    task_agent_enabled: bool = Field(default=False, description="P1-3 任务式Agent功能")
    skill_market_enabled: bool = Field(default=False, description="P2-2 Skills生态功能")
    shared_kb_enabled: bool = Field(default=False, description="P2-1 共享知识库功能")
    user_auth_enabled: bool = Field(default=False, description="用户认证功能")
    mindmap_export_enabled: bool = Field(default=False, description="P0-3 思维导图导出功能")


# ========== 主配置类 ==========

class Settings(BaseSettings):
    """应用主配置类（分组管理版）。
    
    所有配置项都支持通过环境变量或.env文件覆盖。
    环境变量名使用大写，如 APP_NAME, DB_MILVUS_HOST 等。
    
    v2.1 更新：支持分组配置访问（settings.database.milvus_host）
    同时保持原有扁平属性访问的向后兼容（settings.milvus_host）。
    """
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
    
    # 分组配置实例
    app: AppSettings = Field(default_factory=AppSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    rag: RAGSettings = Field(default_factory=RAGSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    performance: PerformanceSettings = Field(default_factory=PerformanceSettings)
    image: ImageSettings = Field(default_factory=ImageSettings)
    aliyun: AliyunSettings = Field(default_factory=AliyunSettings)
    elk: ELKSettings = Field(default_factory=ELKSettings)
    features: FeatureFlagsSettings = Field(default_factory=FeatureFlagsSettings)
    ops: OpsSettings = Field(default_factory=OpsSettings)
    
    # ========== 向后兼容属性（直接映射常用字段）==========
    @property
    def app_name(self) -> str:
        """向后兼容：应用名称。"""
        return self.app.name
    
    @property
    def debug(self) -> bool:
        """向后兼容：调试模式。"""
        return self.app.debug
    
    @property
    def environment(self) -> Environment:
        """向后兼容：运行环境。"""
        return self.app.environment
    
    @property
    def api_host(self) -> str:
        """向后兼容：API监听地址。"""
        return self.app.api_host
    
    @property
    def api_port(self) -> int:
        """向后兼容：API监听端口。"""
        return self.app.api_port
    
    @property
    def cors_origins(self) -> list:
        """向后兼容：CORS来源。"""
        return self.app.cors_origins
    
    @property
    def enable_metrics(self) -> bool:
        """向后兼容：启用指标。"""
        return self.app.enable_metrics
    
    @property
    def milvus_host(self) -> str:
        """向后兼容：Milvus主机。"""
        return self.database.milvus_host
    
    @property
    def milvus_port(self) -> int:
        """向后兼容：Milvus端口。"""
        return self.database.milvus_port
    
    @property
    def milvus_user(self) -> Optional[str]:
        """向后兼容：Milvus用户名。"""
        return self.database.milvus_user
    
    @property
    def milvus_password(self) -> Optional[str]:
        """向后兼容：Milvus密码。"""
        return self.database.milvus_password
    
    @property
    def neo4j_uri(self) -> str:
        """向后兼容：Neo4j URI。"""
        return self.database.neo4j_uri
    
    @property
    def neo4j_user(self) -> str:
        """向后兼容：Neo4j用户名。"""
        return self.database.neo4j_user
    
    @property
    def neo4j_password(self) -> str:
        """向后兼容：Neo4j密码。"""
        return self.database.neo4j_password
    
    @property
    def neo4j_database(self) -> str:
        """向后兼容：Neo4j数据库。"""
        return self.database.neo4j_database
    
    @property
    def redis_url(self) -> str:
        """向后兼容：Redis URL。"""
        return self.database.redis_url
    
    @property
    def redis_max_connections(self) -> int:
        """向后兼容：Redis最大连接数。"""
        return self.database.redis_max_connections
    
    @property
    def mongodb_url(self) -> Optional[str]:
        """向后兼容：MongoDB URL。"""
        return self.database.mongodb_url
    
    @property
    def mongodb_db_name(self) -> str:
        """向后兼容：MongoDB数据库名。"""
        return self.database.mongodb_db_name
    
    @property
    def embedding_model_name(self) -> str:
        """向后兼容：嵌入模型名称。"""
        return self.llm.embedding_model_name
    
    @property
    def embedding_api_base(self) -> str:
        """向后兼容：嵌入API基础URL。"""
        return self.llm.embedding_api_base
    
    @property
    def embedding_api_key(self) -> Optional[str]:
        """向后兼容：嵌入API密钥。"""
        return self.llm.embedding_api_key
    
    @property
    def embedding_timeout(self) -> int:
        """向后兼容：嵌入超时。"""
        return self.llm.embedding_timeout
    
    @property
    def embedding_dimension(self) -> int:
        """向后兼容：嵌入维度。"""
        return self.llm.embedding_dimension
    
    @property
    def rerank_model_name(self) -> str:
        """向后兼容：重排序模型名称。"""
        return self.llm.rerank_model_name
    
    @property
    def rerank_api_base(self) -> str:
        """向后兼容：重排序API基础URL。"""
        return self.llm.rerank_api_base
    
    @property
    def rerank_api_key(self) -> Optional[str]:
        """向后兼容：重排序API密钥。"""
        return self.llm.rerank_api_key
    
    @property
    def agent_model_name(self) -> str:
        """向后兼容：Agent模型名称。"""
        return self.llm.agent_model_name
    
    @property
    def agent_api_base(self) -> str:
        """向后兼容：Agent API基础URL。"""
        return self.llm.agent_api_base
    
    @property
    def agent_api_key(self) -> Optional[str]:
        """向后兼容：Agent API密钥。"""
        return self.llm.agent_api_key

    @property
    def multimodal_config(self) -> Dict[str, Any]:
        """获取多模态模型配置字典。"""
        return {
            "model": self.llm.multimodal_model_name or self.aliyun.vision_model,
            "api_base": self.llm.multimodal_api_base,
            "api_key": self.llm.multimodal_api_key or self.aliyun.access_key_secret or self.llm.agent_api_key,
            "provider": self.llm.multimodal_provider,
        }

    # 向后兼容属性（支持 settings.multimodal_model_name 等扁平访问）
    @property
    def multimodal_model_name(self) -> str:
        return self.llm.multimodal_model_name or self.aliyun.vision_model

    @property
    def multimodal_api_base(self) -> str:
        return self.llm.multimodal_api_base

    @property
    def multimodal_api_key(self) -> Optional[str]:
        return self.llm.multimodal_api_key or self.aliyun.access_key_secret or self.llm.agent_api_key

    @property
    def multimodal_provider(self) -> str:
        return self.llm.multimodal_provider

    @property
    def rag_initial_top_k(self) -> int:

        """向后兼容：初始召回数量。"""
        return self.rag.initial_top_k
    
    @property
    def rag_top_k(self) -> int:
        """向后兼容：最终返回数量。"""
        return self.rag.top_k
    
    @property
    def rag_use_hyde(self) -> bool:
        """向后兼容：启用HyDE。"""
        return self.rag.use_hyde
    
    @property
    def rag_use_rrf(self) -> bool:
        """向后兼容：启用RRF。"""
        return self.rag.use_rrf
    
    @property
    def rag_use_reranker(self) -> bool:
        """向后兼容：启用重排序。"""
        return self.rag.use_reranker
    
    @property
    def rag_use_time_decay(self) -> bool:
        """向后兼容：启用时间衰减。"""
        return self.rag.use_time_decay
    
    @property
    def rerank_rrf_k(self) -> int:
        """向后兼容：RRF参数k。"""
        return self.rag.rerank_rrf_k
    
    @property
    def rag_time_decay_half_life_days(self) -> float:
        """向后兼容：时间衰减半衰期。"""
        return self.rag.time_decay_half_life_days
    
    @property
    def chunk_size(self) -> int:
        """向后兼容：文本块大小。"""
        return self.rag.chunk_size
    
    @property
    def chunk_overlap(self) -> int:
        """向后兼容：块重叠大小。"""
        return self.rag.chunk_overlap
    
    @property
    def rate_limit_requests(self) -> int:
        """向后兼容：限流请求数。"""
        return self.security.rate_limit_requests
    
    @property
    def rate_limit_window_seconds(self) -> int:
        """向后兼容：限流窗口。"""
        return self.security.rate_limit_window_seconds
    
    @property
    def max_file_size_mb(self) -> int:
        """向后兼容：最大文件大小。"""
        return self.security.max_file_size_mb
    
    @property
    def allowed_extensions(self) -> set:
        """向后兼容：允许的文件扩展名。"""
        return self.security.allowed_extensions
    
    @property
    def api_key_enabled(self) -> bool:
        """向后兼容：API Key启用。"""
        return self.security.api_key_enabled
    
    @property
    def api_keys(self) -> set:
        """向后兼容：API Key列表。"""
        return self.security.api_keys
    
    @property
    def batch_max_concurrency(self) -> int:
        """向后兼容：批量并发数。"""
        return self.performance.batch_max_concurrency
    
    @property
    def embedding_max_concurrency(self) -> int:
        """向后兼容：嵌入并发数。"""
        return self.performance.embedding_max_concurrency
    
    @property
    def embedding_batch_size(self) -> int:
        """向后兼容：嵌入批大小。"""
        return self.performance.embedding_batch_size
    
    @property
    def query_timeout(self) -> int:
        """向后兼容：查询超时。"""
        return self.performance.query_timeout
    
    @property
    def process_timeout(self) -> int:
        """向后兼容：处理超时。"""
        return self.performance.process_timeout
    
    @property
    def enable_image_understanding(self) -> bool:
        """向后兼容：启用图像理解。"""
        return self.image.enable_understanding
    
    @property
    def min_image_width(self) -> int:
        """向后兼容：最小图像宽度。"""
        return self.image.min_width
    
    @property
    def min_image_height(self) -> int:
        """向后兼容：最小图像高度。"""
        return self.image.min_height
    
    @property
    def image_description_level(self) -> str:
        """向后兼容：图像描述级别。"""
        return self.image.description_level
    
    @property
    def image_cache_enabled(self) -> bool:
        """向后兼容：图像缓存启用。"""
        return self.image.cache_enabled
    
    @property
    def image_cache_dir(self) -> str:
        """向后兼容：图像缓存目录。"""
        return self.image.cache_dir
    
    @property
    def aliyun_access_key_id(self) -> Optional[str]:
        """向后兼容：阿里云AccessKey ID。"""
        return self.aliyun.access_key_id
    
    @property
    def aliyun_access_key_secret(self) -> Optional[str]:
        """向后兼容：阿里云AccessKey Secret。"""
        return self.aliyun.access_key_secret
    
    @property
    def aliyun_region(self) -> str:
        """向后兼容：阿里云区域。"""
        return self.aliyun.region
    
    @property
    def aliyun_vision_model(self) -> str:
        """向后兼容：阿里云视觉模型。"""
        return self.aliyun.vision_model
    
    @property
    def upload_dir(self) -> str:
        """向后兼容：上传目录。"""
        return self.storage.upload_dir
    
    @property
    def auto_cleanup(self) -> bool:
        """向后兼容：自动清理。"""
        return self.storage.auto_cleanup
    
    @property
    def file_retention_days(self) -> int:
        """向后兼容：文件保留天数。"""
        return self.storage.file_retention_days

    @property
    def elk_enabled(self) -> bool:
        """向后兼容：ELK 是否启用。"""
        return self.elk.enabled

    @property
    def elk_logstash_host(self) -> str:
        """向后兼容：Logstash 主机。"""
        return self.elk.logstash_host

    @property
    def elk_logstash_port(self) -> int:
        """向后兼容：Logstash 端口。"""
        return self.elk.logstash_port
    
    # ========== 功能开关 向后兼容 ==========
    @property
    def web_search_enabled(self) -> bool:
        return self.features.web_search_enabled

    @property
    def speech_enabled(self) -> bool:
        return self.features.speech_enabled

    @property
    def memory_enabled(self) -> bool:
        return self.features.memory_enabled

    @property
    def task_agent_enabled(self) -> bool:
        return self.features.task_agent_enabled

    @property
    def skill_market_enabled(self) -> bool:
        return self.features.skill_market_enabled

    @property
    def shared_kb_enabled(self) -> bool:
        return self.features.shared_kb_enabled

    @property
    def user_auth_enabled(self) -> bool:
        return self.features.user_auth_enabled

    @property
    def mindmap_export_enabled(self) -> bool:
        return self.features.mindmap_export_enabled
    
    # ========== 向后兼容：扁平属性代理 ==========
    # 通过 __getattr__ 实现，见下方
    
    # ========== 验证器 ==========
    @field_validator("security", mode="after")
    @classmethod
    def validate_security(cls, v: SecuritySettings) -> SecuritySettings:
        """验证安全配置一致性。"""
        if v.api_key_enabled and not v.api_keys:
            raise ValueError(
                "启用API Key验证(api_key_enabled=True)"
                "但未配置任何API Key(api_keys为空)"
            )
        return v
    
    @model_validator(mode="after")
    def validate_config_consistency(self) -> "Settings":
        """验证跨组配置一致性。"""
        # 嵌入维度一致性检查
        if self.database.embedding_dimension != self.llm.embedding_dimension:
            # 以 database 为准，同步 llm
            self.llm.embedding_dimension = self.database.embedding_dimension
        return self
    
    # ========== 便捷属性 ==========
    @property
    def is_production(self) -> bool:
        """判断是否为生产环境。"""
        return self.app.environment == Environment.PRODUCTION
    
    @property
    def is_debug(self) -> bool:
        """判断是否为调试模式。"""
        return self.app.debug or self.app.environment in {
            Environment.DEVELOPMENT, Environment.TESTING
        }
    
    @property
    def milvus_uri(self) -> str:
        """获取Milvus连接URI。"""
        db = self.database
        if db.milvus_user and db.milvus_password:
            return (
                f"http://{db.milvus_user}:{db.milvus_password}"
                f"@{db.milvus_host}:{db.milvus_port}"
            )
        return f"http://{db.milvus_host}:{db.milvus_port}"
    
    @property
    def embedding_config(self) -> Dict[str, Any]:
        """获取嵌入模型配置字典。"""
        return {
            "model": self.llm.embedding_model_name,
            "api_base": self.llm.embedding_api_base,
            "api_key": self.llm.embedding_api_key,
            "timeout": self.llm.embedding_timeout,
            "dimension": self.llm.embedding_dimension,
        }
    
    @property
    def reranker_config(self) -> Dict[str, Any]:
        """获取重排序模型配置字典。"""
        return {
            "model": self.llm.rerank_model_name,
            "api_base": self.llm.rerank_api_base,
            "api_key": self.llm.rerank_api_key,
        }
    
    @property
    def aliyun_config(self) -> Dict[str, Any]:
        """获取阿里云服务配置字典。"""
        return {
            "access_key_id": self.aliyun.access_key_id,
            "access_key_secret": self.aliyun.access_key_secret,
            "region": self.aliyun.region,
            "vision_model": self.aliyun.vision_model,
        }
    
    @property
    def agent_config(self) -> Dict[str, Any]:
        """获取Agent LLM配置字典。"""
        return {
            "model": self.llm.agent_model_name,
            "api_base": self.llm.agent_api_base,
            "api_key": self.llm.agent_api_key,
        }

    @property
    def image_understanding_config(self) -> Dict[str, Any]:
        """获取图像理解配置字典。"""
        return {
            "enabled": self.image.enable_understanding,
            "vision_model": self.aliyun.vision_model,
            "description_level": self.image.description_level,
            "cache_enabled": self.image.cache_enabled,
            "cache_dir": self.image.cache_dir,
            "min_image_width": self.image.min_width,
            "min_image_height": self.image.min_height,
        }

    @property
    def database_url(self) -> str:
        """获取数据库连接URL（向后兼容旧版连接池）。"""
        return "sqlite+aiosqlite:///./kquery.db"
    
    def __getattr__(self, name: str) -> Any:
        """向后兼容：允许通过 settings.xxx 访问分组配置中的属性。
        
        搜索顺序：
        1. 自身属性
        2. 各分组配置（支持大小写不敏感模糊匹配）
        """
        # 避免递归
        if name.startswith("_"):
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")
        
        groups = [
            self.app, self.database, self.llm, self.rag,
            self.security, self.storage, self.performance,
            self.image, self.aliyun, self.elk, self.features, self.ops,
        ]
        
        # 精确匹配
        for group in groups:
            if hasattr(group, name):
                return getattr(group, name)
        
        # 大小写不敏感模糊匹配（向后兼容旧版大写命名）
        name_lower = name.lower()
        for group in groups:
            for attr_name in dir(group):
                if attr_name.lower() == name_lower and not attr_name.startswith("_"):
                    return getattr(group, attr_name)
        
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'. "
            f"尝试在分组配置中查找但未找到。"
        )


@lru_cache()
def get_settings() -> Settings:
    """获取全局Settings实例（单例）。
    
    使用lru_cache确保整个应用共享同一个实例。
    
    Returns:
        Settings单例对象
        
    Example:
        >>> settings = get_settings()
        >>> print(settings.app_name)
        'KQuery Pro'
        >>> print(settings.is_production)
        False
    """
    return Settings()


def reload_settings() -> Settings:
    """重新加载配置（热更新）。
    
    清除缓存并重新从环境变量/.env文件加载配置。
    用于运行时配置更新，无需重启服务。
    
    Returns:
        新的Settings实例
        
    Example:
        >>> new_settings = reload_settings()
        >>> print(new_settings.app_name)
    """
    get_settings.cache_clear()
    return get_settings()


# ========== 导出列表 ==========
__all__ = [
    "Settings",
    "Environment",
    "get_settings",
    "reload_settings",
    # 分组配置类
    "AppSettings",
    "DatabaseSettings",
    "LLMSettings",
    "RAGSettings",
    "SecuritySettings",
    "StorageSettings",
    "PerformanceSettings",
    "ImageSettings",
    "AliyunSettings",
    "ELKSettings",
    "FeatureFlagsSettings",
    "OpsSettings",
]
