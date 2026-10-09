/** 数据源标签（stage-4 §2.2 / stage-7 §4）。
 *
 * 抽成独立文件是因为它同时被 QueryAnalysisPanel（子问题的期望来源）与
 * EvidenceCard（证据的实际来源）使用——"子问题要去 sql 源"和"这条证据来自 sql 源"
 * 必须是同一套视觉语言，用户才能对上。
 */
const SOURCE_STYLE: Record<string, { label: string; color: string; icon: string }> = {
  vector: { label: "vector", color: "#3b82f6", icon: "📚" },
  sql: { label: "sql", color: "#10b981", icon: "🗄" },
  web: { label: "web", color: "#06b6d4", icon: "🌐" },
  graph: { label: "graph", color: "#ec4899", icon: "🔗" },
  image: { label: "image", color: "#0891b2", icon: "🖼" },
};

export function SourceTag({
  source,
  onClick,
}: {
  source: string;
  onClick?: (source: string) => void;
}) {
  const style = SOURCE_STYLE[source] ?? SOURCE_STYLE.vector;
  const className = `route-tag route-${SOURCE_STYLE[source] ? source : "vector"}`;
  const content = (
    <>
      <span aria-hidden>{style.icon}</span> {style.label}
    </>
  );
  if (!onClick) {
    return (
      <span className={className} aria-label={`数据源 ${style.label}`}>
        {content}
      </span>
    );
  }
  return (
    <button
      type="button"
      className={className}
      onClick={() => onClick(source)}
      aria-label={`按数据源 ${style.label} 筛选证据`}
    >
      {content}
    </button>
  );
}

export { SOURCE_STYLE };