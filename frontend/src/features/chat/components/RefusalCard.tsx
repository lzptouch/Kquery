/** 拒答卡片（阶段 5 前端规范 §2）。
 *
 * 拒答是一等公民（不是错误态）：按拒答原因展示差异化引导。
 * 安全细节：权限类拒答不暴露文档名（统一回退为"当前账号权限下未找到相关内容"）。
 */
import { useState } from "react";
import { submitRefusalFeedback } from "../../../lib/api-client";
import type { RefusalReason } from "../../../types";

interface Props {
  reason: RefusalReason;
  traceId?: string;
  originalQuestion: string;
  onReask: (question: string) => void;
}

const GUIDANCE: Record<string, { title: string; tips: string[] }> = {
  no_content: {
    title: "知识库中没有足够证据回答该问题",
    tips: ["换个表述重新提问", "上传相关文档到知识库", "联系管理员补充该主题的资料"],
  },
  insufficient_evidence: {
    title: "检索到的内容不足以支撑可靠回答",
    tips: ["补充更具体的限定条件（时间、型号、部门）", "拆成多个简单问题分别提问", "上传更相关的文档"],
  },
  permission_denied: {
    title: "当前账号权限下未找到相关内容",
    tips: ["联系管理员申请访问权限", "确认当前登录的租户是否正确"],
  },
};

const FEEDBACK_OPTIONS = [
  { value: "should_answer", label: "这个问题应该能回答" },
  { value: "missing_doc", label: "缺少相关文档" },
  { value: "wrong_reason", label: "拒答原因判断有误" },
  { value: "other", label: "其他" },
];

export function RefusalCard({ reason, traceId, originalQuestion, onReask }: Props) {
  const guidance = GUIDANCE[reason || "no_content"] ?? GUIDANCE.no_content;
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [feedbackType, setFeedbackType] = useState("should_answer");
  const [note, setNote] = useState("");
  const [ticket, setTicket] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    setSubmitting(true);
    try {
      const envelope = await submitRefusalFeedback({
        feedback_type: feedbackType,
        refusal_reason: reason || "no_content",
        user_note: note,
        trace_id: traceId ?? "unknown",
      });
      if (envelope.data) {
        setTicket(envelope.data.ticket_id);
        setFeedbackOpen(false);
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="refusal-card" role="note" aria-label="拒答">
      <div className="refusal-head">
        <span aria-hidden>🛑</span>
        <strong>{guidance.title}</strong>
      </div>
      <div className="refusal-tips">
        <div className="trace-id">您可以尝试：</div>
        <ul>
          {guidance.tips.map((tip) => (
            <li key={tip}>{tip}</li>
          ))}
        </ul>
      </div>
      <div className="refusal-actions">
        <button type="button" className="btn" onClick={() => onReask(originalQuestion)}>
          重新提问
        </button>
        <button type="button" className="btn-text" onClick={() => setFeedbackOpen(!feedbackOpen)}>
          反馈此问题
        </button>
        {traceId && <span className="trace-id mono">trace: {traceId}</span>}
      </div>

      {feedbackOpen && (
        <div className="feedback-form">
          <select value={feedbackType} onChange={(e) => setFeedbackType(e.target.value)} aria-label="反馈类型">
            {FEEDBACK_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
          <input
            type="text"
            placeholder="补充说明（可选）"
            value={note}
            maxLength={500}
            onChange={(e) => setNote(e.target.value)}
          />
          <button type="button" className="btn btn-primary" disabled={submitting} onClick={() => void submit()}>
            {submitting ? "提交中…" : "提交反馈"}
          </button>
        </div>
      )}
      {ticket && (
        <div className="pending-note" style={{ marginTop: 8 }}>
          ✓ 反馈已受理，工单号 <span className="mono">{ticket}</span>（可在右上角查询处理状态）
        </div>
      )}
    </div>
  );
}
