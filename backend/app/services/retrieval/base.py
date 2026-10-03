"""检索器抽象（阶段 2 README §3.2）。

三项能力约定：
1. 统一检索入口：传入查询与条数，返回「块 + 分数」列表；
2. 自带名称标识：融合时标注证据来自哪一路，供前端展示与排障；
3. 多实现并存：阶段 3 图检索、阶段 7 跨模态检索可直接注册，无需改融合层。

分数契约是弱契约：只承诺"分数越大越相关"，不允许跨路直接比较分数
（BM25 是 0~∞ 无界分，向量是 -1~1 余弦分），跨路比较统一交给 RRF。
"""
from dataclasses import dataclass, field
from typing import Protocol

from app.domain.schemas import Chunk


@dataclass
class RetrievalCandidate:
    chunk: Chunk
    score: float  # 单路原始分，语义因路而异，不可跨路比较


@dataclass
class FusedCandidate:
    """RRF 融合后的候选：带融合分与来源路径集合。"""

    chunk: Chunk
    rrf_score: float
    routes: list[str] = field(default_factory=list)  # 命中该候选的检索路名称
    rerank_score: float | None = None  # 重排相关度分（重排后填充）
    # 阶段 4：证据由哪条改写/哪个子问题带回（双向定位的支点）
    origins: list[dict] = field(default_factory=list)

    @property
    def source_route(self) -> str:
        """keyword / vector / both（两路融合命中）"""
        return "both" if len(self.routes) > 1 else (self.routes[0] if self.routes else "vector")

    @property
    def origin(self) -> dict | None:
        return self.origins[0] if self.origins else None


class Retriever(Protocol):
    name: str  # 检索路唯一标识："keyword" / "vector" / ...

    def search(
        self, query: str, top_n: int, tenant_id: str, doc_id: str | None = None
    ) -> list[RetrievalCandidate]: ...
