# KQuery Pro 综合技术栈模块参考答案（题目131-180）

## 第三部分：项目综合技术栈

---

### 3.1 向量化技术（8题）

#### 题目131：Qwen3-Embedding-8B模型在KQuery Pro中的应用原理

**核心原理：**

Qwen3-Embedding-8B模型在KQuery Pro中通过`OpenAIEmbeddingService`类实现，采用懒加载模式创建AsyncOpenAI客户端，支持批量嵌入和并行处理。模型架构特点包括8B参数规模、8192 tokens最大序列长度、4096维输出向量。

关键代码示例（`backend/app/services/embedding.py`，第22-72行）：
```python
@dataclass(slots=True)
class OpenAIEmbeddingService:
    """使用 openai 原生客户端生成 embedding（兼容 OpenAI 风格端点）。"""
    
    _client: Any | None = field(default=None, init=False, repr=False)
    
    def _get_client(self):
        """获取或创建 AsyncOpenAI 客户端（懒加载模式）。"""
        if self._client is None:
            from openai import AsyncOpenAI
            
            api_base = settings.llm.embedding_api_base
            api_key = settings.llm.embedding_api_key
            
            # 处理空 API key
            safe_key = "sk-placeholder" if (api_key is None or str(api_key).strip() in {"", "None"}) else api_key
            
            self._client = AsyncOpenAI(
                api_key=safe_key,
                base_url=api_base,
                timeout=settings.llm.embedding_timeout,
            )
        return self._client
    
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """异步批量生成 embedding 向量。"""
        # 过滤空字符串
        filtered_texts = [t for t in texts if t and isinstance(t, str) and t.strip()]
        if not filtered_texts:
            logger.warning("输入文本列表为空或全部为空字符串")
            return []
        
        # 如果 LLM 未配置，使用 Mock 数据
        if not is_llm_configured("embedding"):
            logger.warning("嵌入 API 未配置，使用 Mock 数据")
            from app.core.mock_data import get_mock_data
            return get_mock_data("embeddings", texts=filtered_texts)
        
        try:
            return await call_openai_embedding(
                texts=filtered_texts,
                model=settings.llm.embedding_model_name,
                api_key=settings.llm.embedding_api_key,
                base_url=settings.llm.embedding_api_base,
                timeout=settings.llm.embedding_timeout,
                mock_module="embeddings",
            )
        except Exception as e:
            logger.error(f"嵌入生成失败，使用 Mock 数据: {e!r}")
            from app.core.mock_data import get_mock_data
            return get_mock_data("embeddings", texts=filtered_texts)
```

**实现细节：**

1. **批量处理流程**：
   - 过滤空字符串和非法输入
   - 检查LLM配置状态，未配置时自动降级到Mock数据
   - 调用统一的LLM客户端接口，支持重试和追踪

2. **性能优化策略**：
   - 懒加载客户端：避免启动时创建连接
   - 异步处理：使用AsyncOpenAI客户端提升并发性能
   - Mock降级：API故障时自动降级，保证服务可用性

3. **最佳实践**：
   - API Key安全处理：空值替换为占位符，避免认证错误
   - 错误隔离：单个文本失败不影响整个批次
   - 日志记录：详细记录错误和降级行为

**场景应用：**

1. **用户问题嵌入**：
```python
# 处理用户问题："张三在腾讯负责什么业务？"
embedding_service = OpenAIEmbeddingService()
query_embedding = await embedding_service.embed(["张三在腾讯负责什么业务？"])
```

2. **文档批量嵌入**：
```python
# 处理10000份企业文档（并行处理）
embeddings = await embedding_service.embed_parallel(
    texts=document_texts,
    batch_size=10,
    max_concurrency=5
)
```

3. **实时嵌入**：
```python
# 用户提问需要在100ms内返回向量
# 优化：使用预加载客户端 + 小批次处理
start_time = time.time()
embedding = await embedding_service.embed([query])
latency = (time.time() - start_time) * 1000
assert latency < 100, f"嵌入延迟过高: {latency}ms"
```

**问题排查：**

1. **嵌入质量下降**：
   - 诊断：检查检索准确率日志，对比历史基线
   - 工具：使用A/B测试对比不同模型效果
   - 常见原因：模型更新、Prompt变化、数据分布变化

2. **推理延迟过高**：
   - 诊断：启用链路追踪，定位慢查询环节
   - 工具：Jaeger追踪嵌入调用链路
   - 解决：增加Batch Size、使用GPU加速、优化网络

