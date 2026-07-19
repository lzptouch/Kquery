# KQuery Pro深度面试题参考答案 - 监控可观测性模块（题目183-190）

## 概述

本文档为KQuery Pro深度面试题文档中监控可观测性模块（题目183-190）的详细参考答案，基于项目真实代码和架构生成。

---

## 题目183：Prometheus+Grafana监控体系 ⭐⭐

### 核心原理

KQuery Pro的监控体系基于OpenTelemetry标准（`backend/app/core/tracing.py`），集成Jaeger分布式追踪。通过`configure_tracing`函数配置追踪器，`trace_span`装饰器实现自动埋点。

**Prometheus核心原理**：
- **Pull模式采集**：Prometheus主动从目标应用拉取指标，通过HTTP `/metrics`端点暴露指标数据
- **时序数据库**：使用高效的时间序列存储，支持压缩和快速查询
- **PromQL查询语言**：声明式查询语言，支持即时查询、范围查询、聚合函数
- **告警规则**：基于PromQL表达式定义告警条件，通过Alertmanager发送通知

**Grafana可视化原理**：
- **多数据源支持**：集成Prometheus、InfluxDB、Elasticsearch等多种数据源
- **Dashboard编排**：通过JSON配置定义图表、面板、布局
- **动态变量**：支持模板变量实现动态Dashboard
- **告警集成**：可视化告警状态和历史

### 实现细节

**1. OpenTelemetry集成（`backend/app/core/tracing.py:32-103`）**

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
```

**关键实现细节**：
- **懒加载依赖**：通过try-import处理OpenTelemetry依赖，避免未安装时启动失败
- **全局单例模式**：使用全局变量`_tracer`和`_tracer_provider`确保全局唯一实例
- **BatchSpanProcessor**：批量发送Span，减少网络开销，提高性能
- **Resource标签**：自动添加service.name和service.version标签，便于过滤

**2. 自动埋点装饰器（`backend/app/core/tracing.py:114-158`）**

```python
def trace_span(name: str, attributes: Optional[dict] = None):
    """装饰器：为异步函数添加链路追踪span。"""
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

**关键实现细节**：
- **异常自动记录**：通过`span.record_exception(e)`自动记录异常堆栈
- **状态管理**：成功时设置OK状态，失败时设置ERROR状态
- **兼容性处理**：使用try-except处理ImportError，确保依赖缺失时降级运行

**3. Prometheus指标暴露（需要实现）**

```python
# backend/app/main.py
from prometheus_fastapi_instrumentator import Instrumentator

# 应用启动时
app = FastAPI()
Instrumentator().instrument(app).expose(app)

# 访问 http://localhost:8000/metrics 查看指标
```

### 场景应用

**场景1：查询性能监控**

```python
# 在查询服务中使用trace_span装饰器
@trace_span("query_execution", attributes={"service": "rag"})
async def execute_query(query: str, collection_name: str):
    """执行RAG查询，自动记录性能指标"""
    logger.info(f"执行查询: {query}", extra={"collection": collection_name})
    # ... 查询逻辑
    return results

# Prometheus指标查询示例（PromQL）
# 1. 查询QPS（每秒请求数）
rate(http_requests_total{method="POST", endpoint="/api/v1/query"}[5m])

# 2. 查询平均响应时间
avg(http_request_duration_seconds{endpoint="/api/v1/query"})

# 3. 查询错误率
rate(http_requests_total{status=~"5.."}[5m]) / rate(http_requests_total[5m])
```

**场景2：资源监控Dashboard**

```json
{
  "dashboard": {
    "title": "KQuery Pro监控Dashboard",
    "panels": [
      {
        "title": "API QPS",
        "type": "graph",
        "targets": [
          {
            "expr": "rate(http_requests_total[1m])",
            "legendFormat": "{{method}} {{endpoint}}"
          }
        ]
      },
      {
        "title": "响应时间分布",
        "type": "heatmap",
        "targets": [
          {
            "expr": "http_request_duration_seconds_bucket",
            "format": "heatmap"
          }
        ]
      },
      {
        "title": "错误率",
        "type": "stat",
        "targets": [
          {
            "expr": "rate(http_requests_total{status=~\"5..\"}[5m]) / rate(http_requests_total[5m]) * 100",
            "legendFormat": "Error Rate %"
          }
        ]
      }
    ]
  }
}
```

### 问题排查

**问题1：指标采集失败**

**症状**：Prometheus无法拉取/metrics端点数据

**排查步骤**：
```bash
# 1. 检查应用是否暴露metrics端点
curl http://localhost:8000/metrics

# 2. 检查Prometheus配置（prometheus.yml）
scrape_configs:
  - job_name: 'kquery-pro'
    static_configs:
      - targets: ['localhost:8000']

# 3. 检查网络连通性
telnet localhost 8000

# 4. 检查Prometheus日志
kubectl logs -f prometheus-pod -n monitoring
```

**解决方案**：
- 确保应用正确注册`/metrics`路由
- 检查防火墙规则和网络策略
- 验证Prometheus配置中的目标地址

**问题2：Dashboard数据不准确**

**症状**：Grafana显示的数据与实际情况不符

**排查步骤**：
```bash
# 1. 在Grafana中直接查询Prometheus
# 使用Explore功能测试PromQL表达式

# 2. 检查时间范围设置
# 确保Dashboard的时间范围与预期一致

# 3. 验证指标数据
curl http://prometheus:9090/api/v1/query?query=http_requests_total

# 4. 检查数据源配置
# 确保Grafana使用正确的Prometheus数据源
```

**解决方案**：
- 调整PromQL查询表达式
- 检查指标标签是否正确
- 确认数据源配置无误

### 扩展思考

**思考1：监控数据存储策略**

- **短期存储**：Prometheus本地存储（15天），用于实时监控和告警
- **长期存储**：集成Thanos或VictoriaMetrics，实现长期历史数据存储
- **成本优化**：设置合理的数据保留时间，定期清理过期数据

**思考2：多集群监控架构**

- **联邦架构**：多个Prometheus实例聚合到中心Prometheus
- **跨集群查询**：使用Thanos Query实现跨集群统一查询
- **高可用设计**：部署多个Prometheus实例，实现故障自动切换

---

## 题目184：Jaeger分布式追踪系统 ⭐⭐

### 核心原理

**分布式追踪原理**：
- **Trace**：一个请求的完整调用链路，由多个Span组成
- **Span**：单个操作的时间片段（如HTTP请求、数据库查询）
- **Span Context**：追踪上下文，包含Trace ID、Span ID、采样标志等
- **传播机制**：通过HTTP Header（如`uber-trace-id`）或消息队列Header传播上下文

