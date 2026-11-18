/** 渲染期错误边界（`X-22` / `KQ-37`）。
 *
 * 此前全仓**没有任何 ErrorBoundary**：任何一个页面组件在渲染期抛异常
 * （例如后端字段缺失导致的 `undefined.map`、`JSON.parse` 失败后的空对象取值），
 * React 18 会卸载整棵树 → **整站白屏**，而且用户没有任何可操作的出口，
 * 侧边栏、导航、其它页面一起消失。
 *
 * 三个设计约束：
 * 1. **兜底必须是可操作的**：重试（重新挂载子树）+ 重新加载（整页）。
 *    只显示一句"出错了"等于把白屏换成白屏。
 * 2. **重试要真的重新挂载**：`setState({error: null})` 后 React 会重渲染同一棵
 *    子树，若异常是确定性的会立刻再抛一次、看起来像"按钮没反应"。所以用
 *    `retryKey` 作为子树 key 强制重建。
 * 3. **错误信息要露出来**：渲染期异常拿不到后端 `trace_id`，`error.name +
 *    message + componentStack` 是唯一的定位线索，生产环境也要显示（它不含
 *    用户数据，只有组件栈）。
 */
import { Component, Fragment, type ErrorInfo, type ReactNode } from "react";

export interface ErrorBoundaryProps {
  children: ReactNode;
  /** 替代文案的标题（路由级可给更具体的说法）。 */
  title?: string;
}

interface ErrorBoundaryState {
  error: Error | null;
  retryKey: number;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null, retryKey: 0 };

  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 生产环境没有 DevTools，日志是唯一的事后线索；
    // 只记组件栈，不记 props（可能含用户问题原文）
    console.error("[ErrorBoundary] 渲染失败", error.name, error.message, info.componentStack);
  }

  private handleRetry = (): void => {
    this.setState((prev) => ({ error: null, retryKey: prev.retryKey + 1 }));
  };

  private handleReload = (): void => {
    window.location.reload();
  };

  render(): ReactNode {
    const { error, retryKey } = this.state;
    if (!error) {
      // key 变了就重建子树；初次渲染 key=0 不额外增加一层开销
      return <Fragment key={retryKey}>{this.props.children}</Fragment>;
    }
    const { title = "页面渲染出错" } = this.props;
    return (
      <div
        className="empty-state"
        role="alert"
        style={{ padding: 32, maxWidth: 620, margin: "48px auto" }}
      >
        <div style={{ fontSize: 16, marginBottom: 8 }}>⚠ {title}</div>
        <div className="muted" style={{ fontSize: 13, lineHeight: 1.7, marginBottom: 12 }}>
          这一处的异常不会影响其它页面。可以先点「重试」；如果反复出现，请把下面的
          信息发给管理员。
        </div>
        <pre
          style={{
            fontSize: 12,
            lineHeight: 1.5,
            padding: 12,
            marginBottom: 12,
            background: "rgba(127,127,127,0.08)",
            borderRadius: 6,
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
          }}
        >
          {error.name}: {error.message}
        </pre>
        <div style={{ display: "flex", gap: 8 }}>
          <button type="button" className="btn" onClick={this.handleRetry}>
            重试
          </button>
          <button type="button" className="btn" onClick={this.handleReload}>
            重新加载页面
          </button>
        </div>
      </div>
    );
  }
}
