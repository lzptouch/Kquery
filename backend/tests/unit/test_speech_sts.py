"""F2 语音 STS 端点单测。"""
import httpx
import pytest

from app.core.config import Settings
from app.core.envelope import ApiError, ErrCode


class _Resp:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or str(self._payload)

    def json(self):
        return self._payload


class TestSpeechSts:
    def test_disabled_returns_503(self, client):
        """未配置密钥 → 503 + 3803（不是 500）。"""
        resp = client.post("/speech/sts-token")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "error"
        assert body["error"]["code"] == 3803

    def test_status_probe_reports_unavailable(self, client):
        body = client.get("/speech/status").json()
        assert body["status"] == "ok"
        assert body["data"]["available"] is False

    def test_success_does_not_leak_key(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        settings = Settings(
            data_dir=tmp_path,
            model_provider="mock",
            auth_enabled=False,
            volc_appid="APP-123",
            volc_access_key="SECRET-KEY",
        )
        captured: dict = {}

        def _fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _Resp(200, {"result": {"token": "STS-TOKEN-VALUE"}})

        import app.api.speech as speech_mod  # noqa: F401 - 保证模块已加载

        # speech 模块里 httpx 是局部导入（未配置语音时不付导入成本），
        # 因此这里改的是 httpx 模块本身。
        monkeypatch.setattr(httpx, "post", _fake_post)

        from app.api.speech import sts_token

        envelope = sts_token(settings=settings, principal=_principal())
        data = envelope.data
        assert data["token"] == "STS-TOKEN-VALUE"
        assert data["appid"] == "APP-123"
        assert "SECRET-KEY" not in str(data), "响应体不得出现 access_key"
        assert "SECRET-KEY" not in str(envelope), "整个信封不得出现 access_key"
        # 密钥只出现在发往上游的请求头里
        assert captured["headers"]["X-Api-Access-Key"] == "SECRET-KEY"

    def test_upstream_error_mapped(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        settings = Settings(
            data_dir=tmp_path,
            model_provider="mock",
            auth_enabled=False,
            volc_appid="APP-123",
            volc_access_key="SECRET-KEY",
        )

        def _fake_post(url, json=None, headers=None, timeout=None):
            return _Resp(500, {}, "upstream boom")

        monkeypatch.setattr(httpx, "post", _fake_post)

        from app.api.speech import sts_token

        with pytest.raises(ApiError) as exc:
            sts_token(settings=settings, principal=_principal())
        assert exc.value.http_status == 502
        assert exc.value.code == ErrCode.DEPENDENCY_UNAVAILABLE

    def test_upstream_4xx_mapped_to_unavailable(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        settings = Settings(
            data_dir=tmp_path,
            model_provider="mock",
            auth_enabled=False,
            volc_appid="APP-123",
            volc_access_key="SECRET-KEY",
        )

        def _fake_post(url, json=None, headers=None, timeout=None):
            return _Resp(403, {}, "forbidden: account APP-123")

        monkeypatch.setattr(httpx, "post", _fake_post)

        from app.api.speech import sts_token

        with pytest.raises(ApiError) as exc:
            sts_token(settings=settings, principal=_principal())
        assert exc.value.code == ErrCode.SPEECH_UNAVAILABLE
        assert "SECRET-KEY" not in exc.value.message


def _principal():
    from app.services.security.auth import Principal

    return Principal(user_id="u1", tenant_id="default", roles=[])