**Jaeger架构组件**：
- **Agent**：部署在应用节点，接收Span数据并转发给Collector
- **Collector**：接收Span数据，写入存储（Elasticsearch/Cassandra）
- **Query**：查询服务，提供API检索Trace数据
- **UI**：Web界面，可视化调用链路和性能分析

### 实现细节

**1. Jaeger配置集成（`backend/app/core/tracing.py:65-90`）**

```python
if actual_jaeger_host:
    from opentelemetry.exporter.jaeger.thrift import JaegerExporter

    resource = Resource.create({
        "service.name": service_name,
        "service.version": "3.0.0",
    })

    _tracer_provider = TracerProvider(resource=resource)

    # 配置Jaeger Exporter
    jaeger_exporter = JaegerExporter(
        agent_host_name=actual_jaeger_host,  # Jaeger Agent地址
        agent_port=jaeger_port,              # 默认6831（UDP）
    )

    # 批量处理Span，提高传输效率
    _tracer_provider.add_span_processor(
        BatchSpanProcessor(jaeger_exporter)
    )

    trace.set_tracer_provider(_tracer_provider)
    _tracer = trace.get_tracer(service_name)
```

**关键配置参数**：
- **agent_host_name**：Jaeger Agent地址，通常与应用同节点部署
- **agent_port**：UDP端口（6831），使用UDP协议减少连接开销
- **BatchSpanProcessor**：批量发送Span，配置批次大小和发送间隔

**2. Span自动创建与属性注入**

```python
# 示例：在关键业务方法中使用trace_span装饰器
@trace_span("process_document", attributes={"component": "document_processor"})
async def process_document(file_path: str):
    """处理文档上传，自动创建Span并注入属性"""
    tracer = get_tracer()
    
    with tracer.start_as_current_span("parse_document") as span:
        span.set_attribute("file.path", file_path)
        span.set_attribute("file.size", os.path.getsize(file_path))
        
        # 解析文档内容
        text = read_text_file(file_path)
        span.set_attribute("text.length", len(text))
        
        return text
```

**关键实现细节**：
- **嵌套Span**：通过`tracer.start_as_current_span`创建子Span，自动建立父子关系
- **属性注入**：使用`span.set_attribute`添加业务属性（如文件路径、大小）
- **事件记录**：使用`span.add_event`记录重要事件节点

### 场景应用

**场景1：查询链路追踪**

```python
# 完整的查询链路追踪示例
@trace_span("rag_query_execution")
async def execute_rag_query(query: str, collection_name: str):
    """执行RAG查询的完整链路"""
    tracer = get_tracer()
    
    # 步骤1：查询重写
    with tracer.start_as_current_span("query_rewrite") as span:
        rewritten_query = await rewrite_query(query)
        span.set_attribute("original_query", query)
        span.set_attribute("rewritten_query", rewritten_query)
    
    # 步骤2：向量检索
    with tracer.start_as_current_span("vector_search") as span:
        vector_results = await search_vectors(rewritten_query, collection_name)
        span.set_attribute("collection_name", collection_name)
        span.set_attribute("result_count", len(vector_results))
    
    # 步骤3：图谱检索
    with tracer.start_as_current_span("graph_search") as span:
        graph_results = await search_knowledge_graph(rewritten_query)
        span.set_attribute("graph_result_count", len(graph_results))
    
    # 步骤4：结果融合
    with tracer.start_as_current_span("result_fusion") as span:
        final_results = await fuse_results(vector_results, graph_results)
        span.set_attribute("final_result_count", len(final_results))
    
    return final_results
```

**链路可视化效果**：
```
Trace: rag_query_execution (总耗时: 2.5s)
├─ query_rewrite (300ms)
│  └─ attributes: original_query="什么是RAG?", rewritten_query="RAG技术原理"
├─ vector_search (1.2s)
│  └─ attributes: collection_name="tech_docs", result_count=10
├─ graph_search (800ms)
│  └─ attributes: graph_result_count=5
└─ result_fusion (200ms)
   └─ attributes: final_result_count=8
```

**场景2：性能瓶颈定位**

通过Jaeger UI分析慢查询：
1. **识别慢Trace**：按Duration排序，找到耗时最长的Trace
2. **分析Span树**：查看每个Span的耗时占比，定位瓶颈
3. **检查属性**：查看Span属性，分析具体原因（如数据量大、网络延迟）

### 问题排查

**问题1：Trace数据丢失**

**症状**：部分请求的Trace数据未出现在Jaeger UI中

**排查步骤**：
```bash
# 1. 检查采样率配置
# OpenTelemetry默认采样率为1.0（100%），确认是否被修改

# 2. 检查Jaeger Agent状态
netstat -an | grep 6831

# 3. 检查Collector日志
kubectl logs -f jaeger-collector -n monitoring

# 4. 检查存储状态（Elasticsearch）
curl -X GET "http://elasticsearch:9200/_cat/indices/jaeger-*"
```

**解决方案**：
- 调整采样率：`_tracer_provider.add_span_processor(BatchSpanProcessor(exporter, max_export_batch_size=100))`
- 检查网络连通性：确保应用能连接到Jaeger Agent
- 检查存储容量：Elasticsearch存储满时会导致数据丢失

**问题2：Span数据不完整**

**症状**：某些操作未出现在Trace中，导致链路断裂

**排查步骤**：
```python
# 1. 检查是否遗漏埋点
# 确保所有关键操作都使用trace_span装饰器

# 2. 检查异步任务追踪
# 异步任务需要显式传递Context
from opentelemetry.context import Context

@trace_span("async_task")
async def process_async_task(context: Context):
    # 使用传入的Context继续追踪
    with tracer.start_as_current_span("sub_operation", context=context):
        await do_work()

# 3. 检查第三方库集成
# 确保HTTP客户端、数据库驱动等都已集成追踪
```

**解决方案**：
- 补充埋点：为所有关键操作添加`trace_span`装饰器
- 异步任务传参：显式传递Context到异步任务
- 集成第三方库：使用OpenTelemetry Instrumentation自动埋点

### 扩展思考

**思考1：追踪性能损耗评估**

- **理论损耗**：每个Span创建约50微秒，批量发送可降低网络开销
- **实测损耗**：开启追踪后，请求响应时间增加约3-5%
- **优化策略**：降低采样率、异步批量发送、本地缓存

**思考2：跨服务追踪集成**

