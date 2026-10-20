/** 引用编号芯片 [#N]：蓝色圆角胶囊，6 色循环，悬停显示证据预览。 */
interface Props {
  rank: number;
  preview?: string;
  onClick?: (rank: number) => void;
}

export function citeClass(rank: number): string {
  return `cite-${((rank - 1) % 6) + 1}`;
}

export function CitationChip({ rank, preview, onClick }: Props) {
  return (
    <button
      type="button"
      className={`citation-chip ${citeClass(rank)}`}
      title={preview ? `证据预览：${preview.slice(0, 50)}…` : `查看证据 #${rank}`}
      aria-label={`引用 ${rank}，跳转到证据`}
      onClick={() => onClick?.(rank)}
    >
      [#{rank}]
    </button>
  );
}
