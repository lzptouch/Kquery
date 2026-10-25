/** 浏览器端语音输入（F2）。
 *
 * 交互：点击麦克风 → 录音 → 停止 → 转写文本**回填输入框但不自动发送**。
 *
 * 两条通路（由后端 `/speech/status` 的 `mode` 决定）：
 *
 * | mode | 做法 | 特点 |
 * |---|---|---|
 * | `transcribe` | 浏览器录 **16kHz 单声道 16bit 裸 PCM** → POST `/speech/transcribe` → 后端代持云厂商凭据转写 | 不依赖任何闭源前端 SDK；后端可换厂商；代价是"停止后才有结果" |
 * | `sts` | 后端换火山 STS token → 前端用 `byted-ailab-speech-sdk` 直连火山 WebSocket | 流式、延迟低；需要火山 AppID/AccessKey |
 *
 * 为什么默认走 `transcribe`：Chrome 的 MediaRecorder 只能产出 `webm/opus`，
 * 云端 ASR 普遍不收；而火山 STS 那条路需要 APPID + AccessKey 这类**云账号级**
 * 凭据，配置成本高。后端录音代理用 PCM 绕开了容器格式问题，也不需要闭源 SDK。
 *
 * 降级纪律（能力是可选的，任何一环不可用都不能让页面报错）：
 * 1. 浏览器不支持 `getUserMedia` → 按钮 disabled + tooltip；
 * 2. 用户拒绝麦克风权限 → 按钮 disabled + tooltip（不抛未捕获异常）；
 * 3. 后端未配置语音能力（`available=false`）→ 按钮 disabled + tooltip；
 * 4. SDK 动态导入失败（仅 `sts` 模式）→ 按钮 disabled + tooltip。
 *
 * 密钥安全：STS token / 云厂商 API Key 都由后端持有，前端永远拿不到。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { fetchSpeechStatus, fetchStsToken, transcribeAudio } from "../../../lib/api-client";

interface Props {
  /** 转写文本回调（由 ChatPage 回填输入框） */
  onTranscript: (text: string) => void;
  disabled?: boolean;
}

type State = "idle" | "recording" | "transcribing" | "unavailable";
type Mode = "sts" | "transcribe" | "none";

/** 火山引擎语音识别 WebSocket 地址（浏览器直连，服务端不中转音频） */
const ASR_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel";
/** 录音采样率：云端 ASR 统一按 16k 单声道收 */
const SAMPLE_RATE = 16000;

interface PcmRecorder {
  stop: () => Blob;
  close: () => void;
}

/**
 * 录一段 PCM。
 *
 * 用 `AudioContext` + `ScriptProcessorNode` 而非 `MediaRecorder`：
 * MediaRecorder 的容器格式由浏览器决定（Chrome 只有 webm/opus），
 * 而我们需要一个云端都认的中间格式。ScriptProcessor 已标记废弃，
 * 但兼容性最好；升级到 AudioWorklet 只是把同样的逻辑搬个地方。
 */
async function recordPcm(): Promise<PcmRecorder> {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
    },
  });
  const Ctor =
    window.AudioContext ??
    (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Ctor) {
    stream.getTracks().forEach((t) => t.stop());
    throw new Error("当前浏览器不支持 AudioContext");
  }
  const ctx = new Ctor({ sampleRate: SAMPLE_RATE });
  await ctx.resume();
  const source = ctx.createMediaStreamSource(stream);
  const processor = ctx.createScriptProcessor(4096, 1, 1);
  const chunks: Int16Array[] = [];

  processor.onaudioprocess = (event) => {
    const input = event.inputBuffer.getChannelData(0);
    const buffer = new Int16Array(input.length);
    for (let i = 0; i < input.length; i += 1) {
      const sample = Math.max(-1, Math.min(1, input[i]));
      buffer[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
    }
    chunks.push(buffer);
  };

  // 必须连到 destination，onaudioprocess 才会被触发；
  // 用增益 0 的节点接入，避免麦克风声音回放到扬声器（啸叫）。
  const silent = ctx.createGain();
  silent.gain.value = 0;
  source.connect(processor);
  processor.connect(silent);
  silent.connect(ctx.destination);

  return {
    stop: () => {
      const total = chunks.reduce((sum, c) => sum + c.length, 0);
      const merged = new Int16Array(total);
      let offset = 0;
      for (const c of chunks) {
        merged.set(c, offset);
        offset += c.length;
      }
      return new Blob([merged.buffer], { type: "audio/pcm" });
    },
    close: () => {
      try {
        processor.onaudioprocess = null;
        source.disconnect();
        processor.disconnect();
        silent.disconnect();
        void ctx.close();
        stream.getTracks().forEach((t) => t.stop());
      } catch {
        /* 忽略：关闭路径不应抛错 */
      }
    },
  };
}

