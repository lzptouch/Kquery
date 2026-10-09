/** 文档解析预览（/knowledge/preview/:docId，阶段 3 前端规范 §4）。
 *
 * 三栏布局：左（大纲/表格索引/RAPTOR 树）｜中（切块列表，含切块器色板/父子双层）｜右（单块详情）。
 * 切块器色板：Recursive 灰 / ParentChild 蓝 / RAPTOR 紫 / Semantic 青。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { DataState } from "../../components/DataState";
import { WindowedList } from "../../components/WindowedList";
import { useParams } from "react-router-dom";
import { fetchChunkDetail, fetchPreview } from "../../lib/api-client";
import type { ChunkDetail, ChunkListItem, DocPreview } from "../../types";

/** 切块行高（虚拟滚动的前提：定高）。改这里要同步改 .windowed-row 的 CSS。 */
const CHUNK_ROW_HEIGHT = 96;

const CHUNKER_STYLE: Record<string, { label: string; color: string }> = {
  recursive: { label: "递归", color: "#64748b" },
  semantic: { label: "语义", color: "#0891b2" },
  parent_child: { label: "父子", color: "#3b82f6" },
  raptor: { label: "RAPTOR", color: "#8b5cf6" },
  table: { label: "表格", color: "#d97706" },
};

function ChunkerTag({ chunker }: { chunker: string }) {
  const style = CHUNKER_STYLE[chunker] ?? CHUNKER_STYLE.recursive;
  return (
    <span className="route-tag" style={{ background: `color-mix(in srgb, ${style.color} 15%, transparent)`, color: style.color }} title={`切块器：${chunker}`}>
      {style.label}
    </span>
  );
}

function ChunkRow({
  item,
  selected,
  indent,
  onClick,
}: {
  item: ChunkListItem;
  selected: boolean;
  indent?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className={`chunk-row${selected ? " selected" : ""}`}
      style={indent ? { marginLeft: 16 } : undefined}
      onClick={onClick}
      aria-current={selected}
    >
      <span className="mono trace-id">#{item.seq}</span>
      <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{item.preview}</span>
      <ChunkerTag chunker={item.chunker} />
      {item.level > 0 && <span className="route-tag route-both">L{item.level}</span>}
      {item.modality !== "text" && <span className="route-tag route-keyword">{item.modality === "table" ? "表格" : item.modality}</span>}
      {item.is_short && <span className="route-tag" style={{ background: "color-mix(in srgb, var(--warning) 15%, transparent)", color: "var(--warning)" }} title="超短块">短</span>}
    </button>
  );
}

