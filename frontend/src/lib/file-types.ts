/** 支持的文件类型（F3 · 与后端 `app/services/parsers/router.py` 的 SUPPORTED_EXTENSIONS 对齐）。
 *
 * 白名单**必须与后端一致**：前端放行、后端拒绝 → 用户看到的是"前端能选、
 * 上传后报错"；前端拦掉、后端支持 → 用户永远传不上去。两份清单各写各的，
 * 迟早对不上，所以这里集中一处。
 *
 * 后端真源：`backend/app/services/parsers/router.py::SUPPORTED_EXTENSIONS`
 */

/** 文档类（可解析为文本） */
export const DOC_EXTENSIONS = [
  ".pdf", ".md", ".markdown", ".docx", ".doc", ".xlsx", ".pptx",
  ".txt", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml",
  ".html", ".htm", ".log", ".ini", ".toml",
];

/** 代码类（按纯文本入索引） */
export const CODE_EXTENSIONS = [
  ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".rb", ".php",
  ".c", ".h", ".cpp", ".hpp", ".cs", ".kt", ".swift", ".scala", ".sh", ".bash",
  ".zsh", ".sql", ".r", ".m", ".lua", ".pl", ".vue", ".svelte",
];

/** 图片类（走 OCR） */
export const IMAGE_EXTENSIONS = [".png", ".jpg", ".jpeg"];

/** 音频类（走转写） */
export const AUDIO_EXTENSIONS = [".mp3", ".wav"];

/** 全部支持的扩展名 */
export const ALLOWED_EXTENSIONS = [
  ...DOC_EXTENSIONS,
  ...CODE_EXTENSIONS,
  ...IMAGE_EXTENSIONS,
  ...AUDIO_EXTENSIONS,
];

/** HTML `accept` 属性值 */
export const ACCEPT_ATTRIBUTE = ALLOWED_EXTENSIONS.join(",");

/** 常见但**不支持**的扩展名 → 可读提示（避免用户反复试错） */
export const UNSUPPORTED_HINTS: Record<string, string> = {
  ".xls": "不支持旧版 Excel 格式（.xls），请另存为 .xlsx 后重试",
  ".ppt": "不支持旧版 PowerPoint（.ppt），请另存为 .pptx 后重试",
};

/** 取文件扩展名（小写，含点；无扩展名返回空串） */
export function extOf(name: string): string {
  const idx = name.lastIndexOf(".");
  return idx > 0 ? name.slice(idx).toLowerCase() : "";
}

/** 校验文件类型，返回可读错误（通过则返回 null） */
export function validateExtension(name: string): string | null {
  const ext = extOf(name);
  if (!ext) return "缺少文件扩展名，无法判断类型";
  if (ALLOWED_EXTENSIONS.includes(ext)) return null;
  return UNSUPPORTED_HINTS[ext] ?? `暂不支持 ${ext} 格式`;
}
