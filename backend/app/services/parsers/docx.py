"""DOCX 解析器（调研附录 §2.1 / §2.2）：python-docx 结构化提取。

- 按 **Heading 样式**（Heading 1~9 / 标题 1~9）产出大纲与**结构化分段**
  （按标题切而非固定长度切——调研实测 top-5 命中率提升 20%~30%）；
- 段落与表格按 XML 文档顺序迭代（w:p / w:tbl），不破坏叙事顺序；
- 表格渲染 Markdown 单独成块（与 PDF 表格同一契约），行列数存 TableRecord；
- 嵌入图片（word/media/）存档为 MediaAsset；
- 老式 .doc：LibreOffice headless 转 .docx 后走同一管线；
  无 LibreOffice 时诚实失败（结构全丢的"成功"比失败更有害）。

python-docx 是纯规则解析库（无权重无 GPU），符合「解析库 vs 模型能力」分界。
"""
import hashlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.core.envelope import ApiError, ErrCode
from app.domain.schemas import MediaAsset, TableRecord
from app.services.parsers.base import ParseResult
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

_HEADING_RE = re.compile(r"^(?:Heading|标题)\s*(\d+)", re.IGNORECASE)

SOFFICE_CANDIDATES = [
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/local/bin/soffice",
    "/opt/homebrew/bin/soffice",
    "soffice",
]


def find_soffice() -> str | None:
    for candidate in SOFFICE_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return shutil.which("soffice")


def convert_doc_to_docx(doc_path: Path, out_dir: Path) -> Path:
    """LibreOffice headless 把老式 .doc 转成 .docx。"""
    soffice = find_soffice()
    if soffice is None:
        raise ApiError(
            ErrCode.DOCUMENT_PARSE_FAILED,
            "老式 .doc 格式需要 LibreOffice 转换后解析。请安装 LibreOffice，或将文件另存为 .docx 后重新上传。",
        )
    proc = subprocess.run(
        [soffice, "--headless", "--convert-to", "docx", "--outdir", str(out_dir), str(doc_path)],
        capture_output=True,
        timeout=120,
    )
    converted = out_dir / (doc_path.stem + ".docx")
    if proc.returncode != 0 or not converted.exists():
        raise ApiError(
            ErrCode.DOCUMENT_PARSE_FAILED,
            f"LibreOffice 转换失败: {proc.stderr.decode(errors='ignore')[:200]}",
        )
    return converted


def _heading_level(paragraph: Paragraph) -> int | None:
    style_name = paragraph.style.name if paragraph.style is not None else ""
    m = _HEADING_RE.match(style_name.strip())
    return int(m.group(1)) if m else None


def _table_to_markdown(table: Table) -> tuple[str, int, int]:
    rows_data: list[list[str]] = []
    for row in table.rows:
        rows_data.append([cell.text.strip().replace("\n", " ") for cell in row.cells])
    if not rows_data:
        return "", 0, 0
    cols = max(len(row) for row in rows_data)
    header = rows_data[0] + [""] * (cols - len(rows_data[0]))
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * cols) + " |",
    ]
    lines.extend(
        "| " + " | ".join(row + [""] * (cols - len(row))) + " |" for row in rows_data[1:]
    )
    return "\n".join(lines), len(rows_data), cols


