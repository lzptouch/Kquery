"""语义切块。

相邻句子的语义距离 1 - cosine(v_i, v_{i+1}) 超过阈值即切；
min_sentences 避免单句成块（上下文断裂），max_sentences 防止永不切（buffer 膨胀）。
距离越小越像、越大越不同——与"切"的方向一致。
"""
import re

import numpy as np

from app.domain.schemas import Chunk
from app.services.embeddings import Embeddings

_SENTENCE_RE = re.compile(r"(?<=[。！？!?；;])|\n+")


def split_sentences(text: str) -> list[str]:
    parts = _SENTENCE_RE.split(text)
    return [p.strip() for p in parts if p and p.strip()]


class SemanticChunker:
    name = "semantic"

    def __init__(self, embeddings: Embeddings) -> None:
        self._embeddings = embeddings

    def chunk(
        self,
        doc_id: str,
        segments: list[tuple[str, str]],
        tenant_id: str,
        threshold: float = 0.4,
        min_sentences: int = 2,
        max_sentences: int = 12,
        min_chars: int = 20,
    ) -> list[Chunk]:
        chunks: list[Chunk] = []
        seq = 0
        for source, text in segments:
            sentences = split_sentences(text)
            if not sentences:
                continue
            vectors = [np.array(v, dtype=np.float32) for v in self._embeddings.embed_texts(sentences)]
            # 相邻句子的余弦距离
            distances = []
            for i in range(len(sentences) - 1):
                denom = float(np.linalg.norm(vectors[i]) * np.linalg.norm(vectors[i + 1]))
                sim = float(np.dot(vectors[i], vectors[i + 1]) / denom) if denom else 1.0
                distances.append(1.0 - sim)

            buffer: list[str] = [sentences[0]]
            groups: list[list[str]] = []
            for i, dist in enumerate(distances):
                over_min = len(buffer) >= min_sentences
                hit_max = len(buffer) >= max_sentences
                if (dist > threshold and over_min) or hit_max:
                    groups.append(buffer)
                    buffer = []
                buffer.append(sentences[i + 1])
            if buffer:
                groups.append(buffer)

            for group in groups:
                content = "".join(group).strip()
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
                        chunker=self.name,
                        level=0,
                    )
                )
        return chunks