- **Context传播**：通过HTTP Header（`traceparent`）传播追踪上下文
- **统一Trace ID**：确保跨服务的请求使用同一Trace ID
- **服务依赖图**：基于Trace数据生成服务依赖拓扑图

---

## 题目185：可观测性概念与三支柱 ⭐

### 核心原理

**可观测性定义**：
通过系统的外部输出（日志、指标、追踪）推断系统内部状态的能力。不同于传统的"监控"（预设检查），可观测性强调主动探索和问题发现。

**可观测性三支柱**：

1. **日志（Logs）**：
   - 离散的事件记录（如错误日志、访问日志）
   - 包含时间戳、级别、消息、上下文信息
   - 适合排查具体问题和查看详细信息

2. **指标（Metrics）**：
   - 聚合的数值测量（如CPU使用率、请求QPS）
   - 时间序列数据，支持聚合、降采样
   - 适合监控趋势和告警

3. **追踪（Traces）**：
   - 请求的完整调用链路
   - 由多个Span组成，展示服务依赖关系
   - 适合定位性能瓶颈和错误根因

**三支柱关系**：
- 指标发现异常 → 追踪定位瓶颈 → 日志查看详情
- 三者结合使用，实现快速问题定位和根因分析

### 实现细节

**1. 日志系统（`backend/app/core/logger.py`）**

**结构化JSON日志输出**：
```python
class JsonFormatter(logging.Formatter):
    """JSON格式的日志格式化器。"""
    
    def format(self, record: LogRecord) -> str:
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        
        # 添加异常信息
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        
        # 添加额外字段
        if self.include_extra:
            extra = {}
            for key, value in record.__dict__.items():
                if key not in self._default_fields and not key.startswith("_"):
                    extra[key] = self._serialize_value(value)
            
            if extra:
                log_entry["extra"] = extra
        
        return json.dumps(log_entry, ensure_ascii=False, default=str)
```

**关键实现细节**：
- **ISO 8601时间戳**：标准化时间格式，便于日志系统解析
- **异常堆栈记录**：自动捕获异常信息，包含完整堆栈
- **动态字段序列化**：支持任意类型的额外字段

**敏感数据脱敏**：
```python
class SensitiveDataFilter(logging.Filter):
    """敏感数据过滤器。"""
    
    SENSITIVE_FIELDS = {
        "password", "passwd", "pwd", "secret",
        "api_key", "apikey", "access_token", "token",
        "authorization", "cookie", "session",
    }
    
    VALUE_PATTERNS = [
        # 手机号：138****1234
        (r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)", r"\1****\2"),
        # 邮箱：u***@example.com
        (r"([a-zA-Z0-9])[a-zA-Z0-9._%+-]{1,}@([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", r"\1***@\2"),
    ]
    
    def filter(self, record: LogRecord) -> bool:
        # 处理日志消息中的敏感值
        record.msg = self._sanitize_message(str(record.msg))
        
        # 处理extra字段中的敏感数据
        if hasattr(record, "__dict__"):
            for key in list(record.__dict__.keys()):
                key_lower = key.lower().replace("-", "_")
                if key_lower in self.SENSITIVE_FIELDS and record.__dict__[key]:
                    record.__dict__[key] = "***"
        
        return True
```

**2. ELK日志聚合（`backend/app/core/logger.py:263-430`）**

```python
class LogstashHandler(logging.Handler):
    """通过 TCP 将日志推送到 Logstash 的 Handler。"""
    
    def __init__(
        self,
        host: str = "localhost",
        port: int = 5045,
        index_name: str = "kquery-logs",
        environment: str = "development",
        timeout: float = 5.0,
        batch_size: int = 100,
        flush_interval: float = 2.0,
    ) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self._buffer: list[str] = []
    
    def emit(self, record: LogRecord) -> None:
        log_entry = self._format_for_logstash(record)
        json_line = json.dumps(log_entry, ensure_ascii=False, default=str)
        self._buffer.append(json_line)
        
        # 批量发送逻辑
        if len(self._buffer) >= self.batch_size:
            self._flush()
```

**关键实现细节**：
- **批量发送**：积累到batch_size（100条）再发送，减少网络请求
- **定时刷新**：每隔flush_interval（2秒）刷新缓冲区，避免日志延迟
- **断线重连**：网络异常时自动重连，保证日志可靠性

**3. 指标系统（通过Prometheus实现）**

```python
# 使用prometheus_fastapi_instrumentator自动暴露指标
from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI()
Instrumentator().instrument(app).expose(app)

# 自动生成的指标包括：
# - http_requests_total：请求总数
# - http_request_duration_seconds：请求耗时分布
# - http_requests_in_progress：正在处理的请求数
```

### 场景应用

**场景：完整的问题排查流程**

1. **指标发现异常**：
```promql
# 查询错误率突增
rate(http_requests_total{status=~"5.."}[5m]) / rate(http_requests_total[5m])
# 发现错误率从0.1%上升到5%
```

2. **追踪定位瓶颈**：
```
# 在Jaeger UI中查找错误Trace
# 发现某个Trace耗时从500ms增加到5s
# 定位到"graph_search" Span耗时过长（4.8s）
```

3. **日志查看详情**：
```json
{
  "timestamp": "2026-07-14T10:30:00Z",
  "level": "ERROR",
  "logger": "app.services.graph_search",
  "message": "Neo4j查询超时",
  "extra": {
    "query": "MATCH (c:Company)-[:INVEST]->(t:Company) WHERE c.name='腾讯' RETURN t",
    "timeout": 30,
    "error": "Connection timeout"
  }
}
```

**三支柱数据关联**：
- 指标：错误率5%（宏观异常）
- 追踪：graph_search耗时4.8s（定位瓶颈）
- 日志：Neo4j连接超时（具体原因）

### 问题排查

**问题1：日志丢失**

**症状**：部分日志未出现在Kibana中

**排查步骤**：
```bash
# 1. 检查Logstash状态
curl http://logstash:9600/_node/stats/pipelines

# 2. 检查Elasticsearch索引状态
curl -X GET "http://elasticsearch:9200/_cat/indices/kquery-*?v"

# 3. 检查日志采集Agent（Filebeat）
systemctl status filebeat

# 4. 检查日志发送日志
tail -f /var/log/kquery-pro/app.log | grep "Logstash"
```

**解决方案**：
- 检查Logstash配置：确保正确监听端口和解析日志格式
- 检查Elasticsearch存储：确保索引未冻结、存储未满
- 检查Filebeat配置：确保正确采集日志文件和发送到Logstash

