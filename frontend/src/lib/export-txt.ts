/** 导出回答为纯文本（F10 · 对齐 doc-qa 的"导出 TXT"）。
 *
 * 纯前端实现：拼字符串 → Blob → 临时 <a download> → 立即 revoke。
 * 不走后端导出接口——一次问答的内容客户端全有，多一个接口就多一处
 * 鉴权与格式约定的维护成本。
 */
import type { ChatMessage } from "../types";

function timestamp(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(
    d.getMinutes(),
  )}`;
}

/** 把一条回答拼成纯文本（答案 + 证据清单）。 */
export function buildAnswerText(message: ChatMessage, question?: string): string {
  const lines: string[] = [];
  lines.push(`导出时间：${timestamp()}`);
  if (question) lines.push(`问题：${question}`);
  lines.push("");
  lines.push("回答：");
  lines.push(message.content || "");
  if (message.evidence.length > 0) {
    lines.push("");
    lines.push(`参考（${message.evidence.length} 条）：`);
    for (const ev of message.evidence) {
      lines.push("");
      lines.push(`[#${ev.rank}] ${ev.filename || ev.doc_id || ""} · ${ev.source || ""}`);
      lines.push(ev.content || "");
    }
  }
  if (message.traceId) {
    lines.push("");
    lines.push(`追踪号：${message.traceId}`);
  }
  return lines.join("\n");
}

/** 触发浏览器下载。返回生成的文件名（便于测试断言）。 */
export function downloadAnswerAsTxt(message: ChatMessage, question?: string): string {
  const text = buildAnswerText(message, question);
  const stamp = timestamp().replace(/[-: ]/g, "");
  const filename = `kquery-answer-${stamp}.txt`;
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // 立刻 revoke 会让部分浏览器取消下载：延后一拍
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return filename;
}