class DocxParser:
    name = "docx"

    def __init__(self, assets_dir: Path) -> None:
        self._assets_dir = assets_dir

    def parse(self, path: Path, doc_id: str, tenant_id: str, policy: dict) -> ParseResult:
        actual_path = path
        tmp_dir = None
        if path.suffix.lower() == ".doc":
            tmp_dir = Path(tempfile.mkdtemp(prefix="kquery-doc-"))
            actual_path = convert_doc_to_docx(path, tmp_dir)

        try:
            document = Document(str(actual_path))
        except Exception as exc:
            raise ApiError(ErrCode.DOCUMENT_PARSE_FAILED, f"DOCX 打开失败（可能损坏或加密）: {exc}") from exc

        result = ParseResult()
        # 先抽图片：正文遍历要用 rel_id → asset_id 映射来定位"这张图在第几段"（M4），
        # 顺序反了会取不到映射（属性尚未赋值）。
        result.assets, self._rel_asset_ids = self._extract_images(document, doc_id, tenant_id)
        outline: list[dict] = []
        # 结构化分段：遇标题打包前段（§3 调研共识 1）
        sections: list[tuple[str, list[str]]] = []
        current_title = "前言"
        current_lines: list[str] = []

        def flush() -> None:
            nonlocal current_lines
            text = "\n".join(current_lines).strip()
            if text:
                sections.append((current_title, [text]))
            current_lines = []

        table_seq = 0
        for child in document.element.body.iterchildren():
            if child.tag.endswith("}p"):
                paragraph = Paragraph(child, document)
                level = _heading_level(paragraph)
                if level is not None and paragraph.text.strip():
                    flush()
                    title = paragraph.text.strip()
                    outline.append({"level": min(level, 3), "title": title})
                    current_title = title
                elif paragraph.text.strip():
                    current_lines.append(paragraph.text.strip())
                # M4：段落里的内嵌图 → 绑定到**当前章节**
                # （DOCX 无页码概念，章节级是可达到的最细粒度）。
                # 不把标记塞进正文：图片标记会参与 BM25 分词，污染检索。
                for rel_id in _paragraph_image_rel_ids(paragraph):
                    asset_id = self._rel_asset_ids.get(rel_id)
                    if asset_id:
                        result.asset_bindings.append((asset_id, current_title))
            elif child.tag.endswith("}tbl"):
                table_seq += 1
                markdown, rows, cols = _table_to_markdown(Table(child, document))
                if rows >= 2 and cols >= 2 and markdown:
                    result.tables.append(
                        TableRecord(
                            table_id=f"{doc_id}#tbl-0-{table_seq}",
                            doc_id=doc_id,
                            page=0,  # docx 无页码概念，0 表示按文档序
                            table_index=table_seq,
                            rows=rows,
                            cols=cols,
                            markdown=markdown,
                        )
                    )
        flush()

        result.segments = [(f"{title}", text) for title, lines in sections for text in lines]
        result.outline = outline

        if not result.segments and not result.tables:
            raise ApiError(ErrCode.DOCUMENT_PARSE_FAILED, "DOCX 未提取到有效内容")
        # 表格正文以独立片段进入切块（source 含 "·表格" 标记，入库时按此识别 modality）
        for table in result.tables:
            result.segments.append((f"文档序·表格{table.table_index}", table.markdown))
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return result

    def _extract_images(
        self, document: Document, doc_id: str, tenant_id: str
    ) -> tuple[list[MediaAsset], dict[str, str]]:
        """word/media/ 嵌入图存档（描述文本由 qwen-vl 图像理解生成）。

        返回 ``(资产列表, rel_id → asset_id)``：第二个返回值用于
        **按文档流顺序**把图片绑定到所在章节（M4）——
        关系表的顺序不等于正文顺序，只有 rel_id 映射才能对回正文。
        """
        assets: list[MediaAsset] = []
        rel_asset_ids: dict[str, str] = {}
        self._assets_dir.mkdir(parents=True, exist_ok=True)
        idx = 0
        for rel in document.part.rels.values():
            if "image" not in rel.reltype:
                continue
            try:
                blob = rel.target_part.blob
            except Exception:  # noqa: BLE001 - DOCX 内嵌图片读取失败：跳过该图
                logger.debug("DOCX 内嵌图片读取失败：跳过该图")
                continue
            idx += 1
            content_hash = hashlib.sha256(blob).hexdigest()
            ext = Path(rel.target_part.partname).suffix or ".png"
            asset_path = self._assets_dir / f"{doc_id}-img{idx}{ext}"
            asset_path.write_bytes(blob)
            asset_id = f"{doc_id}#img-{idx}"
            assets.append(
                MediaAsset(
                    asset_id=asset_id,
                    doc_id=doc_id,
                    uri=str(asset_path),
                    caption=f"DOCX 嵌入图 {idx}（描述文本待图像理解生成）",
                    hash=content_hash,
                    tenant_id=tenant_id,
                    kind="image",
                )
            )
            rel_asset_ids[rel.rId] = asset_id
        return assets, rel_asset_ids


def _paragraph_image_rel_ids(paragraph) -> list[str]:
    """取段落里内嵌图片的 rel id 列表（M4 图文绑定）。

    DOCX 的图片是 ``<a:blip r:embed="rIdN">`` 引用 —— 靠它把正文里的
    图片位置和关系表里的资产对上。只认 ``r:embed``（内嵌），
    不认 ``r:link``（外链图片没有本地字节）。
    """
    ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
    out: list[str] = []
    try:
        for blip in paragraph._p.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}blip"):
            rel_id = blip.get(ns)
            if rel_id:
                out.append(rel_id)
    except Exception:  # noqa: BLE001 - 取不到就当没有图片，不影响解析
        return []
    return out