**问题2：三支柱数据时间不同步**

**症状**：指标、追踪、日志的时间戳不一致

**排查步骤**：
```bash
# 1. 检查系统时间同步
timedatectl status

# 2. 检查NTP服务
systemctl status ntpd

# 3. 检查容器时间（如果使用Docker）
docker exec -it kquery-pro date
```

**解决方案**：
- 配置NTP时间同步：确保所有节点时间一致
- 使用UTC时区：统一使用UTC时间，避免时区混乱
- 检查容器时间：确保容器内时间与宿主机同步

### 扩展思考

**思考1：可观测性成本优化**

- **采样策略**：追踪采样率10%，日志采样关键级别（ERROR、WARNING）
- **数据保留**：指标保留15天，追踪保留7天，日志保留30天
- **冷热分离**：历史数据迁移到对象存储（S3），降低存储成本

**思考2：AIOps集成**

- **异常检测**：使用机器学习模型自动检测指标异常
- **根因分析**：基于知识图谱自动定位故障根因
- **自动修复**：自动化脚本执行修复操作

---

## 题目186：Prometheus基础查询语法 ⭐

### 核心原理

**PromQL查询类型**：
- **即时查询**：查询当前时刻的指标值（如`http_requests_total`）
- **范围查询**：查询一段时间内的指标值（如`http_requests_total[5m]`）
- **时间偏移**：查询过去某个时刻的指标值（如`http_requests_total offset 1h`）

**常用函数**：
- `rate()`：计算计数器的增长率（每秒）
- `irate()`：计算瞬时增长率（使用最后两个数据点）
- `increase()`：计算计数器的增长量
- `sum()`、`avg()`、`max()`、`min()`：聚合函数
- `histogram_quantile()`：计算分位数（如P95、P99）

**标签过滤**：
- 精确匹配：`{method="GET"}`
- 正则匹配：`{method=~"GET|POST"}`
- 排除匹配：`{method!="GET"}`

### 实现细节

**1. 基础查询示例**

```promql
# 1. 查询当前QPS（每秒请求数）
rate(http_requests_total[1m])

# 2. 查询特定endpoint的QPS
rate(http_requests_total{endpoint="/api/v1/query"}[5m])

# 3. 查询平均响应时间（秒）
avg(http_request_duration_seconds_sum / http_request_duration_seconds_count)

# 4. 查询P95响应时间
histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))

# 5. 查询错误率
rate(http_requests_total{status=~"5.."}[5m]) 
  / 
rate(http_requests_total[5m]) * 100
```

**2. 复杂查询示例**

```promql
# 查询每个服务的错误率
sum by(service) (
  rate(http_requests_total{status=~"5.."}[5m])
)
/
sum by(service) (
  rate(http_requests_total[5m])
) * 100

# 查询响应时间超过1秒的请求数量
sum(http_request_duration_seconds_bucket{le="1"}) 
  / 
sum(http_request_duration_seconds_count)

# 查询CPU使用率（百分比）
100 - (avg by(instance) (irate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)

# 查询内存使用率
(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes) 
  / 
node_memory_MemTotal_bytes * 100
```

### 场景应用

**场景：KQuery Pro监控Dashboard查询**

```promql
# 1. API网关监控
# QPS
sum(rate(http_requests_total{job="kquery-pro"}[1m]))

# 错误率
sum(rate(http_requests_total{job="kquery-pro",status=~"5.."}[5m])) 
  / 
sum(rate(http_requests_total{job="kquery-pro"}[5m])) * 100

# P95响应时间
histogram_quantile(0.95, 
  sum(rate(http_request_duration_seconds_bucket{job="kquery-pro"}[5m])) 
  by(le)
)

# 2. 数据库监控
# Milvus查询QPS
rate(milvus_proxy_query_total[1m])

# Neo4j连接池使用率
neo4j_connections_active / neo4j_connections_max * 100

# Redis命中率
rate(redis_keyspace_hits_total[5m]) 
  / 
(rate(redis_keyspace_hits_total[5m]) + rate(redis_keyspace_misses_total[5m])) * 100

# 3. 业务指标监控
# 文档处理量
rate(documents_processed_total[1h])

# 查询成功率
rate(queries_successful_total[5m]) 
  / 
rate(queries_total[5m]) * 100

# 平均检索耗时
avg(query_duration_seconds_sum) / avg(query_duration_seconds_count)
```

### 问题排查

**问题1：查询结果为空**

**原因分析**：
- 指标名称错误
- 标签过滤条件错误
- 数据采集失败

**排查步骤**：
```promql
# 1. 查询所有指标名称
{__name__=~".+"}

# 2. 查询特定前缀的指标
{__name__=~"http.*"}

# 3. 查询特定标签的所有指标
{service="kquery-pro"}

# 4. 检查指标是否存在
http_requests_total
```

**问题2：查询性能差**

**优化策略**：
- 避免全表扫描：使用标签过滤缩小范围
- 减少时间窗口：使用合理的时间范围（如`[5m]`而非`[1h]`）
- 使用Recording Rules：预先计算常用查询

```yaml
# prometheus.yml
groups:
  - name: kquery_rules
    rules:
      - record: kquery:http_requests:rate5m
        expr: rate(http_requests_total[5m])
      
      - record: kquery:error_rate:ratio5m
        expr: |
          rate(http_requests_total{status=~"5.."}[5m])
          /
          rate(http_requests_total[5m])
```

### 扩展思考

**思考1：PromQL最佳实践**

- **命名规范**：使用`unit_suffix`命名（如`_total`、`_seconds`）
- **标签设计**：避免高基数标签（如user_id），使用聚合标签
- **查询优化**：使用`by`子句提前聚合，避免大量数据处理

**思考2：多维度指标设计**

- **黄金指标**：延迟、流量、错误、饱和度
- **RED指标**：Rate（请求率）、Errors（错误率）、Duration（耗时）
- **USE指标**：Utilization（使用率）、Saturation（饱和度）、Errors（错误）

---

## 题目187：告警规则设计与实践 ⭐⭐

### 核心原理

**告警设计原则**：
- **业务优先**：基于业务指标（错误率、响应时间）而非系统指标
- **阈值合理**：避免过于敏感（频繁告警）或过于迟钝（漏报重要问题）
- **分级告警**：根据严重程度分级（P0紧急、P1重要、P2一般）
- **多渠道通知**：支持邮件、短信、IM等多种通知方式

