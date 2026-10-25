/** 检索过程可视化面板。
 *
 * 默认折叠的"查看检索过程"链接 → 展开后展示双路候选、融合、精排与分阶段耗时。
 * 四段耗时分开展示是排查"召回慢/融合慢/重排慢"的关键（后端 §4.4）。
 * 色板：BM25 紫 / 向量 蓝 / RRF 青 / Rerank 琥珀。
 */
import type { CragInfo, RetrievalStats } from "../../../types";

interface Props {
  stats: Partial<RetrievalStats> | null;
  fromCache?: boolean;
  reranked?: boolean;
  crag?: CragInfo | null;
  /** CRAG 精炼前后查询（用于对比展示） */
  refineFrom?: string;
  refineTo?: string;
  /** 因权限被过滤的条数（只报数量） */
  aclFiltered?: number;
}

const BRANCH_STYLE: Record<string, { label: string; color: string }> = {
  correct: { label: "CORRECT（证据充分）", color: "var(--success)" },
  ambiguous: { label: "AMBIGUOUS（中间区间）", color: "var(--info)" },
  incorrect: { label: "INCORRECT（证据不足）", color: "var(--warning)" },
};

function RouteCard({
  title,
  color,
  candidates,
  ms,
  hint,
}: {
  title: string;
  color: string;
  candidates?: number;
  ms?: number;
  hint: string;
}) {
  return (
    <div className="trace-route" title={hint}>
      <span className="trace-bar" style={{ background: color }} />
      <div>
        <div className="trace-title">{title}</div>
        <div className="mono" style={{ fontSize: 13 }}>
          候选 {candidates ?? "-"} · {ms ?? 0}ms
        </div>
      </div>
    </div>
  );
}

export function RetrievalTracePanel({
  stats,
  fromCache,
  reranked,
  crag,
  refineFrom,
  refineTo,
  aclFiltered,
}: Props) {
  if (fromCache) {
    return (
      <div className="trace-panel">
        <span className="cache-badge" title="语义缓存：问题语义相似（余弦 ≥ 阈值）且 TTL 未过期时直接复用响应">
          ⚡ 来自缓存（TTL 1 小时）
        </span>
        <span className="trace-id">未触发检索与生成</span>
      </div>
    );
  }
  if (!stats) return null;

  // 嵌套口径优先：后端同时下发平铺字段（内部）与嵌套对象，
  // 缺任一侧都能渲染——这是"新旧消费者不必同步上线"的代价。
  const latency = stats.latency_ms ?? {
    keyword: stats.keyword_ms,
    vector: stats.vector_ms,
    fusion: stats.fusion_ms,
    rerank: stats.rerank_ms,
  };
  const funnel = stats.candidates ?? {
    keyword: stats.keyword_candidates,
    vector: stats.vector_candidates,
    fused: stats.fused_count,
    reranked: stats.reranked_count,
    into_context: stats.into_context_count,
  };

  return (
    <div className="trace-panel" aria-label="检索过程">
      <div className="trace-routes">
        <RouteCard
          title="BM25 关键词"
          color="#8b5cf6"
          candidates={funnel.keyword}
          ms={latency.keyword}
          hint="关键词精确匹配：编号 / 型号召回靠它"
        />
        <RouteCard
          title="向量语义"
          color="#3b82f6"
          candidates={funnel.vector}
          ms={latency.vector}
          hint="语义相似度召回：表述不同但语义相近的内容靠它"
        />
      </div>
      <div className="trace-arrow">↓ RRF 融合（{latency.fusion ?? 0}ms）</div>
      <div className="trace-fused">
        <span className="trace-bar" style={{ background: "#06b6d4" }} />
        <span className="mono">
          融合候选 {funnel.fused ?? "-"} →
          <span style={{ color: "#f59e0b" }}> gte-rerank-v2 精排（{latency.rerank ?? 0}ms） </span>→ 入上下文 {funnel.into_context ?? "-"}
        </span>
      </div>
      {/* Agent 路径额外给出的工具级统计（工具数与其中失败数） */}
      {stats.tool_calls !== undefined && stats.tool_calls > 0 && (
        <div className="trace-id">
          检索工具调用 {stats.tool_calls} 次
          {stats.tool_errors ? `（其中 ${stats.tool_errors} 次失败）` : ""}
        </div>
      )}
      {reranked === false && (
        <div className="degrade-note" role="note">
          本次未进行精排（重排服务降级），结果可能不够精确
        </div>
      )}
      {/* 只报数量，绝不展示被过滤文档的任何元信息 */}
      {(aclFiltered ?? stats.acl_filtered ?? 0) > 0 && (
        <div className="degrade-note" role="note">
          另有 {aclFiltered ?? stats.acl_filtered} 条结果因权限未展示
        </div>
      )}

      {/* CRAG 分支轮次：触发分支整段用对应颜色高亮 */}
      {crag && crag.rounds.length > 0 && (
        <div className="crag-rounds">
          {crag.rounds.map((round) => {
            const style = BRANCH_STYLE[round.branch] ?? BRANCH_STYLE.ambiguous;
            return (
              <div key={round.round} className="crag-round" style={{ borderLeftColor: style.color }}>
                <div className="mono" style={{ color: style.color, fontSize: 12 }}>
                  第 {round.round} 轮 · {style.label} · 平均分 {round.avg_score}
                </div>
                {round.refined_query && (
                  <div className="trace-id">→ 精炼查询：{round.refined_query}</div>
                )}
              </div>
            );
          })}
          {/* 精炼前后对比（ §三）：只显示 after 会让用户无法判断"改了什么" */}
          {refineFrom && refineTo && refineFrom !== refineTo && (
            <div className="crag-round" style={{ borderLeftColor: "var(--info)" }}>
              <div className="mono" style={{ color: "var(--info)", fontSize: 12 }}>
                精炼前后对比
              </div>
              <div className="trace-id">前：{refineFrom}</div>
              <div className="trace-id">后：{refineTo}</div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