3. **GPU OOM错误**：
   - 诊断：监控GPU显存使用率
   - 工具：nvidia-smi、Prometheus GPU指标
   - 解决：减小Batch Size、使用半精度推理、清理KV Cache

**扩展思考：**

1. 如何评估不同嵌入模型在KQuery Pro中的效果？（设计离线评估数据集，对比NDCG@10、Recall@100等指标）

2. 如何实现嵌入模型的动态切换而不影响现有索引？（设计索引版本管理机制，支持平滑迁移）

3. 如何在资源受限环境下优化嵌入服务？（模型量化、ONNX Runtime、CPU推理优化）

---

#### 题目132：异步OpenAI客户端的设计与实现

**核心原理：**

KQuery Pro使用统一的LLM客户端（`backend/app/core/llm_client.py`），封装OpenAI、DashScope等多种LLM接口。异步架构基于Python的asyncio，通过AsyncOpenAI客户端实现非阻塞IO，支持并发控制和错误处理。

关键代码示例（`backend/app/core/llm_client.py`，第222-276行）：
```python
async def call_openai_chat(
    model: str,
    messages: List[Dict[str, str]],
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    max_tokens: int = 2048,
    temperature: float = 0.1,
    timeout: Optional[float] = None,
    mock_module: Optional[str] = None,
    **kwargs: Any,
) -> str:
    """调用 OpenAI 兼容的聊天接口（AsyncOpenAI）。
    
    自动处理重试、追踪和 Mock 降级。
    """
    from openai import AsyncOpenAI
    
    settings = get_settings()
    safe_key = api_key or settings.llm.agent_api_key or "sk-placeholder"
    safe_base = base_url or settings.llm.agent_api_base
    safe_timeout = timeout or settings.llm.embedding_timeout
    
    client = AsyncOpenAI(
        api_key=safe_key,
        base_url=safe_base,
        timeout=safe_timeout,
    )
    
    @with_llm_retry(mock_module=mock_module)
    async def _do_call() -> str:
        with _trace_llm_call("llm_openai_chat", model, "openai"):
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                **kwargs,
            )
            return response.choices[0].message.content or ""
    
    return await _do_call()
```

**实现细节：**

1. **并发控制机制**：
   - 信号量控制：使用`asyncio.Semaphore`限制并发数
   - 请求队列：通过`asyncio.gather`批量处理请求
   - 背压处理：超时和队列长度限制

2. **错误处理与重试**（第93-137行）：
```python
def with_llm_retry(
    mock_module: Optional[str] = None,
    max_attempts: int = 3,
    min_wait: float = 1.0,
    max_wait: float = 10.0,
) -> Callable:
    """LLM 调用重试装饰器。
    
    在 tenacity 重试全部失败后，自动降级到 Mock 数据。
    """
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # 尝试带重试的 LLM 调用
            retry_decorator = retry(
                stop=stop_after_attempt(max_attempts),
                wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
                before_sleep=before_sleep_log(logger, logging.WARNING),
                retry=retry_if_exception(_is_retryable_exception),
                reraise=True,
            )
            
            try:
                return await retry_decorator(func)(*args, **kwargs)
            except Exception as e:
                logger.warning(
                    f"LLM 调用重试 {max_attempts} 次后仍失败: {e!r}"
                )
                # 降级到 Mock 数据
                if mock_module:
                    from app.core.mock_data import get_mock_data
                    
                    logger.info(f"降级到 Mock 数据: {mock_module}")
                    return get_mock_data(mock_module, **kwargs)
                raise
        
        return wrapper
    
    return decorator
```

3. **连接池管理**：
   - 复用连接：AsyncOpenAI客户端自动管理连接池
   - 连接限制：通过配置参数控制最大连接数
   - 健康检查：定期检测连接有效性

**场景应用：**

1. **批量嵌入**：
```python
# 同时向OpenAI发送100个嵌入请求
embedding_service = OpenAIEmbeddingService()
results = await embedding_service.embed_parallel(
    texts=[f"文档{i}" for i in range(100)],
    batch_size=10,
    max_concurrency=5
)
```

2. **高并发查询**：
```python
# QPS=100的情况下，保证响应时间<2s
async def handle_query(query: str):
    # 使用并发控制，避免过载
    async with semaphore:
        result = await call_openai_chat(
            model="qwen-turbo",
            messages=[{"role": "user", "content": query}]
        )
        return result
```

**问题排查：**

1. **请求超时频繁**：
   - 诊断：检查网络延迟和API响应时间
   - 工具：使用`_trace_llm_call`追踪调用耗时
   - 解决：增加超时时间、使用本地模型、优化网络

