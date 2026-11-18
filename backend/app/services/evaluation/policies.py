"""切块策略预设与语料切块（批次 1 的 chunk 策略实验用）。

**为什么单独一层**：切块实验要比较的是"哪套策略更好"，如果实验自己实现一套切块，
那测的就是"实验的切块"而不是"生产用的切块"——结论没法上线。
所以这里直接调用**生产的切块工厂** `apply_chunk_policy`，只把策略参数化。

预设命名：`<strategy>[-<关键参数>]`（见 `PRESETS`）。
"""
from __future__ import annotations

from pathlib import Path

#: 策略预设与解析**住在 core 层**（`MP-05` 的准备）：
#: 预设是**配置数据**，生产与评测共用同一份——两份定义必然漂移，
#: 而漂移一次的代价是「实验测的策略」与「生产用的策略」不是一回事。
from app.core.chunk_policies import (  # noqa: E402
    DEFAULT_PRESET,
    PRESETS,
    available_presets,
    resolve_policy,
)

__all__ = ["PRESETS", "DEFAULT_PRESET", "resolve_policy", "available_presets", "chunk_corpus"]


def chunk_corpus(
    corpus_dir: Path,
    spec: str | None = None,
    tenant_id: str = "eval-tenant",
    embeddings=None,
    llm=None,
) -> list:
    """按指定策略切整个语料目录，返回块列表（父块不进向量库，与生产一致）。

    走**生产切块工厂** `apply_chunk_policy`——实验测的必须是生产路径。
    """
    # 按需载入：仅在特定路径用到
    from app.services.chunkers.factory import apply_chunk_policy
    # 按需载入：仅在特定路径用到
    from app.services.embeddings import MockEmbeddings
    # 按需载入：仅在特定路径用到
    from app.services.llm import MockLLM

    embeddings = embeddings or MockEmbeddings(dim=1024)
    llm = llm or MockLLM()
    resolved = resolve_policy(spec)
    policy = resolved["policy"]

    chunks: list = []
    # **递归**扫描：图文语料有子目录（images/ captions/ docs/），
    # 只扫顶层会漏掉 captions/ 里的图注文本 —— 而那正是图文评测的"答案块"。
    for path in sorted(Path(corpus_dir).rglob("*")):
        if path.suffix.lower() not in (".md", ".txt"):
            continue
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        result = apply_chunk_policy(
            path.stem, [(path.name, text)], tenant_id, policy,
            embeddings=embeddings, llm=llm,
            chunk_size=resolved["chunk_size"], chunk_overlap=resolved["overlap"],
        )
        chunks.extend(result.chunks)
    return chunks
