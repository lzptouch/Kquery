"""审计日志数据库存储后端。

提供审计日志的数据库持久化存储功能。
"""

import json
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from sqlalchemy import Column, Integer, String, Text, DateTime, Float, Index, select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import Base, get_db

from app.core.logger import get_logger

logger = get_logger(__name__)


class AuditLog(Base):
    """审计日志数据库模型。"""
    
    __tablename__ = "audit_logs"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(36), unique=True, nullable=False, index=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    event_type = Column(String(50), nullable=False, index=True)
    user_id = Column(Integer, nullable=True, index=True)
    username = Column(String(100), nullable=True, index=True)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(500), nullable=True)
    resource = Column(String(500), nullable=True)
    action = Column(String(500), nullable=True)
    details = Column(Text, nullable=True)
    status = Column(String(20), nullable=True)
    request_id = Column(String(50), nullable=True)
    severity = Column(String(20), nullable=True)
    duration_ms = Column(Float, nullable=True)
    
    __table_args__ = (

        Index("idx_audit_timestamp_type", "timestamp", "event_type"),
        Index("idx_audit_user_timestamp", "user_id", "timestamp"),
    )
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "id": self.id,
            "event_id": self.event_id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "event_type": self.event_type,
            "user_id": self.user_id,
            "username": self.username,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "resource": self.resource,
            "action": self.action,
            "details": json.loads(self.details) if self.details else {},
            "status": self.status,
            "request_id": self.request_id,
            "severity": self.severity,
            "duration_ms": self.duration_ms,
        }


