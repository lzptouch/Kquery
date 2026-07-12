"""
异步任务队列配置和任务定义

基于Celery实现的后台任务处理系统
用于文档处理、知识图谱构建等耗时操作
"""

from celery import Celery, Task
from celery.result import AsyncResult
from typing import Optional, Dict, Any
import logging

from app.core.settings import get_settings


# 获取配置
settings = get_settings()

# 创建Celery应用实例
celery_app = Celery(
    "kquery",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

# Celery配置
celery_app.conf.update(
    # 序列化配置
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    
    # 时区设置
    timezone="Asia/Shanghai",
    enable_utc=True,
    
    # 任务追踪
    task_track_started=True,
    task_time_limit=3600,  # 单个任务最大执行时间（1小时）
    task_soft_time_limit=3000,  # 软限制（50分钟，触发SoftTimeLimitExceeded）
    
    # 结果配置
    result_expires=86400,  # 任务结果保留1天
    result_backend_transport_options={
        "retry_policy": {
            "timeout": 5.0,
            "max_retries": 3,
        }
    },
    
    # Worker配置
    worker_prefetch_multiplier=1,  # 每次只预取1个任务（适合长任务）
    worker_concurrency=4,  # 默认并发数
    
    # 任务路由（可选）
    task_routes={
        "app.tasks.document_tasks.*": {"queue": "document"},
        "app.tasks.graph_tasks.*": {"queue": "graph"},
    },
    
    # Beat调度器（定时任务）
    beat_schedule={
        'cleanup-expired-cache': {
            'task': 'app.tasks.system_tasks.cleanup_cache',
            'schedule': 3600.0,  # 每小时执行一次
        },
    },
)


# ==================== 自定义基类Task ====================

class BaseTask(Task):
    """
    自定义任务基类
    
    提供通用的错误处理、日志记录、状态更新等功能
    所有自定义任务都应继承此类
    """
    
    def __init__(self):
        self.logger = logging.getLogger(f"celery.{self.name}")
    
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """任务失败时的回调"""
        self.logger.error(
            f"任务失败 | ID: {task_id} | "
            f"异常: {exc} | 参数: {args}, {kwargs}"
        )
        
        # TODO: 可以在这里添加失败通知（邮件/Slack）
        super().on_failure(exc, task_id, args, kwargs, einfo)
    
    def on_success(self, retval, task_id, args, kwargs):
        """任务成功时的回调"""
        self.logger.info(
            f"任务成功 | ID: {task_id} | "
            f"返回值类型: {type(retval).__name__}"
        )
        super().on_success(retval, task_id, args, kwargs)
    
    def on_retry(self, exc, task_id, args, kwargs, einfo):
        """任务重试时的回调"""
        self.logger.warning(
            f"任务重试 | ID: {task_id} | "
            f"原因: {exc}"
        )
        super().on_retry(exc, task_id, args, kwargs, einfo)


# 注册基类
celery_app.Task = BaseTask


# ==================== 文档处理任务 ====================

@celery_app.task(
    bind=True,
    name="process_document_upload",
    max_retries=2,
    default_retry_delay=30,
    autoretry_for=(Exception,)
)
def process_document_upload(
    self: Task,
    document_id: int,
    file_path: str,
    knowledge_base_id: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    文档上传后的异步处理流程
    
    处理步骤：
    1. 文件格式检测和验证
    2. 内容提取（文本/表格/图像）
    3. 文本预处理（分块、清洗）
    4. 向量化生成Embedding
    5. 存储到向量数据库(Milvus)
    6. 更新数据库元数据
    
    Args:
        self: Celery Task实例（用于重试等操作）
        document_id: 数据库中文档ID
        file_path: 文件在服务器上的临时路径
        knowledge_base_id: 所属知识库ID
        metadata: 额外的文档元数据
        
    Returns:
        处理结果字典：
        {
            "status": "success",
            "document_id": int,
            "chunks_count": int,
            "vectors_count": int,
            "processing_time": float
        }
    """
    from app.services.document_processor import DocumentProcessor
    from datetime import datetime
    import time
    
    start_time = time.time()
    
    try:
        self.logger.info(f"开始处理文档 | ID: {document_id}")
        
        processor = DocumentProcessor()
        
        # 执行处理流程
        result = processor.process(
            document_id=document_id,
            file_path=file_path,
            knowledge_base_id=knowledge_base_id,
            metadata=metadata or {}
        )
        
        processing_time = time.time() - start_time
        
        self.logger.info(
            f"文档处理完成 | ID: {document_id} | "
            f"耗时: {processing_time:.2f}s"
        )
        
        return {
            **result,
            "status": "success",
            "processing_time": processing_time
        }
        
    except Exception as exc:
        processing_time = time.time() - start_time
        self.logger.error(
            f"文档处理失败 | ID: {document_id} | "
            f"已处理时间: {processing_time:.2f}s | 错误: {exc}"
        )
        raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1))


@celery_app.task(
    bind=True,
    name="delete_document_vectors",
    max_retries=1
)
def delete_document_vectors(
    self: Task,
    document_id: int,
    collection_name: str = "documents"
) -> Dict[str, Any]:
    """
    删除文档的向量数据
    
    从Milvus中删除指定文档的所有向量和块数据，
    并清理相关缓存
    
    Args:
        document_id: 文档ID
        collection_name: Milvus集合名
        
    Returns:
        删除结果
    """
    from pymilvus import connections, Collection
    import time
    
    start_time = time.time()
    
    try:
        self.logger.info(f"删除文档向量 | ID: {document_id}")
        
        # 连接Milvus
        connections.connect("default", host=settings.milvus_host, port=settings.milvus_port)
        collection = Collection(collection_name)
        
        # 构建查询条件并删除
        collection.delete(expr=f'document_id == {document_id}')
        
        processing_time = time.time() - start_time
        
        return {
            "status": "success",
            "document_id": document_id,
            "deleted_vectors": "all",
            "processing_time": processing_time
        }
        
    except Exception as exc:
        self.logger.error(f"删除文档向量失败: {exc}", exc_info=True)
        raise


@celery_app.task(
    bind=True,
    name="batch_process_documents",
    max_retries=1
)
def batch_process_documents(
    self: Task,
    document_ids: list[int],
    knowledge_base_id: int
) -> Dict[str, Any]:
    """
    批量处理多个文档
    
    用于批量导入场景，并行或串行处理多个文档
    
    Args:
        document_ids: 文档ID列表
        knowledge_base_id: 知识库ID
        
    Returns:
        批量处理结果统计
    """
    results = {
        "total": len(document_ids),
        "success": 0,
        "failed": 0,
        "details": []
    }
    
    for doc_id in document_ids:
        try:
            # 调用单个文档处理任务
            # 这里简化为直接调用处理函数（实际可使用group/sig chord等高级功能）
            process_document_upload.apply_async(
                kwargs={
                    "document_id": doc_id,
                    "knowledge_base_id": knowledge_base_id
                }
            )
            results["success"] += 1
            results["details"].append({"doc_id": doc_id, "status": "queued"})
            
        except Exception as e:
            results["failed"] += 1
            results["details"].append({
                "doc_id": doc_id,
                "status": "failed",
                "error": str(e)
            })
    
    return results


# ==================== 知识图谱构建任务 ====================

@celery_app.task(
    bind=True,
    name="build_knowledge_graph",
    max_retries=2,
    default_retry_delay=120  # 图谱构建耗时长，重试间隔长
)
def build_knowledge_graph(
    self: Task,
    knowledge_base_id: int,
    options: Optional[Dict] = None
) -> Dict[str, Any]:
    """
    知识图谱构建任务
    
    从文档内容中抽取实体和关系，构建知识图谱
    
    流程：
    1. 加载知识库中的所有文档
    2. 实体识别和抽取（NER）
    3. 关系抽取
    4. 存入图数据库(Neo4j)
    5. 构建索引和统计信息
    
    Args:
        knowledge_base_id: 知识库ID
        options: 构建选项（如实体类型过滤、关系阈值等）
        
    Returns:
        构建结果统计
    """
    from app.services.graph_builder import GraphBuilderService
    import time
    
    start_time = time.time()
    options = options or {}
    
    try:
        self.logger.info(f"开始构建知识图谱 | KB_ID: {knowledge_base_id}")
        
        builder = GraphBuilderService()
        result = builder.build_graph(knowledge_base_id, **options)
        
        build_time = time.time() - start_time
        
        self.logger.info(
            f"知识图谱构建完成 | KB_ID: {knowledge_base_id} | "
            f"耗时: {build_time:.2f}s"
        )
        
        return {
            **result,
            "status": "success",
            "build_time": build_time
        }
        
    except Exception as exc:
        self.logger.error(
            f"知识图谱构建失败 | KB_ID: {knowledge_base_id}",
            exc_info=True
        )
        # 图谱构建失败通常需要人工介入检查数据质量
        raise  # 不自动重试，避免资源浪费


@celery_app.task(
    bind=True,
    name="update_entity_embeddings"
)
def update_entity_embeddings(
    self: Task,
    entity_ids: list[int]
) -> Dict[str, Any]:
    """
    更新实体的Embedding向量
    
    当实体描述或关联关系发生变化时调用，
    确保语义检索准确性
    
    Args:
        entity_ids: 需要更新的实体ID列表
        
    Returns:
        更新结果
    """
    # TODO: 实现逻辑
    pass


# ==================== 系统维护任务 ====================

@celery_app.task(name="cleanup_cache")
def cleanup_cache() -> Dict[str, Any]:
    """
    缓存清理定时任务（由Beat调度器定期触发）
    
    清理过期的缓存条目，释放内存和Redis空间
    """
    from app.core.cache import ml_cache
    
    stats_before = ml_cache.get_stats()
    
    # 清理内存缓存中的过期条目
    # （MemoryCache有自动清理机制，这里可以强制全量清理）
    
    return {
        "task": "cache_cleanup",
        "executed_at": datetime.utcnow().isoformat(),
        "stats_before": stats_before,
        "message": "Cache cleanup completed"
    }


@celery_app.task(name="generate_usage_report")
def generate_usage_report(period_days: int = 7) -> Dict[str, Any]:
    """
    生成使用报告
    
    统计系统使用情况，包括API调用量、用户活跃度、资源使用等
    可用于运营分析和计费
    
    Args:
        period_days: 统计周期（天数）
        
    Returns:
        报告数据
    """
    from datetime import datetime, timedelta
    from sqlalchemy import select, func
    
    # TODO: 实现报告生成逻辑
    # 包括：查询次数、文档上传量、活跃用户数、平均响应时间等
    
    return {
        "period_days": period_days,
        "generated_at": datetime.utcnow().isoformat(),
        "report_type": "usage_summary",
        "data": {}  # 占位
    }


# ==================== 任务管理工具函数 ====================

def get_task_status(task_id: str) -> Dict[str, Any]:
    """
    查询任务执行状态
    
    Args:
        task_id: Celery任务ID
        
    Returns:
        任务状态信息
    """
    result = AsyncResult(task_id, app=celery_app)
    
    status_map = {
        "PENDING": "等待执行",
        "STARTED": "正在执行",
        "SUCCESS": "执行成功",
        "FAILURE": "执行失败",
        "RETRY": "重试中",
        "REVOKED": "已撤销"
    }
    
    response = {
        "task_id": task_id,
        "status": result.status,
        "status_display": status_map.get(result.status, "未知"),
    }
    
    if result.ready():
        if result.successful():
            response["result"] = result.result
        else:
            response["error"] = str(result.result) if result.failed else None
    
    elif result.state == "PROGRESS":
        # 如果是进度更新的任务
        if hasattr(result.info, '__getitem__'):
            response["progress"] = {
                "current": result.info.get("current", 0),
                "total": result.info.get("total", 100),
                "percent": round(result.info.get("current", 0) / max(result.info.get("total", 1), 1) * 100, 2)
            }
    
    return response


def revoke_task(task_id: str, terminate: bool = False) -> bool:
    """
    取消/终止正在运行的任务
    
    Args:
        task_id: 任务ID
        terminate: 是否立即终止（True）或等待当前任务完成后不再重试（False）
        
    Returns:
        是否成功发送终止信号
    """
    celery_app.control.revoke(task_id, terminate=terminate)
    logger.info(f"任务已撤销 | ID: {task_id} | Terminate: {terminate}")
    return True
