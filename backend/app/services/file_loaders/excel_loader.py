"""Excel/CSV 文件加载器

支持格式：.xlsx, .xls, .csv, .tsv

功能特点：
- 支持 Excel/CSV 文件的读取和结构化解析
- 自动检测编码和表格格式
- 支持多工作表合并处理
- 提供表格结构化数据提取（NormalizedDocument / NormalizedTable）
- 按表头映射生成知识图谱实体/关系（TableMapper）
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterator, List, Dict, Any, Optional

from app.services.file_loaders.base import (
    BaseLoader, DocumentChunk, ChunkType, FileLoadError,
    link_document_chunks
)
from app.services.document_models import (
    NormalizedDocument, NormalizedTable, DocumentCategory
)
from app.core.logger import get_logger

logger = get_logger(__name__)


try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False
    logger.warning("pandas library not available. Excel files will not be processed.")


class ExcelLoader(BaseLoader):
    """Excel/CSV 文件加载器（兼容 BaseLoader）"""

    def __init__(self, use_ocr: bool = False, preserve_structure: bool = True,
                 sheet_name: Optional[str] = None, header_row: int = 0,
                 csv_encoding: Optional[str] = None):
        super().__init__(use_ocr=use_ocr, preserve_structure=preserve_structure)
        self.sheet_name = sheet_name
        self.header_row = header_row
        self.csv_encoding = csv_encoding

    @property
    def supported_extensions(self) -> list[str]:
        return ['.xlsx', '.xls', '.csv', '.tsv']

    def load(self, file_path: Path) -> str:
        """加载 Excel/CSV 文件为纯文本。"""
        self._validate_file(file_path)

        if not PANDAS_AVAILABLE:
            raise FileLoadError("pandas library is required for Excel/CSV file processing", file_path)

        try:
            suffix = file_path.suffix.lower()
            if suffix == '.csv' or suffix == '.tsv':
                df = self._read_csv(file_path)
                return self._dataframe_to_text(df, sheet_name=file_path.stem)

            if self.sheet_name is None:
                excel_file = pd.ExcelFile(file_path)
                contents = []
                for sheet in excel_file.sheet_names:
                    df = pd.read_excel(file_path, sheet_name=sheet, header=self.header_row)
                    contents.append(self._dataframe_to_text(df, sheet_name=sheet))
                return "\n\n".join(contents)
            else:
                df = pd.read_excel(file_path, sheet_name=self.sheet_name, header=self.header_row)
                return self._dataframe_to_text(df, sheet_name=self.sheet_name)

        except Exception as e:
            raise FileLoadError(f"Failed to load Excel/CSV: {e}", file_path) from e

    def load_stream(self, file_path: Path) -> Iterator[str]:
        """流式加载 Excel/CSV 文件（按工作表）。"""
        content = self.load(file_path)
        for sheet_text in content.split('\n\n工作表: '):
            if sheet_text.strip():
                yield sheet_text.strip()

    def load_document_chunks(self, file_path: Path) -> list[DocumentChunk]:
        """加载 Excel/CSV 文件为按工作表分块的文档块。"""
        self._validate_file(file_path)

        if not PANDAS_AVAILABLE:
            raise FileLoadError("pandas library is required for Excel/CSV file processing", file_path)

        try:
            suffix = file_path.suffix.lower()
            chunks = []

            if suffix == '.csv' or suffix == '.tsv':
                df = self._read_csv(file_path)
                sheet_text = self._dataframe_to_text(df, sheet_name=file_path.stem)
                rows_data = self._dataframe_to_rows(df)
                chunks.append(DocumentChunk(
                    content=sheet_text,
                    metadata=self.metadata.copy(),
                    chunk_type=ChunkType.TABLE,
                    source_file=str(file_path),
                    table_data={"rows": rows_data}
                ))
            else:
                excel_file = pd.ExcelFile(file_path)
                sheets_to_read = [self.sheet_name] if self.sheet_name else excel_file.sheet_names

                for sheet in sheets_to_read:
                    df = pd.read_excel(file_path, sheet_name=sheet, header=self.header_row)
                    sheet_text = self._dataframe_to_text(df, sheet_name=sheet)
                    rows_data = self._dataframe_to_rows(df)
                    chunks.append(DocumentChunk(
                        content=sheet_text,
                        metadata=self.metadata.copy(),
                        chunk_type=ChunkType.TABLE,
                        source_file=str(file_path),
                        table_data={"rows": rows_data}
                    ))

            chunks = link_document_chunks(chunks)

            self.metadata.update(self.get_document_metadata(file_path))
            self.metadata.update({
                "total_sheets": len(chunks),
                "sheet_name": self.sheet_name,
            })

            return chunks

        except Exception as e:
            raise FileLoadError(f"Failed to load Excel/CSV chunks: {e}", file_path) from e

    def load_normalized_document(self, file_path: Path, doc_id: Optional[str] = None) -> NormalizedDocument:
        """加载 Excel/CSV 文件并输出统一归一化结构
        
        保留结构化表格数据，供 TableMapper 直接进行表头->实体/关系映射。
        """
        self._validate_file(file_path)

        if not PANDAS_AVAILABLE:
            raise FileLoadError("pandas library is required for Excel/CSV file processing", file_path)

        suffix = file_path.suffix.lower()
        tables: List[NormalizedTable] = []

        try:
            if suffix == '.csv' or suffix == '.tsv':
                df = self._read_csv(file_path)
                tables.append(self._dataframe_to_normalized_table(df, file_path.stem, {
                    "file_type": "csv",
                    "sheet_index": 0
                }))
            else:
                excel_file = pd.ExcelFile(file_path)
                sheets_to_read = [self.sheet_name] if self.sheet_name else excel_file.sheet_names
                for idx, sheet in enumerate(sheets_to_read):
                    df = pd.read_excel(file_path, sheet_name=sheet, header=self.header_row)
                    tables.append(self._dataframe_to_normalized_table(df, sheet, {
                        "file_type": "excel",
                        "sheet_index": idx,
                        "sheet_name": sheet
                    }))

            metadata = self.get_document_metadata(file_path)
            metadata.update({
                "doc_type": DocumentCategory.STRUCTURED.value,
                "md5_hash": self._compute_md5(file_path),
                "table_count": len(tables),
                "total_rows": sum(t.row_count for t in tables)
            })

            return NormalizedDocument(
                doc_id=doc_id or f"doc-{file_path.stem}-{metadata.get('md5_hash', '')[:8]}",
                doc_type=DocumentCategory.STRUCTURED.value,
                source_file=str(file_path),
                metadata=metadata,
                tables=tables
            )

        except Exception as e:
            raise FileLoadError(f"Failed to normalize Excel/CSV: {e}", file_path) from e

    def _read_csv(self, file_path: Path) -> "pd.DataFrame":
        """读取 CSV/TSV 文件，支持编码回退。"""
        sep = '\t' if file_path.suffix.lower() == '.tsv' else ','
        encodings = [self.csv_encoding] if self.csv_encoding else ["utf-8", "gbk", "latin1"]
        last_error = None
        for encoding in encodings:
            try:
                return pd.read_csv(file_path, sep=sep, encoding=encoding)
            except Exception as e:
                last_error = e
                continue
        raise FileLoadError(f"Failed to read CSV/TSV: {last_error}", file_path)

    def _dataframe_to_rows(self, df) -> List[List[str]]:
        """将 DataFrame 转换为行列表（含表头）。"""
        rows = []
        if not df.empty:
            rows.append([str(c) for c in df.columns])
            for _, row in df.iterrows():
                rows.append([str(v) if pd.notna(v) else "" for v in row])
        return rows

    def _dataframe_to_normalized_table(self, df, sheet_name: str, position_meta: Dict[str, Any]) -> NormalizedTable:
        """将 DataFrame 转换为 NormalizedTable
        
        处理：过滤空行/空列、合并单元格展开（前向填充）、统一表头命名、
        日期/数值格式化为 ISO 字符串。
        """
        if df.empty:
            return NormalizedTable(
                table_id=f"table-{sheet_name}",
                sheet_name=sheet_name,
                headers=[],
                rows=[],
                position_meta=position_meta
            )

        # 1. 统一列名为字符串，去除首尾空格
        df.columns = [str(c).strip() if pd.notna(c) else f"col_{i}" for i, c in enumerate(df.columns)]

        # 2. 前向填充处理合并单元格（简化版：按列前向填充空值）
        df = df.fillna(method="ffill")

        # 3. 删除全空行和全空列
        df = df.dropna(how="all").dropna(axis=1, how="all")

        # 4. 日期/数值统一处理为字符串
        headers = [str(c) for c in df.columns]
        rows = []
        for _, row in df.iterrows():
            row_dict = {}
            for col in df.columns:
                value = row[col]
                if pd.isna(value):
                    row_dict[col] = ""
                elif isinstance(value, pd.Timestamp):
                    row_dict[col] = value.isoformat()
                else:
                    row_dict[col] = str(value)
            rows.append(row_dict)

        return NormalizedTable(
            table_id=f"table-{sheet_name}",
            sheet_name=sheet_name,
            headers=headers,
            rows=rows,
            position_meta=position_meta
        )

    def _dataframe_to_text(self, df, sheet_name: str) -> str:
        """将 DataFrame 转换为文本格式。"""
        lines = [f"工作表: {sheet_name}"]

        if not df.empty:
            columns = [str(col) for col in df.columns]
            lines.append(f"列名: {', '.join(columns)}")

            for idx, row in df.iterrows():
                row_data = []
                for col in df.columns:
                    value = row[col]
                    row_data.append(str(value) if pd.notna(value) else "")
                lines.append(f"行 {idx}: {', '.join(row_data)}")
        else:
            lines.append("表格为空")

        return "\n".join(lines)

    @staticmethod
    def _compute_md5(file_path: Path) -> str:
        """计算文件 MD5。"""
        h = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

