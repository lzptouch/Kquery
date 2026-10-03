/** 置信度条（阶段 5 前端规范 §1.1）。
 *
 * 判色规则（必须按区间显色，不能只显示数字）：
 * 0.85~1.0 绿（高可信）/ 0.5~0.85 青（建议核对）/ 0~0.5 橙（必须核对）。
 * score = null → "未校验"灰显（评分器/自检器降级，如实提示）。
 */
interface Props {
  score: number | null;
  unsupportedClaimsCount?: number;
  regenerated?: boolean;
}

function levelOf(score: number): { label: string; color: string; hint: string } {
  if (score >= 0.85) return { label: "高可信", color: "var(--success)", hint: "答案与证据高度一致" };
  if (score >= 0.5) return { label: "一般可信", color: "var(--info)", hint: "建议人工核对原文" };
  return { label: "低可信", color: "var(--warning)", hint: "必须人工核对原文" };
}

export function ConfidenceBar({ score, unsupportedClaimsCount = 0, regenerated }: Props) {
  if (score == null) {
    return (
      <div className="confidence-bar unvalidated" title="评分或自检服务降级，本次未做证据校验">
        <span className="trace-id">置信度：未知（本次未做证据校验）</span>
      </div>
    );
  }
  const level = levelOf(score);
  return (
    <div className="confidence-bar" title={level.hint} aria-label={`置信度 ${Math.round(score * 100)}%，${level.label}`}>
      <div className="confidence-track" role="progressbar" aria-valuenow={Math.round(score * 100)} aria-valuemax={100}>
        <div className="confidence-fill" style={{ width: `${score * 100}%`, background: level.color }} />
      </div>
      <span className="confidence-value" style={{ color: level.color }}>
        {Math.round(score * 100)}%
      </span>
      <span className="confidence-label" style={{ color: level.color }}>{level.label}</span>
      {regenerated && (
        <span className="route-tag" style={{ background: "color-mix(in srgb, var(--info) 12%, transparent)", color: "var(--info)" }} title="系统检测到无据声明并重新生成了一次">
          🔄 重生成 1 次
        </span>
      )}
      {unsupportedClaimsCount > 0 && (
        <span className="trace-id">{unsupportedClaimsCount} 条声明无据</span>
      )}
    </div>
  );
}
