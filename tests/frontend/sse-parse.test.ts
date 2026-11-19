/** SSE 分帧（`KQ-36`）。
 *
 * 这里测的是"字节怎么变成事件"，与 React 无关——所以能覆盖那些**UI 测试永远
 * 抓不到**的协议细节：多行 `data`、跨 chunk 半帧、`\r\n` 分隔、注释心跳。
 * 旧实现丢多行 `data` 时的表现是"后端发了、界面没变"，属于最难排查的一类。
 */
import { SseParser } from "../../frontend/src/lib/sse-parse";

const collect = (chunks: string[]) => {
  const parser = new SseParser();
  const frames: { event: string; data: string; id?: string }[] = [];
  let comments = 0;
  for (const chunk of chunks) {
    const out = parser.feed(chunk);
    frames.push(...out.frames);
    comments += out.comments;
  }
  const tail = parser.flush();
  frames.push(...tail.frames);
  comments += tail.comments;
  return { frames, comments };
};

describe("SseParser", () => {
  it("解析 event + data", () => {
    const { frames } = collect(['event: token\ndata: {"content":"你"}\n\n']);
    expect(frames).toEqual([{ event: "token", data: '{"content":"你"}' }]);
  });

  it("同一帧的多行 data 用换行拼接", () => {
    // 服务端或中间代理把长 JSON 折行时，旧实现只保留最后一行 → JSON.parse 失败
    const { frames } = collect(['event: done\ndata: {"a":1,\ndata: "b":2}\n\n']);
    expect(frames).toHaveLength(1);
    expect(JSON.parse(frames[0].data)).toEqual({ a: 1, b: 2 });
  });

  it("跨 chunk 的半帧留到下一次", () => {
    const { frames } = collect(['event: tok', 'en\ndata: {"content":"x"}\n\n']);
    expect(frames).toEqual([{ event: "token", data: '{"content":"x"}' }]);
  });

  it("支持 \\r\\n 分隔（反向代理可能改写换行）", () => {
    const { frames } = collect(['event: token\r\ndata: {"a":1}\r\n\r\n']);
    expect(frames).toEqual([{ event: "token", data: '{"a":1}' }]);
  });

  it("注释行只计心跳、不产生事件", () => {
    const { frames, comments } = collect([": ping\n\n", ": keep-alive\n\n"]);
    expect(frames).toHaveLength(0);
    expect(comments).toBe(2);
  });

  it("心跳与事件混在同一批里", () => {
    const { frames, comments } = collect([': ping\n\nevent: token\ndata: {"c":1}\n\n']);
    expect(comments).toBe(1);
    expect(frames).toEqual([{ event: "token", data: '{"c":1}' }]);
  });

  it("只去掉一个前导空格（其余空格属于数据）", () => {
    const { frames } = collect(["event: token\ndata:  两个空格\n\n"]);
    expect(frames[0].data).toBe(" 两个空格");
  });

  it("无 event 的帧按规范记为 message", () => {
    const { frames } = collect(["data: hi\n\n"]);
    expect(frames[0]).toEqual({ event: "message", data: "hi" });
  });

  it("解析 id 字段（断线回放用）", () => {
    const { frames } = collect(["id: 42\nevent: token\ndata: x\n\n"]);
    expect(frames[0].id).toBe("42");
  });

  it("flush 包容缺结尾空行的最后一帧", () => {
    // 正常后端每条事件都以 \n\n 结尾；宽容处理是为了避免"答案少一段"
    const { frames } = collect(['event: done\ndata: {"ok":true}']);
    expect(frames).toEqual([{ event: "done", data: '{"ok":true}' }]);
  });

  it("空 buffer 的 flush 不产生事件", () => {
    const { frames, comments } = collect([]);
    expect(frames).toHaveLength(0);
    expect(comments).toBe(0);
  });
});
