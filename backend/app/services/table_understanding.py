"""表格理解服务模块。

提供表格内容的智能理解和描述生成功能，包括：
- 分析表格结构（表头、数据行、合并单元格等）
- 生成表格内容的自然语言描述
- 识别表格中的关键信息和趋势
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from app.core.settings import get_settings
from app.core.logger import get_logger

settings = get_settings()

logger = get_logger(__name__)


@dataclass
class TableUnderstandingResult:
    """表格理解结果。
    
    Attributes:
        table_data: 原始表格数据
        description: 表格自然语言描述
        summary: 表格摘要
        key_insights: 关键洞察
        success: 是否成功
        error_message: 错误消息（如果有）
    """
    table_data: Dict[str, Any]
    description: str = ""
    summary: str = ""
    key_insights: List[str] = field(default_factory=list)
    success: bool = True
    error_message: Optional[str] = None


class TableUnderstandingService:
    """表格理解服务类。"""
    
    def __init__(self):
        """初始化表格理解服务。"""
        self.config = settings
    
    def _format_table_as_text(self, table_data: Dict[str, Any]) -> str:
        """将表格数据格式化为文本。
        
        Args:
            table_data: 表格数据字典
            
        Returns:
            格式化的表格文本
        """
        lines = []
        
        rows = table_data.get("rows", [])
        if not rows:
            return "空表格"
        
        max_widths = []
        for row in rows:
            for idx, cell in enumerate(row):
                cell_len = len(str(cell))
                if idx >= len(max_widths):
                    max_widths.append(cell_len)
                else:
                    max_widths[idx] = max(max_widths[idx], cell_len)
        
        for row_idx, row in enumerate(rows):
            formatted_cells = []
            for idx, cell in enumerate(row):
                formatted_cell = str(cell).ljust(max_widths[idx])
                formatted_cells.append(formatted_cell)
            lines.append(" | ".join(formatted_cells))
            
            if row_idx == 0 and len(rows) > 1:
                separator = "-+-".join("-" * w for w in max_widths)
                lines.append(separator)
        
        return "\n".join(lines)
    
    def _analyze_table_structure(self, table_data: Dict[str, Any]) -> Dict[str, Any]:
        """分析表格结构。
        
        Args:
            table_data: 表格数据字典
            
        Returns:
            表格结构分析结果
        """
        analysis = {
            "row_count": 0,
            "column_count": 0,
            "has_header": False,
            "empty_cells": 0,
            "total_cells": 0,
            "numeric_columns": [],
            "text_columns": []
        }
        
        rows = table_data.get("rows", [])
        analysis["row_count"] = len(rows)
        
        if rows:
            analysis["column_count"] = len(rows[0])
            analysis["has_header"] = True
        
        for row_idx, row in enumerate(rows):
            for col_idx, cell in enumerate(row):
                analysis["total_cells"] += 1
                if not str(cell).strip():
                    analysis["empty_cells"] += 1
        
        return analysis
    
    def _extract_key_insights(self, table_data: Dict[str, Any], structure: Dict[str, Any]) -> List[str]:
        """从表格中提取关键洞察。
        
        Args:
            table_data: 表格数据字典
            structure: 表格结构分析结果
            
        Returns:
            关键洞察列表
        """
        insights = []
        
        rows = table_data.get("rows", [])
        if not rows or len(rows) < 2:
            return insights
        
        insights.append(f"表格共有 {structure['row_count']} 行 {structure['column_count']} 列")
        
        if structure['empty_cells'] > 0:
            insights.append(f"表格包含 {structure['empty_cells']} 个空单元格")
        
        header = rows[0]
        data_rows = rows[1:]
        
        for col_idx, col_name in enumerate(header):
            numeric_values = []
            for row in data_rows:
                if col_idx < len(row):
                    cell_value = str(row[col_idx]).strip()
                    try:
                        num_value = float(cell_value)
                        numeric_values.append(num_value)
                    except ValueError:
                        pass
            
            if numeric_values:
                avg_value = sum(numeric_values) / len(numeric_values)
                max_value = max(numeric_values)
                min_value = min(numeric_values)
                insights.append(f"'{col_name}' 列: 平均值 {avg_value:.2f}, 最大值 {max_value}, 最小值 {min_value}")
        
        return insights
    
    def _generate_description(
        self,
        table_data: Dict[str, Any],
        structure: Dict[str, Any],
        insights: List[str]
    ) -> str:
        """生成表格自然语言描述。
        
        Args:
            table_data: 表格数据字典
            structure: 表格结构分析结果
            insights: 关键洞察列表
            
        Returns:
            表格自然语言描述
        """
        parts = []
        
        parts.append(f"这是一个包含 {structure['row_count']} 行和 {structure['column_count']} 列的表格。")
        
        rows = table_data.get("rows", [])
        if rows:
            header = rows[0]
            parts.append(f"表格的列包括：{', '.join(str(col) for col in header)}。")
        
        if insights:
            parts.append("\n关键信息：")
            for insight in insights[:5]:
                parts.append(f"- {insight}")
        
        table_text = self._format_table_as_text(table_data)
        if table_text:
            parts.append(f"\n表格内容：\n{table_text}")
        
        return "\n".join(parts)
    
    def _generate_summary(
        self,
        table_data: Dict[str, Any],
        structure: Dict[str, Any]
    ) -> str:
        """生成表格摘要。
        
        Args:
            table_data: 表格数据字典
            structure: 表格结构分析结果
            
        Returns:
            表格摘要
        """
        rows = table_data.get("rows", [])
        
        if not rows:
            return "空表格"
        
        summary_parts = []
        
        summary_parts.append(f"{structure['row_count']}行×{structure['column_count']}列")
        
        if rows and len(rows) > 0:
            header = rows[0]
            if len(header) <= 5:
                summary_parts.append(f"列：{', '.join(str(col) for col in header)}")
            else:
                summary_parts.append(f"列：{', '.join(str(col) for col in header[:3])}...")
        
        return " | ".join(summary_parts)
    
    def understand_table(
        self,
        table_data: Union[Dict[str, Any], List[List[Any]]],
        context_text: Optional[str] = None
    ) -> TableUnderstandingResult:
        """理解表格内容。
        
        Args:
            table_data: 表格数据（字典格式或二维列表格式）
            context_text: 表格周围的上下文文本（可选）
            
        Returns:
            表格理解结果
        """
        result = TableUnderstandingResult(
            table_data=table_data if isinstance(table_data, dict) else {"rows": table_data}
        )
        
        try:
            if isinstance(table_data, list):
                table_data = {"rows": table_data}
            
            structure = self._analyze_table_structure(table_data)
            insights = self._extract_key_insights(table_data, structure)
            description = self._generate_description(table_data, structure, insights)
            summary = self._generate_summary(table_data, structure)
            
            result.description = description
            result.summary = summary
            result.key_insights = insights
            result.success = True
            
            logger.info(f"表格理解成功: {summary}")
            
        except Exception as e:
            logger.error(f"表格理解失败: {e}")
            result.success = False
            result.error_message = str(e)
        
        return result
    
    def understand_tables_batch(
        self,
        tables_data: List[Union[Dict[str, Any], List[List[Any]]]],
        context_texts: Optional[List[Optional[str]]] = None
    ) -> List[TableUnderstandingResult]:
        """批量理解表格内容。
        
        Args:
            tables_data: 表格数据列表
            context_texts: 每个表格对应的上下文文本列表（可选）
            
        Returns:
            表格理解结果列表
        """
        results = []
        
        for idx, table_data in enumerate(tables_data):
            context_text = None
            if context_texts and idx < len(context_texts):
                context_text = context_texts[idx]
            
            result = self.understand_table(table_data, context_text)
            results.append(result)
        
        return results


table_understanding_service = TableUnderstandingService()
