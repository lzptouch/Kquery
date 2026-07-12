"""
数据库查询优化工具

提供批量操作、索引优化建议、N+1问题检测等数据库性能优化功能
"""

from typing import List, Dict, Any, Optional, Type, TypeVar
from sqlalchemy import select, update, delete, func, text, inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from contextlib import asynccontextmanager
import time

from app.core.logger import get_logger


logger = get_logger(__name__)

# 泛型类型，用于类型提示
T = TypeVar('T')


class QueryOptimizer:
    """
    数据库查询优化器
    
    提供常用的数据库优化模式和最佳实践封装
    
    使用示例:
        optimizer = QueryOptimizer()
        
        # 批量插入
        await optimizer.bulk_insert(session, User, users_data)
        
        # 高效分页查询
        result = await optimizer.paginate(
            session, Document,
            filters=[Document.kb_id == kb_id],
            page=1, page_size=20
        )
    """
    
    def __init__(self):
        """初始化优化器"""
        self._query_stats = {
            "total_queries": 0,
            "slow_queries": [],  # 超过阈值慢查询记录
            "slow_threshold": 2.0  # 慢查询阈值（秒）
        }
    
    @asynccontextmanager
    async def timed_query(self, query_description: str = ""):
        """
        计时上下文管理器
        
        用于测量查询执行时间并记录慢查询
        
        使用示例:
            with optimizer.timed_query("加载用户列表") as timer:
                result = await session.execute(stmt)
                
        Yields:
            None (仅用于计时)
        """
        start_time = time.time()
        yield
        
        elapsed_time = time.time() - start_time
        self._query_stats["total_queries"] += 1
        
        if elapsed_time > self._query_stats["slow_threshold"]:
            slow_query_record = {
                "description": query_description,
                "duration": round(elapsed_time, 4),
                "timestamp": time.time()
            }
            self._query_stats["slow_queries"].append(slow_query_record)
            
            logger.warning(
                f"⚠ 慢查询检测 | {query_description} | "
                f"耗时: {elapsed_time:.4f}s"
            )
        else:
            logger.debug(f"✓ 查询完成 | {query_description} | {elapsed_time:.4f}s")
    
    async def paginate(
        self,
        db: AsyncSession,
        model: Type[T],
        *,
        filters: list = None,
        order_by: Any = None,
        page: int = 1,
        page_size: int = 20,
        max_page_size: int = 100,
        eager_loads: list = None
    ) -> Dict[str, Any]:
        """
        通用高效分页查询
        
        自动处理：
        - 页码和页大小验证
        - 总数统计（使用COUNT优化）
        - OFFSET/LIMIT分页
        - 预加载关联关系（解决N+1问题）
        - 返回标准化的分页结果格式
        
        Args:
            db: 数据库会话
            model: SQLAlchemy模型类
            filters: 过滤条件列表 [Model.field == value]
            order_by: 排序条件 Model.created_at.desc()
            page: 当前页码（从1开始）
            page_size: 每页数量
            max_page_size: 最大允许的页大小
            eager_loads: 预加载的关系列表 [selectinload(Model.related_field)]
            
        Returns:
            分页结果字典：
            {
                "items": [...],
                "total": int,
                "page": int,
                "page_size": int,
                "total_pages": int,
                "has_next": bool,
                "has_prev": bool
            }
        """
        from sqlalchemy.orm import Query as SAQuery
        
        # 参数校验
        page = max(1, page)
        page_size = min(max(1, page_size), max_page_size)
        
        # 构建基础查询
        query = select(model)
        
        if filters:
            for filter_condition in filters:
                query = query.where(filter_condition)
        
        if order_by is not None:
            query = query.order_by(order_by)
        
        # 添加预加载（避免N+1查询）
        if eager_loads:
            for loader in eager_loads:
                query = query.options(loader)
        
        # 统计总数（优化的COUNT查询）
        count_stmt = select(func.count()).select_from(query.subquery())
        
        with self.timed_query("分页-计数"):
            total_result = await db.execute(count_stmt)
            total = total_result.scalar() or 0
        
        # 计算分页参数
        offset = (page - 1) * page_size
        total_pages = (total + page_size - 1) // page_size if total > 0 else 0
        
        # 执行分页查询
        paginated_query = query.offset(offset).limit(page_size)
        
        with self.timed_query(f"分页-数据(page={page})"):
            result = await db.execute(paginated_query)
            items = list(result.scalars().all())
        
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_prev": page > 1
        }
    
    async def bulk_insert(
        self,
        db: AsyncSession,
        model: Type[T],
        items: List[Dict[str, Any]],
        batch_size: int = 500
    ) -> int:
        """
        批量插入数据（性能优化版）
        
        相比逐条INSERT提升10倍以上性能：
        - 分批提交（避免单次事务过大）
        - 使用bulk_insert_mappings（比save更快）
        - 自动错误处理和回滚
        
        Args:
            db: 数据库会话
            model: 目标模型类
            items: 要插入的数据字典列表
            batch_size: 每批处理的数量
            
        Returns:
            成功插入的总条数
            
        Raises:
            Exception: 插入失败时抛出（已自动回滚）
        """
        if not items:
            return 0
        
        inserted_count = 0
        total_items = len(items)
        
        try:
            # 分批处理
            for i in range(0, total_items, batch_size):
                batch = items[i:i + batch_size]
                
                with self.timed_query(f"批量插入-{i//batch_size + 1}"):
                    await db.bulk_insert_mappings(model, batch)
                    await db.flush()
                
                inserted_count += len(batch)
                logger.debug(
                    f"批量插入进度: {inserted_count}/{total_items}"
                )
            
            # 全部成功后提交
            await db.commit()
            
            logger.info(
                f"✓ 批量插入完成 | 表: {model.__tablename__} | "
                f"数量: {inserted_count} | 批次数: {(len(items)-1)//batch_size + 1}"
            )
            
            return inserted_count
            
        except Exception as e:
            await db.rollback()
            logger.error(
                f"✗ 批量插入失败 | 表: {model.__tablename__} | "
                f"已插入: {inserted_count} | 错误: {e}",
                exc_info=True
            )
            raise
    
    async def bulk_update(
        self,
        db: AsyncSession,
        model: Type[T],
        updates: List[Dict[str, Any]],
        id_column: str = "id",
        batch_size: int = 200
    ) -> int:
        """
        批量更新数据（性能优化版）
        
        Args:
            db: 数据库会话
            model: 目标模型类
            updates: 要更新的数据字典列表，每个必须包含id_column字段
            id_column: 主键/唯一标识列名
            batch_size: 批次大小
            
        Returns:
            成功更新的总条数
        """
        if not updates:
            return 0
        
        updated_count = 0
        
        try:
            for i in range(0, len(updates), batch_size):
                batch = updates[i:i + batch_size]
                
                with self.timed_query(f"批量更新-{i//batch_size + 1}"):
                    await db.bulk_update_mappings(model, batch)
                    await db.flush()
                
                updated_count += len(batch)
            
            await db.commit()
            
            logger.info(
                f"✓ 批量更新完成 | 表: {model.__tablename__} | "
                f"数量: {updated_count}"
            )
            
            return updated_count
            
        except Exception as e:
            await db.rollback()
            logger.error(f"✗ 批量更新失败: {e}", exc_info=True)
            raise
    
    async def bulk_delete(
        self,
        db: AsyncSession,
        model: Type[T],
        ids: List[Any],
        id_column: str = "id",
        batch_size: int = 500
    ) -> int:
        """
        批量删除数据（性能优化版）
        
        使用WHERE IN子句一次性删除，而非逐条DELETE
        
        Args:
            db: 数据库会话
            model: 目标模型类
            ids: 要删除的ID列表
            id_column: ID列名
            batch_size: 每批删除数量（防止单次SQL过长）
            
        Returns:
            删除的总行数
        """
        if not ids:
            return 0
        
        deleted_total = 0
        
        try:
            for i in range(0, len(ids), batch_size):
                batch_ids = ids[i:i + batch_size]
                
                stmt = delete(model).where(
                    getattr(model, id_column).in_(batch_ids)
                )
                
                with self.timed_query(f"批量删除-{i//batch_size + 1}"):
                    result = await db.execute(stmt)
                    deleted = result.rowcount
                    await db.flush()
                    
                    deleted_total += deleted
            
            await db.commit()
            
            logger.info(
                f"✓ 批量删除完成 | 表: {model.__tablename__} | "
                f"数量: {deleted_total}"
            )
            
            return deleted_total
            
        except Exception as e:
            await db.rollback()
            logger.error(f"✗ 批量删除失败: {e}", exc_info=True)
            raise
    
    def suggest_indexes(self, db_session, model: Type[T]) -> List[Dict]:
        """
        建议添加的索引（基于常见查询模式分析）
        
        分析模型的查询模式并推荐缺失的索引。
        注意：这是静态分析，实际效果需要根据具体业务调整。
        
        Args:
            db_session: 同步数据库会话（用于inspect）
            model: 要分析的模型类
            
        Returns:
            索引建议列表：
            [{
                "columns": ["column_name"],
                "type": "index|unique",
                "reason": "建议原因",
                "priority": "high|medium|low"
            }]
        """
        suggestions = []
        
        inspector = inspect(db_session.bind)
        existing_indexes = inspector.get_indexes(model.__tablename__)
        existing_columns = set()
        
        for idx in existing_indexes:
            existing_columns.update(idx["column_names"])
        
        # 分析模型定义的外键和常用查询字段
        mapper = inspect(model)
        
        for column in mapper.columns:
            col_name = column.name
            
            # 外键应该有索引
            if column.foreign_keys and col_name not in existing_columns:
                suggestions.append({
                    "columns": [col_name],
                    "type": "index",
                    "reason": f"外键列 '{col_name}' 缺少索引，JOIN查询可能很慢",
                    "priority": "high"
                })
            
            # 有unique约束的字段
            if column.unique and col_name not in existing_columns:
                suggestions.append({
                    "columns": [col_name],
                    "type": "unique",
                    "reason": f"唯一约束字段 '{col_name}' 需要索引支持快速查找",
                    "priority": "high"
                })
            
            # 常用于过滤或排序的字段（需要根据实际业务判断）
            if hasattr(column, 'index') and column.index and col_name not in existing_columns:
                suggestions.append({
                    "columns": [col_name],
                    "type": "index",
                    "reason": f"标记为index的字段 '{col_name}' 未创建索引",
                    "priority": "medium"
                })
        
        return suggestions
    
    def get_slow_queries(self) -> List[Dict]:
        """获取所有慢查询记录"""
        return self._query_stats["slow_queries"]
    
    def reset_stats(self) -> None:
        """重置统计信息"""
        self._query_stats = {
            "total_queries": 0,
            "slow_queries": [],
            "slow_threshold": self._query_stats["slow_threshold"]
        }


