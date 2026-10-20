"""上传共享工具：文件名收敛与流式落盘（api 层共用）。

两条红线（对照分析文档 KQ-01 / KQ-05）：
1. 客户端文件名不可信——含路径分隔符或 `..` 一律拒绝，不做静默截断；
2. 文件体先进临时文件、边写边计数、超限即中断删除——不允许整文件进内存后再校验。
"""
import logging
from pathlib import Path

from fastapi import UploadFile

from app.core.envelope import ApiError, ErrCode

logger = logging.getLogger(__name__)

_CHUNK = 1024 * 1024  # 1MB：够小（不占内存），够大（系统调用次数可忽略）


def sanitize_filename(filename: str) -> str:
    """校验并返回纯文件名。任何目录成分都视为攻击或误用，直接 400。"""
    raw = (filename or "").strip()
    if not raw or raw in (".", ".."):
        raise ApiError(ErrCode.INVALID_INPUT, "文件名不能为空", http_status=400)
    normalized = raw.replace("\\", "/")
    if "/" in normalized or ".." in normalized.split("/"):
        raise ApiError(
            ErrCode.INVALID_INPUT,
            f"非法文件名（不允许包含路径成分）: {raw!r}",
            http_status=400,
        )
    if Path(normalized).name != normalized:
        raise ApiError(
            ErrCode.INVALID_INPUT,
            f"非法文件名: {raw!r}",
            http_status=400,
        )
    return normalized


async def stream_to_disk(upload: UploadFile, dest: Path, max_mb: int) -> int:
    """把上传内容分块写入 dest，边写边累计字节数。超限即删除半成品并抛 413 类错误。"""
    limit = max_mb * 1024 * 1024
    total = 0
    try:
        with dest.open("wb") as fh:
            while chunk := await upload.read(_CHUNK):
                total += len(chunk)
                if total > limit:
                    raise ApiError(
                        ErrCode.FILE_TOO_LARGE,
                        f"{upload.filename} 超过 {max_mb}MB 限制",
                    )
                fh.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return total
