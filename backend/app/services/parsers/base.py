"""解析器抽象与产物模型。

解析层只做"文件 → 结构化产物"，不做切块与索引。
分界约定（全系列红线）：
- 解析库（PDF 文字抽取、表格几何识别、Markdown 切分）= 进程内规则与几何算法，不涉及模型；
- 模型能力（OCR、摘要）= 平台接口调用，无本地推理。
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.domain.schemas import MediaAsset, TableRecord


@dataclass
class ParseResult:
    segments: list[tuple[str, str]] = field(default_factory=list)  # (source, text)
    tables: list[TableRecord] = field(default_factory=list)
    assets: list[MediaAsset] = field(default_factory=list)
    #: 图像与文本的绑定：(asset_id, 来源提示)。提示与 `segments` 的 source 同构
    #: （PDF 是 "第N页"，DOCX 是章节标题）→ 入库时据此把图片绑到文本块。
    asset_bindings: list[tuple[str, str]] = field(default_factory=list)
    outline: list[dict] = field(default_factory=list)  # [{level, title}] 章节大纲
    warnings: list[str] = field(default_factory=list)  # 质量红线标记（如单页文字量异常）


class Parser(Protocol):
    name: str

    def parse(self, path: Path, doc_id: str, tenant_id: str, policy: dict) -> ParseResult: ...


def extract_md_outline(text: str) -> list[dict]:
    """Markdown 章节大纲（预览页左侧结构）。"""
    outline = []
    for line in text.splitlines():
        m = re.match(r"^(#{1,3})\s+(.+)$", line.strip())
        if m:
            outline.append({"level": len(m.group(1)), "title": m.group(2).strip()})
    return outline