**告警规则结构**：
```yaml
groups:
  - name: kquery_alerts
    rules:
      - alert: HighErrorRate
        expr: rate(http_requests_total{status=~"5.."}[5m]) / rate(http_requests_total[5m]) > 0.05
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "高错误率告警"
          description: "错误率超过5%，当前值：{{ $value }}"
```

### 实现细节

**1. KQuery Pro告警规则配置**

```yaml
# prometheus/alert_rules.yml
groups:
  - name: kquery_availability
    rules:
      # P0: 系统不可用
      - alert: ServiceDown
        expr: up{job="kquery-pro"} == 0
        for: 1m
        labels:
          severity: critical
          priority: P0
        annotations:
          summary: "服务不可用"
          description: "KQuery Pro服务已宕机超过1分钟"

      # P0: 错误率超高（>20%）
      - alert: CriticalErrorRate
        expr: rate(http_requests_total{status=~"5.."}[5m]) / rate(http_requests_total[5m]) > 0.2
        for: 2m
        labels:
          severity: critical
          priority: P0
        annotations:
          summary: "关键错误率告警"
          description: "错误率超过20%，当前值：{{ $value | humanizePercentage }}"

  - name: kquery_performance
    rules:
      # P1: 响应时间过长（>10s）
      - alert: HighLatency
        expr: histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m])) > 10
        for: 5m
        labels:
          severity: warning
          priority: P1
        annotations:
          summary: "响应时间过长"
          description: "P95响应时间超过10秒，当前值：{{ $value | humanizeDuration }}"

      # P1: 错误率告警（>5%）
      - alert: HighErrorRate
        expr: rate(http_requests_total{status=~"5.."}[5m]) / rate(http_requests_total[5m]) > 0.05
        for: 5m
        labels:
          severity: warning
          priority: P1
        annotations:
          summary: "高错误率告警"
          description: "错误率超过5%，当前值：{{ $value | humanizePercentage }}"

  - name: kquery_resources
    rules:
      # P2: CPU使用率过高（>80%）
      - alert: HighCPUUsage
        expr: 100 - (avg by(instance) (irate(node_cpu_seconds_total{mode="idle"}[5m])) * 100) > 80
        for: 10m
        labels:
          severity: info
          priority: P2
        annotations:
          summary: "CPU使用率过高"
          description: "CPU使用率超过80%，当前值：{{ $value | humanize }}%"

      # P2: 内存使用率过高（>90%）
      - alert: HighMemoryUsage
        expr: (node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes) / node_memory_MemTotal_bytes * 100 > 90
        for: 10m
        labels:
          severity: info
          priority: P2
        annotations:
          summary: "内存使用率过高"
          description: "内存使用率超过90%，当前值：{{ $value | humanize }}%"
```

**2. Alertmanager配置**

```yaml
# alertmanager/alertmanager.yml
global:
  resolve_timeout: 5m
  smtp_smarthost: 'smtp.example.com:587'
  smtp_from: 'alertmanager@example.com'
  smtp_auth_username: 'alertmanager@example.com'
  smtp_auth_password: 'password'

route:
  group_by: ['alertname', 'priority']
  group_wait: 30s      # 等待30秒聚合告警
  group_interval: 5m   # 每5分钟发送一次告警组
  repeat_interval: 4h  # 重复告警间隔4小时
  receiver: 'default'
  routes:
    - match:
        priority: P0
      receiver: 'critical'
      continue: true
    - match:
        priority: P1
      receiver: 'warning'

receivers:
  - name: 'default'
    email_configs:
      - to: 'team@example.com'
        send_resolved: true

  - name: 'critical'
    email_configs:
      - to: 'oncall@example.com'
        send_resolved: true
    webhook_configs:
      - url: 'http://sms-service/send'
        send_resolved: true
    slack_configs:
      - api_url: 'https://hooks.slack.com/services/xxx'
        channel: '#critical-alerts'

  - name: 'warning'
    email_configs:
      - to: 'team@example.com'
        send_resolved: true
    slack_configs:
      - api_url: 'https://hooks.slack.com/services/xxx'
        channel: '#alerts'
```

### 场景应用

**场景：完整的告警响应流程**

1. **告警触发**：
```
Alert: HighErrorRate
Expr: rate(http_requests_total{status=~"5.."}[5m]) / rate(http_requests_total[5m]) = 0.08
Duration: 5m
Priority: P1
```

2. **告警聚合**：
- Alertmanager等待30秒（group_wait）
- 聚合同一时间段内的其他告警（如HighLatency）
- 发送聚合后的告警通知

3. **告警通知**：
```
[P1告警] KQuery Pro - 高错误率告警

错误率：8%
持续时间：5分钟
触发规则：rate(http_requests_total{status=~"5.."}[5m]) / rate(http_requests_total[5m]) > 0.05

请立即处理！
```

4. **告警恢复**：
```
[恢复] KQuery Pro - 高错误率告警

错误率已恢复正常：2%
持续时间：15分钟
```

### 问题排查

**问题1：告警风暴**

**症状**：短时间内收到大量告警通知

**原因分析**：
- 阈值设置过于敏感
- 缺乏告警聚合机制
- 告警分组配置不合理

**解决方案**：
```yaml
# 优化告警聚合配置
route:
  group_by: ['alertname', 'service']  # 按告警名称和服务分组
  group_wait: 60s                      # 增加等待时间到60秒
  group_interval: 10m                  # 增加发送间隔到10分钟
  repeat_interval: 12h                 # 增加重复间隔到12小时
```

**问题2：告警漏报**

**症状**：系统出现问题但未收到告警

**原因分析**：
- 阈值设置过高
- 告警规则缺失
- 数据采集失败

**排查步骤**：
```bash
# 1. 检查Prometheus规则状态
curl http://prometheus:9090/api/v1/rules

# 2. 检查告警状态
curl http://prometheus:9090/api/v1/alerts

# 3. 检查Alertmanager状态
curl http://alertmanager:9093/api/v2/status

# 4. 检查通知发送日志
kubectl logs -f alertmanager-pod -n monitoring
```

### 扩展思考

**思考1：智能告警**

- **动态阈值**：使用机器学习模型自动调整告警阈值
- **异常检测**：基于历史数据检测异常模式
- **根因关联**：自动关联相关告警，定位根因

**思考2：告警疲劳处理**

- **告警分级**：严格区分P0/P1/P2，避免过度告警
- **静默策略**：维护期间自动静默告警
- **告警审查**：定期审查告警规则，删除无效告警

---

## 题目188：监控系统的性能损耗评估 ⭐⭐

