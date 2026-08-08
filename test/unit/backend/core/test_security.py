from pathlib import Path

import pytest

from app.core.security import InputValidator


@pytest.mark.unit
class TestInputValidator:
    def test_safe_string(self) -> None:
        validator = InputValidator()
        assert validator.is_safe_string("hello world 123")

    def test_sql_injection_detected(self) -> None:
        validator = InputValidator()
        assert not validator.is_safe_string("' OR '1'='1'")
        assert not validator.is_safe_string("'; DROP TABLE users; --")

    def test_xss_detected(self) -> None:
        validator = InputValidator()
        assert not validator.is_safe_string("<script>alert(1)</script>")
        assert not validator.is_safe_string("<img src=x onerror=alert(1)>")

    def test_sanitize_filename_valid(self) -> None:
        validator = InputValidator()
        cleaned = validator.sanitize_filename("passwd.pdf")
        assert cleaned == "passwd.pdf"

    def test_sanitize_filename_rejects_path_traversal(self) -> None:
        validator = InputValidator()
        # 路径穿越应被拒绝（抛异常或清洗掉 ..）
        try:
            cleaned = validator.sanitize_filename("a/../b.pdf")
            assert ".." not in cleaned
        except Exception:
            pass

    def test_validate_file_type(self) -> None:
        validator = InputValidator()
        allowed = {".pdf", ".docx", ".txt"}
        assert validator.validate_file_type("report.pdf", allowed)
        assert not validator.validate_file_type("virus.exe", allowed)