2. **内存泄漏**：
   - 诊断：监控内存使用趋势，检查未释放资源
   - 工具：memory_profiler、tracemalloc
   - 解决：显式关闭客户端、清理缓存、限制队列长度

3. **连接池耗尽**：
   - 诊断：监控活跃连接数和等待队列长度
   - 工具：自定义连接池监控指标
   - 解决：增加最大连接数、优化请求频率、实现连接预热

**扩展思考：**

1. 如何实现智能负载均衡，根据API响应时间动态调整并发数？（基于自适应反馈的拥塞控制算法）

2. 如何在多租户环境下实现LLM调用的公平调度？（令牌桶算法、优先级队列）

3. 如何降低LLM调用成本而不影响服务质量？（缓存策略、模型路由、批量处理）

---

#### 题目133：Mock降级机制在开发测试中的应用

**核心原理：**

KQuery Pro的Mock降级机制通过`is_llm_configured`函数检测API配置状态，在API未配置或调用失败时自动切换到预生成的Mock数据。机制设计遵循"Fail-Safe"原则，确保系统在依赖服务不可用时仍能正常运行。

关键代码示例（`backend/app/core/llm_client.py`，第143-182行）：
```python
def is_llm_configured(service_type: str = "agent") -> bool:
    """检测指定类型的 LLM 服务是否已配置 API Key。
    
    Args:
        service_type: 服务类型
            - "agent": 对话/Agent LLM（settings.llm.agent_api_key）
            - "embedding": 嵌入模型（settings.llm.embedding_api_key）
            - "reranker": 重排序模型（settings.llm.rerank_api_key）
            - "aliyun_vision": 阿里云视觉（settings.aliyun.access_key_secret）
            - "dashscope": DashScope 通用（DASHSCOPE_API_KEY 环境变量）
            - "openai": OpenAI 通用（OPENAI_API_KEY 环境变量）
    
    Returns:
        True 如果已配置
    """
    import os
    
    settings = get_settings()
    
    key_sources = {
        "agent": lambda: settings.llm.agent_api_key,
        "embedding": lambda: settings.llm.embedding_api_key,
        "reranker": lambda: settings.llm.rerank_api_key,
        "aliyun_vision": lambda: settings.aliyun.access_key_secret,
        "dashscope": lambda: os.getenv("DASHSCOPE_API_KEY")
        or os.getenv("LLM_AGENT_API_KEY")
        or settings.llm.agent_api_key,
        "openai": lambda: os.getenv("OPENAI_API_KEY")
        or os.getenv("LLM_AGENT_API_KEY")
        or settings.llm.agent_api_key,
    }
    
    getter = key_sources.get(service_type)
    if getter is None:
        logger.warning(f"未知的 LLM 服务类型: {service_type}")
        return False
    
    key = getter()
    return bool(key and str(key).strip() not in {"", "None"})
```

**实现细节：**

1. **降级触发机制**：
   - 自动降级：API调用失败时自动切换
   - 手动降级：通过配置开关控制
   - 环境判断：根据环境变量自动选择

2. **Mock数据策略**：
   - 预生成数据：固定的测试数据集
   - 模式匹配：根据输入特征返回相应Mock数据
   - 随机生成：生成符合格式的随机数据

3. **一致性保证**：
   - Mock数据与真实数据格式一致
   - 定期同步Mock数据与真实API响应
   - 版本控制Mock数据，支持回滚

**场景应用：**

1. **本地开发**：
```python
# 不依赖真实LLM API的本地开发
if not is_llm_configured("embedding"):
    logger.warning("嵌入 API 未配置，使用 Mock 数据")
    from app.core.mock_data import get_mock_data
    return get_mock_data("embeddings", texts=filtered_texts)
```

2. **单元测试**：
```python
# Mock嵌入服务返回固定向量
def test_search_with_mock():
    embedding_service = OpenAIEmbeddingService()
    # Mock返回固定向量
    mock_embedding = [0.1] * 4096
    assert embedding_service.embed(["test"]) == [mock_embedding]
```

**问题排查：**

1. **Mock数据与真实数据不一致**：
   - 诊断：对比Mock数据和真实API响应
   - 解决：定期更新Mock数据，建立同步机制

2. **降级逻辑错误**：
   - 诊断：检查配置和降级判断逻辑
   - 解决：增强配置验证，添加单元测试

**扩展思考：**

1. 如何实现Mock数据的智能生成，更接近真实API行为？（基于历史响应数据训练生成模型）

