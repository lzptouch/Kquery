/** 四态容器（stage-0 §三：loading / empty / error / data）。
 *
 * 此前 17 个页面里 11 个缺 loading、6 个缺 error、4 个缺 empty，还有两处
 * （GraphPage、ReportsPage）把"加载中"渲染成"暂无数据"——管理员会据此以为
 * 图谱/报告真的为空，然后花十分钟排查一个根本没发生的问题。
 *
 * 这个组件把四态收敛到一处：页面只管给数据与文案，加载/失败/空三种渲染
 * 由这里统一承担，避免每个页面各写一遍、各漏一遍。
 */
import type { ReactNode } from "react";

export interface DataStateProps {
  loading: boolean;
  error?: string | null;
  /** 数据是否为空（length === 0 / null）。空态与失败态必须区分。 */
  empty?: boolean;
  emptyText?: string;
  emptyHint?: string;
  loadingText?: string;
  onRetry?: () => void;
  children?: ReactNode;
}

/** 加载占位：骨架屏而非转圈——转圈只说明"在忙"，骨架屏说明"马上会是什么"。 */
function Skeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="skeleton-group" role="status" aria-live="polite" aria-label="加载中">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skeleton-row" style={{ width: `${100 - i * 12}%` }} />
      ))}
    </div>
  );
}

export function DataState({
  loading,
  error,
  empty = false,
  emptyText = "暂无数据",
  emptyHint,
  loadingText = "加载中…",
  onRetry,
  children,
}: DataStateProps) {
  if (loading) {
    return (
      <div className="data-state">
        <Skeleton />
        <span className="sr-only">{loadingText}</span>
      </div>
    );
  }
  if (error) {
    return (
      <div className="empty-state" role="alert" style={{ padding: 24 }}>
        <div style={{ marginBottom: 8 }}>⚠ {error}</div>
        {onRetry && (
          <button type="button" className="btn" onClick={onRetry}>
            重试
          </button>
        )}
      </div>
    );
  }
  if (empty) {
    return (
      <div className="empty-state" style={{ padding: 24 }}>
        <div>{emptyText}</div>
        {emptyHint && (
          <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>{emptyHint}</div>
        )}
      </div>
    );
  }
  return <>{children}</>;
}