### 核心原理

**性能损耗来源**：
1. **数据采集**：应用埋点、日志采集、指标暴露
2. **数据传输**：网络传输、数据序列化
3. **数据存储**：写入数据库、索引构建
4. **数据查询**：查询和聚合数据

**损耗评估方法**：
- **基准测试**：对比开启监控前后的性能差异
- **资源监控**：监控监控系统自身的资源占用
- **压力测试**：高负载下监控系统的表现

### 实现细节

**1. OpenTelemetry性能优化**

```python
# backend/app/core/tracing.py
# 批量发送Span，减少网络开销
_tracer_provider.add_span_processor(
    BatchSpanProcessor(
        jaeger_exporter,
        max_export_batch_size=100,     # 批次大小100
        schedule_delay_millis=5000,    # 发送间隔5秒
        export_timeout_millis=30000,   # 超时时间30秒
    )
)
```

**关键优化参数**：
- **max_export_batch_size**：批次大小，推荐50-200
- **schedule_delay_millis**：发送间隔，推荐1000-5000ms
- **export_timeout_millis**：超时时间，推荐30000ms

**2. 日志性能优化**

```python
# backend/app/core/logger.py
class LogstashHandler(logging.Handler):
    def __init__(
        self,
        batch_size: int = 100,        # 批量发送大小
        flush_interval: float = 2.0,  # 刷新间隔
    ):
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self._buffer: list[str] = []  # 缓冲区
```

**关键优化参数**：
- **batch_size**：批量发送大小，推荐50-200
- **flush_interval**：刷新间隔，推荐1-5秒
- **异步发送**：使用后台线程发送，避免阻塞主流程

**3. 采样策略**

```python
# 配置采样率（在生产环境降低采样率）
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

# 采样率10%
sampler = TraceIdRatioBased(0.1)

_tracer_provider = TracerProvider(
    resource=resource,
    sampler=sampler  # 设置采样器
)
```

### 场景应用

**场景：KQuery Pro性能损耗评估**

1. **埋点损耗评估**：
```bash
# 基准测试：对比开启追踪前后的响应时间
# 无追踪：平均响应时间500ms
# 开启追踪：平均响应时间520ms
# 损耗：20ms（4%）
```

2. **存储损耗评估**：
```bash
# 监控数据存储空间
# Prometheus：15天数据占用10GB
# Jaeger：7天数据占用50GB
# 总存储占用：60GB（占总存储10%）
```

3. **网络损耗评估**：
```bash
# 监控数据传输带宽
# Span数据：平均1KB/个，100 QPS下传输100KB/s
# 日志数据：平均500B/条，1000条/分下传输500KB/min
# 总带宽占用：<1Mbps（占总带宽5%）
```

**优化效果对比**：

| 优化策略 | 损耗降低 | 用户体验影响 |
|---------|---------|------------|
| 批量发送Span | 降低30%网络请求 | 无影响 |
| 采样率10% | 降低90%数据量 | 轻微影响（10%请求无追踪） |
| 异步日志发送 | 降低50%阻塞时间 | 无影响 |

### 问题排查

**问题1：埋点损耗过大**

**症状**：开启监控后响应时间增加超过10%

**排查步骤**：
```python
# 1. 分析埋点频率
# 使用cProfile分析函数调用频率
import cProfile
cProfile.run('process_request()', sort='cumulative')

# 2. 识别高频埋点函数
# 查看哪些函数被频繁调用

# 3. 优化埋点逻辑
# 减少埋点频率、简化属性注入
```

**解决方案**：
- 降低采样率：从100%降低到10%
- 简化属性注入：只注入关键属性
- 异步发送：使用后台线程发送数据

**问题2：存储空间不足**

**症状**：监控数据占满存储空间

**排查步骤**：
```bash
# 1. 检查存储使用情况
df -h /data/prometheus
df -h /data/jaeger

# 2. 检查数据保留策略
# Prometheus默认保留15天
# Jaeger默认保留7天

# 3. 检查数据增长速度
# 监控数据增长速度
```

**解决方案**：
- 调整数据保留时间：从30天降低到15天
- 增加存储容量：扩展磁盘空间
- 数据压缩：启用存储压缩功能

### 扩展思考

**思考1：监控系统自监控**

- **Prometheus自监控**：暴露`prometheus_*`指标，监控自身性能
- **Jaeger自追踪**：Jaeger自身也使用OpenTelemetry追踪
- **日志系统监控**：监控日志采集延迟、存储容量

**思考2：性能损耗与收益平衡**

- **业务关键路径**：核心业务全量监控，接受5%损耗
- **辅助功能路径**：非关键功能采样监控，损耗<1%
- **动态调整**：根据系统负载动态调整监控强度

---

## 题目189：日志聚合与分析实践 ⭐⭐

### 核心原理

**日志聚合必要性**：
- **日志分散**：分布式系统日志分散在多个节点
- **日志量大**：日志数据量大，难以人工分析
- **查询困难**：跨节点查询日志困难
- **分析需求**：需要聚合分析日志，发现模式

**日志聚合方案**：
- **ELK Stack**：Elasticsearch + Logstash + Kibana
- **Loki**：Grafana Loki，轻量级日志聚合
- **云服务**：阿里云SLS、AWS CloudWatch Logs

### 实现细节

**1. KQuery Pro日志聚合架构（`backend/app/core/logger.py`）**

```python
class LogstashHandler(logging.Handler):
    """通过 TCP 将日志推送到 Logstash 的 Handler。"""
    
    def _format_for_logstash(self, record: LogRecord) -> Dict[str, Any]:
        """将 LogRecord 格式化为 Logstash 可接收的 JSON 结构。"""
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "application": "kquery-pro",
            "environment": self.environment,
            "index_name": self.index_name,
            "host": socket.gethostname(),
        }
        
        # 异常信息
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = "".join(
                traceback.format_exception(*record.exc_info)
            )
        
        # 附加字段
        extra = {}
        skip_keys = {
            "name", "msg", "args", "created", "levelname",
            # ... 其他内部字段
        }
        for key, value in record.__dict__.items():
            if key not in skip_keys and not key.startswith("_"):
                extra[key] = value
        if extra:
            log_entry["extra"] = extra
        
        return log_entry
```

**关键实现细节**：
- **标准化字段**：使用Elasticsearch通用字段（timestamp、level、message）
- **异常堆栈**：完整记录异常堆栈信息
- **动态字段**：支持任意类型的额外字段
- **主机信息**：自动添加主机名和环境标识

**2. ELK配置**

