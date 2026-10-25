"""递归字符切块。

分隔符优先级：段落（空行）→ 行（换行）→ 中文句号 → 空格 → 字符兜底。
按字符而非 token 计数：中文场景偏差小，且与用户可读长度直接对应。
chunk_id = {doc_id}#{seq}（seq 从 1 起），重建索引保持不变——这是引用回溯、
缓存命中、跨版本比对的基石，绝对不用随机 UUID。
"""
from app.domain.schemas import Chunk

SEPARATORS = ["\n\n", "\n", "。", " ", ""]


def _merge_splits(splits: list[str], separator: str, chunk_size: int, overlap: int) -> list[str]:
    """把原子片段贪心合并成不超过 chunk_size 的块，相邻块保留 overlap 重叠。"""
    sep_len = len(separator)
    docs: list[str] = []
    current: list[str] = []
    total = 0
    for piece in splits:
        piece_len = len(piece)
        if total + piece_len + (sep_len if current else 0) > chunk_size:
            if current:
                doc = separator.join(current).strip()
                if doc:
                    docs.append(doc)
                # 从头部弹出，直到剩余内容长度落入 overlap 预算
                while total > overlap or (
                    total + piece_len + (sep_len if current else 0) > chunk_size and total > 0
                ):
                    total -= len(current[0]) + (sep_len if len(current) > 1 else 0)
                    current.pop(0)
        current.append(piece)
        total += piece_len + (sep_len if len(current) > 1 else 0)
    doc = separator.join(current).strip()
    if doc:
        docs.append(doc)
    return docs


def _recursive_split(text: str, separators: list[str], chunk_size: int, overlap: int) -> list[str]:
    final: list[str] = []
    separator = separators[-1]
    remaining: list[str] = []
    for i, sep in enumerate(separators):
        if sep == "" or sep in text:
            separator = sep
            remaining = separators[i + 1 :]
            break

    splits = text.split(separator) if separator else list(text)
    splits = [s for s in splits if s.strip()]

    good: list[str] = []
    for piece in splits:
        if len(piece) < chunk_size:
            good.append(piece)
        else:
            if good:
                final.extend(_merge_splits(good, separator, chunk_size, overlap))
                good = []
            # 超长片段用下一级分隔符继续切，字符兜底保证不产生超长块
            final.extend(_recursive_split(piece, remaining or [""], chunk_size, overlap))
    if good:
        final.extend(_merge_splits(good, separator, chunk_size, overlap))
    return final


def split_text(text: str, chunk_size: int = 300, overlap: int = 50) -> list[str]:
    """对外入口：把一段文本切成不超过 chunk_size 的块列表。"""
    if not text or not text.strip():
        return []
    if len(text) <= chunk_size:
        return [text.strip()]
    return _recursive_split(text, SEPARATORS, chunk_size, overlap)


def build_chunks(
    doc_id: str,
    segments: list[tuple[str, str]],
    tenant_id: str,
    chunk_size: int = 300,
    overlap: int = 50,
    min_chars: int = 20,
) -> list[Chunk]:
    """把 (source, text) 片段列表切成 Chunk，生成稳定 chunk_id。

    - 空块在入库前过滤；
    - 超短块（< min_chars）保留但标记 is_short，检索展示端可识别。
    """
    chunks: list[Chunk] = []
    seq = 0
    for source, text in segments:
        for piece in split_text(text, chunk_size, overlap):
            content = piece.strip()
            if not content:
                continue
            seq += 1
            chunks.append(
                Chunk(
                    chunk_id=f"{doc_id}#{seq}",
                    doc_id=doc_id,
                    seq=seq,
                    content=content,
                    tenant_id=tenant_id,
                    source=source,
                    is_short=len(content) < min_chars,
                )
            )
    return chunks
