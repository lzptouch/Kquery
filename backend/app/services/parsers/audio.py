"""音频解析器（阶段 3 §3.4）：语音 → 带时间戳文本。

时间戳不是装饰而是功能：阶段 3 的核心交互是"答案跳转到音视频对应位置"，
没有 [起-止] 的分段入库等于把这个交互丢掉——所以真实路径与 Mock 同构产出分段。

平台调用走百炼异步范式：本地文件上传 → 提交异步转写任务 → 轮询任务 → 拉取结果。
不用同步接口是因为单次转写的时长远长于一次 LLM 调用，同步 HTTP 会先撞超时墙。

三类平台失败必须分开对待（语义与 llm.py 同源，混起来会让运维拿不到结论）：
- 限流 → 指数退避重试；
- 超时 → 退避重试，重试仍失败则明确报错（不返回半截答案）；
- 配额不足 → 立即失败，属运维事件，重试只会把限流窗口进一步压窄。

长音频按平台时长上限切分后并行提交，切点优先落在静音段：把一句话拦腰截断，
识别错误会同时污染相邻两片。平台时长上限是硬约束，宁可切点落在有声处也不越界。
非 WAV（mp3 等）读不到 PCM 头：整文件直传并跳过切分，而不是静默产出空结果。
"""
import hashlib
import logging
import math
import tempfile
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.core.envelope import ApiError, ErrCode
from app.domain.schemas import MediaAsset
from app.services.parsers.base import ParseResult

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "paraformer-v2"  # 文档强制的模型基线，禁止改成本地模型或其它厂商
ENERGY_WINDOW_S = 0.05  # 短时能量窗（静音检测的时间分辨率）
MAX_PARALLEL_CHUNKS = 4  # 并行度上限：再高只会加剧平台限流


def _fmt_range(begin: float, end: float) -> str:
    return f"[{max(0.0, begin):.1f}s-{max(0.0, end):.1f}s]"


class Transcriber(Protocol):
    warnings: list[str]  # 质量预检等非阻断告警（由上层收进 ParseResult.warnings）

    def transcribe(self, audio_path: Path, policy: dict | None = None) -> list[tuple[str, str]]:
        """返回 [(时间段 source, 文本)]，如 ("[0.0s-2.5s]", "你好")。"""
        ...


class TranscribeBackend(Protocol):
    """平台协议层：上传 / 提交异步任务 / 轮询取结果。

    单独抽出来的理由：平台返回体形态随模型与接口变化（转写 URL、transcripts、
    transcription 列表各不相同），把它收敛在这一层，解析器只处理统一结构，
    测试也能注入假实现而不必真的连平台。
    """

    def upload(self, path: Path) -> str: ...
    def submit(self, file_url: str, model: str) -> str: ...
    def wait(self, task_id: str, timeout_s: float) -> dict: ...


# ---- 失败分类 ----

_RATE_LIMIT_HINTS = ("throttl", "429", "rate limit", "ratelimit")
_QUOTA_HINTS = ("quota", "allocation", "insufficient", "exceed the maximum")
_TIMEOUT_HINTS = ("timeout", "timed out", "deadline")

_FAILURE_MESSAGES = {
    "rate_limited": "语音转写被平台限流，已重试 {attempts} 次仍失败，请稍后重试",
    "timeout": "语音转写超时（平台长时间未返回结果），请重试或缩短音频分片",
    "quota": "平台配额不足，无法完成语音转写，请联系管理员确认百炼调用额度",
    "other": "语音转写失败：{detail}",
}
# 配额不会自愈，重试只会浪费额度并压窄限流窗口
_NO_RETRY_KINDS = {"quota"}


