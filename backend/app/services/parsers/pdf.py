"""PDF 解析。

按页混合策略（每页独立判定）：
- 文字页（文本层字符数 ≥ 阈值）：文字流抽取 + find_tables 几何表格识别；
- 扫描页（文本层缺失/极短）：渲染为位图 → qwen-vl-ocr 整页识别，
  VL 模型直接输出 Markdown（表格保留行列结构、公式转 LaTeX、标题保留层级），
  其中 Markdown 表格块被剥离为独立 TableRecord（modality=table），
  避免被切块器拦腰切断——与文字流表格享受同等的结构保护；
- 整篇无文本且 OCR 未启用/不可用 → 明确报错（不静默产出空索引）。

页面位图进资源目录（MediaAsset，kind=page_image），与文本分开存放，
溯源时前端可回显原页图像。
"""
import hashlib
import os
import re
import tempfile
from pathlib import Path

import pymupdf

from app.core.envelope import ApiError, ErrCode
from app.domain.schemas import MediaAsset, TableRecord
from app.services.parsers.base import ParseResult
from app.services.parsers.ocr import OcrEngine, _clean_ocr_text
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

# 单页文字量低于该阈值视为疑似扫描页（质量红线，§6.2）
MIN_PAGE_CHARS = 30

# OCR 输出中的 Markdown 表格块（连续的 | 开头行，至少含表头+分隔+一行数据）
_MD_TABLE_RE = re.compile(r"(?:^[ \t]*\|[^\n]*\|[ \t]*$\n?){3,}", re.MULTILINE)


