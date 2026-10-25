/** 轻量虚拟滚动（ §四 / ）。
 *
 * 文档点名要求：分块预览 500+ 块、审计日志分页、实体列表 >200 项、长报告。
 * 这些场景下全量渲染会一次性创建上千个 DOM 节点，首屏直接卡住。
 *
 * 为什么自己写而不是引 react-window：本项目只有列表型虚拟化一种需求，
 * 为它引入一个新依赖（约 6KB gzip）不值当。这里实现的是最朴素的定高窗口
 * ——覆盖"行高基本一致"的表格/卡片列表，超高内容请分页而不是靠虚拟化兜底。
 *
 * 关键点：
 * - 只渲染可视区 ± overscan，DOM 节点数与总条数解耦；
 * - 用 ResizeObserver 跟随容器高度变化（侧边栏折叠、窗口 resize）；
 * - `scrollToIndex` 供"点击跳转到第 N 条"使用（ref 转发）；
 * - 行高未知时用固定行高，不做动态测量（动态测量反而更抖）。
 */
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
  type Ref,
  type UIEvent,
} from "react";

export interface WindowedListProps<T> {
  items: T[];
  /** 单行高度（px）。定高是本实现的前提。 */
  itemHeight: number;
  /** 可视区外额外渲染的行数，避免快速滚动时露白 */
  overscan?: number;
  /** 容器高度；传 "100%" 则跟随父级 */
  height?: number | string;
  renderItem: (item: T, index: number) => ReactNode;
  /** 每行的稳定 key（不要用 index：列表会重排） */
  itemKey: (item: T, index: number) => string;
  onRowClick?: (item: T, index: number) => void;
  className?: string;
  emptyState?: ReactNode;
  /** 是否启用虚拟化；关闭则全量渲染（数据量小的时候省事） */
  enabled?: boolean;
  /** 低于该条数不虚拟化：50 条以内全量渲染反而更简单更快 */
  threshold?: number;
}

export interface WindowedListHandle {
  /** 滚动到第 N 条（供"跳转到第 N 块"使用） */
  scrollToIndex: (index: number) => void;
}

function WindowedListInner<T>(
  {
    items,
    itemHeight,
    overscan = 4,
    height = 520,
    renderItem,
    itemKey,
    onRowClick,
    className,
    emptyState,
    enabled = true,
    threshold = 50,
  }: WindowedListProps<T>,
  ref: Ref<WindowedListHandle>,
) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [viewportHeight, setViewportHeight] = useState(
    typeof height === "number" ? height : 400,
  );
  const [scrollTop, setScrollTop] = useState(0);

  const virtualize = enabled && items.length > threshold;

  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const sync = () => setViewportHeight(el.clientHeight || viewportHeight);
    sync();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(sync);
    observer.observe(el);
    return () => observer.disconnect();
  }, [height, viewportHeight]);

  const onScroll = useCallback((e: UIEvent<HTMLDivElement>) => {
    setScrollTop(e.currentTarget.scrollTop);
  }, []);

  const scrollToIndex = useCallback(
    (index: number) => {
      const el = containerRef.current;
      if (!el) return;
      el.scrollTop = Math.max(0, index * itemHeight - itemHeight);
    },
    [itemHeight],
  );

  useImperativeHandle(ref, () => ({ scrollToIndex }), [scrollToIndex]);

  const { start, end, padTop, padBottom } = useMemo(() => {
    if (!virtualize) {
      return { start: 0, end: items.length, padTop: 0, padBottom: 0 };
    }
    const first = Math.floor(scrollTop / itemHeight) - overscan;
    const visible = Math.ceil(viewportHeight / itemHeight) + overscan * 2;
    const s = Math.max(0, first);
    const e = Math.min(items.length, s + visible);
    return {
      start: s,
      end: e,
      padTop: s * itemHeight,
      padBottom: Math.max(0, (items.length - e) * itemHeight),
    };
  }, [virtualize, items.length, scrollTop, itemHeight, overscan, viewportHeight]);

  // 列表变短时把滚动位置拉回有效范围，否则会停在空白区
  useEffect(() => {
    if (scrollTop > items.length * itemHeight) setScrollTop(0);
  }, [items.length, itemHeight, scrollTop]);

  if (items.length === 0) {
    return <>{emptyState ?? null}</>;
  }

  const slice = virtualize ? items.slice(start, end) : items;

  return (
    <div
      ref={containerRef}
      className={`windowed-list${className ? ` ${className}` : ""}`}
      style={{
        height: virtualize ? height : undefined,
        overflowY: virtualize ? "auto" : undefined,
        overflowAnchor: "none",
      }}
      onScroll={virtualize ? onScroll : undefined}
      role="region"
      aria-label={`列表，共 ${items.length} 项`}
      tabIndex={virtualize ? 0 : undefined}
    >
      {padTop > 0 && <div style={{ height: padTop }} aria-hidden />}
      {slice.map((item, i) => {
        const index = start + i;
        return (
          <div
            key={itemKey(item, index)}
            className="windowed-row"
            style={
              virtualize
                ? { height: itemHeight, overflow: "hidden" }
                : onRowClick
                  ? { cursor: "pointer" }
                  : undefined
            }
            onClick={onRowClick ? () => onRowClick(item, index) : undefined}
            data-index={index}
          >
            {renderItem(item, index)}
          </div>
        );
      })}
      {padBottom > 0 && <div style={{ height: padBottom }} aria-hidden />}
    </div>
  );
}

/**
 * 泛型组件 + ref 需要包一层断言：`forwardRef` 的类型参数不支持泛型，
 * 但运行时完全安全——渲染函数是被泛型化的，调用侧由 TS 从 props 推断 T。
 */
export const WindowedList = forwardRef(WindowedListInner) as <T>(
  props: WindowedListProps<T> & { ref?: Ref<WindowedListHandle> },
) => ReactElement | null;