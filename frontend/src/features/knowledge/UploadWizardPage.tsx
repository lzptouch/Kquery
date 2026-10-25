/** 多模态上传向导（/knowledge/upload-advanced， §2.1）。
 *
 * 四步：选择文件 → 解析策略 → 切块策略 → 确认提交。
 * 提交后创建入库任务并跳转任务进度页。
 */
import { useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ingestTask } from "../../lib/api-client";
import {
  ACCEPT_ATTRIBUTE,
  AUDIO_EXTENSIONS,
  IMAGE_EXTENSIONS,
  extOf,
  validateExtension,
} from "../../lib/file-types";
import type { ChunkPolicy } from "../../types";

const MAX_SIZE_MB = 50;
const MAX_FILES = 20;

const IMAGE_EXT = IMAGE_EXTENSIONS;
const AUDIO_EXT = AUDIO_EXTENSIONS;
const PDF_EXT = [".pdf"];

interface FileEntry {
  file: File;
  error?: string;
  warning?: string;
}

export function UploadWizardPage() {
  const navigate = useNavigate();
  const [step, setStep] = useState(1);
  const [entries, setEntries] = useState<FileEntry[]>([]);
  const [dragover, setDragover] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // 解析策略
  const [pdfTables, setPdfTables] = useState(true);
  const [ocrEnabled, setOcrEnabled] = useState(true);
  const [transcribeEnabled, setTranscribeEnabled] = useState(true);

  // 切块策略
  const [policy, setPolicy] = useState<ChunkPolicy>({ strategy: "recursive", params: {} });
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const validFiles = useMemo(() => entries.filter((e) => !e.error), [entries]);
  const typeSet = useMemo(() => {
    const exts = new Set(validFiles.map((e) => extOf(e.file.name)));
    return {
      pdf: [...exts].some((e) => PDF_EXT.includes(e)),
      image: [...exts].some((e) => IMAGE_EXT.includes(e)),
      audio: [...exts].some((e) => AUDIO_EXT.includes(e)),
    };
  }, [validFiles]);

  const addFiles = (files: FileList | File[]) => {
    const next: FileEntry[] = [];
    for (const file of Array.from(files)) {
      const extError = validateExtension(file.name);
      if (extError) {
        next.push({ file, error: extError });
        continue;
      }
      if (file.size > MAX_SIZE_MB * 1024 * 1024) {
        next.push({ file, warning: `超过 ${MAX_SIZE_MB}MB，解析可能需要数分钟` });
        continue;
      }
      next.push({ file });
    }
    setEntries((prev) => {
      const merged = [...prev, ...next];
      return merged.length > MAX_FILES
        ? [...merged.slice(0, MAX_FILES - 1), { file: next[next.length - 1].file, error: `单次最多 ${MAX_FILES} 个，已截取` }]
        : merged;
    });
  };

  const policyValid = useMemo(() => {
    const p = policy.params;
    if (policy.strategy === "semantic") {
      const t = p.threshold ?? 0.4;
      const min = p.min_sentences ?? 2;
      const max = p.max_sentences ?? 12;
      return t > 0 && t < 1 && min >= 1 && max > min;
    }
    if (policy.strategy === "parent_child") {
      return (p.child_size ?? 200) >= 50 && (p.child_size ?? 200) < (p.parent_size ?? 1200);
    }
    if (policy.strategy === "raptor") return (p.max_clusters ?? 4) >= 2;
    return true;
  }, [policy]);

  const setParam = (key: string, value: number) =>
    setPolicy((prev) => ({ ...prev, params: { ...prev.params, [key]: value } }));

  const submit = async () => {
    setSubmitting(true);
    setSubmitError(null);
    try {
      const envelope = await ingestTask(
        validFiles.map((e) => e.file),
        policy,
        { pdf_tables: pdfTables, ocr_enabled: ocrEnabled, transcribe_enabled: transcribeEnabled },
      );
      if (envelope.data) {
        navigate(`/knowledge/tasks/${envelope.data.task_id}`);
      } else {
        setSubmitError(envelope.error?.message ?? "提交失败");
      }
    } catch {
      setSubmitError("网络异常，请重试");
    } finally {
      setSubmitting(false);
    }
  };

  const estimatedSeconds = useMemo(() => {
    // 粗估：文本秒级；OCR 每图约 3s；音频约 10s
    let s = 0;
    for (const e of validFiles) {
      const ext = extOf(e.file.name);
      if (IMAGE_EXT.includes(ext)) s += ocrEnabled ? 3 : 0;
      else if (AUDIO_EXT.includes(ext)) s += transcribeEnabled ? 10 : 0;
      else s += 1;
    }
    if (policy.strategy === "raptor") s += validFiles.length * 10;
    return s;
  }, [validFiles, policy, ocrEnabled, transcribeEnabled]);

  return (
    <div className="page">
      <div className="page-inner" style={{ maxWidth: 720 }}>
        <div className="card">
          <div className="wizard-steps" aria-label="上传向导步骤">
            {["选择文件", "解析策略", "切块策略", "确认提交"].map((label, i) => (
              <span key={label} className={`wizard-step${step === i + 1 ? " active" : ""}${step > i + 1 ? " done" : ""}`}>
                {i + 1}. {label}
              </span>
            ))}
          </div>

          {step === 1 && (
            <>
              <div
                className={`uploader-zone${dragover ? " dragover" : ""}`}
                role="button"
                tabIndex={0}
                onClick={() => inputRef.current?.click()}
                onKeyDown={(e) => e.key === "Enter" && inputRef.current?.click()}
                onDragOver={(e) => { e.preventDefault(); setDragover(true); }}
                onDragLeave={() => setDragover(false)}
                onDrop={(e) => { e.preventDefault(); setDragover(false); addFiles(e.dataTransfer.files); }}
              >
                <div style={{ fontSize: 28 }} aria-hidden>⬆</div>
                <div>拖拽文件到这里，或点击选择</div>
                <div className="trace-id">支持 PDF / Markdown / Word / Excel / PPT / 纯文本 / 代码 / 图片 / 音频，单文件 ≤ {MAX_SIZE_MB}MB，最多 {MAX_FILES} 个</div>
                <input ref={inputRef} type="file" accept={ACCEPT_ATTRIBUTE} multiple hidden onChange={(e) => { if (e.target.files) addFiles(e.target.files); e.target.value = ""; }} />
              </div>
              {entries.length > 0 && (
                <div className="upload-queue">
                  {entries.map((e, i) => (
                    <div key={i} className={`upload-item${e.error ? " invalid" : ""}`}>
                      <span style={{ minWidth: 200 }}>{e.file.name}</span>
                      <span className="trace-id">{(e.file.size / 1024).toFixed(0)}KB</span>
                      {e.error && <span style={{ color: "var(--error)", flex: 1 }}>{e.error}</span>}
                      {e.warning && <span style={{ color: "var(--warning)", flex: 1 }}>{e.warning}</span>}
                      <button type="button" className="btn-text" onClick={() => setEntries((prev) => prev.filter((_, j) => j !== i))}>✕</button>
                    </div>
                  ))}
                </div>
              )}
              <div className="wizard-nav">
                <button type="button" className="btn btn-primary" disabled={validFiles.length === 0} onClick={() => setStep(2)}>
                  继续 →
                </button>
              </div>
            </>
          )}

          {step === 2 && (
            <>
              {typeSet.pdf && (
                <>
                  <h4>PDF 文件</h4>
                  <label className="config-row">
                    <input type="checkbox" checked onChange={() => {}} disabled /> 文字抽取（PyMuPDF）
                  </label>
                  <label className="config-row">
                    <input type="checkbox" checked={pdfTables} onChange={(e) => setPdfTables(e.target.checked)} /> 表格识别（页面表格转 Markdown）
                  </label>
                </>
              )}
              {typeSet.image && (
                <>
                  <h4>图片文件</h4>
                  <label className="config-row">
                    <input type="checkbox" checked={ocrEnabled} onChange={(e) => setOcrEnabled(e.target.checked)} /> OCR 识别（千问 qwen-vl-ocr，按页计费）
                  </label>
                </>
              )}
              {typeSet.audio && (
                <>
                  <h4>音频文件</h4>
                  <label className="config-row">
                    <input type="checkbox" checked={transcribeEnabled} onChange={(e) => setTranscribeEnabled(e.target.checked)} /> 语音转文字（千问 paraformer-v2，带时间戳）
                  </label>
                </>
              )}
              {!typeSet.pdf && !typeSet.image && !typeSet.audio && <p className="trace-id">本批全部为 Markdown 文档，无需额外解析配置。</p>}
              <div className="wizard-nav">
                <button type="button" className="btn" onClick={() => setStep(1)}>← 返回</button>
                <button type="button" className="btn btn-primary" onClick={() => setStep(3)}>继续 →</button>
              </div>
            </>
          )}

          {step === 3 && (
            <>
              <div className="strategy-list" role="radiogroup" aria-label="切块策略">
                {([
                  { key: "recursive", label: "Recursive（默认）", desc: "简单可预测，零额外开销" },
                  { key: "semantic", label: "Semantic", desc: "相邻句子语义距离阈值切分，主题完整" },
                  { key: "parent_child", label: "ParentChild", desc: "子块检索、父块生成，召回与上下文兼顾" },
                  { key: "raptor", label: "RAPTOR", desc: "多层级摘要树，长文档概览问题（索引构建较贵）" },
                ] as const).map((s) => (
                  <label key={s.key} className={`strategy-item${policy.strategy === s.key ? " active" : ""}`}>
                    <input
                      type="radio"
                      name="strategy"
                      checked={policy.strategy === s.key}
                      onChange={() => setPolicy({ strategy: s.key, params: {} })}
                    />
                    <span>
                      <strong>{s.label}</strong>
                      <span className="trace-id" style={{ display: "block" }}>{s.desc}</span>
                    </span>
                  </label>
                ))}
              </div>

              {policy.strategy === "semantic" && (
                <div className="config-row">
                  threshold <input type="number" step={0.05} min={0.05} max={0.95} defaultValue={0.4} onChange={(e) => setParam("threshold", Number(e.target.value))} />
                  min <input type="number" min={1} defaultValue={2} onChange={(e) => setParam("min_sentences", Number(e.target.value))} />
                  max <input type="number" min={2} defaultValue={12} onChange={(e) => setParam("max_sentences", Number(e.target.value))} />
                </div>
              )}
              {policy.strategy === "parent_child" && (
                <div className="config-row">
                  parent_size <input type="number" min={400} defaultValue={1200} onChange={(e) => setParam("parent_size", Number(e.target.value))} />
                  child_size <input type="number" min={50} defaultValue={200} onChange={(e) => setParam("child_size", Number(e.target.value))} />
                </div>
              )}
              {policy.strategy === "raptor" && (
                <div className="config-row">
                  max_clusters <input type="number" min={2} defaultValue={4} onChange={(e) => setParam("max_clusters", Number(e.target.value))} />
                  max_top_nodes <input type="number" min={1} defaultValue={3} onChange={(e) => setParam("max_top_nodes", Number(e.target.value))} />
                </div>
              )}
              {!policyValid && <div className="error-inline">参数不合法，请检查阈值与上下界关系</div>}

              <div className="wizard-nav">
                <button type="button" className="btn" onClick={() => setStep(2)}>← 返回</button>
                <button type="button" className="btn btn-primary" disabled={!policyValid} onClick={() => setStep(4)}>继续 →</button>
              </div>
            </>
          )}

          {step === 4 && (
            <>
              <h4>配置摘要</h4>
              <ul className="trace-id" style={{ lineHeight: 2 }}>
                <li>文件数：{validFiles.length} 个（{typeSet.pdf && "PDF "}{typeSet.image && "图片 "}{typeSet.audio && "音频"}）</li>
                <li>解析策略：{typeSet.pdf && `表格识别 ${pdfTables ? "开" : "关"}；`}{typeSet.image && `OCR ${ocrEnabled ? "开（qwen-vl-ocr）" : "关"}；`}{typeSet.audio && `转写 ${transcribeEnabled ? "开（paraformer-v2）" : "关"}`}{!typeSet.pdf && !typeSet.image && !typeSet.audio && "默认"}</li>
                <li>切块策略：{policy.strategy}（{JSON.stringify(policy.params)}）</li>
                <li>预估耗时：约 {estimatedSeconds} 秒（OCR / 转写为平台调用，按量计费）</li>
              </ul>
              {submitError && <div className="error-inline" role="alert"><span aria-hidden>⚠</span>{submitError}</div>}
              <div className="wizard-nav">
                <button type="button" className="btn" onClick={() => setStep(3)}>← 返回修改</button>
                <button type="button" className="btn btn-primary" disabled={submitting} onClick={() => void submit()}>
                  {submitting ? "提交中…" : "确认提交，开始处理"}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
