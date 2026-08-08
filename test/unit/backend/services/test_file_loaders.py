from pathlib import Path

import pytest

from app.services.file_loaders import LoaderRegistry
from app.services.file_loaders.pdf_loader import PDFLoader
from app.services.file_loaders.word_loader import WordLoader
from app.services.file_loaders.markdown_loader import MarkdownLoader
from app.services.file_loaders.excel_loader import ExcelLoader


@pytest.mark.unit
class TestLoaderRegistry:
    def test_registry_has_pdf_loader(self) -> None:
        registry = LoaderRegistry()
        loader = registry.get_loader(Path("doc.pdf"))
        assert loader is not None
        assert isinstance(loader, PDFLoader)

    def test_registry_has_word_loader(self) -> None:
        registry = LoaderRegistry()
        loader = registry.get_loader(Path("doc.docx"))
        assert isinstance(loader, WordLoader)

    def test_registry_unknown_extension(self) -> None:
        from app.services.file_loaders.base import UnsupportedFileTypeError
        registry = LoaderRegistry()
        with pytest.raises(UnsupportedFileTypeError):
            registry.get_loader(Path("doc.xyz"))


@pytest.mark.unit
class TestPDFLoader:
    def test_load_nonexistent_file(self) -> None:
        loader = PDFLoader()
        with pytest.raises(FileNotFoundError):
            loader.load(Path("/tmp/not_exists.pdf"))


@pytest.mark.unit
class TestWordLoader:
    def test_load_nonexistent_file(self) -> None:
        loader = WordLoader()
        with pytest.raises(FileNotFoundError):
            loader.load(Path("/tmp/not_exists.docx"))


@pytest.mark.unit
class TestMarkdownLoader:
    def test_load_simple_markdown(self, tmp_path: Path) -> None:
        md = tmp_path / "test.md"
        md.write_text("# Title\n\ncontent line\n", encoding="utf-8")
        loader = MarkdownLoader()
        text = loader.load(md)
        assert isinstance(text, str)
        assert "Title" in text
        assert "content line" in text


@pytest.mark.unit
class TestExcelLoader:
    def test_load_nonexistent_file(self) -> None:
        loader = ExcelLoader()
        with pytest.raises(FileNotFoundError):
            loader.load(Path("/tmp/not_exists.xlsx"))
