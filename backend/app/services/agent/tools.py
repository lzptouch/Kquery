"""工具注册机制（阶段 6 §3）。

- 每个工具：name + description（Planner 选工具的唯一依据）+ run(query)；
- 目录随场景变化：闲聊场景仅留直答；未配置外部依赖的工具不进目录；
- 工具名幻觉兜底：LLM 输出未注册的工具名 → 降级到注册表第一个工具 + 日志告警
  （降级而非崩溃，是 Agent 系统的工程纪律）；
- 单步返回条数有上限（防上下文引爆）；工具超时/异常 → 该步失败但流程不中断。
"""
import ast
import logging
import operator
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Protocol

from app.domain.schemas import RetrievalConfig
from app.services.retrieval.hybrid import HybridRetriever

logger = logging.getLogger(__name__)

TOOL_TIMEOUT_S = 30  # 第三层熔断：单步超时
TOOL_RESULT_CAP = 4  # 单工具返回条数上限


@dataclass
class ToolResult:
    items: list[dict] = field(default_factory=list)  # 证据条目（含 chunk_id/content/score/source）
    elapsed_ms: int = 0
    error: str = ""  # 异常摘要，成功为空
    truncated_from: int = 0  # 截断前条数


class Tool(Protocol):
    name: str
    description: str

    def run(self, query: str, tenant_id: str) -> ToolResult: ...


class VectorSearchTool:
    """在文档知识库中检索与 query 相关的文本片段（BM25+向量+RRF+精排）。"""

    name = "vector_search"
    description = "在文档知识库中检索与 query 相关的文本片段（适用：产品/政策/教材等已入库文档的事实型问题）"

    def __init__(self, hybrid: HybridRetriever, config: RetrievalConfig):
        self._hybrid = hybrid
        self._config = config

    def run(self, query: str, tenant_id: str) -> ToolResult:
        started = time.monotonic()
        result = self._hybrid.search(query, tenant_id, self._config)
        elapsed = int((time.monotonic() - started) * 1000)
        total = len(result.candidates)
        items = [
            {
                "chunk_id": c.chunk.chunk_id,
                "content": c.chunk.content,
                "score": round(c.rerank_score if result.reranked else c.rrf_score, 4),
                "source": c.chunk.source,
                "doc_id": c.chunk.doc_id,
                "source_route": c.source_route,
            }
            for c in result.candidates[:TOOL_RESULT_CAP]
        ]
        return ToolResult(items=items, elapsed_ms=elapsed, truncated_from=total)


# calculator 支持的安全运算符白名单
_ALLOWED_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
}
_ALLOWED_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


class CalculatorTool:
    """数学表达式求值（AST 白名单解析，拒绝任意代码执行）。"""

    name = "calculator"
    description = "数学表达式求值（适用：四则运算、百分比、汇率换算等确定性计算）"

    def run(self, query: str, tenant_id: str) -> ToolResult:
        started = time.monotonic()
        try:
            value = self._eval(query)
            text = f"{query} = {value}"
        except Exception as exc:
            return ToolResult(
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=f"表达式无法求值: {exc}",
            )
        return ToolResult(
            items=[{
                "chunk_id": f"calc#{abs(hash(query)) % 100000}",
                "content": text,
                "score": 1.0,
                "source": "计算器",
                "doc_id": "__calculator__",
                "source_route": "tool",
            }],
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    @classmethod
    def _eval(cls, expr: str):
        tree = ast.parse(expr.strip(), mode="eval")
        return cls._node(tree.body)

    @classmethod
    def _node(cls, node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
            return _ALLOWED_BINOPS[type(node.op)](cls._node(node.left), cls._node(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
            return _ALLOWED_UNARY[type(node.op)](cls._node(node.operand))
        raise ValueError(f"不支持的表达式元素: {ast.dump(node)[:60]}")


class GraphSearchTool:
    """知识图谱实体关系检索（阶段 7 §4.3）：实体 → BFS 关系路径。

    失败模式纪律（§3.4）：实体未命中返回空结果，
    由 Agent 反思层决定回退向量检索，不允许直接判无答案。
    """

    name = "graph_search"
    description = "在知识图谱中查询实体关系（适用：'A 和 B 是什么关系'、'A 投资/收购了谁'、实体链式多跳问题）"

    def __init__(self, graph_query):
        self._gq = graph_query

    def run(self, query: str, tenant_id: str) -> ToolResult:
        started = time.monotonic()
        result = self._gq.search(query, tenant_id)
        items = result["evidence_items"][:TOOL_RESULT_CAP]
        # 实体未命中 → 空结果（视为该步失败但不报错），由反思层决定回退向量检索
        return ToolResult(
            items=items,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            truncated_from=len(result["evidence_items"]),
        )


class ImageSearchTool:
    """跨模态图像检索（阶段 7 §4.3）：自然语言 → 相似图片元数据。"""

    name = "image_search"
    description = "以文字描述检索知识库中的图片/图表/扫描页（适用：'有没有 XX 的示意图'、'XX 表格/流程图'等图文问题）"

    def __init__(self, image_index):
        self._index = image_index

    def run(self, query: str, tenant_id: str) -> ToolResult:
        started = time.monotonic()
        hits = self._index.search(query, top_k=TOOL_RESULT_CAP, tenant_id=tenant_id)
        items = [
            {
                "chunk_id": f"img#{h['asset_id']}",
                "content": h["caption"],
                "score": h["score"],
                "source": f"图像 · 第{h['page']}页" if h["page"] else "图像",
                "doc_id": h["doc_id"] or "__images__",
                "source_route": "image",
                "modality": "image",
                "uri": h["uri"],
            }
            for h in hits
        ]
        return ToolResult(items=items, elapsed_ms=int((time.monotonic() - started) * 1000))


class ToolRegistry:
    """工具目录：Planner 的工具清单来源，执行期的分发入口。"""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def catalog(self, skip_retrieval: bool = False) -> list[dict]:
        """目录随场景变化：闲聊场景不暴露检索工具。"""
        tools = list(self._tools.values())
        if skip_retrieval:
            tools = [t for t in tools if t.name == "calculator"]
        return [{"name": t.name, "description": t.description} for t in tools]

    def resolve(self, name: str) -> tuple[Tool, bool]:
        """按名取工具；幻觉工具名兜底到注册表第一个工具 + 告警（降级不崩溃）。

        返回 (tool, hallucinated)。
        """
        tool = self._tools.get(name)
        if tool is not None:
            return tool, False
        fallback = next(iter(self._tools.values()))
        logger.warning("工具名幻觉兜底: %r 未注册，降级到 %r", name, fallback.name)
        return fallback, True

    def run_with_timeout(self, name: str, query: str, tenant_id: str) -> tuple[ToolResult, bool, str]:
        """带第三层熔断（单步超时）的工具执行。返回 (result, hallucinated, actual_tool_name)。"""
        tool, hallucinated = self.resolve(name)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(tool.run, query, tenant_id)
            try:
                return future.result(timeout=TOOL_TIMEOUT_S), hallucinated, tool.name
            except FutureTimeout:
                return (
                    ToolResult(error=f"工具调用超过 {TOOL_TIMEOUT_S}s 超时"),
                    hallucinated,
                    tool.name,
                )
            except Exception as exc:  # noqa: BLE001
                return ToolResult(error=f"工具异常: {exc}"), hallucinated, tool.name
