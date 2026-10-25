"""父子分块。

子块（200 字）入向量库保证召回粒度；父块（1200 字）入 DocStore 保证上下文完整。
检索命中子块 → 按 parent_id 回查父块 → 完整父块喂给 LLM。
"""
from app.domain.schemas import Chunk
from app.services.chunking import split_text


class ParentChildChunker:
    name = "parent_child"

    def chunk(
        self,
        doc_id: str,
        segments: list[tuple[str, str]],
        tenant_id: str,
        parent_size: int = 1200,
        child_size: int = 200,
        child_overlap: int = 40,
        min_chars: int = 20,
    ) -> tuple[list[Chunk], list[Chunk]]:
        """返回 (子块列表, 父块列表)。子块带 parent_id。"""
        children: list[Chunk] = []
        parents: list[Chunk] = []
        parent_seq = 0
        child_seq = 0
        for source, text in segments:
            for parent_text in split_text(text, chunk_size=parent_size, overlap=parent_size // 10):
                parent_text = parent_text.strip()
                if not parent_text:
                    continue
                parent_seq += 1
                parent_id = f"{doc_id}#P{parent_seq}"
                parents.append(
                    Chunk(
                        chunk_id=parent_id,
                        doc_id=doc_id,
                        seq=parent_seq,
                        content=parent_text,
                        tenant_id=tenant_id,
                        source=source,
                        chunker=self.name,
                        level=1,  # 父块为上层
                    )
                )
                for child_text in split_text(parent_text, chunk_size=child_size, overlap=child_overlap):
                    child_text = child_text.strip()
                    if not child_text:
                        continue
                    child_seq += 1
                    children.append(
                        Chunk(
                            chunk_id=f"{doc_id}#{child_seq}",
                            doc_id=doc_id,
                            seq=child_seq,
                            content=child_text,
                            tenant_id=tenant_id,
                            source=source,
                            is_short=len(child_text) < min_chars,
                            chunker=self.name,
                            level=0,
                            parent_id=parent_id,
                        )
                    )
        return children, parents
