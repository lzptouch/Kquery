"""
安全审计日志模块

记录用户操作和安全相关事件，用于合规审计和问题追踪
"""

import json
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from enum import Enum
from fastapi import Request
import uuid

from app.core.logger import get_logger


logger = get_logger(__name__)


class AuditEventType(str, Enum):
    """
    审计事件类型枚举
    
    分类：
    - DATA: 数据访问和修改（文档CRUD、查询操作）
    - SYSTEM: 系统配置和管理操作
    - SECURITY: 安全相关事件（异常检测）
    """
    # 数据操作
    DOCUMENT_UPLOAD = "DATA:DOCUMENT:UPLOAD"
    DOCUMENT_DELETE = "DATA:DOCUMENT:DELETE"
    QUERY_EXECUTE = "DATA:QUERY:EXECUTE"
    KNOWLEDGE_BASE_MODIFY = "DATA:KNOWLEDGE_BASE:MODIFY"
    
    # 系统管理
    CONFIG_CHANGE = "SYSTEM:CONFIG_CHANGE"
    
    # 安全事件
    RATE_LIMIT_TRIGGERED = "SECURITY:RATE_LIMIT"
    SUSPICIOUS_ACTIVITY = "SECURITY:SUSPICIOUS"


class AuditEvent:
    """
    审计事件类
    
    表示一个完整的审计日志条目
    
    Attributes:
        event_id: 全局唯一的事件ID
        timestamp: 事件发生时间（ISO格式UTC时间）
        event_type: 事件类型（AuditEventType枚举值）
        user_id: 触发事件的用户ID（未认证时为None或"anonymous"）
        username: 用户名
        ip_address: 客户端IP地址
        user_agent: 客户端User-Agent
        resource: 操作的资源路径
        action: 执行的动作描述
        details: 附加详情字典
        status: 事件状态 ("success", "failure", "warning")
        request_id: 关联的请求ID（用于关联请求日志）
        severity: 严重级别 (info, warning, error, critical)
    """
    
    def __init__(
        self,
        event_type: AuditEventType,
        user_id: Optional[int] = None,
        username: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        resource: Optional[str] = None,
        action: str = "",
        details: Optional[Dict[str, Any]] = None,
        status: str = "success",
        request_id: Optional[str] = None,
        severity: str = "info",
        **kwargs
    ):
        """初始化审计事件"""
        self.event_id = str(uuid.uuid4())
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.event_type = event_type.value if isinstance(event_type, AuditEventType) else event_type
        self.user_id = user_id
        self.username = username or "system"
        self.ip_address = ip_address or "unknown"
        self.user_agent = user_agent[:500] if user_agent else "unknown"
        self.resource = resource or ""
        self.action = action
        self.details = details or {}
        self.status = status
        self.request_id = request_id or ""
        self.severity = severity
        
        # 添加额外信息到details
        for key, value in kwargs.items():
            if key not in ["event_type", "user_id", "username"]:
                self.details[key] = value
    
    def to_dict(self) -> dict:
        """转换为字典格式"""
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "user_id": self.user_id,
            "username": self.username,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "resource": self.resource,
            "action": self.action,
            "details": self.details,
            "status": self.status,
            "request_id": self.request_id,
            "severity": self.severity
        }
    
    def to_json(self) -> str:
        """转换为JSON字符串"""
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)
    
    def to_log_message(self) -> str:
        """生成适合日志输出的格式化消息"""
        return (
            f"[AUDIT] {self.timestamp} | {self.severity.upper()} | "
            f"{self.event_type} | User:{self.username}({self.user_id}) | "
            f"{self.action} | Status:{self.status} | "
            f"IP:{self.ip_address} | ReqID:{self.request_id}"
        )