# 全局实例
query_optimizer = QueryOptimizer()


class NPlusOneDetector:
    """
    N+1查询问题检测器
    
    用于开发环境自动检测N+1查询模式，
    在生产环境中应禁用以减少开销
    
    使用方式:
        detector = NPlusOneDetector()
        with detector.detect():
            # 正常执行代码
            users = await get_all_users()
            for user in users:
                posts = await get_user_posts(user.id)  # 可能触发N+1
                
        report = detector.get_report()
    """
    
    def __init__(self):
        """初始化检测器"""
        self._queries_log: List[Dict] = []
        self._active = False
        self._threshold = 5
        self._sql_templates: Dict[str, List[int]] = {}
        self._similar_queries: Dict[str, List[Dict]] = {}
    
    @asynccontextmanager
    async def detect(self):
        """启动检测模式的上下文管理器"""
        self._active = True
        self._queries_log.clear()
        self._sql_templates.clear()
        self._similar_queries.clear()
        yield
        self._active = False
        self._analyze()
    
    def _log_query(self, sql_template: str, params: tuple):
        """记录一次查询"""
        if not self._active:
            return
        
        normalized = self._normalize_sql(sql_template)
        self._queries_log.append({
            "sql_template": normalized,
            "params": params,
            "timestamp": time.time()
        })
        
        if normalized not in self._sql_templates:
            self._sql_templates[normalized] = []
            self._similar_queries[normalized] = []
        self._sql_templates[normalized].append(len(self._queries_log) - 1)
        self._similar_queries[normalized].append({
            "params": params,
            "index": len(self._queries_log) - 1,
        })
    
    def _normalize_sql(self, sql: str) -> str:
        """规范化SQL模板，提取参数占位符"""
        import re
        normalized = re.sub(r'\d+(?:\.\d+)?', '?', sql)
        normalized = re.sub(r"'[^']*'", '?', normalized)
        normalized = re.sub(r'"[^"]*"', '?', normalized)
        return normalized[:100]
    
    def _analyze(self) -> Dict[str, Any]:
        """分析查询日志，识别可能的N+1模式"""
        results = {
            "total_queries": len(self._queries_log),
            "potential_n_plus_one_patterns": [],
            "recommendations": []
        }
        
        for template, indices in self._sql_templates.items():
            if len(indices) >= self._threshold:
                query_groups = self._group_similar_queries(template)
                
                for group_key, group_queries in query_groups.items():
                    if len(group_queries) >= self._threshold:
                        pattern = self._detect_n_plus_one_pattern(
                            template, group_queries
                        )
                        if pattern:
                            results["potential_n_plus_one_patterns"].append(pattern)
        
        results["recommendations"] = self._generate_recommendations(
            results["potential_n_plus_one_patterns"]
        )
        
        return results
    
    def _group_similar_queries(self, template: str) -> Dict[str, List[Dict]]:
        """将相似的查询分组"""
        groups: Dict[str, List[Dict]] = {}
        queries = self._similar_queries.get(template, [])
        
        for query in queries:
            params = query.get("params", ())
            if not params:
                group_key = "no_params"
            else:
                group_key = f"params_count_{len(params)}"
            
            if group_key not in groups:
                groups[group_key] = []
            groups[group_key].append(query)
        
        return groups
    
    def _detect_n_plus_one_pattern(
        self,
        template: str,
        queries: List[Dict],
    ) -> Optional[Dict[str, Any]]:
        """检测N+1模式"""
        if len(queries) < self._threshold:
            return None
        
        params_list = [q.get("params", ()) for q in queries]
        
        base_pattern = None
        variant_count = 0
        
        for params in params_list:
            if base_pattern is None:
                base_pattern = params
            elif params == base_pattern:
                continue
            else:
                variant_count += 1
                if variant_count > self._threshold // 2:
                    return None
        
        avg_interval = self._calculate_avg_interval(queries)
        
        return {
            "type": "n_plus_one",
            "template": template,
            "occurrence_count": len(queries),
            "variant_count": variant_count,
            "avg_execution_interval_ms": round(avg_interval, 2),
            "severity": "high" if variant_count < 2 else "medium",
            "suggestion": "考虑使用批量查询或JOIN操作替代循环查询"
        }
    
    def _calculate_avg_interval(self, queries: List[Dict]) -> float:
        """计算查询的平均间隔"""
        if len(queries) < 2:
            return 0.0
        
        timestamps = [self._queries_log[q["index"]]["timestamp"] for q in queries]
        timestamps.sort()
        
        intervals = []
        for i in range(1, len(timestamps)):
            interval = (timestamps[i] - timestamps[i-1]) * 1000
            intervals.append(interval)
        
        return sum(intervals) / len(intervals) if intervals else 0.0
    
    def _generate_recommendations(
        self,
        patterns: List[Dict],
    ) -> List[str]:
        """生成优化建议"""
        recommendations = []
        
        n_plus_one_count = sum(
            1 for p in patterns if p.get("type") == "n_plus_one"
        )
        
        if n_plus_one_count > 0:
            recommendations.append(
                f"检测到 {n_plus_one_count} 个潜在的N+1查询模式，"
                "建议使用SQLAlchemy的selectinload或joinedload预加载关联数据"
            )
        
        if recommendations:
            recommendations.append(
                "使用 eager_loads 参数预加载关联关系："
                "query.options(selectinload(Model.related_field))"
            )
        
        return recommendations
    
    def get_report(self) -> Dict[str, Any]:
        """获取检测报告"""
        return self._analyze()


# 全局检测器实例（默认不启用）
n_plus_one_detector = NPlusOneDetector()