export function PreviewPage() {
  const { docId = "" } = useParams();
  const [preview, setPreview] = useState<DocPreview | null>(null);
  const [detail, setDetail] = useState<ChunkDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [collapsedParents, setCollapsedParents] = useState<Set<string>>(new Set());

  useEffect(() => {
    void (async () => {
      setLoading(true);
      setError(null);
      try {
        const envelope = await fetchPreview(docId);
        if (envelope.data) {
          setPreview(envelope.data);
          const firstLeaf = envelope.data.chunks.find((c) => c.level === 0);
          if (firstLeaf) void selectChunk(firstLeaf.chunk_id);
        } else {
          setError(envelope.error?.message ?? "加载失败");
        }
      } catch {
        setError("网络异常");
      } finally {
        setLoading(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docId]);

  const selectChunk = useCallback(
    async (chunkId: string) => {
      const envelope = await fetchChunkDetail(docId, chunkId);
      if (envelope.data) setDetail(envelope.data);
    },
    [docId],
  );

  // 中间列：父子双层组织（父块/摘要节点 → 缩进子块）；无层级关系按原序
  const organizedChunks = useMemo(() => {
    if (!preview) return [];
    // 父块来源两处：DocStore 父块（parent_child）与向量库摘要节点（RAPTOR level>0）
    const parentItems: ChunkListItem[] = [
      ...(preview.parents ?? []),
      ...preview.chunks.filter((c) => c.level > 0),
    ];
    if (parentItems.length === 0) {
      return preview.chunks.filter((c) => c.level === 0).map((c) => ({ item: c, indent: false }));
    }
    const rows: { item: ChunkListItem; indent: boolean }[] = [];
    const childrenOf = (pid: string) => preview.chunks.filter((c) => c.parent_id === pid);
    const standalone = preview.chunks.filter((c) => c.level === 0 && !c.parent_id);
    for (const p of [...parentItems].sort((a, b) => a.level - b.level || a.seq - b.seq)) {
      rows.push({ item: p, indent: false });
      if (!collapsedParents.has(p.chunk_id)) {
        for (const child of childrenOf(p.chunk_id)) rows.push({ item: child, indent: true });
      }
    }
    for (const c of standalone) rows.push({ item: c, indent: false });
    return rows;
  }, [preview, collapsedParents]);

  if (error) {
    return (
      <div className="page">
        <div className="page-inner">
          <div className="error-inline" role="alert">⚠ {error}</div>
        </div>
      </div>
    );
  }
  // 四态：加载中不能渲染成空页面（用户会误以为"这个文档没有块"）
  if (loading || !preview) {
    return (
      <div className="page">
        <div className="page-inner">
          <DataState loading emptyText="该文档尚未解析出任何切块" />
        </div>
      </div>
    );
  }

  return (
    <div className="page" style={{ padding: 16 }}>
      <div className="preview-layout">
        {/* 左栏：文档结构 */}
        <aside className="preview-left card">
          <h4 style={{ marginTop: 0 }}>{preview.doc.filename}</h4>
          <div className="trace-id" style={{ marginBottom: 8 }}>
            {preview.chunks.length} 块 · {preview.tables.length} 表格 · {preview.assets.length} 资源
          </div>
          {preview.outline.length > 0 && (
            <>
              <div className="nav-section">章节大纲</div>
              {preview.outline.map((o, i) => (
                <div key={i} className="outline-item" style={{ paddingLeft: (o.level - 1) * 12 }}>
                  {o.title}
                </div>
              ))}
            </>
          )}
          {preview.raptor_tree.length > 0 && (
            <>
              <div className="nav-section">RAPTOR 摘要树</div>
              {preview.raptor_tree.map((node) => (
                <button key={node.chunk_id} type="button" className="outline-item outline-link" onClick={() => void selectChunk(node.chunk_id)}>
                  {"⭐".repeat(Math.min(node.level, 3))} L{node.level}（含 {node.child_count} 子节点）
                </button>
              ))}
            </>
          )}
          {preview.tables.length > 0 && (
            <>
              <div className="nav-section">表格索引</div>
              {preview.tables.map((t) => (
                <div key={t.table_id} className="outline-item" title={t.markdown}>
                  第{t.page}页·表格{t.table_index}（{t.rows}×{t.cols}）
                </div>
              ))}
            </>
          )}
          {preview.assets.length > 0 && (
            <>
              <div className="nav-section">媒体资源</div>
              {preview.assets.map((a) => (
                <div key={a.asset_id} className="outline-item" title={a.caption}>
                  {a.kind === "audio" ? "🎵" : "🖼"} {a.caption.slice(0, 30)}…
                </div>
              ))}
            </>
          )}
        </aside>

        {/* 中栏：切块列表。stage-3 §11 要求 500+ 块走虚拟滚动，
            否则一次性创建上千个 DOM 节点，页签切换直接卡住。 */}
        <section className="preview-mid card" aria-label="切块列表">
          <div className="nav-section" style={{ paddingTop: 0 }}>切块列表（{organizedChunks.length}）</div>
          <WindowedList
            items={organizedChunks}
            itemHeight={CHUNK_ROW_HEIGHT}
            height="100%"
            itemKey={(row) => row.item.chunk_id}
            renderItem={(row) => (
              <ChunkRow
                item={row.item}
                indent={row.indent}
                selected={detail?.chunk_id === row.item.chunk_id}
                onClick={() => {
                  const { item } = row;
                  if (item.level > 0 && preview.chunks.some((x) => x.parent_id === item.chunk_id)) {
                    setCollapsedParents((prev) => {
                      const next = new Set(prev);
                      if (next.has(item.chunk_id)) next.delete(item.chunk_id);
                      else next.add(item.chunk_id);
                      return next;
                    });
                  }
                  void selectChunk(item.chunk_id);
                }}
              />
            )}
          />
        </section>

        {/* 右栏：单块详情 */}
        <section className="preview-right card" aria-label="块详情">
          {detail ? (
            <>
              <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
                <span className="mono trace-id">{detail.chunk_id}</span>
                <ChunkerTag chunker={detail.chunker} />
                {detail.level > 0 && <span className="route-tag route-both">L{detail.level}</span>}
              </div>
              <div className="trace-id" style={{ marginBottom: 8 }}>原文位置：{detail.position}</div>
              <div className="chunk-detail-content">{detail.content}</div>
              {detail.parent_content && (
                <details style={{ marginTop: 12 }}>
                  <summary className="trace-id">父块内容（{detail.parent_id}）</summary>
                  <div className="chunk-detail-content" style={{ marginTop: 8 }}>{detail.parent_content}</div>
                </details>
              )}
              {detail.children.length > 0 && (
                <div style={{ marginTop: 12 }}>
                  <div className="trace-id">子块（{detail.children.length}）：</div>
                  {detail.children.map((c) => (
                    <button key={c.chunk_id} type="button" className="btn-text" onClick={() => void selectChunk(c.chunk_id)}>
                      {c.chunk_id}
                    </button>
                  ))}
                </div>
              )}
              <div style={{ marginTop: 12 }}>
                <button type="button" className="btn-text" onClick={() => void navigator.clipboard.writeText(detail.content)}>
                  复制内容
                </button>
              </div>
            </>
          ) : (
            <div className="empty-state" style={{ padding: 24 }}>点击中间列的块查看详情</div>
          )}
        </section>
      </div>
    </div>
  );
}