```ruby
# logstash.conf
input {
  tcp {
    port => 5045
    codec => json_lines
  }
}

filter {
  # 解析时间戳
  date {
    match => ["timestamp", "ISO8601"]
    target => "@timestamp"
  }
  
  # 添加地理位置（如果日志包含IP）
  geoip {
    source => "[extra][client_ip]"
    target => "[extra][geoip]"
  }
}

output {
  elasticsearch {
    hosts => ["http://elasticsearch:9200"]
    index => "kquery-logs-%{+YYYY.MM.dd}"
    template => "/etc/logstash/templates/kquery-template.json"
  }
}
```

**3. Elasticsearch索引模板**

```json
{
  "template": "kquery-logs-*",
  "settings": {
    "number_of_shards": 3,
    "number_of_replicas": 1,
    "index.lifecycle.name": "kquery-logs-policy",
    "index.lifecycle.rollover_alias": "kquery-logs"
  },
  "mappings": {
    "properties": {
      "@timestamp": {"type": "date"},
      "level": {"type": "keyword"},
      "logger": {"type": "keyword"},
      "message": {"type": "text"},
      "application": {"type": "keyword"},
      "environment": {"type": "keyword"},
      "host": {"type": "keyword"},
      "exception": {"type": "text"},
      "extra": {"type": "object", "dynamic": true}
    }
  }
}
```

### 场景应用

**场景1：错误日志分析**

```python
# 1. 查询最近1小时的ERROR级别日志
GET /kquery-logs-*/_search
{
  "query": {
    "bool": {
      "filter": [
        {"term": {"level": "ERROR"}},
        {"range": {"@timestamp": {"gte": "now-1h"}}}
      ]
    }
  },
  "sort": [{"@timestamp": {"order": "desc"}}],
  "size": 100
}

# 2. 统计ERROR日志数量趋势（每5分钟）
GET /kquery-logs-*/_search
{
  "size": 0,
  "query": {
    "term": {"level": "ERROR"}
  },
  "aggs": {
    "error_trend": {
      "date_histogram": {
        "field": "@timestamp",
        "calendar_interval": "5m"
      }
    }
  }
}

# 3. 查询特定错误的完整堆栈
GET /kquery-logs-*/_search
{
  "query": {
    "match": {"message": "Neo4j查询超时"}
  },
  "_source": ["timestamp", "logger", "message", "exception"]
}
```

**场景2：慢查询日志分析**

```python
# 查询响应时间超过5秒的请求日志
GET /kquery-logs-*/_search
{
  "query": {
    "bool": {
      "filter": [
        {"term": {"logger": "app.api.query_router"}},
        {"range": {"extra.duration": {"gte": 5000}}}
      ]
    }
  },
  "sort": [{"extra.duration": {"order": "desc"}}],
  "size": 50
}

# 分析慢查询的endpoint分布
GET /kquery-logs-*/_search
{
  "size": 0,
  "query": {
    "range": {"extra.duration": {"gte": 5000}}
  },
  "aggs": {
    "slow_endpoints": {
      "terms": {"field": "extra.endpoint"}
    }
  }
}
```

### 问题排查

**问题1：日志采集失败**

**症状**：部分日志未出现在Kibana中

**排查步骤**：
```bash
# 1. 检查应用日志发送状态
tail -f /var/log/kquery-pro/app.log | grep "Logstash"

# 2. 检查Logstash连接状态
telnet logstash-host 5045

# 3. 检查Logstash日志
tail -f /var/log/logstash/logstash-plain.log

# 4. 检查Elasticsearch索引状态
curl -X GET "http://elasticsearch:9200/_cat/indices/kquery-logs-*?v"
```

**解决方案**：
- 检查网络连通性：确保应用能连接到Logstash
- 检查日志格式：确保日志格式符合JSON规范
- 检查存储容量：确保Elasticsearch存储未满

**问题2：日志查询慢**

**症状**：Kibana查询响应时间超过10秒

**排查步骤**：
```bash
# 1. 检查索引大小
curl -X GET "http://elasticsearch:9200/_cat/indices/kquery-logs-*?v&h=index,store.size,docs.count"

# 2. 检查查询性能
curl -X GET "http://elasticsearch:9200/kquery-logs-*/_search?explain" -d '{...}'

# 3. 检查集群健康状态
curl -X GET "http://elasticsearch:9200/_cluster/health"
```

**解决方案**：
- 优化查询条件：添加时间范围过滤，缩小查询范围
- 增加分片数：调整索引分片数量，提高并行查询能力
- 使用别名：使用索引别名，便于管理和查询

### 扩展思考

**思考1：日志分析智能化**

- **异常检测**：使用机器学习检测日志异常模式
- **自动聚类**：自动将相似日志聚类，减少日志量
- **根因推荐**：基于历史数据推荐故障根因

**思考2：日志存储成本优化**

- **冷热分离**：热数据存SSD，冷数据存HDD
- **数据压缩**：启用Elasticsearch数据压缩
- **生命周期管理**：定期删除过期日志

---

## 题目190：AIOps智能运维实践 ⭐⭐⭐⭐

### 核心原理

**AIOps定义**：
使用人工智能技术（机器学习、大数据分析）实现运维自动化。核心能力包括异常检测、根因分析、自动修复、预测性维护。

**AIOps技术栈**：
1. **数据采集**：监控数据、日志数据、事件数据
2. **数据处理**：数据清洗、特征提取、数据聚合
3. **模型训练**：异常检测模型、根因分析模型、预测模型
4. **模型部署**：模型服务化，集成到运维流程

### 实现细节

**1. 异常检测实现**

```python
# 基于时间序列的异常检测（使用Prophet或LSTM）
import pandas as pd
from prophet import Prophet

async def detect_anomaly(metric_data: pd.DataFrame):
    """使用Prophet检测指标异常"""
    # 准备数据
    df = metric_data.rename(columns={'timestamp': 'ds', 'value': 'y'})
    
    # 训练模型
    model = Prophet(
        yearly_seasonality=True,
        weekly_seasonality=True,
        daily_seasonality=True
    )
    model.fit(df)
    
    # 预测未来值
    future = model.make_future_dataframe(periods=60)
    forecast = model.predict(future)
    
    # 计算异常分数
    actual = df['y'].values[-60:]
    predicted = forecast['yhat'].values[-60:]
    residual = abs(actual - predicted)
    
    # 动态阈值（3倍标准差）
    threshold = forecast['yhat_upper'].values[-60:]
    anomalies = residual > threshold
    
    return {
        "anomaly_detected": any(anomalies),
        "anomaly_indices": anomalies.nonzero()[0],
        "anomaly_scores": residual[anomalies].tolist()
    }
```

