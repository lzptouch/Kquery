"""F3 纯文本 / HTML / 代码文件解析器单测。"""
from pathlib import Path

import pytest

from app.core.envelope import ApiError, ErrCode
from app.services.parsers.plaintext import PlainTextParser, decode_bytes, html_to_text


class TestPlainTextParser:
    def test_gbk_fallback(self, tmp_path: Path):
        path = tmp_path / "gbk.txt"
        path.write_bytes("中文内容：知识库问答".encode("gbk"))
        result = PlainTextParser().parse(path, "doc1", "t1", {})
        assert "知识库问答" in result.segments[0][1]

    def test_decode_bytes_prefers_utf8(self):
        text, _ = decode_bytes("中文".encode("utf-8"))
        assert text == "中文"

    def test_html_strips_script(self, tmp_path: Path):
        path = tmp_path / "page.html"
        path.write_text(
            "<html><head><style>body{color:red}</style></head>"
            "<body><script>var secret = 1;</script><p>可见正文</p></body></html>",
            encoding="utf-8",
        )
        result = PlainTextParser().parse(path, "doc1", "t1", {})
        body = result.segments[0][1]
        assert "可见正文" in body
        assert "secret" not in body, "script 内容不得进入索引"
        assert "color:red" not in body, "style 内容不得进入索引"

    def test_html_to_text_keeps_block_breaks(self):
        out = html_to_text("<p>一</p><p>二</p>")
        assert "一" in out and "二" in out
        assert "\n" in out

    def test_code_file_kept_verbatim(self, tmp_path: Path):
        source = "def hello():\n    return 'world'\n"
        path = tmp_path / "a.py"
        path.write_text(source, encoding="utf-8")
        result = PlainTextParser().parse(path, "doc1", "t1", {})
        assert result.segments[0][1] == source, "代码文件必须逐字保留"

    def test_json_is_pretty_printed(self, tmp_path: Path):
        path = tmp_path / "a.json"
        path.write_text('{"a":1,"b":[2,3]}', encoding="utf-8")
        result = PlainTextParser().parse(path, "doc1", "t1", {})
        assert "\n" in result.segments[0][1]

    def test_empty_file_raises(self, tmp_path: Path):
        path = tmp_path / "empty.txt"
        path.write_text("   \n  ", encoding="utf-8")
        with pytest.raises(ApiError) as exc:
            PlainTextParser().parse(path, "doc1", "t1", {})
        assert exc.value.code == ErrCode.PARSE_FAILED