class PdfParser:
    name = "pdf"

    def __init__(
        self,
        ocr_engine: OcrEngine | None = None,
        assets_dir: Path | None = None,
        ocr_enabled: bool = True,
        ocr_dpi: int = 120,
        ocr_max_pages: int = 200,
        scan_min_chars: int = MIN_PAGE_CHARS,
    ) -> None:
        self._ocr_engine = ocr_engine
        self._assets_dir = assets_dir
        self._ocr_enabled = ocr_enabled
        self._ocr_dpi = ocr_dpi
        self._ocr_max_pages = ocr_max_pages
        self._scan_min_chars = scan_min_chars

    def parse(self, path: Path, doc_id: str, tenant_id: str, policy: dict) -> ParseResult:
        extract_tables = policy.get("pdf_tables", True)
        ocr_enabled = policy.get("pdf_ocr", self._ocr_enabled) and self._ocr_engine is not None
        try:
            pdf = pymupdf.open(str(path))
        except Exception as exc:
            raise ApiError(ErrCode.DOCUMENT_PARSE_FAILED, f"PDF 打开失败（可能已加密或损坏）: {exc}") from exc
        if pdf.needs_pass:
            # 加密是**可区分**的失败（人工解密后重传即可），不该和「文件损坏」
            # 共用一个码（`A-10`：调用方要按码差异化处置）。
            raise ApiError(ErrCode.PARSE_ENCRYPTED, "PDF 已加密，无法解析")

        result = ParseResult()
        ocr_pages = 0
        try:
            for page_idx, page in enumerate(pdf, start=1):
                # sort=True：按几何位置排序，缓解双栏左右混杂（调研附录 §1.4）
                text = (page.get_text(sort=True) or "").strip()
                if len(text) >= self._scan_min_chars:
                    # ---- 文字页：文本流 + 几何表格识别 ----
                    result.segments.append((f"第{page_idx}页", text))
                    if extract_tables:
                        result.tables.extend(self._extract_tables(page, page_idx, doc_id))
                    continue

                # ---- 扫描页：整页 OCR ----
                if ocr_enabled and ocr_pages < self._ocr_max_pages:
                    ocr_pages += 1
                    self._ocr_page(pdf, page, page_idx, doc_id, tenant_id, result)
                else:
                    reason = "OCR 未启用" if not ocr_enabled else f"超出单文档 OCR 页数上限（{self._ocr_max_pages}）"
                    result.warnings.append(
                        f"第{page_idx}页文字量异常偏低（{len(text)} 字），疑似扫描页；{reason}，本页内容未入库"
                    )
            # 字号法大纲仅对文字页有意义（扫描页无 span 信息）
            result.outline = self._extract_outline(pdf)
        finally:
            pdf.close()

        if ocr_pages:
            result.warnings.append(
                f"本文档包含 {ocr_pages} 个扫描页，已自动转 OCR 识别（qwen-vl-ocr），"
                "表格以 Markdown 形式保留，原页图像可在溯源中查看"
            )
        if not result.segments and not result.tables:
            raise ApiError(
                ErrCode.DOCUMENT_PARSE_FAILED,
                "PDF 未抽取到文本（扫描件且 OCR 不可用，请检查 OCR 配置）",
            )
        # 表格正文以独立片段进入后续切块（modality 在入库时标记）
        for table in result.tables:
            result.segments.append((f"第{table.page}页·表格{table.table_index}", table.markdown))
        return result

    # ---- 扫描页 OCR ----

    def _ocr_page(
        self,
        pdf,
        page,
        page_idx: int,
        doc_id: str,
        tenant_id: str,
        result: ParseResult,
    ) -> None:
        """渲染整页 → VL OCR → 清洗 → 剥离表格块 → 段落与表格分别入结果。"""
        pix = page.get_pixmap(dpi=self._ocr_dpi)
        # mkstemp 返回的 fd 必须关闭：Windows 下句柄独占会让后续写入该文件失败
        fd, tmp_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        tmp = Path(tmp_path)
        try:
            pix.save(str(tmp))
            raw = self._ocr_engine.recognize(tmp)
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(
                ErrCode.DOCUMENT_PARSE_FAILED, f"第{page_idx}页 OCR 识别失败: {exc}"
            ) from exc
        finally:
            tmp.unlink(missing_ok=True)

        text = _clean_ocr_text(raw)
        if not text:
            result.warnings.append(f"第{page_idx}页 OCR 未识别到有效文字（可能是空白页/纯图页）")
            return

        # 页面位图入资源目录（溯源回显原页的依据）
        asset = self._save_page_asset(pdf, page_idx, doc_id, tenant_id)
        if asset:
            result.assets.append(asset)
            # M4：页图绑定到该页的文本块（提示与 segments 的 source 同构）
            result.asset_bindings.append((asset.asset_id, f"第{page_idx}页"))

        # 剥离 Markdown 表格块 → 独立 TableRecord（结构保护，不被切块器切断）
        body, tables = self._split_ocr_tables(text, page_idx, doc_id)
        result.tables.extend(tables)
        if body.strip():
            result.segments.append((f"第{page_idx}页·OCR", body.strip()))

    def _save_page_asset(
        self, pdf, page_idx: int, doc_id: str, tenant_id: str
    ) -> MediaAsset | None:
        if self._assets_dir is None:
            return None
        pix = pdf[page_idx - 1].get_pixmap(dpi=self._ocr_dpi)
        content = pix.tobytes("png")
        self._assets_dir.mkdir(parents=True, exist_ok=True)
        asset_path = self._assets_dir / f"{doc_id}#page-{page_idx}.png"
        asset_path.write_bytes(content)
        return MediaAsset(
            asset_id=f"{doc_id}#page-{page_idx}",
            doc_id=doc_id,
            uri=str(asset_path),
            page=page_idx,
            caption=f"第{page_idx}页扫描图像",
            hash=hashlib.sha256(content).hexdigest(),
            tenant_id=tenant_id,
            kind="page_image",
        )

    @staticmethod
    def _split_ocr_tables(
        text: str, page_idx: int, doc_id: str
    ) -> tuple[str, list[TableRecord]]:
        """从 OCR 文本中剥离 Markdown 表格块：表格独立成块，正文保留其余部分。"""
        tables: list[TableRecord] = []
        parts: list[str] = []
        last = 0
        for i, match in enumerate(_MD_TABLE_RE.finditer(text), start=1):
            parts.append(text[last : match.start()])
            markdown = match.group(0).strip()
            rows = markdown.count("\n") + 1 - 1  # 减分隔行
            cols = max(line.count("|") - 1 for line in markdown.splitlines() if line.strip())
            if rows >= 2 and cols >= 2:
                tables.append(
                    TableRecord(
                        table_id=f"{doc_id}#tbl-{page_idx}-ocr{i}",
                        doc_id=doc_id,
                        page=page_idx,
                        table_index=len(tables) + 1,
                        rows=rows,
                        cols=cols,
                        markdown=markdown,
                    )
                )
            else:
                parts.append(markdown)  # 不像真表格，留在正文
            last = match.end()
        parts.append(text[last:])
        return "\n\n".join(p for p in parts if p.strip()), tables

    # ---- 文字页能力（原有逻辑，未改动）----

    @staticmethod
    def _extract_outline(pdf) -> list[dict]:
        """字号法标题大纲：span 字号显著大于正文中位数 → 标题；按字号梯队分级（最多 3 级）。"""
        sizes: list[float] = []
        spans: list[tuple[float, bool, str]] = []  # (size, bold, text)
        for page in pdf:
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    line_text = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
                    if not line_text:
                        continue
                    size = max((s.get("size", 0.0) for s in line.get("spans", [])), default=0.0)
                    bold = any(bool(s.get("flags", 0) & 16) for s in line.get("spans", []))
                    if size > 0:
                        sizes.append(size)
                        spans.append((size, bold, line_text))
        if len(sizes) < 3:
            return []
        median = sorted(sizes)[len(sizes) // 2]
        threshold = median * 1.15
        candidates = [(size, text) for size, bold, text in spans if (size >= threshold or (bold and size >= median * 1.05)) and len(text) <= 60]
        if not candidates:
            return []
        # 按字号梯队映射到 1~3 级
        distinct = sorted({round(s, 1) for s, _ in candidates}, reverse=True)[:3]
        level_of = {size: i + 1 for i, size in enumerate(distinct)}
        outline: list[dict] = []
        for size, text in candidates:
            outline.append({"level": level_of[round(size, 1)], "title": text})
        return outline

    @staticmethod
    def _extract_tables(page, page_idx: int, doc_id: str) -> list[TableRecord]:
        tables: list[TableRecord] = []
        try:
            finder = page.find_tables()
        except Exception as exc:
            logger.warning("第%d页表格识别失败（跳过表格流）: %s", page_idx, exc)
            return tables
        for table_idx, table in enumerate(finder.tables, start=1):
            try:
                markdown = table.to_markdown(clean=True)
                rows = table.row_count
                cols = table.col_count
            except Exception as exc:
                logger.warning("第%d页表格%d转换失败: %s", page_idx, table_idx, exc)
                continue
            if rows < 2 or cols < 2 or not markdown.strip():
                continue
            tables.append(
                TableRecord(
                    table_id=f"{doc_id}#tbl-{page_idx}-{table_idx}",
                    doc_id=doc_id,
                    page=page_idx,
                    table_index=table_idx,
                    rows=rows,
                    cols=cols,
                    markdown=markdown,
                )
            )
        return tables
