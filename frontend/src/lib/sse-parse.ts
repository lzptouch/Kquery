/** SSE 帧解析（`KQ-36`）。按 W3C EventSource 的帧规则实现，与传输层解耦，可单测。
 *
 * ## 为什么不能"一行 `data: ` 就完事"
 *
 * 旧实现在读循环里直接 `rawEvent.split("\n")` 然后：
 *
 * ```
 * if (line.startsWith("event: ")) event = ...
 * else if (line.startsWith("data: ")) data = ...
 * ```
 *
 * 三个问题，都不会让任何现有测试变红：
 *
 * 1. **多行 `data` 被丢掉**：SSE 规范里同一帧的多条 `data:` 要用 `\n` 拼接后再解析。
 *    旧写法每行都**覆盖** `data`，只剩最后一行 → 一旦服务端（或中间代理）把长
 *    JSON 折行，前端拿到的是半截 JSON，`JSON.parse` 失败后被静默吞掉，
 *    表现是"后端发了、前端没显示"。
 * 2. **心跳注释被当成异常输入**：保活行是 `: ping`（冒号开头），规范要求忽略。
 *    忽略本身没错，但**连"收到了字节"这个事实也没人用**——于是无法区分
 *    "连接还活着只是没内容"与"连接已经悄悄死了"，前端只能一直转圈。
 * 3. **`id` / `retry` 字段完全不认**：断线重连的 `Last-Event-ID` 语义无从实现。
 *
 * ## 边界
 *
 * 帧分隔符同时接受 `\n\n` 与 `\r\n\r\n`（有的反向代理会改写换行）。
 * `id` 字段会被解析出来但**当前没有消费方**：后端尚未实现事件回放，
 * 发了 `Last-Event-ID` 也没有事件可补。保留解析能力是为了接入回放时
 * 不需要再动解析器（现在乱发这个头只会造成"看起来能续传"的假象）。
 */

export interface SseFrame {
  /** 事件名；服务端未给 `event:` 时按规范为 `message` */
  event: string;
  /** 同一帧多条 `data:` 以 `\n` 拼接后的原文 */
  data: string;
  /** `id:` 字段（断线回放用；当前后端不产生） */
  id?: string;
}

export interface SseParseResult {
  frames: SseFrame[];
  /** 本批收到的注释行数（心跳 `: ping`），调用方据此判断"连接还活着" */
  comments: number;
}

/** `\n\n` 或 `\r\n\r\n` */
const FRAME_BOUNDARY = /\r?\n\r?\n/;
const LINE_BREAK = /\r?\n/;

function parseFrame(raw: string): { frame: SseFrame | null; comments: number } {
  let event = "";
  const dataLines: string[] = [];
  let id: string | undefined;
  let comments = 0;

  for (const line of raw.split(LINE_BREAK)) {
    if (line === "") continue;
    if (line.startsWith(":")) {
      comments += 1;
      continue;
    }
    const colon = line.indexOf(":");
    // 无冒号的行按规范是"字段名 + 空值"，对我们的字段集没有意义
    if (colon === -1) continue;
    const field = line.slice(0, colon);
    let value = line.slice(colon + 1);
    // 规范：只去掉**一个**前导空格，其余空格属于数据
    if (value.startsWith(" ")) value = value.slice(1);

    if (field === "event") event = value;
    else if (field === "data") dataLines.push(value);
    else if (field === "id" && !value.includes("\0")) id = value;
  }

  // 只有注释的帧（心跳）不产生事件，但要透出 comments
  if (!event && dataLines.length === 0) return { frame: null, comments };
  return {
    frame: { event: event || "message", data: dataLines.join("\n"), ...(id !== undefined ? { id } : {}) },
    comments,
  };
}

export class SseParser {
  private buffer = "";

  /** 喂入一段已解码文本，返回其中**完整**的帧。跨 chunk 的半帧留在 buffer 里。 */
  feed(chunk: string): SseParseResult {
    this.buffer += chunk;
    const frames: SseFrame[] = [];
    let comments = 0;

    for (;;) {
      const match = FRAME_BOUNDARY.exec(this.buffer);
      if (!match) break;
      const raw = this.buffer.slice(0, match.index);
      this.buffer = this.buffer.slice(match.index + match[0].length);
      const parsed = parseFrame(raw);
      comments += parsed.comments;
      if (parsed.frame) frames.push(parsed.frame);
    }
    return { frames, comments };
  }

  /** 流结束时的收尾：把残留（缺结尾空行）也解析出来。
   *
   * 规范要求丢弃不完整帧，但我们的服务端每条事件都以 `\n\n` 结尾，正常情况
   * buffer 是空的；这里**宽容解析**而不是丢弃——丢掉最后一帧的表现是
   * "答案少一段 / 没收到 done"，比多解析一次严重得多。
   */
  flush(): SseParseResult {
    if (!this.buffer.trim()) {
      this.buffer = "";
      return { frames: [], comments: 0 };
    }
    const raw = this.buffer;
    this.buffer = "";
    const parsed = parseFrame(raw);
    return { frames: parsed.frame ? [parsed.frame] : [], comments: parsed.comments };
  }
}
