"""F2 语音转写代理（后端 ASR）单测。"""
import struct

import pytest

from app.core.config import Settings
from app.core.envelope import ApiError
from app.services.speech.asr import (
    DashScopeTranscriber,
    OpenAICompatTranscriber,
    build_transcriber,
    guess_audio_format,
    pcm_to_wav,
)


def _settings(**kwargs) -> Settings:
    base = dict(
        data_dir="/tmp/kquery-test",
        model_provider="mock",
        auth_enabled=False,
        dashscope_api_key="test-key",
    )
    base.update(kwargs)
    return Settings(_env_file=None, **base)


class TestPcmToWav:
    def test_header_is_valid_riff_wave(self):
        pcm = b"\x00\x01" * 160  # 160 个 Int16 样本
        wav = pcm_to_wav(pcm, sample_rate=16000)
        assert wav[:4] == b"RIFF"
        assert wav[8:12] == b"WAVE"
        assert wav[12:16] == b"fmt "
        assert wav[36:40] == b"data"

        riff_size, = struct.unpack("<I", wav[4:8])
        assert riff_size == 36 + len(pcm)
        assert len(wav) == 44 + len(pcm)

        audio_format, channels, rate, byte_rate, align, bits = struct.unpack(
            "<HHIIHH", wav[20:36]
        )
        assert (audio_format, channels, rate, byte_rate, align, bits) == (
            1, 1, 16000, 32000, 2, 16,
        )

    def test_payload_preserved(self):
        pcm = bytes(range(256)) * 2
        wav = pcm_to_wav(pcm, sample_rate=16000)
        assert wav[44:] == pcm

    def test_rejects_non_16bit(self):
        with pytest.raises(ValueError):
            pcm_to_wav(b"\x00" * 10, bits=8)


class TestGuessAudioFormat:
    @pytest.mark.parametrize(
        ("filename", "content_type", "expected"),
        [
            ("a.wav", "audio/wav", "wav"),
            ("a.mp3", "audio/mpeg", "mp3"),
            ("a.m4a", "audio/mp4", "m4a"),
            ("a.pcm", "audio/pcm", "pcm"),
            ("a", "audio/webm;codecs=opus", "wav"),  # 未知格式按 wav 兜底
            ("a.ogg", "", "ogg"),
        ],
    )
    def test_mapping(self, filename, content_type, expected):
        assert guess_audio_format(filename, content_type) == expected


class TestDashScopeTranscriber:
    def test_extracts_text(self, monkeypatch):
        captured = {}

        def _fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _Resp(200, {
                "output": {"choices": [{"message": {"content": [{"text": "你好世界"}]}}]},
                "request_id": "req-1",
            })

        monkeypatch.setattr("app.services.speech.asr.httpx.post", _fake_post)
        t = DashScopeTranscriber(_settings())
        result = t.transcribe(b"RIFFfake", filename="a.wav", content_type="audio/wav")
        assert result.text == "你好世界"
        assert result.provider == "dashscope"
        assert result.model == "qwen3-asr-flash"
        # 音频必须内联成 data URI：内网部署没有公网可访问 URL
        assert captured["json"]["input"]["messages"][0]["content"][0]["audio"].startswith(
            "data:audio/wav;base64,"
        )
        assert captured["headers"]["Authorization"] == "Bearer test-key"

    def test_handles_string_content(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.speech.asr.httpx.post",
            lambda *a, **k: _Resp(200, {"output": {"choices": [{"message": {"content": "纯文本"}}]}}),
        )
        t = DashScopeTranscriber(_settings())
        assert t.transcribe(b"x").text == "纯文本"

    def test_4xx_maps_to_502_speech_unavailable(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.speech.asr.httpx.post",
            lambda *a, **k: _Resp(401, {"code": "InvalidApiKey"}),
        )
        t = DashScopeTranscriber(_settings())
        with pytest.raises(ApiError) as exc:
            t.transcribe(b"x")
        assert exc.value.http_status == 502
        assert exc.value.code == 3803

    def test_5xx_maps_to_502_dependency(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.speech.asr.httpx.post", lambda *a, **k: _Resp(503, {})
        )
        t = DashScopeTranscriber(_settings())
        with pytest.raises(ApiError) as exc:
            t.transcribe(b"x")
        assert exc.value.http_status == 502
        assert exc.value.code == 5201

    def test_network_error_maps_to_502(self, monkeypatch):
        def _boom(*a, **k):
            raise ConnectionError("no route")

        monkeypatch.setattr("app.services.speech.asr.httpx.post", _boom)
        t = DashScopeTranscriber(_settings())
        with pytest.raises(ApiError) as exc:
            t.transcribe(b"x")
        assert exc.value.http_status == 502

    def test_empty_text_is_error(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.speech.asr.httpx.post",
            lambda *a, **k: _Resp(200, {"output": {"choices": [{"message": {"content": []}}]}}),
        )
        t = DashScopeTranscriber(_settings())
        with pytest.raises(ApiError):
            t.transcribe(b"x")


class TestOpenAICompatTranscriber:
    def test_extracts_text(self, monkeypatch):
        captured = {}

        def _fake_post(url, files=None, data=None, headers=None, timeout=None):
            captured["url"] = url
            captured["data"] = data
            return _Resp(200, {"text": "  转写结果  "})

        monkeypatch.setattr("app.services.speech.asr.httpx.post", _fake_post)
        t = OpenAICompatTranscriber(_settings(speech_asr_model="whisper-1"))
        result = t.transcribe(b"RIFF", filename="a.wav", content_type="audio/wav")
        assert result.text == "转写结果"
        assert captured["data"] == {"model": "whisper-1"}


class TestBuildTranscriber:
    def test_dashscope(self):
        t = build_transcriber(_settings(speech_asr_provider="dashscope"))
        assert isinstance(t, DashScopeTranscriber)

    def test_openai(self):
        t = build_transcriber(_settings(speech_asr_provider="openai"))
        assert isinstance(t, OpenAICompatTranscriber)

    def test_none_provider(self):
        assert build_transcriber(_settings(speech_asr_provider="none")) is None

    def test_missing_key_disables(self):
        t = build_transcriber(
            _settings(speech_asr_provider="dashscope", dashscope_api_key="", speech_asr_api_key="")
        )
        assert t is None

    def test_unknown_provider_disables(self):
        assert build_transcriber(_settings(speech_asr_provider="whisper.cpp")) is None


class _Resp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload
