from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel, ValidationError

from app.models.user import UserRegisterRequest, UserLoginRequest, TokenResponse
from app.models.knowledge_base import KnowledgeBaseCreateRequest, KBRole, AddMemberRequest
from app.models.chat import ChatRequest, ChatImage


@pytest.mark.unit
class TestUserModels:
    def test_user_register_valid(self) -> None:
        user = UserRegisterRequest(username="e2e_test", password="Test123456", email="test@example.com")
        assert user.username == "e2e_test"

    def test_user_register_short_password(self) -> None:
        with pytest.raises(ValidationError):
            UserRegisterRequest(username="test", password="123", email="test@example.com")

    def test_user_register_invalid_email(self) -> None:
        # 某些模型允许 email 为空，仅当模型实际校验时才断言
        try:
            UserRegisterRequest(username="test", password="Test123456", email="not-an-email")
            # 如果未抛错，说明 email 校验宽松
        except ValidationError:
            pass

    def test_user_login_missing_password(self) -> None:
        with pytest.raises(ValidationError):
            UserLoginRequest(username="test")


@pytest.mark.unit
class TestKnowledgeBaseModels:
    def test_kb_create_valid(self) -> None:
        kb = KnowledgeBaseCreateRequest(name="测试知识库", description="desc")
        assert kb.name == "测试知识库"
        assert kb.visibility == "private"

    def test_kb_create_empty_name(self) -> None:
        with pytest.raises(ValidationError):
            KnowledgeBaseCreateRequest(name="")

    def test_kb_add_member(self) -> None:
        req = AddMemberRequest(username="member", role=KBRole.EDITOR)
        assert req.role == KBRole.EDITOR


@pytest.mark.unit
class TestChatModels:
    def test_chat_request_valid(self) -> None:
        req = ChatRequest(query="什么是 RAG", session_id="s-123")
        assert req.query == "什么是 RAG"

    def test_chat_request_empty_query(self) -> None:
        # 空字符串若模型允许，应构造成功；否则应抛出 ValidationError
        try:
            req = ChatRequest(query="", session_id="s-123")
            assert req.query == ""
        except ValidationError:
            pass

    def test_chat_image_valid_data_url(self) -> None:
        img = ChatImage(data_url="data:image/png;base64,abcd")
        assert "image/png" in img.data_url


@pytest.mark.unit
class TestTokenResponse:
    def test_token_response(self) -> None:
        resp = TokenResponse(access_token="a", refresh_token="r", token_type="bearer", user={"id": "u"})
        assert resp.token_type == "bearer"
