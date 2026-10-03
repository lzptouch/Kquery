/** 检索过程可视化面板（阶段 2 前端规范 §2.1）。
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

export function RetrievalTracePanel({ stats, fromCache, reranked, crag }: Props) {
  if (fromCache) {
    return (
      <div className="trace-panel">
        <span className="cache-badge" title="语义缓存：相同问题在 TTL 内直接复用响应">
          ⚡ 来自缓存（TTL 1 小时）
        </span>
        <span className="trace-id">未触发检索与生成</span>
      </div>
    );
  }
  if (!stats) return null;
  return (
    <div className="trace-panel" aria-label="检索过程">
      <div className="trace-routes">
        <RouteCard
          title="BM25 关键词"
          color="#8b5cf6"
          candidates={stats.keyword_candidates}
          ms={stats.keyword_ms}
          hint="关键词精确匹配：编号 / 型号召回靠它"
        />
        <RouteCard
          title="向量语义"
          color="#3b82f6"
          candidates={stats.vector_candidates}
          ms={stats.vector_ms}
          hint="语义相似度召回：表述不同但语义相近的内容靠它"
        />
      </div>
      <div className="trace-arrow">↓ RRF 融合（{stats.fusion_ms ?? 0}ms）</div>
      <div className="trace-fused">
        <span className="trace-bar" style={{ background: "#06b6d4" }} />
        <span className="mono">
          融合候选 {stats.fused_count ?? "-"} →
          <span style={{ color: "#f59e0b" }}> gte-rerank-v2 精排（{stats.rerank_ms ?? 0}ms） </span>→ 入上下文 {stats.into_context_count ?? "-"}
        </span>
      </div>
      {reranked === false && (
        <div className="degrade-note" role="note">
          本次未进行精排（重排服务降级），结果可能不够精确
        </div>
      )}

      {/* CRAG 分支轮次（阶段 5 §3）：触发分支整段用对应颜色高亮 */}
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
        </div>
      )}
    </div>
  );
}