export function VoiceRecorder({ onTranscript, disabled }: Props) {
  const [state, setState] = useState<State>("idle");
  const [hint, setHint] = useState("");
  const [mode, setMode] = useState<Mode>("none");
  const asrRef = useRef<{ stopRecord: () => void } | null>(null);
  const recorderRef = useRef<PcmRecorder | null>(null);
  const finalTextRef = useRef("");

  // 组件卸载时务必停掉录音：麦克风指示灯一直亮着是用户最敏感的"出问题了"信号
  useEffect(() => {
    return () => {
      try {
        asrRef.current?.stopRecord();
      } catch {
        /* 忽略：卸载路径不应抛错 */
      }
      asrRef.current = null;
      try {
        recorderRef.current?.close();
      } catch {
        /* 忽略 */
      }
      recorderRef.current = null;
    };
  }, []);

  const disable = useCallback((reason: string) => {
    setState("unavailable");
    setHint(reason);
  }, []);

  // 挂载即探测：浏览器不支持 / 后端未配置 → 按钮直接 disabled
  // （不靠用户点一次才发现——那会先弹权限框再报错，体验很差）
  useEffect(() => {
    if (typeof navigator === "undefined" || !navigator.mediaDevices?.getUserMedia) {
      disable("当前浏览器不支持麦克风录音");
      return;
    }
    let cancelled = false;
    void fetchSpeechStatus()
      .then((envelope) => {
        if (cancelled) return;
        // 探测端点不消耗 token：只在能力不可用时才禁用
        if (!envelope.data || envelope.data.available === false) {
          disable("语音服务未配置");
          return;
        }
        setMode(envelope.data.mode ?? "transcribe");
      })
      .catch(() => {
        /* 网络异常不阻塞输入框：保持按钮可用，点击时再报错 */
      });
    return () => {
      cancelled = true;
    };
  }, [disable]);

  /** STS 通路：前端直连火山 WebSocket 流式识别 */
  const startSts = useCallback(async () => {
    let sts: { appid: string; token: string; cluster?: string; duration: number };
    try {
      const envelope = await fetchStsToken();
      if (!envelope.data) {
        disable(envelope.error?.message ?? "语音服务不可用");
        return;
      }
      sts = envelope.data;
    } catch {
      disable("语音服务不可用");
      return;
    }

    let LabASR: (params: Record<string, unknown>) => {
      connect: (opts: { url: string; config: Record<string, unknown> }) => void;
      startRecord: (options?: Record<string, unknown>) => Promise<void>;
      stopRecord: () => void;
    };
    try {
      const mod = await import("byted-ailab-speech-sdk");
      LabASR = mod.LabASR as never;
    } catch {
      disable("语音识别组件加载失败");
      return;
    }

    finalTextRef.current = "";
    try {
      const asr = LabASR({
        onMessage: (data: string) => {
          try {
            const parsed = JSON.parse(data);
            const text: string = parsed?.result?.text ?? "";
            if (text) {
              finalTextRef.current = text;
              onTranscript(text);
            }
          } catch {
            /* 单条消息解析失败不影响后续 */
          }
        },
        onError: () => disable("语音识别出错，请重试"),
        onClose: () => setState((prev) => (prev === "recording" ? "idle" : prev)),
      });
      asr.connect({
        url: ASR_URL,
        config: {
          app: { appid: sts.appid, cluster: sts.cluster ?? "volcengine_input_common", token: sts.token },
          user: { uid: "kquery-user" },
          audio: { format: "pcm", codec: "pcm", rate: 16000, bits: 16, channel: 1 },
          request: {
            reqid: String(Date.now()),
            sequence: 1,
            nbest: 1,
            workflow: "speech_recognition",
            result_type: "full",
            enable_itn: true,
            enable_punc: true,
          },
        },
      });
      await asr.startRecord();
      asrRef.current = asr;
      setState("recording");
    } catch {
      disable("无法启动语音识别");
    }
  }, [disable, onTranscript]);

  /** 转写通路：录 PCM → 上传后端 → 回填文本 */
  const startTranscribe = useCallback(async () => {
    let recorder: PcmRecorder;
    try {
      recorder = await recordPcm();
    } catch {
      disable("未获得麦克风权限，请在浏览器设置中允许");
      return;
    }
    recorderRef.current = recorder;
    setState("recording");
  }, [disable]);

  const start = useCallback(async () => {
    if (state === "recording" || state === "transcribing") return;
    setHint("");

    // ① 浏览器能力
    if (typeof navigator === "undefined" || !navigator.mediaDevices?.getUserMedia) {
      disable("当前浏览器不支持麦克风录音");
      return;
    }
    // ② 麦克风权限（先探一次：拒绝权限时立刻禁用，别等录完才发现）
    try {
      const probe = await navigator.mediaDevices.getUserMedia({ audio: true });
      probe.getTracks().forEach((t) => t.stop());
    } catch {
      disable("未获得麦克风权限，请在浏览器设置中允许");
      return;
    }
    // ③ 按后端给的 mode 选通路
    if (mode === "sts") {
      await startSts();
    } else {
      await startTranscribe();
    }
  }, [disable, mode, startSts, startTranscribe, state]);

  const stop = useCallback(() => {
    const recorder = recorderRef.current;
    recorderRef.current = null;
    if (!recorder) {
      // STS 通路：没有本地录音器
      try {
        asrRef.current?.stopRecord();
      } catch {
        /* 忽略 */
      }
      asrRef.current = null;
      setState("idle");
      return;
    }

    const blob = recorder.stop();
    recorder.close();
    setState("transcribing");
    void transcribeAudio(blob, SAMPLE_RATE)
      .then((envelope) => {
        const text = envelope.data?.text?.trim();
        if (text) onTranscript(text);
        else setHint("未识别到语音内容");
      })
      .catch(() => {
        setHint("语音转写失败，请重试");
      })
      .finally(() => {
        setState("idle");
      });
  }, [onTranscript]);

  const unavailable = state === "unavailable";
  const recording = state === "recording";
  const transcribing = state === "transcribing";

  return (
    <button
      type="button"
      className={`btn voice-btn${recording ? " recording" : ""}`}
      disabled={disabled || unavailable || transcribing}
      onClick={recording ? stop : () => void start()}
      title={
        unavailable
          ? hint
          : recording
            ? "停止语音输入"
            : transcribing
              ? "转写中…"
              : "语音输入（转写结果回填输入框）"
      }
      aria-label={recording ? "停止语音输入" : "语音输入"}
      data-state={state}
    >
      {recording ? "■ 停止录音" : transcribing ? "… 转写中" : "🎤"}
    </button>
  );
}

export default VoiceRecorder;