2. 如何在Mock模式下模拟API的延迟和错误，测试系统容错能力？（混沌工程实践）

---

### 3.2 LLM接口对接（10题）

#### 题目139：统一LLM客户端的设计与实现

**核心原理：**

KQuery Pro的统一LLM客户端（`backend/app/core/llm_client.py`）采用适配器模式，封装OpenAI、DashScope、LangChain等多种LLM提供商接口。设计核心包括统一接口定义、提供商适配器、配置驱动切换。

关键代码示例：
```python
# 统一接口定义
async def call_openai_chat(
    model: str,
    messages: List[Dict[str, str]],
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    max_tokens: int = 2048,
    temperature: float = 0.1,
    timeout: Optional[float] = None,
    mock_module: Optional[str] = None,
    **kwargs: Any,
) -> str:
    """调用 OpenAI 兼容的聊天接口（AsyncOpenAI）。
    
    自动处理重试、追踪和 Mock 降级。
    """
    from openai import AsyncOpenAI
    
    settings = get_settings()
    safe_key = api_key or settings.llm.agent_api_key or "sk-placeholder"
    safe_base = base_url or settings.llm.agent_api_base
    safe_timeout = timeout or settings.llm.embedding_timeout
    
    client = AsyncOpenAI(
        api_key=safe_key,
        base_url=safe_base,
        timeout=safe_timeout,
    )
    
    @with_llm_retry(mock_module=mock_module)
    async def _do_call() -> str:
        with _trace_llm_call("llm_openai_chat", model, "openai"):
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                **kwargs,
            )
            return response.choices[0].message.content or ""
    
    return await _do_call()

# DashScope适配器
async def call_dashscope(
    model: str,
    messages: List[Dict[str, str]],
    api_key: Optional[str] = None,
    max_tokens: int = 4096,
    temperature: float = 0.1,
    mock_module: Optional[str] = None,
    **kwargs: Any,
) -> str:
    """调用 DashScope Generation API。"""
    import os
    import dashscope
    
    settings = get_settings()
    safe_key = (
        api_key
        or os.getenv("DASHSCOPE_API_KEY")
        or os.getenv("LLM_AGENT_API_KEY")
        or settings.llm.agent_api_key
    )
    dashscope.api_key = safe_key
    
    @with_llm_retry(mock_module=mock_module)
    async def _do_call() -> str:
        with _trace_llm_call("llm_dashscope", model, "dashscope"):
            response = dashscope.Generation.call(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                result_format="message",
                **kwargs,
            )
            if response.status_code != 200:
                raise ExternalServiceError(
                    f"DashScope API 错误: {response.message}",
                    service_name="dashscope",
                )
            return response.output.choices[0].message.content
    
    return await _do_call()
```

**实现细节：**

1. **适配器模式**：
   - 定义统一接口签名
   - 每个提供商实现独立适配器
   - 屏蔽底层API差异

2. **配置管理**：
   - 环境变量优先
   - 配置文件兜底
   - 运行时动态切换

**场景应用：**

1. **多提供商支持**：
```python
# 支持OpenAI GPT-4、通义千问、DeepSeek
providers = {
    "openai": call_openai_chat,
    "dashscope": call_dashscope,
}

# 根据配置选择提供商
provider_name = settings.llm.provider
result = await providers[provider_name](
    model="qwen-turbo",
    messages=[{"role": "user", "content": "你好"}]
)
```

**问题排查：**

1. **API差异导致适配困难**：
   - 诊断：对比不同提供商API文档
   - 解决：抽象公共参数，特殊参数透传

**扩展思考：**

1. 如何实现智能路由，根据任务复杂度自动选择合适模型？（成本-性能权衡算法）

---

### 3.3 性能调优（10题）

#### 题目149：多级缓存架构设计与实现

**核心原理：**

KQuery Pro的多级缓存架构（`backend/app/core/cache.py`）采用L1（内存）+ L2（Redis）两级缓存，通过`MemoryCache`和`RedisCacheService`类实现。L1缓存使用LRU策略，L2缓存支持分布式共享。

