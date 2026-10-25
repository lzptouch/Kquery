"""F2 语音转写端点（`POST /speech/transcribe`）端到端单测。"""
import io

import pytest

from app.core.config import Settings


class _FakeTranscriber:
    name = "fake"

    def __init__(self):
        self.calls: list[dict] = []

    def transcribe(self, audio, *, filename="audio.wav", content_type="audio/wav", sample_rate=16000):
        self.calls.append(
            {
                "audio": audio,
                "filename": filename,
                "content_type": content_type,
                "sample_rate": sample_rate,
            }
        )
        from app.services.speech.asr import ASRResult

        return ASRResult(
            text="转写出来的文字",
            language="zh",
            provider="fake",
            model="fake-model",
            duration_ms=12,
        )


@pytest.fixture()
def fake_transcriber(monkeypatch):
    tr = _FakeTranscriber()
    import app.api.speech as speech_mod

    monkeypatch.setattr(speech_mod, "build_transcriber", lambda settings: tr)
    return tr


class TestTranscribeEndpoint:
    def test_transcribes_pcm(self, client, fake_transcriber):
        """裸 PCM 上传 → 后端包 WAV 头 → 转发上游。"""
        pcm = b"\x01\x02" * 800
        resp = client.post(
            "/speech/transcribe",
            files={"file": ("audio.pcm", io.BytesIO(pcm), "audio/pcm")},
            data={"sample_rate": "16000"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["text"] == "转写出来的文字"
        assert data["language"] == "zh"

        call = fake_transcriber.calls[0]
        assert call["filename"] == "audio.wav", "裸 PCM 必须包成 WAV 再发上游"
        assert call["content_type"] == "audio/wav"
        assert call["audio"][:4] == b"RIFF"
        assert call["audio"][44:] == pcm, "包头不能动原始样本"

    def test_passthrough_container_format(self, client, fake_transcriber):
        """已经是容器格式的音频直接透传，不重复包头。"""
        wav = b"RIFF" + b"\x00" * 100
        resp = client.post(
            "/speech/transcribe",
            files={"file": ("a.wav", io.BytesIO(wav), "audio/wav")},
        )
        assert resp.status_code == 200, resp.text
        call = fake_transcriber.calls[0]
        assert call["audio"] == wav
        assert call["filename"] == "a.wav"

    def test_empty_audio_rejected(self, client, fake_transcriber):
        resp = client.post(
            "/speech/transcribe", files={"file": ("a.pcm", io.BytesIO(b""), "audio/pcm")}
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == 2001

    def test_oversize_rejected(self, client, fake_transcriber, monkeypatch):
        """超过体积上限 → 400（2004），且不调用上游。"""
        import asyncio
        import threading

        from app.api.speech import transcribe
        from app.core.envelope import ApiError
        from app.dependencies import get_app_settings  # noqa: F401 - 仅确认依赖存在
        from app.services.security.auth import Principal

        settings = Settings(
            data_dir="/tmp/kquery-test",
            model_provider="mock",
            auth_enabled=False,
            speech_max_audio_mb=1,
        )
        monkeypatch.setattr("app.api.speech.build_transcriber", lambda s: fake_transcriber)

        payload = b"\x00\x01" * (1024 * 1024)  # 2MB > 1MB 上限

        async def _call():
            from starlette.datastructures import Headers, UploadFile

            upload = UploadFile(
                filename="audio.pcm",
                file=io.BytesIO(payload),
                headers=Headers({"content-type": "audio/pcm"}),
            )
            return await transcribe(
                file=upload,
                sample_rate=16000,
                settings=settings,
                principal=Principal(user_id="u1", tenant_id="default", roles=[]),
            )

        box: dict = {}

        def _target():
            try:
                box["value"] = asyncio.run(_call())
            except BaseException as exc:  # noqa: BLE001 - 原样抛回调用线程
                box["error"] = exc

        thread = threading.Thread(target=_target)
        thread.start()
        thread.join()

        error = box.get("error")
        assert isinstance(error, ApiError)
        assert error.http_status == 400
        assert error.code == 2004
        assert fake_transcriber.calls == [], "超限音频不该发给上游"

    def test_unconfigured_returns_503(self, client, monkeypatch):
        import app.api.speech as speech_mod

        monkeypatch.setattr(speech_mod, "build_transcriber", lambda settings: None)
        resp = client.post(
            "/speech/transcribe", files={"file": ("a.pcm", io.BytesIO(b"\x00\x01"), "audio/pcm")}
        )
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == 3803


class TestSpeechStatus:
    def test_mode_transcribe_when_asr_configured(self, client, monkeypatch):
        import app.api.speech as speech_mod

        monkeypatch.setattr(speech_mod, "build_transcriber", lambda settings: _FakeTranscriber())
        data = client.get("/speech/status").json()["data"]
        assert data["available"] is True
        assert data["mode"] == "transcribe"

    def test_mode_sts_wins(self, client, monkeypatch):
        import app.api.speech as speech_mod

        monkeypatch.setattr(speech_mod, "build_transcriber", lambda settings: _FakeTranscriber())
        monkeypatch.setattr(
            speech_mod, "get_app_settings",
            lambda: Settings(
                data_dir="/tmp/kquery-test",
                model_provider="mock",
                auth_enabled=False,
                volc_appid="APP-1",
                volc_access_key="SECRET",
            ),
        )
        # 直接调函数更可靠（依赖注入在路由层）
        from app.api.speech import speech_status

        envelope = speech_status(
            settings=Settings(
                data_dir="/tmp/kquery-test",
                model_provider="mock",
                auth_enabled=False,
                volc_appid="APP-1",
                volc_access_key="SECRET",
            )
        )
        assert envelope.data["mode"] == "sts"
        assert envelope.data["available"] is True

    def test_mode_none(self, client, monkeypatch):
        import app.api.speech as speech_mod

        monkeypatch.setattr(speech_mod, "build_transcriber", lambda settings: None)
        data = client.get("/speech/status").json()["data"]
        assert data["available"] is False
        assert data["mode"] == "none"
