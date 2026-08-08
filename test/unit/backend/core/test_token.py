import time
from datetime import timedelta

import pytest
from jose import jwt

from app.core.auth import create_access_token, create_refresh_token, decode_token, hash_password, verify_password
from app.core.settings import get_settings

settings = get_settings()


@pytest.mark.unit
class TestPasswordHash:
    def test_hash_and_verify(self) -> None:
        plain = "Test123456"
        hashed = hash_password(plain)
        assert hashed != plain
        assert verify_password(plain, hashed)
        assert not verify_password("wrong", hashed)


@pytest.mark.unit
class TestAccessToken:
    def test_create_and_decode(self) -> None:
        token = create_access_token("user-123")
        payload = decode_token(token)
        assert payload["sub"] == "user-123"
        assert payload["type"] == "access"

    def test_token_expired(self) -> None:
        token = create_access_token("user-123", expires_delta=timedelta(seconds=-1))
        with pytest.raises(Exception):
            decode_token(token)

    def test_refresh_token_type(self) -> None:
        token = create_refresh_token("user-123")
        payload = decode_token(token)
        assert payload["type"] == "refresh"
        assert payload["sub"] == "user-123"

    def test_invalid_token(self) -> None:
        with pytest.raises(Exception):
            decode_token("not.a.token")


@pytest.mark.unit
class TestAlgorithm:
    def test_jwt_algorithm_config(self) -> None:
        assert settings.security.jwt_algorithm in ("HS256", "HS384", "HS512", "RS256")
        # 快速验证 settings 中 secret 非空
        assert settings.security.jwt_secret_key