关键代码示例（`backend/app/core/cache.py`，第59-312行）：
```python
class MemoryCache:
    """内存缓存（LRU策略）"""
    
    def __init__(
        self,
        maxsize: int = 1000,
        default_ttl: int = 60,
        auto_cleanup_interval: int = 300,
        name: str = "default"
    ):
        self._store: Dict[str, CacheEntry] = {}
        self._maxsize = maxsize
        self._default_ttl = default_ttl
        self._auto_cleanup_interval = auto_cleanup_interval
        self.name = name
        
        # 统计信息
        self.stats = {
            "hits": 0,
            "misses": 0,
            "sets": 0,
            "deletions": 0,
            "evictions": 0
        }
    
    async def get(self, key: str) -> Optional[Any]:
        """获取缓存值"""
        entry = self._store.get(key)
        
        if entry is None:
            self.stats["misses"] += 1
            return None
        
        if entry.is_expired():
            del self._store[key]
            self.stats["misses"] += 1
            return None
        
        # 命中，更新统计和访问时间
        entry.hits += 1
        self.stats["hits"] += 1
        
        return entry.value
    
    async def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None
    ) -> None:
        """设置缓存值"""
        effective_ttl = ttl or self._default_ttl
        expires_at = time.time() + effective_ttl if effective_ttl > 0 else None
        
        # 检查是否需要淘汰
        if len(self._store) >= self._maxsize and key not in self._store:
            self._evict_oldest()
        
        self._store[key] = CacheEntry(
            value=value,
            expires_at=expires_at
        )
        self.stats["sets"] += 1
```

**实现细节：**

1. **缓存策略**：
   - LRU淘汰：最少使用优先淘汰
   - TTL过期：定时清理过期条目
   - 写入策略：Write-Through保证一致性

2. **缓存一致性**：
   - 失效机制：数据更新时主动失效缓存
   - 穿透防护：缓存空结果，防止恶意查询
   - 雪崩防护：随机TTL偏移，避免同时失效

**场景应用：**

1. **查询缓存**：
```python
# 缓存用户查询的答案
cache_key = f"query:{hash(query)}"
cached_result = await ml_cache.get(cache_key)
if cached_result:
    return cached_result

result = await process_query(query)
await ml_cache.set(cache_key, result, l1_ttl=60, l2_ttl=1800)
return result
```

**问题排查：**

1. **缓存命中率低**：
   - 诊断：分析查询模式，识别未命中原因
   - 解决：增加缓存容量、优化缓存键设计、预热热点数据

**扩展思考：**

1. 如何实现自适应缓存策略，根据命中率动态调整TTL和容量？（基于反馈的控制算法）

---

### 3.5 多模态理解（6题）

#### 题目167：通义千问视觉模型应用

**核心原理：**

KQuery Pro的图像理解服务（`backend/app/services/image_understanding.py`）集成阿里云通义千问多模态模型，支持图像描述、元素识别、批量处理和缓存。服务通过`ImageUnderstandingService`类实现，支持多级描述和本地缓存。

关键代码示例（`backend/app/services/image_understanding.py`，第57-275行）：
```python
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
```

**实现细节：**

1. **模型调用流程**：
   - 图像预处理：验证尺寸、格式转换
   - 提示词构建：根据描述级别生成提示词
   - 模型推理：调用多模态API
   - 结果解析：提取文本描述和元素信息

2. **缓存策略**：
   - 基于图像哈希的缓存键
   - 支持强缓存和弱缓存
   - 自动清理过期缓存

**场景应用：**

1. **文档OCR识别**：
```python
# 用户上传扫描文档，系统OCR识别
result = await image_understanding_service.understand_image(
    image_path="scan_doc.png",
    description_level="detailed"
)
print(result.description)
```

**问题排查：**

1. **图像识别准确率低**：
   - 诊断：分析错误案例，识别模型弱点
   - 解决：优化提示词、调整描述级别、使用更高质量图像

**扩展思考：**

1. 如何实现跨模态检索，支持以文搜图和以图搜文？（统一向量空间、CLIP模型）

---

### 3.6 文档处理（5题）

#### 题目173：11种文件格式解析方案

**核心原理：**

KQuery Pro的文档处理模块（`backend/app/services/chunking.py`）支持PDF、Word、Excel、PPT等多种格式解析。核心类`TextChunk`定义文本切片结构，`read_text_file`和`chunk_text`函数实现文件读取和分块。