class AuditLogStorage:
    """审计日志数据库存储后端。"""
    
    def __init__(self, max_batch_size: int = 100):
        """初始化存储后端。
        
        Args:
            max_batch_size: 最大批量写入数量
        """
        self.max_batch_size = max_batch_size
        self._buffer: List[AuditLog] = []
        self._initialized = False
        logger.info("审计日志数据库存储后端初始化")
    
    async def initialize(self) -> None:
        """初始化数据库表"""
        if self._initialized:
            return
        
        try:
            from app.core.database import async_engine
            
            async with async_engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            
            self._initialized = True
            logger.info("审计日志表初始化完成")
        except Exception as e:
            logger.error(f"审计日志表初始化失败: {e}", exc_info=True)
            raise
    
    async def store(self, event) -> None:
        """存储审计事件到数据库。
        
        Args:
            event: AuditEvent实例
        """
        if not self._initialized:
            await self.initialize()
        
        try:
            log_entry = AuditLog(
                event_id=event.event_id,
                timestamp=datetime.fromisoformat(event.timestamp) if isinstance(event.timestamp, str) else event.timestamp,
                event_type=event.event_type,
                user_id=event.user_id,
                username=event.username,
                ip_address=event.ip_address,
                user_agent=event.user_agent,
                resource=event.resource,
                action=event.action,
                details=json.dumps(event.details, ensure_ascii=False, default=str) if event.details else None,
                status=event.status,
                request_id=event.request_id,
                severity=event.severity,
                duration_ms=event.duration_ms if hasattr(event, 'duration_ms') else None,
            )
            
            self._buffer.append(log_entry)
            
            if len(self._buffer) >= self.max_batch_size:
                await self.flush()
        
        except Exception as e:
            logger.error(f"审计日志存储失败: {e}", exc_info=True)
    
    async def flush(self) -> None:
        """刷新缓冲区，将日志写入数据库"""
        if not self._buffer:
            return
        
        try:
            async for session in get_db():
                try:
                    session.add_all(self._buffer)
                    await session.commit()
                    logger.debug(f"审计日志批量写入成功: {len(self._buffer)} 条")
                    self._buffer.clear()
                    break
                except Exception as e:
                    await session.rollback()
                    logger.error(f"审计日志批量写入失败: {e}", exc_info=True)
                    raise
        except Exception as e:
            logger.error(f"审计日志刷新失败: {e}", exc_info=True)
    
    async def query(
        self,
        event_type: Optional[str] = None,
        user_id: Optional[int] = None,
        username: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        severity: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[dict]:
        """查询审计日志。
        
        Args:
            event_type: 事件类型过滤
            user_id: 用户ID过滤
            username: 用户名过滤
            start_time: 开始时间
            end_time: 结束时间
            severity: 严重级别过滤
            limit: 返回数量限制
            offset: 偏移量
            
        Returns:
            审计日志列表
        """
        if not self._initialized:
            await self.initialize()
        
        try:
            async for session in get_db():
                stmt = select(AuditLog)
                
                if event_type:
                    stmt = stmt.where(AuditLog.event_type == event_type)
                if user_id:
                    stmt = stmt.where(AuditLog.user_id == user_id)
                if username:
                    stmt = stmt.where(AuditLog.username == username)
                if start_time:
                    stmt = stmt.where(AuditLog.timestamp >= start_time)
                if end_time:
                    stmt = stmt.where(AuditLog.timestamp <= end_time)
                if severity:
                    stmt = stmt.where(AuditLog.severity == severity)
                
                stmt = stmt.order_by(AuditLog.timestamp.desc())
                stmt = stmt.limit(limit).offset(offset)
                
                result = await session.execute(stmt)
                logs = result.scalars().all()
                
                return [log.to_dict() for log in logs]
        
        except Exception as e:
            logger.error(f"审计日志查询失败: {e}", exc_info=True)
            return []
    
    async def get_statistics(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> dict:
        """获取审计日志统计信息。
        
        Args:
            start_time: 开始时间
            end_time: 结束时间
            
        Returns:
            统计信息字典
        """
        if not self._initialized:
            await self.initialize()
        
        try:
            async for session in get_db():
                base_query = select(AuditLog)
                if start_time:
                    base_query = base_query.where(AuditLog.timestamp >= start_time)
                if end_time:
                    base_query = base_query.where(AuditLog.timestamp <= end_time)
                
                count_stmt = select(func.count()).select_from(base_query.subquery())
                total_result = await session.execute(count_stmt)
                total = total_result.scalar() or 0
                
                type_stmt = select(
                    AuditLog.event_type,
                    func.count(AuditLog.id)
                ).group_by(AuditLog.event_type)
                type_result = await session.execute(type_stmt)
                by_type = {row[0]: row[1] for row in type_result.all()}
                
                severity_stmt = select(
                    AuditLog.severity,
                    func.count(AuditLog.id)
                ).group_by(AuditLog.severity)
                severity_result = await session.execute(severity_stmt)
                by_severity = {row[0]: row[1] for row in severity_result.all()}
                
                return {
                    "total_count": total,
                    "by_event_type": by_type,
                    "by_severity": by_severity,
                }
        
        except Exception as e:
            logger.error(f"审计日志统计失败: {e}", exc_info=True)
            return {
                "total_count": 0,
                "by_event_type": {},
                "by_severity": {},
            }
    
    async def get_query_stats_by_period(
        self,
        granularity: str = "month",
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """按日/月/年聚合查询量与文档处理量。
        
        Args:
            granularity: 聚合粒度，可选 day|month|year
            start_time: 开始时间
            end_time: 结束时间
            
        Returns:
            按时间周期聚合的统计数据列表
        """
        if not self._initialized:
            await self.initialize()
        
        fmt_map = {
            "hour": "%Y-%m-%d %H",
            "day": "%Y-%m-%d",
            "month": "%Y-%m",
            "year": "%Y",
        }
        fmt = fmt_map.get(granularity, "%Y-%m")
        
        try:
            async for session in get_db():
                stmt = select(
                    func.strftime(fmt, AuditLog.timestamp).label("period"),
                    func.count(AuditLog.id).filter(
                        AuditLog.event_type == "DATA:QUERY:EXECUTE"
                    ).label("queries"),
                    func.count(AuditLog.id).filter(
                        AuditLog.event_type == "DATA:DOCUMENT:UPLOAD"
                    ).label("documents"),
                    func.avg(AuditLog.duration_ms).label("avg_duration_ms"),
                    func.count(AuditLog.id).filter(
                        AuditLog.status == "failure"
                    ).label("failures"),
                ).group_by(
                    func.strftime(fmt, AuditLog.timestamp)
                ).order_by(
                    func.strftime(fmt, AuditLog.timestamp)
                )
                
                if start_time:
                    stmt = stmt.where(AuditLog.timestamp >= start_time)
                if end_time:
                    stmt = stmt.where(AuditLog.timestamp <= end_time)
                
                result = await session.execute(stmt)
                rows = result.all()
                
                return [
                    {
                        "name": row.period,
                        "queries": row.queries or 0,
                        "documents": row.documents or 0,
                        "avg_duration_ms": round(row.avg_duration_ms or 0, 2),
                        "failures": row.failures or 0,
                    }
                    for row in rows
                ]
        
        except Exception as e:
            logger.error(f"查询统计聚合失败: {e}", exc_info=True)
            return []


audit_log_storage = AuditLogStorage()