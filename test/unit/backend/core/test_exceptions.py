import pytest
from pydantic import ValidationError

from app.core.exceptions import ErrorCode, ValidationError as KQueryValidationError


@pytest.mark.unit
class TestErrorCode:
    def test_validation_error_codes(self) -> None:
        assert ErrorCode.VAL_REQUIRED_FIELD.value == "VAL_001"
        assert ErrorCode.VAL_INVALID_FORMAT.value == "VAL_002"
        assert ErrorCode.VAL_FILE_TOO_LARGE.value == "VAL_006"

    def test_resource_error_codes(self) -> None:
        assert ErrorCode.RES_NOT_FOUND.value == "RES_001"
        assert ErrorCode.RES_ALREADY_EXISTS.value == "RES_002"


@pytest.mark.unit
class TestKQueryValidationError:
    def test_to_dict(self) -> None:
        exc = KQueryValidationError(
            message="字段不能为空",
            field="name",
            code=ErrorCode.VAL_REQUIRED_FIELD,
        )
        data = exc.to_dict()
        assert data["error"]["code"] == "VAL_001"
        assert "name" in data["error"].get("details", {}).get("field", "")
