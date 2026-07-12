"""图表数据 API (v2.0)。

为前端 recharts 等图表库提供统一的数据接口，
支持饼图、柱状图、折线图、面积图、散点图、雷达图、漏斗图等。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query

from app.api.knowledge_graph import StandardResponse
from app.core.logger import get_logger
from app.core.audit_storage import audit_log_storage

logger = get_logger(__name__)
router = APIRouter(tags=["chart"])


# ============================================================
# 辅助函数：模拟数据生成（实际环境应从数据库/统计服务获取）
# ============================================================

def _get_mock_pie_data() -> List[Dict[str, Any]]:
    return [
        {"name": "技术文档", "value": 450},
        {"name": "产品手册", "value": 320},
        {"name": "会议记录", "value": 280},
        {"name": "研究报告", "value": 190},
        {"name": "其他", "value": 160},
    ]


def _get_mock_bar_data() -> List[Dict[str, Any]]:
    return [
        {"name": "1月", "queries": 1200, "documents": 340},
        {"name": "2月", "queries": 1800, "documents": 420},
        {"name": "3月", "queries": 2400, "documents": 510},
        {"name": "4月", "queries": 2100, "documents": 480},
        {"name": "5月", "queries": 2800, "documents": 620},
        {"name": "6月", "queries": 3200, "documents": 700},
    ]


def _get_mock_line_data() -> List[Dict[str, Any]]:
    return [
        {"name": "周一", "accuracy": 82, "latency": 450},
        {"name": "周二", "accuracy": 85, "latency": 420},
        {"name": "周三", "accuracy": 88, "latency": 380},
        {"name": "周四", "accuracy": 86, "latency": 400},
        {"name": "周五", "accuracy": 90, "latency": 350},
        {"name": "周六", "accuracy": 91, "latency": 340},
        {"name": "周日", "accuracy": 89, "latency": 360},
    ]


def _get_mock_area_data() -> List[Dict[str, Any]]:
    return [
        {"name": "00:00", "queries": 120},
        {"name": "04:00", "queries": 80},
        {"name": "08:00", "queries": 450},
        {"name": "12:00", "queries": 680},
        {"name": "16:00", "queries": 520},
        {"name": "20:00", "queries": 380},
        {"name": "24:00", "queries": 200},
    ]


def _get_mock_scatter_data() -> List[Dict[str, Any]]:
    return [
        {"x": 120, "y": 82, "size": 30, "category": "技术"},
        {"x": 180, "y": 85, "size": 45, "category": "产品"},
        {"x": 240, "y": 88, "size": 60, "category": "技术"},
        {"x": 300, "y": 86, "size": 50, "category": "管理"},
        {"x": 150, "y": 90, "size": 40, "category": "产品"},
        {"x": 200, "y": 91, "size": 55, "category": "技术"},
        {"x": 280, "y": 89, "size": 70, "category": "管理"},
    ]


def _get_mock_radar_data() -> List[Dict[str, Any]]:
    return [
        {"subject": "检索速度", "A": 90, "fullMark": 100},
        {"subject": "准确率", "A": 85, "fullMark": 100},
        {"subject": "覆盖度", "A": 78, "fullMark": 100},
        {"subject": "实时性", "A": 92, "fullMark": 100},
        {"subject": "用户体验", "A": 88, "fullMark": 100},
    ]


def _get_mock_funnel_data() -> List[Dict[str, Any]]:
    return [
        {"name": "文档上传", "value": 1000},
        {"name": "解析成功", "value": 850},
        {"name": "向量化完成", "value": 780},
        {"name": "知识图谱构建", "value": 620},
        {"name": "可被检索", "value": 580},
    ]


# ============================================================
# 端点定义
# ============================================================

@router.get("/charts/types", response_model=StandardResponse)
async def list_chart_types():
    """列出支持的图表类型。"""
    return StandardResponse(
        success=True,
        data={
            "types": [
                {"id": "pie", "name": "饼图", "description": "展示占比分布"},
                {"id": "bar", "name": "柱状图", "description": "展示数值对比"},
                {"id": "line", "name": "折线图", "description": "展示趋势变化"},
                {"id": "area", "name": "面积图", "description": "展示累积趋势"},
                {"id": "scatter", "name": "散点图", "description": "展示相关性"},
                {"id": "radar", "name": "雷达图", "description": "展示多维度评分"},
                {"id": "funnel", "name": "漏斗图", "description": "展示转化流程"},
            ]
        }
    )


@router.get("/charts/data", response_model=StandardResponse)
async def get_chart_data(
    chart_type: str = Query(..., description="图表类型: pie|bar|line|area|scatter|radar|funnel"),
    collection: Optional[str] = Query(None, description="知识库名称（可选）"),
):
    """获取指定类型的图表数据。"""
    
    type_map = {
        "pie": ("文档类型分布", _get_mock_pie_data()),
        "bar": ("查询与文档月度统计", _get_mock_bar_data()),
        "line": ("系统性能趋势", _get_mock_line_data()),
        "area": ("查询量时段分布", _get_mock_area_data()),
        "scatter": ("文档量 vs 准确率", _get_mock_scatter_data()),
        "radar": ("系统能力评估", _get_mock_radar_data()),
        "funnel": ("文档处理转化漏斗", _get_mock_funnel_data()),
    }
    
    if chart_type not in type_map:
        return StandardResponse(
            success=False,
            data=None,
            message=f"不支持的图表类型: {chart_type}"
        )
    
    title, data = type_map[chart_type]
    
    # TODO: 实际生产环境中，根据 collection 查询真实统计
    
    return StandardResponse(
        success=True,
        data={
            "chart_type": chart_type,
            "title": title,
            "data": data,
            "metadata": {
                "collection": collection,
                "data_source": "mock" if not collection else "database",
                "timestamp": "2025-06-29T00:00:00Z",
            }
        },
        message="图表数据获取成功"
    )


async def _get_real_bar_data(collection: Optional[str] = None) -> tuple:
    """获取真实的查询与文档月度统计数据。"""
    try:
        now = datetime.now(timezone.utc)
        start_time = now - timedelta(days=180)  # 最近 6 个月
        stats = await audit_log_storage.get_query_stats_by_period(
            granularity="month",
            start_time=start_time,
            end_time=now,
        )
        if stats:
            return ("查询与文档月度统计", stats)
    except Exception as e:
        logger.warning(f"获取真实月度统计失败: {e}")
    
    # 无数据时回退到模拟数据
    return ("查询与文档月度统计", _get_mock_bar_data())


@router.get("/charts/query-stats", response_model=StandardResponse)
async def get_query_stats(
    granularity: str = Query("month", description="聚合粒度: hour|day|month|year"),
    date: Optional[str] = Query(None, description="指定日期 ISO 字符串，用于下钻"),
    start: Optional[str] = Query(None, description="开始时间 ISO 字符串"),
    end: Optional[str] = Query(None, description="结束时间 ISO 字符串"),
    limit: int = Query(12, ge=1, le=365),
):
    """获取查询与文档处理统计数据（按小时/日/月/年聚合）。"""
    now = datetime.now(timezone.utc)
    
    if date:
        # 下钻模式：根据 date 和 granularity 计算时间范围
        dt = datetime.fromisoformat(date)
        # 确保 dt 带时区信息，避免与数据库 timezone-aware 字段比较出错
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        
        if granularity == "hour":
            # 查看某天的小时分布：date 格式 "2026-07-05"
            start_time = dt.replace(hour=0, minute=0, second=0, microsecond=0)
            end_time = start_time + timedelta(days=1) - timedelta(microseconds=1)
        elif granularity == "day":
            # 查看某月的每天分布：date 格式 "2026-07-01"
            start_time = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if dt.month == 12:
                end_time = dt.replace(year=dt.year + 1, month=1, day=1) - timedelta(microseconds=1)
            else:
                end_time = dt.replace(month=dt.month + 1, day=1) - timedelta(microseconds=1)
        else:  # month / year
            # 查看某年的每月分布：date 格式 "2026-01-01"
            start_time = dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            end_time = dt.replace(year=dt.year + 1, month=1, day=1) - timedelta(microseconds=1)
    else:
        # 非下钻模式：使用 start/end/limit 计算时间范围（现有逻辑）
        end_time = datetime.fromisoformat(end) if end else now
        if end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=timezone.utc)
        if start:
            start_time = datetime.fromisoformat(start)
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=timezone.utc)
        elif granularity == "hour":
            start_time = end_time - timedelta(hours=limit)
        elif granularity == "day":
            start_time = end_time - timedelta(days=limit)
        elif granularity == "month":
            start_time = end_time - timedelta(days=limit * 30)
        else:
            start_time = datetime(end_time.year - limit, 1, 1, tzinfo=timezone.utc)
    
    stats = await audit_log_storage.get_query_stats_by_period(
        granularity=granularity,
        start_time=start_time,
        end_time=end_time,
    )
    
    return StandardResponse(
        success=True,
        data={
            "chart_type": "bar",
            "title": _get_title(granularity),
            "data": stats,
            "metadata": {
                "granularity": granularity,
                "start": start_time.isoformat(),
                "end": end_time.isoformat(),
            }
        },
        message="查询统计获取成功"
    )


def _get_title(granularity: str) -> str:
    titles = {
        "hour": "查询与文档小时分布",
        "day": "查询与文档日统计",
        "month": "查询与文档月度统计",
        "year": "查询与文档年度统计",
    }
    return titles.get(granularity, "查询与文档统计")


@router.get("/charts/dashboard", response_model=StandardResponse)
async def get_dashboard_charts(
    collection: Optional[str] = Query(None, description="知识库名称（可选）"),
):
    """获取仪表盘所需的全部图表数据。"""
    charts = []
    for ctype in ["pie", "bar", "line", "area"]:
        if ctype == "bar":
            title, data = await _get_real_bar_data(collection)
        else:
            title, data = {
                "pie": ("文档类型分布", _get_mock_pie_data()),
                "line": ("系统性能趋势", _get_mock_line_data()),
                "area": ("查询量时段分布", _get_mock_area_data()),
            }[ctype]
        charts.append({
            "chart_type": ctype,
            "title": title,
            "data": data,
        })
    
    return StandardResponse(
        success=True,
        data={
            "charts": charts,
            "metadata": {
                "collection": collection,
                "total_charts": len(charts),
            }
        },
        message="仪表盘数据获取成功"
    )