class AuditLogger:
    """
    审计日志记录器
    
    提供统一的审计日志接口，支持：
    - 控制台输出（开发环境）
    - 文件存储（生产环境）
    - 数据库存储（可选）
    - 外部系统发送（可选）
    """
    
    SENSITIVE_OPERATIONS = {
        AuditEventType.CONFIG_CHANGE.value,
        AuditEventType.SUSPICIOUS_ACTIVITY.value,
        AuditEventType.RATE_LIMIT_TRIGGERED.value,
    }
    
    def __init__(self, enable_db_storage: bool = True):
        """初始化审计日志器
        
        Args:
            enable_db_storage: 是否启用数据库存储
        """
        self._enable_db_storage = enable_db_storage
        self._storage_backends = []
        
        if enable_db_storage:
            from app.core.audit_storage import audit_log_storage
            self._storage_backends.append(audit_log_storage)
            logger.info("审计日志数据库存储已启用")
    
    async def log_event(self, event: AuditEvent) -> None:
        """记录审计事件。
        
        Args:
            event: AuditEvent实例
        """
        try:
            log_method = getattr(logger, event.severity, logger.info)
            log_method(
                event.to_log_message(),
                extra={
                    # ELK 结构化字段
                    "log_category": "audit",
                    "event_type": event.event_type,
                    "event_id": event.event_id,
                    "audit_user_id": event.user_id,
                    "audit_username": event.username,
                    "audit_ip": event.ip_address,
                    "audit_resource": event.resource,
                    "audit_action": event.action,
                    "audit_status": event.status,
                    "audit_severity": event.severity,
                    "audit_details": event.details,
                },
            )
            
            for backend in self._storage_backends:
                try:
                    await backend.store(event)
                except Exception as e:
                    logger.error(f"审计日志后端写入失败: {e}")
            
            if event.event_type in self.SENSITIVE_OPERATIONS:
                await self._send_alert(event)
                
        except Exception as e:
            logger.error(f"审计日志记录失败: {e}", exc_info=True)
    
    async def _send_alert(self, event: AuditEvent) -> None:
        """发送敏感操作告警"""
        logger.warning(
            f"[ALERT] 敏感操作告警 - {event.to_log_message()}"
        )
    
    # ==================== 便捷方法 ====================
    
    async def log_query_access(
        self,
        request: Request,
        endpoint: str,
        query_text: Optional[str] = None,
        collection_name: Optional[str] = None,
        response_count: int = 0,
        duration_ms: float = 0.0,
        status: str = "success",
        user_id: Optional[int] = None,
        username: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> None:
        """记录查询访问日志。

        Args:
            request: FastAPI Request 对象，用于获取客户端 IP、User-Agent 等
            endpoint: 被访问的 API 端点路径
            query_text: 用户查询文本
            collection_name: 知识库/集合名称
            response_count: 返回结果数量
            duration_ms: 请求处理耗时（毫秒）
            status: 请求状态，success 或 failure
            user_id: 用户 ID（未认证时为 None）
            username: 用户名（未认证时为 None）
            request_id: 关联请求 ID
        """
        await self.log_event(AuditEvent(
            event_type=AuditEventType.QUERY_EXECUTE,
            user_id=user_id,
            username=username or "anonymous",
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            resource=endpoint,
            action="query_access",
            details={
                "query_text": query_text,
                "collection_name": collection_name,
                "response_count": response_count,
                "duration_ms": duration_ms,
                "method": request.method,
            },
            status=status,
            request_id=request_id,
            severity="info",
            duration_ms=duration_ms,
        ))
        # 查询访问日志需要尽快落盘，确保监控实时性
        from app.core.audit_storage import audit_log_storage
        await audit_log_storage.flush()



    async def log_data_access(
        self,
        resource: str,
        action: str,
        ip: str,
        user_id: Optional[int] = None,
        username: Optional[str] = None,
        success: bool = True,
        details: Optional[dict] = None,
        request_id: Optional[str] = None
    ) -> None:
        """记录数据访问/操作"""
        await self.log_event(AuditEvent(
            event_type=AuditEventType.QUERY_EXECUTE if "query" in resource.lower() 
                       else AuditEventType.DOCUMENT_UPLOAD,
            user_id=user_id,
            username=username or "anonymous",
            ip_address=ip,
            resource=resource,
            action=action,
            details=details or {},
            status="success" if success else "failure",
            severity="info",
            request_id=request_id
        ))


# 全局单例实例
audit_logger = AuditLogger()


# FastAPI中间件集成
async def audit_middleware(request: Request, call_next):
    """
    审计日志中间件
    
    自动记录所有API请求的基本信息，
    具体的业务操作由各路由处理器调用audit_logger记录详细事件
    """
    # 跳过静态文件和健康检查
    if request.url.path in ["/docs", "/redoc", "/health", "/openapi.json"]:
        response = await call_next(request)
        return response
    
    start_time = time.time()
    
    try:
        # 继续处理请求
        response = await call_next(request)
        
        duration_ms = (time.time() - start_time) * 1000
        
        # 记录基础访问日志（简化版）
        logger.debug(
            f"[REQUEST] {request.method} {request.url.path} "
            f"| Status: {response.status_code} | Duration: {duration_ms:.2f}ms"
        )
        
        return response
        
    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        logger.error(
            f"[REQUEST_ERROR] {request.method} {request.url.path} "
            f"| Error: {str(e)} | Duration: {duration_ms:.2f}ms"
        )
        raise
