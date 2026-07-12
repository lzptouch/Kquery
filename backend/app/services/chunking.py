"""
文件读取与分块模块
基于KAiSupport的chunking.py，针对Kquery需求进行优化
"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional
from app.core.settings import get_settings
from app.core.logger import get_logger

settings = get_settings()

logger = get_logger(__name__)

# 使用 pypdf 进行 PDF 文件解析
try:
    from pypdf import PdfReader
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False
    logger.warning("pypdf library not available. PDF files will be read as plain text.")


@dataclass(frozen=True, slots=True)
class TextChunk:
    """文本切片"""
    chunk_id: str
    content: str
    source_path: str
    index: int
    page_number: Optional[int] = None
    char_offset: int = 0


def read_text_file(file_path: str | Path, *, stream: bool = False) -> str | Iterator[str]:
    """解析文档内容为纯文本"""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    # 判断文件类型
    file_ext = path.suffix.lower()

    def _read_pdf_pages() -> Iterator[str]:
        """辅助函数：按页读取 PDF 内容（生成器）"""
        if not PYPDF_AVAILABLE:
            logger.warning("pypdf is not available, reading PDF file %s as plain text.", path)
            try:
                # 按行流式读取
                with path.open(encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            yield line
            except Exception as e:
                raise RuntimeError(f"Failed to read PDF file {path} as plain text: {e}") from e

        try:
            reader = PdfReader(str(path))
            for page_num, page in enumerate(reader.pages, start=1):
                try:
                    page_text = page.extract_text()
                    if page_text and page_text.strip():
                        yield page_text.strip()
                except Exception as page_error:
                    logger.warning(
                        "Failed to extract text from page %d of %s: %s",
                        page_num,
                        path,
                        page_error,
                    )
                    continue
        except Exception as e:
            logger.error("Failed to load PDF %s with pypdf: %s", path, e)
            # 降级为按行读取
            try:
                with path.open(encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            yield line
            except Exception as fallback_error:
                raise RuntimeError(f"Failed to read file {path}") from fallback_error

    def _read_text_lines() -> Iterator[str]:
        """辅助函数：按行读取文本文件（生成器）"""
        try:
            with path.open(encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        yield line
        except Exception as e:
            raise RuntimeError(f"Failed to read file {path} as plain text: {e}") from e

    # 流式读取模式
    if stream:
        if file_ext == ".pdf":
            return _read_pdf_pages()
        else:
            return _read_text_lines()

    # 非流式读取模式
    if file_ext == ".pdf":
        if not PYPDF_AVAILABLE:
            logger.warning("pypdf is not available, reading PDF file %s as plain text.", path)
            try:
                return path.read_text(encoding="utf-8", errors="ignore")
            except Exception as e:
                raise RuntimeError(f"Failed to read PDF file {path} as plain text: {e}") from e

        try:
            # 使用 pypdf 解析 PDF
            reader = PdfReader(str(path))
            texts: list[str] = []

            # 提取所有页面的文本
            for page_num, page in enumerate(reader.pages, start=1):
                try:
                    page_text = page.extract_text()
                    if page_text and page_text.strip():
                        texts.append(page_text.strip())
                except Exception as page_error:
                    logger.warning(
                        "Failed to extract text from page %d of %s: %s",
                        page_num,
                        path,
                        page_error,
                    )
                    continue

            result = "\n\n".join(texts).strip()

            # 如果解析结果为空，尝试降级为纯文本读取
            if not result:
                logger.warning(
                    "pypdf returned empty content for %s, falling back to plain text",
                    path,
                )
                return path.read_text(encoding="utf-8", errors="ignore")

            return result

        except Exception as e:
            logger.error("Failed to load PDF %s with pypdf: %s", path, e)
            # 如果 pypdf 解析失败，最后再尝试纯文本读取
            try:
                return path.read_text(encoding="utf-8", errors="ignore")
            except Exception as fallback_error:
                raise RuntimeError(f"Failed to read file {path}") from fallback_error

    # 非 PDF 文件直接按纯文本读取
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        raise RuntimeError(f"Failed to read file {path} as plain text: {e}") from e


def chunk_text(
    text: str,
    *,
    chunk_size: int = settings.rag.chunk_size,
    overlap: int = settings.rag.chunk_overlap,
) -> list[str]:
    """自实现迭代字符分割器进行智能分块"""
    cleaned = (text or "").strip()
    if not cleaned:
        return []

    # 分隔符优先级：从粗粒度到细粒度
    separators = ["\n\n", "\n", "。", "！", "？", ".", "!", "?", " ", ""]
    
    def _split_iterative(
        text: str,
        separators: list[str],
        chunk_size: int,
        overlap: int,
    ) -> list[str]:
        """迭代分割文本，避免递归深度限制"""
        if not text or len(text) <= chunk_size:
            return [text] if text.strip() else []
        
        # 使用栈存储待处理任务：(text, separator_index)
        stack: list[tuple[str, int]] = [(text, 0)]
        final_chunks: list[str] = []
        prev_chunk_tail: str = ""  # 前一个块的尾部，用于 overlap
        
        while stack:
            current_text, sep_idx = stack.pop()
            current_text = current_text.strip()
            if not current_text:
                continue
            
            # 如果文本已经足够小，直接生成 chunk
            if len(current_text) <= chunk_size:
                # 立即处理 overlap：将前一个块的尾部添加到当前块
                if prev_chunk_tail:
                    if len(prev_chunk_tail) >= overlap:
                        overlap_text = prev_chunk_tail[-overlap:]
                    else:
                        # 如果前块长度 < overlap，整块作为 overlap
                        overlap_text = prev_chunk_tail
                    current_text = overlap_text + current_text
                
                if current_text.strip():
                    final_chunks.append(current_text.strip())
                    # 更新 prev_chunk_tail 为当前块的尾部（用于下一个 chunk）
                    prev_chunk_tail = current_text[-min(overlap, len(current_text)):] if overlap > 0 else ""
                continue
            
            # 如果没有更多分隔符，强制按 chunk_size 切分
            if sep_idx >= len(separators):
                start = 0
                while start < len(current_text):
                    end = min(start + chunk_size, len(current_text))
                    chunk = current_text[start:end].strip()
                    if chunk:
                        # 处理 overlap
                        if prev_chunk_tail:
                            if len(prev_chunk_tail) >= overlap:
                                overlap_text = prev_chunk_tail[-overlap:]
                            else:
                                overlap_text = prev_chunk_tail
                            chunk = overlap_text + chunk
                        
                        final_chunks.append(chunk)
                        prev_chunk_tail = chunk[-min(overlap, len(chunk)):] if overlap > 0 else ""
                    start = end
                continue
            
            # 使用当前分隔符分割
            current_sep = separators[sep_idx]
            
            if current_sep == "":
                # 最后一个分隔符是空字符串，表示按字符切分
                start = 0
                while start < len(current_text):
                    end = min(start + chunk_size, len(current_text))
                    chunk = current_text[start:end].strip()
                    if chunk:
                        # 处理 overlap
                        if prev_chunk_tail:
                            if len(prev_chunk_tail) >= overlap:
                                overlap_text = prev_chunk_tail[-overlap:]
                            else:
                                overlap_text = prev_chunk_tail
                            chunk = overlap_text + chunk
                        
                        final_chunks.append(chunk)
                        prev_chunk_tail = chunk[-min(overlap, len(chunk)):] if overlap > 0 else ""
                    start = end
                continue
            
            # 使用正则表达式分割（保留分隔符）
            parts = re.split(f"({re.escape(current_sep)})", current_text) if current_sep else [current_text]
            
            # 合并分隔符和内容
            merged_parts: list[str] = []
            for i in range(0, len(parts), 2):
                content = parts[i] if i < len(parts) else ""
                sep = parts[i + 1] if i + 1 < len(parts) else ""
                if content or sep:
                    merged_parts.append(content + sep)
            
            # 如果只有一个部分，说明当前分隔符没有匹配到，使用下一个分隔符
            if len(merged_parts) <= 1:
                stack.append((current_text, sep_idx + 1))
                continue
            
            # 尝试合并相邻的小块，生成临时 chunks
            temp_chunks: list[str] = []
            current_chunk = ""
            
            for part in merged_parts:
                part = part.strip()
                if not part:
                    continue
                
                # 如果当前块加上新部分不超过 chunk_size，合并
                if len(current_chunk) + len(part) <= chunk_size:
                    current_chunk = (current_chunk + part).strip() if current_chunk else part
                else:
                    # 当前块已满，保存并开始新块
                    if current_chunk:
                        temp_chunks.append(current_chunk)
                    # 如果新部分本身超过 chunk_size，需要进一步分割
                    if len(part) > chunk_size:
                        # 将超长部分压入栈，使用下一个分隔符处理
                        stack.append((part, sep_idx + 1))
                        current_chunk = ""
                    else:
                        current_chunk = part
            
            # 添加最后一个临时块
            if current_chunk:
                temp_chunks.append(current_chunk)
            
            # 处理临时 chunks：如果都符合大小，立即生成最终 chunks；否则压入栈继续分割
            for temp_chunk in temp_chunks:
                if len(temp_chunk) <= chunk_size:
                    # 立即处理 overlap
                    if prev_chunk_tail:
                        if len(prev_chunk_tail) >= overlap:
                            overlap_text = prev_chunk_tail[-overlap:]
                        else:
                            overlap_text = prev_chunk_tail
                        temp_chunk = overlap_text + temp_chunk
                    
                    if temp_chunk.strip():
                        final_chunks.append(temp_chunk.strip())
                        prev_chunk_tail = temp_chunk[-min(overlap, len(temp_chunk)):] if overlap > 0 else ""
                else:
                    # 仍然太大，压入栈继续分割
                    stack.append((temp_chunk, sep_idx + 1))
        
        return final_chunks
    
    # 执行迭代分割
    chunks = _split_iterative(cleaned, separators, chunk_size, overlap)
    
    # 过滤空块并去除首尾空白
    result = [chunk.strip() for chunk in chunks if chunk.strip()]
    
    return result


def chunk_text_for_messages(
    text: str,
    *,
    max_message_len: int = 500,
) -> list[str]:
    """针对消息的文本分条策略"""
    cleaned = (text or "").strip()
    if not cleaned:
        return []

    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    if not lines:
        return []

    results: list[str] = []
    for ln in lines:
        if len(ln) <= max_message_len:
            results.append(ln)
        else:
            # 对超长行使用基础分块器再细分，但不做重叠
            sub_chunks = chunk_text(ln, chunk_size=max_message_len, overlap=0)
            results.extend(sub_chunks)
    return results


def load_and_chunk_files(
    file_paths: Iterable[str | Path],
    *,
    chunk_size: int = settings.rag.chunk_size,
    overlap: int = settings.rag.chunk_overlap,
    max_workers_io: int | None = None,
) -> list[TextChunk]:
    """批量读取文件并分块"""
    def _process_single_file(
        file_path: str | Path,
        chunk_size: int,
        overlap: int,
    ) -> list[TextChunk]:
        """辅助函数：处理单个文件的读取和分块"""
        path = Path(file_path)
        # 跳过系统隐藏文件或非文件对象
        if path.name.startswith(".") or not path.is_file():
            return []

        try:
            logger.info(f"Processing: {path.name}")
            chunks: list[TextChunk] = []
            
            # 一次性读取完整文本
            text = read_text_file(path, stream=False)
            if isinstance(text, str) and text:
                parts = chunk_text(text, chunk_size=chunk_size, overlap=overlap)
                search_start = 0
                for i, content in enumerate(parts):
                    # 计算字符偏移量（在原文档中的起始位置）
                    offset = text.find(content, search_start)
                    if offset == -1:
                        offset = search_start
                    search_start = offset + 1
                    
                    chunk_id = f"{path.name}:{i}"
                    chunks.append(
                        TextChunk(
                            chunk_id=chunk_id,
                            content=content,
                            source_path=str(path),
                            index=i,
                            page_number=None,  # 非结构化读取无法获取页码
                            char_offset=offset,
                        )
                    )
            else:
                logger.warning(f"Empty content in file: {path.name}")
            
            return chunks
        except Exception as e:
            logger.error(f"Error processing {path.name}: {e}")
            return []

    # 过滤有效文件路径
    valid_paths = [
        Path(fp) for fp in file_paths
        if Path(fp).is_file() and not Path(fp).name.startswith(".")
    ]
    
    if not valid_paths:
        return []

    results: list[TextChunk] = []
    
    # 使用线程池并行处理
    with ThreadPoolExecutor(max_workers=max_workers_io) as executor:
        future_to_path = {
            executor.submit(_process_single_file, fp, chunk_size, overlap): fp
            for fp in valid_paths
        }
        
        for future in as_completed(future_to_path):
            path = future_to_path[future]
            try:
                file_chunks = future.result()
                results.extend(file_chunks)
            except Exception as e:
                logger.error(f"Error processing {path}: {e}")
                continue

    return results