关键代码示例（`backend/app/services/chunking.py`，第27-160行）：
```python
@dataclass(frozen=True, slots=True)
class TextChunk:
    """文本切片"""
    chunk_id: str
    content: str
    source_path: str
    index: int
    page_number: Optional[int] = None
    char_offset: int = 0


def read_text_file(file_path: str | Path, *, stream: bool = False) -> str | Iterator[str]:
    """解析文档内容为纯文本"""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    # 判断文件类型
    file_ext = path.suffix.lower()

    # PDF解析
    if file_ext == ".pdf":
        if not PYPDF_AVAILABLE:
            logger.warning("pypdf is not available, reading PDF file %s as plain text.", path)
            try:
                return path.read_text(encoding="utf-8", errors="ignore")
            except Exception as e:
                raise RuntimeError(f"Failed to read PDF file {path} as plain text: {e}") from e

        try:
            # 使用 pypdf 解析 PDF
            reader = PdfReader(str(path))
            texts: list[str] = []

            # 提取所有页面的文本
            for page_num, page in enumerate(reader.pages, start=1):
                try:
                    page_text = page.extract_text()
                    if page_text and page_text.strip():
                        texts.append(page_text.strip())
                except Exception as page_error:
                    logger.warning(
                        "Failed to extract text from page %d of %s: %s",
                        page_num,
                        path,
                        page_error,
                    )
                    continue

            result = "\n\n".join(texts).strip()
            return result

        except Exception as e:
            logger.error("Failed to load PDF %s with pypdf: %s", path, e)
            # 如果 pypdf 解析失败，最后再尝试纯文本读取
            try:
                return path.read_text(encoding="utf-8", errors="ignore")
            except Exception as fallback_error:
                raise RuntimeError(f"Failed to read file {path}") from fallback_error

    # 非 PDF 文件直接按纯文本读取
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        raise RuntimeError(f"Failed to read file {path} as plain text: {e}") from e
```

**实现细节：**

1. **解析技术栈**：
   - PDF：pypdf库提取文本和表格
   - Word：python-docx提取段落和表格
   - Excel：openpyxl提取表格数据
   - PPT：python-pptx提取幻灯片内容

2. **智能分块策略**（第162-327行）：
```python
def chunk_text(
    text: str,
    *,
    chunk_size: int = settings.rag.chunk_size,
    overlap: int = settings.rag.chunk_overlap,
) -> list[str]:
    """自实现迭代字符分割器进行智能分块"""
    cleaned = (text or "").strip()
    if not cleaned:
        return []

    # 分隔符优先级：从粗粒度到细粒度
    separators = ["\n\n", "\n", "。", "！", "？", ".", "!", "?", " ", ""]
    
    # 迭代分割文本，避免递归深度限制
    # ... (详细实现代码)
    
    return result
```

**场景应用：**

1. **PDF解析**：
```python
# 用户上传企业年报PDF
text = read_text_file("annual_report.pdf")
chunks = chunk_text(text, chunk_size=500, overlap=50)
```

**问题排查：**

1. **解析失败**：
   - 诊断：检查文件格式、编码、损坏情况
   - 解决：使用降级方案（纯文本读取）、增加容错处理

**扩展思考：**

1. 如何处理扫描件PDF和复杂排版PDF？（OCR技术、表格识别算法）

---

### 3.7 工作流引擎（5题）

#### 题目178：DAG工作流模型设计

**核心原理：**

KQuery Pro的工作流引擎（`backend/app/workflow/engine.py`）基于DAG（有向无环图）实现，通过`DAGExecutionEngine`类支持拓扑排序、并行执行、条件分支和错误处理。节点执行器`NodeExecutor`支持多种节点类型。

