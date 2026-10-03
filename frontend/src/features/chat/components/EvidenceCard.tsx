/** 证据卡片（阶段 1 §2.3）：左侧 4px 颜色边框与 CitationChip 对位，
 * 顶部来源 + 分数，中部内容，底部"查看原文"。点击标题栏展开/折叠。
 */
import type { Evidence } from "../../../types";
import { citeClass } from "./CitationChip";

interface Props {
  evidence: Evidence;
  expanded: boolean;
  highlighted?: boolean;
  onToggle: () => void;
  onViewOriginal: (evidence: Evidence) => void;
}

export function EvidenceCard({ evidence, expanded, highlighted, onToggle, onViewOriginal }: Props) {
  return (
    <div
      className={`evidence-card ${citeClass(evidence.rank)}${highlighted ? " highlight" : ""}`}
      data-rank={evidence.rank}
    >
      <button
        type="button"
        className="evidence-head"
        onClick={onToggle}
        aria-expanded={expanded}
        aria-label={`证据 ${evidence.rank}，${expanded ? "折叠" : "展开"}`}
      >
        <span className={citeClass(evidence.rank)} style={{ color: "inherit", fontWeight: 600 }}>
          [#{evidence.rank}]
        </span>
        {evidence.used === false && (
          <span
            className="route-tag"
            style={{ background: "color-mix(in srgb, #64748b 12%, transparent)", color: "#64748b" }}
            title={evidence.rejected_reason || "未被采纳进生成上下文"}
          >
            未使用
          </span>
        )}
        <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {evidence.source}
        </span>
        {evidence.origin && (
          <span
            className={`route-tag origin-${evidence.origin.kind}`}
            title={`由「${evidence.origin.text}」带回（${evidence.origin.kind}）`}
          >
            {evidence.origin.kind === "original"
              ? "原始"
              : evidence.origin.kind === "rewrite"
                ? "改写"
                : evidence.origin.kind === "hyde"
                  ? "HyDE"
                  : evidence.origin.kind === "stepback"
                    ? "Step-Back"
                    : "子问题"}
          </span>
        )}
        {evidence.source_route && (
          <span
            className={`route-tag route-${evidence.source_route}`}
            title={
              evidence.source_route === "keyword"
                ? "由 BM25 关键词路召回"
                : evidence.source_route === "vector"
                  ? "由向量语义路召回"
                  : "两路融合命中（更可靠）"
            }
          >
            {evidence.source_route === "keyword"
              ? "关键词"
              : evidence.source_route === "vector"
                ? "语义"
                : evidence.source_route === "graph"
                  ? "图谱"
                  : evidence.source_route === "image"
                    ? "📷 图像"
                    : "融合"}
          </span>
        )}
        {evidence.score != null && <span className="mono">相关度 {evidence.score.toFixed(3)}</span>}
        <span aria-hidden>{expanded ? "▾" : "▸"}</span>
      </button>
      {expanded && (
        <>
          {/* 图像证据：缩略图 + 描述（资源缺失降级占位不整卡失败，§四 4.2） */}
          {evidence.modality === "image" && evidence.uri && (
            <div className="evidence-image">
              <img
                src={`/assets/${encodeURIComponent(evidence.uri.split("/").pop() ?? "")}`}
                alt={evidence.content.slice(0, 50)}
                loading="lazy"
                onError={(e) => {
                  (e.target as HTMLImageElement).style.display = "none";
                  (e.target as HTMLImageElement).nextElementSibling?.removeAttribute("hidden");
                }}
              />
              <div hidden className="trace-id">图片不可用（资源已删除）</div>
            </div>
          )}
          {/* 图谱证据：关系路径带方向标注 + 支撑块溯源 */}
          {evidence.modality === "graph" && (
            <div className="trace-id" style={{ marginBottom: 4 }}>
              🔗 图谱关系（方向敏感）
              {evidence.source_chunks && evidence.source_chunks.length > 0 && (
                <span> · 支撑块：{evidence.source_chunks.join(", ")}</span>
              )}
            </div>
          )}
          <div className="evidence-body">{evidence.content}</div>
          <div className="evidence-actions">
            <button type="button" className="btn-text" onClick={() => onViewOriginal(evidence)}>
              {evidence.modality === "image" ? "查看大图" : "查看原文"}
            </button>
            <span className="trace-id mono">chunk: {evidence.chunk_id}</span>
          </div>
        </>
      )}
    </div>
  );
}
