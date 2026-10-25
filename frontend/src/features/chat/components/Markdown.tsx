/** Markdown 富文本渲染（F1 · 整合 doc-qa 独有功能到 Kquery §2.1）。
 *
 * ## 三条硬约束
 *
 * 1. **保留 Kquery 语义**：引用标记是 `[#N]`（后端 `services/pipeline.py` 产出），
 *    不是 doc-qa 的 `##N$$`。角标必须仍然是可点击的证据跳转入口。
 * 2. **禁用原始 HTML**：答案经 `dangerouslySetInnerHTML` 注入 DOM，是 XSS 面。
 *    marked v15 已移除 `sanitize` 选项，必须显式把 html 渲染器置空。
 * 3. **代码块里的 `[#N]` 不是角标**：代码围栏与行内代码是独立 token，
 *    不走文本渲染器，因此天然隔离。
 *
 * ## 为什么不用 inline extension 识别角标（与计划 §2.1.4 的偏差及理由）
 *
 * 计划里的写法是注册一个 inline extension，用 `start()` 找 `[#`。但 marked 的
 * `start(src)` 收到的是**去掉首字符**的剩余串：若剩余串以反引号开头（行内代码），
 * extension 的 `start` 看不到那个反引号，会把代码内部的 `[#1]` 误判为角标。
 *
 * 改用**文本渲染器**方案：角标替换发生在 `renderer.text` 里，而行内代码
 * （`codespan`）与代码块（`code`）各有各的渲染器，根本不会走到这里。
 * 判定边界由 marked 的分词器保证，不靠我们自己扫源码——后者才是容易出错的地方。
 */
import { Marked, Renderer } from "marked";
import { useMemo, type MouseEvent } from "react";
import { citeClass } from "./CitationChip";

/** Kquery 引用标记（与后端 `build_evidence_block` / MockLLM 产出的格式一致）。 */
const CITE_RE = /\[#(\d+)\]/g;

function chipHtml(rank: number): string {
  return (
    `<button type="button" class="citation-chip ${citeClass(rank)}" data-cite-index="${rank}"` +
    ` title="查看证据 #${rank}" aria-label="引用 ${rank}，跳转到证据">[#${rank}]</button>`
  );
}

// 复用 marked 默认渲染器的转义逻辑：文本 token 必须先转义再插入 HTML，
// 否则答案里的 `<` `&` 会被当成标记。
const baseRenderer = new Renderer();

// 实例只建一次：每次渲染新建 Marked 会重复注册渲染器，热路径上（流式每帧）
// 这是纯浪费。
const markdownWithCites = new Marked({ gfm: true, breaks: false });
markdownWithCites.use({
  renderer: {
    // 原始 HTML 一律丢弃（XSS 面）
    html: () => "",
    text(token) {
      const escaped = baseRenderer.text.call(this, token);
      return escaped.replace(CITE_RE, (_match, rank: string) => chipHtml(Number(rank)));
    },
  },
});

// 不识别角标的实例（引用展示位等场景）：仍要丢弃原始 HTML。
const markdownPlain = new Marked({ gfm: true, breaks: false });
markdownPlain.use({ renderer: { html: () => "" } });

/** 把 Markdown 文本渲染为 HTML（导出以便单测直接断言）。 */
export function renderMarkdown(value: string, enableCitations = true): string {
  if (!value) return "";
  const instance = enableCitations ? markdownWithCites : markdownPlain;
  return instance.parse(value, { async: false }) as string;
}

interface Props {
  value: string;
  className?: string;
  enableCitations?: boolean;
  onCitationClick?: (rank: number) => void;
}

export function Markdown({ value, className, enableCitations = true, onCitationClick }: Props) {
  const html = useMemo(() => renderMarkdown(value ?? "", enableCitations), [value, enableCitations]);

  // 事件委托：HTML 是 dangerouslySetInnerHTML 注入的，里面没有 React 组件，
  // 只能在容器上监听冒泡，再按 data-cite-index 取回编号。
  const onClick = (event: MouseEvent<HTMLDivElement>) => {
    const el = (event.target as HTMLElement | null)?.closest?.("[data-cite-index]");
    if (!el) return;
    const rank = Number(el.getAttribute("data-cite-index"));
    if (Number.isFinite(rank)) onCitationClick?.(rank);
  };

  return (
    <div
      className={className ? `markdown-body ${className}` : "markdown-body"}
      onClick={onClick}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}

export default Markdown;