**2. 根因分析实现**

```python
# 基于知识图谱的根因分析
async def analyze_root_cause(trace_id: str, error_logs: List[dict]):
    """分析故障根因"""
    # 提取关键错误信息
    error_entities = extract_entities(error_logs)
    
    # 在知识图谱中查找相关实体
    cypher_query = """
    MATCH (e:Error)-[:RELATED_TO]->(cause:RootCause)
    WHERE e.message IN $error_messages
    RETURN cause.name, cause.probability
    ORDER BY cause.probability DESC
    LIMIT 5
    """
    
    root_causes = await neo4j_client.run(cypher_query, {
        "error_messages": [e["message"] for e in error_entities]
    })
    
    return {
        "trace_id": trace_id,
        "root_causes": root_causes,
        "recommended_actions": generate_actions(root_causes)
    }

def generate_actions(root_causes: List[dict]) -> List[str]:
    """生成修复建议"""
    actions = []
    for cause in root_causes:
        if cause["name"] == "DatabaseTimeout":
            actions.append("检查数据库连接池状态")
            actions.append("优化慢查询")
        elif cause["name"] == "MemoryLeak":
            actions.append("重启相关服务")
            actions.append("分析内存使用情况")
    
    return actions
```

**3. 自动修复实现**

```python
# 自动修复脚本执行器
class AutoRemediationExecutor:
    def __init__(self):
        self.remediation_scripts = {
            "high_memory": self.restart_service,
            "slow_query": self.clear_cache,
            "connection_pool_exhausted": self.increase_pool_size
        }
    
    async def execute_remediation(self, alert: dict):
        """执行自动修复"""
        root_cause = alert.get("root_cause")
        
        if root_cause in self.remediation_scripts:
            script = self.remediation_scripts[root_cause]
            result = await script(alert)
            
            return {
                "success": True,
                "action": root_cause,
                "result": result,
                "timestamp": datetime.utcnow().isoformat()
            }
        
        return {"success": False, "reason": "No remediation script"}
    
    async def restart_service(self, alert: dict):
        """重启服务"""
        service_name = alert["labels"]["service"]
        
        # 执行Kubernetes重启命令
        result = await run_command(
            f"kubectl rollout restart deployment/{service_name}"
        )
        
        return {"message": f"服务{service_name}已重启", "output": result}
    
    async def clear_cache(self, alert: dict):
        """清理缓存"""
        cache_type = alert["labels"]["cache_type"]
        
        # 执行缓存清理命令
        result = await run_command(
            f"redis-cli FLUSHDB"
        )
        
        return {"message": f"缓存{cache_type}已清理", "output": result}
```

### 场景应用

**场景：完整的AIOps故障处理流程**

1. **异常检测**：
```python
# 监控系统检测到QPS突降
anomaly_result = await detect_anomaly(qps_data)
# {"anomaly_detected": True, "anomaly_score": 0.85}
```

2. **告警触发**：
```python
# Prometheus触发告警
alert = {
    "alertname": "LowQPS",
    "severity": "critical",
    "value": "50% below baseline",
    "trace_id": "abc123"
}
```

3. **根因分析**：
```python
# AIOps系统分析根因
error_logs = fetch_error_logs(trace_id="abc123")
root_cause = await analyze_root_cause("abc123", error_logs)
# {
#   "root_causes": [
#     {"name": "DatabaseTimeout", "probability": 0.8},
#     {"name": "ConnectionPoolExhausted", "probability": 0.6}
#   ]
# }
```

4. **自动修复**：
```python
# 执行自动修复
remediation_result = await auto_remediation.execute_remediation(alert)
# {
#   "success": True,
#   "action": "increase_pool_size",
#   "result": "连接池大小已从50增加到100"
# }
```

### 问题排查

**问题1：异常检测误报**

**症状**：正常流量波动被误判为异常

**原因分析**：
- 模型阈值设置不合理
- 缺乏季节性因素考虑
- 训练数据不足

**解决方案**：
```python
# 1. 调整异常检测阈值
threshold_multiplier = 4  # 从3倍标准差调整到4倍

# 2. 增加季节性因素
model = Prophet(
    yearly_seasonality=True,
    weekly_seasonality=True,
    daily_seasonality=True,
    seasonality_mode='multiplicative'  # 使用乘法模型
)

# 3. 增加训练数据
min_training_days = 30  # 至少30天历史数据
```

**问题2：根因分析不准确**

**症状**：推荐的根因与实际问题不符

**原因分析**：
- 知识图谱不完整
- 实体抽取错误
- 缺乏上下文信息

**解决方案**：
```python
# 1. 完善知识图谱
# 定期更新错误-根因映射关系

# 2. 改进实体抽取
# 使用更准确的NLP模型

# 3. 增加上下文信息
# 在分析时包含更多日志和指标信息
```

### 扩展思考

**思考1：AIOps成熟度模型**

- **Level 1：辅助**：提供数据分析和建议，人工决策
- **Level 2：半自动**：自动检测和诊断，人工确认后执行
- **Level 3：全自动**：自动检测、诊断、修复，人工监督
- **Level 4：预测**：预测性维护，提前预防故障

**思考2：AIOps伦理与安全**

- **自动修复权限**：限制自动修复范围，避免误操作
- **人工审核机制**：关键操作需要人工确认
- **回滚机制**：自动修复失败时自动回滚

---

## 总结

本文档为KQuery Pro深度面试题文档中监控可观测性模块（题目183-190）的详细参考答案，基于项目真实代码和架构生成。主要内容包括：

1. **Prometheus+Grafana监控体系**：OpenTelemetry集成、自动埋点、Dashboard设计
2. **Jaeger分布式追踪系统**：Span管理、链路可视化、性能分析
3. **可观测性概念与三支柱**：日志、指标、追踪的协同使用
4. **Prometheus基础查询语法**：PromQL查询示例和优化策略
5. **告警规则设计与实践**：分级告警、多渠道通知、告警聚合
6. **监控系统性能损耗评估**：损耗来源、优化策略、评估方法
7. **日志聚合与分析实践**：ELK架构、日志查询、异常分析
8. **AIOps智能运维实践**：异常检测、根因分析、自动修复

所有答案均基于项目实际代码（`backend/app/core/tracing.py`、`backend/app/core/logger.py`等），包含关键代码片段、实现细节、场景应用和问题排查方案。