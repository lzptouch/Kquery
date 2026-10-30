"""格式专项测试的共享夹具。

路由与解析器在这里**只用确定性替身**（Mock OCR / Mock 转写）：
格式层的判定口径是"产物结构对不对"，不是"识别得准不准"。
真实云端识别质量属 ⛔ 人工签字用例（矩阵 IMG-05 / AUD-05），不混进自动化。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.parsers.audio import AudioParser, MockTranscriber
from app.services.parsers.docx import DocxParser
from app.services.parsers.ocr import MockOcrEngine, OcrParser
from app.services.parsers.pdf import PdfParser
from app.services.parsers.router import ParserRouter
from app.services.parsers.spreadsheet import SpreadsheetParser


class CountingOcrEngine(MockOcrEngine):
    """记录调用次数的假 OCR —— PDF 的"逐页判定"必须能数出调用了几次。"""

    def __init__(self, text: str | None = None):
        self.calls: list[str] = []
        self._text = text

    def recognize(self, image_path: Path) -> str:
        self.calls.append(image_path.name)
        if self._text is not None:
            return self._text
        return super().recognize(image_path)


@pytest.fixture
def ocr_engine() -> CountingOcrEngine:
    return CountingOcrEngine()


@pytest.fixture
def assets_dir(tmp_path: Path) -> Path:
    d = tmp_path / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def router(ocr_engine: CountingOcrEngine, assets_dir: Path) -> ParserRouter:
    """装好全部解析器的路由（OCR/ASR 走确定性替身）。"""
    return ParserRouter(
        ocr_parser=OcrParser(ocr_engine, assets_dir),
        audio_parser=AudioParser(MockTranscriber(), assets_dir),
        docx_parser=DocxParser(assets_dir),
        pdf_parser=PdfParser(ocr_engine=ocr_engine, assets_dir=assets_dir),
        spreadsheet_parser=SpreadsheetParser(),
    )


@pytest.fixture
def write_sample(tmp_path: Path):
    """把字节写成临时文件，返回 Path。"""
    counter = {"n": 0}

    def _write(data: bytes, filename: str | None = None) -> Path:
        counter["n"] += 1
        name = filename or f"sample-{counter['n']}.bin"
        path = tmp_path / name
        path.write_bytes(data)
        return path

    return _write