def classify_platform_error(exc: BaseException) -> str:
    """把平台/网络异常归为四类失败模式：rate_limited / quota / timeout / other。

    判序固定：限流的报文里常同时出现 "RateQuota" 这类字样，先判限流，
    否则 429 会被误判成配额事件（配额不重试、限流重试，判错一次代价很大）。
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(h in text for h in _RATE_LIMIT_HINTS):
        return "rate_limited"
    if any(h in text for h in _QUOTA_HINTS):
        return "quota"
    if isinstance(exc, TimeoutError) or any(h in text for h in _TIMEOUT_HINTS):
        return "timeout"
    return "other"


# ---- WAV 读取与静音分析 ----


@dataclass(frozen=True)
class WavInfo:
    sample_rate: int
    channels: int
    frames: int
    sampwidth: int

    @property
    def duration_s(self) -> float:
        return self.frames / float(self.sample_rate or 1)


@dataclass
class AudioChunk:
    """一个待转写分片：offset_s 是它在原始音频中的起点（时间戳全局平移的依据）。"""

    index: int
    path: Path
    offset_s: float
    duration_s: float


def read_wav_info(path: Path) -> WavInfo | None:
    """读 WAV 头拿采样率/声道；非 WAV 或文件损坏返回 None（据此跳过切分与预检）。"""
    try:
        with wave.open(str(path), "rb") as wf:
            return WavInfo(
                sample_rate=wf.getframerate(),
                channels=wf.getnchannels(),
                frames=wf.getnframes(),
                sampwidth=wf.getsampwidth(),
            )
    except (wave.Error, EOFError, OSError) as exc:
        logger.info("非 WAV 或文件损坏，跳过本地切分与采样率预检: %s（%s）", path.name, exc)
        return None


def energy_profile(path: Path, info: WavInfo, window_s: float = ENERGY_WINDOW_S) -> list[float]:
    """短时 RMS 能量（归一化 0~1）：静音段能量最低，是切分点的候选位置。"""
    import numpy as np

    dtype = {1: np.uint8, 2: np.int16, 4: np.int32}.get(info.sampwidth, np.int16)
    with wave.open(str(path), "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    samples = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    if info.channels > 1:
        usable = (len(samples) // info.channels) * info.channels
        samples = samples[:usable].reshape(-1, info.channels).mean(axis=1)
    win = max(1, int(info.sample_rate * window_s))
    count = len(samples) // win
    if count == 0:
        return [0.0]
    rms = np.sqrt((samples[: count * win].reshape(count, win) ** 2).mean(axis=1))
    peak = float(rms.max()) or 1.0
    return [float(v) / peak for v in rms]


def _lowest_energy_time(energies: list[float], window_s: float, lo: float, hi: float) -> float:
    """在 [lo, hi] 秒内找能量最低窗，取窗中心作为切点；并列取更早的位置。"""
    start = max(0, int(lo / window_s))
    end = min(len(energies), max(start + 1, int(hi / window_s) + 1))
    best = min(range(start, end), key=lambda i: energies[i])
    return round((best + 0.5) * window_s, 3)


def plan_split_points(
    total_s: float,
    max_chunk_s: float,
    energies: list[float] | None = None,
    window_s: float = ENERGY_WINDOW_S,
    min_chunk_s: float | None = None,
) -> list[float]:
    """返回切分时刻（秒，升序），切点优先落在最低能量的窗口中心。

    先按总时长均分成 n 段（尾部不留超长片段），再在每个理想切点附近找静音。
    片段长度约束**优先于**静音偏好——超过 max_chunk_s 平台直接拒收，
    为了躲一个静音而整篇失败是最差的选择。
    energies 为空（非 WAV 或显式关闭）时退化为等分切点。
    """
    if max_chunk_s <= 0 or total_s <= max_chunk_s:
        return []
    min_chunk_s = min_chunk_s if min_chunk_s is not None else max(1.0, max_chunk_s * 0.5)
    n_chunks = math.ceil(total_s / max_chunk_s)
    nominal = total_s / n_chunks
    # 静音搜索半径：太小找不到静音，太大会挤出片段长度约束
    search = min(30.0, max_chunk_s * 0.3)
    cuts: list[float] = []
    prev = 0.0
    for i in range(1, n_chunks):
        ideal = i * nominal
        lo = max(ideal - search, prev + min_chunk_s, total_s - (n_chunks - i) * max_chunk_s)
        hi = min(ideal + search, prev + max_chunk_s, total_s - (n_chunks - i) * min_chunk_s)
        if hi <= lo:  # 无自由度（总时长正好整除上限）→ 落在理想切点
            point = round(min(max(ideal, lo), total_s), 3)
        elif not energies:
            point = round(min(max(ideal, lo), hi), 3)
        else:
            point = _lowest_energy_time(energies, window_s, lo, hi)
        cuts.append(point)
        prev = point
    return cuts


def materialize_chunks(src: Path, info: WavInfo, cuts: list[float], out_dir: Path) -> list[AudioChunk]:
    """按切点把 PCM 切片写成临时 WAV（边界按帧对齐，避免切出半帧噪声）。"""
    bounds = [0.0, *cuts, info.duration_s]
    chunks: list[AudioChunk] = []
    with wave.open(str(src), "rb") as reader:
        for i in range(len(bounds) - 1):
            start_s, end_s = bounds[i], bounds[i + 1]
            if end_s - start_s <= 0.05:  # 跳过碎片（切点重合时可能产生）
                continue
            reader.setpos(int(start_s * info.sample_rate))
            frames = reader.readframes(int((end_s - start_s) * info.sample_rate))
            dst = out_dir / f"chunk-{i:03d}.wav"
            with wave.open(str(dst), "wb") as writer:
                writer.setnchannels(info.channels)
                writer.setsampwidth(info.sampwidth)
                writer.setframerate(info.sample_rate)
                writer.writeframes(frames)
            samples = (len(frames) / info.sampwidth) / float(info.channels or 1)
            chunks.append(
                AudioChunk(
                    index=len(chunks), path=dst, offset_s=start_s,
                    duration_s=samples / float(info.sample_rate or 1),
                )
            )
    return chunks


# ---- 百炼平台后端 ----


def _ms_to_s(value):
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None


def _segments_from(items, scale: float) -> list[dict]:
    out: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        try:
            begin = float(item.get("begin_time", item.get("begin", 0.0))) * scale
            end = float(item.get("end_time", item.get("end", begin))) * scale
        except (TypeError, ValueError):
            continue
        out.append({"begin": round(begin, 3), "end": round(max(end, begin), 3), "text": text})
    return out


def _ensure_ok(resp, action: str) -> None:
    """状态码与消息原样带进异常文本：上层靠它区分限流/配额/超时。"""
    status = int(getattr(resp, "status_code", 0) or 0)
    if status != 200:
        raise RuntimeError(
            f"{action}失败 HTTP {status}: {getattr(resp, 'message', '')} "
            f"(code={getattr(resp, 'code', '')})"
        )


class DashScopeTranscriberBackend:
    """百炼异步文件转写的平台实现（dashscope SDK，惰性导入，风格对齐 llm.py）。"""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, http_timeout_s: float = 60.0):
        self._api_key = api_key
        self._model = model
        self._http_timeout_s = http_timeout_s

    def upload(self, path: Path) -> str:
        import dashscope

        resp = dashscope.Files.upload(
            file_path=str(path), purpose="inference", api_key=self._api_key,
        )
        _ensure_ok(resp, "文件上传")
        output = getattr(resp, "output", None)
        url = output.get("url") if isinstance(output, dict) else None
        if not url:
            raise RuntimeError(f"文件上传未返回可访问 URL: {output}")
        return str(url)

    def submit(self, file_url: str, model: str) -> str:
        from dashscope.audio.asr import Transcription

        resp = Transcription.async_call(
            model=model or self._model, file_urls=[file_url], api_key=self._api_key,
        )
        _ensure_ok(resp, "提交转写任务")
        output = getattr(resp, "output", None)
        task_id = output.get("task_id") if isinstance(output, dict) else None
        if not task_id:
            raise RuntimeError(f"提交转写任务未返回 task_id: {output}")
        return str(task_id)

    def wait(self, task_id: str, timeout_s: float) -> dict:
        from dashscope.audio.asr import Transcription

        resp = Transcription.wait(
            task=task_id, wait_timeout=int(timeout_s), api_key=self._api_key,
        )
        _ensure_ok(resp, "查询转写任务")
        output = getattr(resp, "output", None)
        output = output if isinstance(output, dict) else {}
        status = str(output.get("task_status", "")).upper()
        if status and status != "SUCCEEDED":
            raise RuntimeError(
                f"转写任务未成功（task_status={status}，code={getattr(resp, 'code', '')}）"
            )
        for item in output.get("results") or []:
            url = item.get("transcription_url") if isinstance(item, dict) else None
            if url:
                return self._normalize(self._fetch_json(url))
        return self._normalize(output)

    def _fetch_json(self, url: str) -> dict:
        import requests

        resp = requests.get(url, timeout=self._http_timeout_s)
        if resp.status_code != 200:
            raise RuntimeError(f"拉取转写结果失败 HTTP {resp.status_code}")
        return resp.json()

    @staticmethod
    def _normalize(payload: dict) -> dict:
        """归一化为 {segments:[{begin,end,text}], duration_s, text}。

        时间单位按来源区分：transcripts（文件转写）是毫秒，transcription（识别类）是秒。
        不靠"数值大于 1000 就是毫秒"去猜——猜错一次整条时间轴会差 1000 倍。
        """
        segments: list[dict] = []
        duration_s = None
        text_parts: list[str] = []
        for transcript in payload.get("transcripts") or []:
            if not isinstance(transcript, dict):
                continue
            duration_s = duration_s or _ms_to_s(transcript.get("content_duration_in_milliseconds"))
            segments.extend(_segments_from(transcript.get("sentences") or [], scale=0.001))
            text_parts.append(str(transcript.get("text") or ""))
        if not segments:
            for key in ("transcription", "results"):
                for container in payload.get(key) or []:
                    if isinstance(container, dict):
                        segments.extend(
                            _segments_from(
                                container.get("transcription") or container.get("sentences") or [],
                                scale=1.0,
                            )
                        )
        if not segments:
            segments.extend(_segments_from(payload.get("sentences") or [], scale=1.0))
        duration_s = duration_s or _ms_to_s((payload.get("properties") or {}).get("duration"))
        text = str(payload.get("text") or "").strip() or " ".join(t for t in text_parts if t).strip()
        return {"segments": segments, "duration_s": duration_s, "text": text}


# ---- 转写器 ----


class ParaformerTranscriber:
    """千问 paraformer-v2 异步文件转写（上传 → 提交任务 → 轮询 → 拉结果 → 全局时间轴）。"""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        max_retries: int = 3,
        backend=None,
        max_chunk_s: float = 600.0,
        min_sample_rate: int = 16000,
        split_on_silence: bool = True,
        max_workers: int = MAX_PARALLEL_CHUNKS,
        poll_timeout_s: float = 1800.0,
        backoff_base_s: float = 1.0,
        sleep=time.sleep,
    ):
        self._api_key = api_key
        self._model = model
        self._max_retries = max(1, max_retries)
        self._backend = backend or DashScopeTranscriberBackend(api_key=api_key)
        self._max_chunk_s = max_chunk_s
        self._min_sample_rate = min_sample_rate
        self._split_on_silence = split_on_silence
        self._max_workers = max_workers
        self._poll_timeout_s = poll_timeout_s
        self._backoff_base_s = backoff_base_s
        self._sleep = sleep  # 注入退避执行器：测试可观测退避节奏而不真的睡
        self.warnings: list[str] = []

    def transcribe(self, audio_path: Path, policy: dict | None = None) -> list[tuple[str, str]]:
        self.warnings = []
        policy = policy or {}
        model = policy.get("transcribe_model") or self._model
        max_chunk_s = float(policy.get("audio_max_chunk_s", self._max_chunk_s))
        min_rate = int(policy.get("audio_min_sample_rate", self._min_sample_rate))
        split_on_silence = bool(policy.get("audio_split_on_silence", self._split_on_silence))
        workers = max(1, min(int(policy.get("audio_max_workers", self._max_workers)), MAX_PARALLEL_CHUNKS))

        info = read_wav_info(audio_path)
        if info is None:
            # mp3 等压缩格式读不到 PCM 头：整文件直传，不做切分也不谎报采样率
            return self._transcribe_chunk(AudioChunk(0, audio_path, 0.0, 0.0), model)
        if info.sample_rate < min_rate:
            # 只标记不阻断：低采样率仍能转写，阻断会让"能搜到内容"退化成"什么都搜不到"
            self.warnings.append(f"转写质量可能不佳（采样率 {info.sample_rate} < {min_rate}）")

        cuts = plan_split_points(
            info.duration_s, max_chunk_s,
            energy_profile(audio_path, info) if split_on_silence else None,
        )
        if not cuts:
            return self._transcribe_chunk(AudioChunk(0, audio_path, 0.0, info.duration_s), model)

        with tempfile.TemporaryDirectory(prefix="kquery-audio-") as tmp:
            chunks = materialize_chunks(audio_path, info, cuts, Path(tmp))
            logger.info(
                "长音频切分为 %d 片（总时长 %.1fs，单片上限 %.1fs），并行度 %d",
                len(chunks), info.duration_s, max_chunk_s, min(workers, len(chunks)),
            )
            return self._transcribe_chunks(chunks, model, workers)

    def _transcribe_chunks(self, chunks: list, model: str, workers: int) -> list:
        results: dict = {}
        with ThreadPoolExecutor(max_workers=min(workers, len(chunks))) as pool:
            futures = {pool.submit(self._transcribe_chunk, chunk, model): chunk for chunk in chunks}
            for future, chunk in futures.items():
                try:
                    results[chunk.index] = future.result()
                except Exception as exc:  # noqa: BLE001
                    results[chunk.index] = exc
        failures = [v for v in results.values() if isinstance(v, Exception)]
        if failures and len(failures) == len(results):
            # 全部分片失败 = 没有任何可入库内容，只能明确失败
            raise failures[0]
        segments: list = []
        for chunk in sorted(chunks, key=lambda c: c.index):
            outcome = results[chunk.index]
            if isinstance(outcome, Exception):
                # 降级：保住时间轴连续性——失败片留占位段，其余片仍可入库可检索
                span = _fmt_range(chunk.offset_s, chunk.offset_s + chunk.duration_s)
                logger.warning("音频分片 %s 转写失败，降级为占位段: %s", chunk.path.name, outcome)
                self.warnings.append(f"{span} 转写失败（{outcome}）")
                segments.append((span, "（该时间段转写失败）"))
                continue
            segments.extend(outcome)
        return segments

    def _transcribe_chunk(self, chunk: AudioChunk, model: str) -> list:
        """单片完整链路，并把局部时间戳平移到全局时间轴。"""
        url = self._with_retry(lambda: self._backend.upload(chunk.path), "上传音频分片")
        task_id = self._with_retry(lambda: self._backend.submit(url, model), "提交转写任务")
        payload = self._with_retry(
            lambda: self._backend.wait(task_id, self._poll_timeout_s), "拉取转写结果"
        )
        out: list = []
        for begin, end, text in self._normalize(payload, chunk):
            g_begin = chunk.offset_s + begin
            g_end = chunk.offset_s + max(end, begin)
            if chunk.duration_s:  # 已知分片长度：不让时间戳溢出到下一片
                g_end = min(g_end, chunk.offset_s + chunk.duration_s)
            out.append((_fmt_range(g_begin, g_end), text))
        return out

    def _normalize(self, payload: dict, chunk: AudioChunk) -> list:
        items = [
            (float(s.get("begin", 0.0)), float(s.get("end", 0.0)), str(s.get("text", "")).strip())
            for s in (payload.get("segments") or [])
        ]
        items = [(b, e, t) for b, e, t in items if t]
        if items:
            return items
        # 平台没给分句时间戳：退化为整段一条，缺口写进 warning（不静默丢弃）
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ApiError(
                ErrCode.DOCUMENT_PARSE_FAILED, f"平台返回空转写结果（{chunk.path.name}）"
            )
        label = (
            _fmt_range(chunk.offset_s, chunk.offset_s + chunk.duration_s)
            if chunk.duration_s else "全片"
        )
        self.warnings.append(f"{label} 未返回分句时间戳，按整段入库")
        return [(0.0, chunk.duration_s, text)]

    def _with_retry(self, call, action: str):
        last_kind, last_exc = "other", None
        for attempt in range(self._max_retries):
            try:
                return call()
            except ApiError:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络/平台异常统一分类后处理
                last_kind, last_exc = classify_platform_error(exc), exc
                if last_kind in _NO_RETRY_KINDS or attempt == self._max_retries - 1:
                    break
                wait_s = self._backoff_base_s * (2 ** attempt)
                logger.warning(
                    "语音转写%s失败（%s），%ss 后重试（%d/%d）: %s",
                    action, last_kind, wait_s, attempt + 1, self._max_retries, exc,
                )
                self._sleep(wait_s)
        message = _FAILURE_MESSAGES[last_kind].format(attempts=self._max_retries, detail=last_exc)
        raise ApiError(ErrCode.DOCUMENT_PARSE_FAILED, f"{action}失败：{message}") from last_exc


class MockTranscriber:
    """确定性假转写：带时间戳的合成文本。"""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def transcribe(self, audio_path: Path, policy: dict | None = None) -> list:
        self.warnings = []
        return [
            ("[0.0s-3.2s]", f"大家好，这是一段来自 {audio_path.name} 的模拟语音转写。"),
            ("[3.2s-7.8s]", "本转写由 Mock 引擎生成，用于无凭据环境下的全链路测试。"),
            ("[7.8s-12.0s]", "实际使用时由千问 paraformer-v2 返回带时间戳的真实转写文本。"),
        ]


class AudioParser:
    name = "audio"

    def __init__(self, transcriber: Transcriber, assets_dir: Path):
        self._transcriber = transcriber
        self._assets_dir = assets_dir

    def parse(self, path: Path, doc_id: str, tenant_id: str, policy: dict) -> ParseResult:
        if not policy.get("transcribe_enabled", True):
            raise ApiError(ErrCode.DOCUMENT_PARSE_FAILED, "未启用语音转写策略，音频文件无法入库")
        segments = self._transcriber.transcribe(path, policy)
        if not segments:
            raise ApiError(ErrCode.DOCUMENT_PARSE_FAILED, "语音转写结果为空")

        content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        self._assets_dir.mkdir(parents=True, exist_ok=True)
        asset_path = self._assets_dir / f"{doc_id}{path.suffix.lower()}"
        if asset_path.resolve() != path.resolve():
            asset_path.write_bytes(path.read_bytes())
        asset = MediaAsset(
            asset_id=f"{doc_id}#aud-1",
            doc_id=doc_id,
            uri=str(asset_path),
            caption=segments[0][1][:100],
            hash=content_hash,
            tenant_id=tenant_id,
            kind="audio",
        )
        # 转写文本按时间戳分段（source 保留时间段，供"跳转到视频位置"交互）
        result = ParseResult(segments=[(str(src), text) for src, text in segments], assets=[asset])
        # 质量预检告警属于 ParseResult.warnings 的一部分，才可能出现在任务视图里
        result.warnings.extend(getattr(self._transcriber, "warnings", None) or [])
        return result


def build_transcriber(
    provider: str, api_key: str, model: str = DEFAULT_MODEL, max_retries: int = 3, **kwargs
) -> Transcriber:
    if provider == "dashscope":
        return ParaformerTranscriber(
            api_key=api_key, model=model, max_retries=max_retries, **kwargs
        )
    logger.info("使用 MockTranscriber（未配置千问 Key 或显式指定 mock）")
    return MockTranscriber()