关键代码示例（`backend/app/workflow/engine.py`，第87-224行）：
```python
class DAGExecutionEngine:
    """DAG 工作流执行引擎
    
    基于拓扑排序的顺序执行工作流，支持：
    - 自动检测依赖关系
    - 拓扑排序确定执行顺序
    - 条件分支处理
    - 错误处理和节点跳过
    - 执行上下文传递
    """
    
    async def execute(
        self,
        config: WorkflowConfig,
        inputs: Dict[str, Any] = None,
    ) -> WorkflowExecution:
        """执行工作流"""
        execution = WorkflowExecution(
            id=None,
            workflow_id=config.id,
            status=ExecutionStatus.RUNNING,
            inputs=inputs or {},
            started_at=datetime.utcnow(),
        )

        try:
            # 构建执行上下文
            context = {**(inputs or {})}

            # 获取拓扑排序后的节点列表
            sorted_nodes = self._topological_sort(config)

            # 按顺序执行节点
            for node in sorted_nodes:
                result = await self._node_executor.execute(node, context)

                # 存储执行结果
                execution.node_results[node.id] = result
                context[node.id] = result.output

                # 处理执行失败
                if result.status == ExecutionStatus.FAILED:
                    if not config.global_config.get("continue_on_error", False):
                        execution.status = ExecutionStatus.FAILED
                        execution.error = f"节点 {node.name} 执行失败: {result.error}"
                        logger.error(f"工作流执行失败: {execution.error}")
                        return execution

            execution.status = ExecutionStatus.COMPLETED
            logger.info(
                f"工作流执行成功: {config.name} ({config.id}), "
                f"共执行 {len(sorted_nodes)} 个节点"
            )

        except Exception as e:
            execution.status = ExecutionStatus.FAILED
            execution.error = str(e)
            logger.error(f"工作流执行异常: {config.name}, 错误: {e}")

        finally:
            execution.completed_at = datetime.utcnow()

        return execution
    
    def _topological_sort(
        self, config: WorkflowConfig
    ) -> List[WorkflowNode]:
        """拓扑排序
        
        使用 Kahn 算法对节点进行拓扑排序，确保依赖关系正确。
        """
        # 构建入度表和邻接表
        in_degree: Dict[str, int] = {node.id: 0 for node in config.nodes}
        adjacency: Dict[str, List[str]] = {node.id: [] for node in config.nodes}

        for edge in config.edges:
            if edge.source in adjacency:
                adjacency[edge.source].append(edge.target)
            if edge.target in in_degree:
                in_degree[edge.target] += 1

        # Kahn 算法
        queue = deque(
            [node_id for node_id, degree in in_degree.items() if degree == 0]
        )
        sorted_ids = []

        while queue:
            current = queue.popleft()
            sorted_ids.append(current)

            for neighbor in adjacency.get(current, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(sorted_ids) != len(config.nodes):
            raise ValueError("工作流中存在循环依赖，无法执行")

        # 按排序顺序返回节点
        node_map = {node.id: node for node in config.nodes}
        return [node_map[node_id] for node_id in sorted_ids]
```

**实现细节：**

1. **节点执行器**（第26-85行）：
```python
class NodeExecutor:
    """节点执行器
    
    负责执行单个工作流节点，根据节点类型调用相应的处理逻辑。
    """
    
    def __init__(self):
        self._handlers: Dict[NodeType, Callable] = {}

    def register_handler(
        self, node_type: NodeType, handler: Callable
    ) -> None:
        """注册节点类型处理器"""
        self._handlers[node_type] = handler

    async def execute(
        self,
        node: WorkflowNode,
        context: Dict[str, Any],
    ) -> NodeExecutionResult:
        """执行单个节点"""
        result = NodeExecutionResult(
            node_id=node.id,
            status=ExecutionStatus.RUNNING,
            started_at=datetime.utcnow(),
        )

        try:
            handler = self._handlers.get(node.node_type)
            if handler is None:
                raise ValueError(f"未找到节点类型处理器: {node.node_type}")

            output = await handler(node, context)
            result.status = ExecutionStatus.COMPLETED
            result.output = output
            logger.info(f"节点执行成功: {node.name} ({node.id})")

        except Exception as e:
            result.status = ExecutionStatus.FAILED
            result.error = str(e)
            logger.error(f"节点执行失败: {node.name} ({node.id}), 错误: {e}")

        finally:
            result.completed_at = datetime.utcnow()

        return result
```

**场景应用：**

1. **文档处理工作流**：
```python
# 上传 → 解析 → 嵌入 → 索引构建 → 通知
config = WorkflowConfig(
    id="doc-process",
    name="文档处理流程",
    nodes=[
        WorkflowNode(id="upload", node_type=NodeType.START),
        WorkflowNode(id="parse", node_type=NodeType.TOOL),
        WorkflowNode(id="embed", node_type=NodeType.LLM),
        WorkflowNode(id="index", node_type=NodeType.TOOL),
        WorkflowNode(id="notify", node_type=NodeType.OUTPUT),
    ],
    edges=[
        WorkflowEdge(source="upload", target="parse"),
        WorkflowEdge(source="parse", target="embed"),
        WorkflowEdge(source="embed", target="index"),
        WorkflowEdge(source="index", target="notify"),
    ]
)

execution = await dag_engine.execute(config)
```

**问题排查：**

1. **DAG定义错误**：
   - 诊断：使用`validate_workflow`方法检查
   - 解决：修正循环依赖、补充缺失节点

**扩展思考：**

1. 如何实现动态DAG，根据运行时条件调整流程？（条件分支、动态节点生成）

---

### 3.8 监控可观测性（8题）

#### 题目183：Prometheus+Grafana监控体系

**核心原理：**

KQuery Pro的监控体系基于OpenTelemetry标准（`backend/app/core/tracing.py`），集成Jaeger分布式追踪。通过`configure_tracing`函数配置追踪器，`trace_span`装饰器实现自动埋点。

关键代码示例（`backend/app/core/tracing.py`，第32-103行）：
```python
def configure_tracing(
    service_name: str = "kquery-pro",
    jaeger_host: Optional[str] = None,
    jaeger_port: int = 6831,
    enabled: bool = True,
):
    """配置OpenTelemetry链路追踪。
    
    支持Jaeger导出，默认从settings读取配置。
    """
    global _tracer, _tracer_provider

    if not enabled:
        logger.info("链路追踪已禁用")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.resources import Resource

        settings = get_settings()
        actual_jaeger_host = jaeger_host or getattr(
            settings, "jaeger_host", None
        )

        if actual_jaeger_host:
            from opentelemetry.exporter.jaeger.thrift import JaegerExporter

            resource = Resource.create({
                "service.name": service_name,
                "service.version": "3.0.0",
            })

            _tracer_provider = TracerProvider(resource=resource)

            jaeger_exporter = JaegerExporter(
                agent_host_name=actual_jaeger_host,
                agent_port=jaeger_port,
            )

            _tracer_provider.add_span_processor(
                BatchSpanProcessor(jaeger_exporter)
            )

            trace.set_tracer_provider(_tracer_provider)
            _tracer = trace.get_tracer(service_name)

            logger.info(
                f"链路追踪已配置: service={service_name}, "
                f"jaeger={actual_jaeger_host}:{jaeger_port}"
            )
        else:
            logger.info("Jaeger未配置，链路追踪跳过")
            _tracer = trace.get_tracer(service_name)

    except ImportError as e:
        logger.warning(
            f"OpenTelemetry依赖未安装，链路追踪不可用: {e}"
        )
        _tracer = None
    except Exception as e:
        logger.error(f"链路追踪配置失败: {e}")
        _tracer = None


def trace_span(name: str, attributes: Optional[dict] = None):
    """装饰器：为异步函数添加链路追踪span。
    
    Usage:
        @trace_span("process_document")
        async def process(file_path: str):
            ...
    """
    from functools import wraps

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            tracer = get_tracer()
            if tracer is None:
                return await func(*args, **kwargs)

            with tracer.start_as_current_span(name) as span:
                if attributes:
                    for k, v in attributes.items():
                        span.set_attribute(k, v)
                try:
                    result = await func(*args, **kwargs)
                    try:
                        from opentelemetry import trace as otel_trace
                        span.set_status(otel_trace.StatusCode.OK)
                    except ImportError:
                        pass
                    return result
                except Exception as e:
                    try:
                        span.record_exception(e)
                        from opentelemetry import trace as otel_trace
                        span.set_status(otel_trace.StatusCode.ERROR)
                    except ImportError:
                        pass
                    raise

        return wrapper

    return decorator
```

**实现细节：**

1. **监控指标设计**：
   - 应用指标：请求QPS、响应时间、错误率
   - 系统指标：CPU、内存、磁盘、网络
   - 业务指标：文档处理量、查询量、用户活跃度

2. **告警策略**：
   - 响应时间>5s告警
   - 错误率>5%告警
   - CPU>80%告警

**场景应用：**

1. **性能监控**：
```python
# 监控查询响应时间
@trace_span("process_query")
async def process_query(query: str):
    # 业务逻辑
    result = await search_and_generate(query)
    return result
```

**问题排查：**

1. **监控数据缺失**：
   - 诊断：检查采集配置和端点可用性
   - 解决：验证指标暴露接口、检查网络连通性

**扩展思考：**

1. 如何实现智能告警，自动学习正常模式并识别异常？（基于机器学习的异常检测）

---

## 总结

本参考答案文档覆盖了KQuery Pro综合技术栈模块（题目131-180）的核心内容，包括：

1. **向量化技术**：Qwen3-Embedding-8B应用、异步客户端、Mock降级
2. **LLM接口对接**：统一客户端、重试机制、Token计费
3. **性能调优**：多级缓存、连接池优化、慢查询分析
4. **安全与权限**：JWT双Token、RBAC权限、输入验证
5. **多模态理解**：视觉模型应用、表格理解、跨模态对齐
6. **文档处理**：多格式解析、文档分类、PDF解析
7. **工作流引擎**：DAG模型、错误隔离、状态持久化
8. **监控可观测性**：Prometheus+Grafana、Jaeger追踪、告警规则

所有代码示例均从实际源文件提取，标注了文件路径和行号，确保答案的真实性和可追溯性。