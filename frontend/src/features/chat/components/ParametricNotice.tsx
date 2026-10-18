/** 通用知识兜底提示条（doc/analysis/通用知识兜底回答.md §6.10）。
 *
 * 与 RefusalCard 互补：知识库未收录时系统仍给出回答，但必须让用户
 * 明确知道这段答案不来自知识库（诚实性原则），并保留反馈入口。
 */
import { useState } from "react";
import { submitRefusalFeedback } from "../../../lib/api-client";

interface Props {
  traceId?: string;
}

export function ParametricNotice({ traceId }: Props) {
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [note, setNote] = useState("");
  const [ticket, setTicket] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    setSubmitting(true);
    try {
      // missing_doc：兜底回答意味着知识库缺口，反馈直接落入运营补知识闭环
      const envelope = await submitRefusalFeedback({
        feedback_type: "missing_doc",
        refusal_reason: "no_content",
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
    <div className="parametric-notice" role="note" aria-label="答案来源提示">
      <div className="parametric-notice-head">
        <span aria-hidden>✨</span>
        <span>
          知识库未收录相关内容，以下回答来自<strong>大模型自身知识</strong>，仅供参考。
        </span>
        <button type="button" className="btn-text" onClick={() => setFeedbackOpen(!feedbackOpen)}>
          反馈此问题
        </button>
      </div>
      {feedbackOpen && (
        <div className="feedback-form">
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
          ✓ 反馈已受理，工单号 <span className="mono">{ticket}</span>
        </div>
      )}
    </div>
  );
}
