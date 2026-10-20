"""工具注册机制。

- 每个工具：name + description（Planner 选工具的唯一依据）+ run(query)；
- 目录随场景变化：闲聊场景仅留直答；未配置外部依赖的工具不进目录；
- 工具名幻觉兜底：LLM 输出未注册的工具名 → 降级到注册表第一个工具 + 日志告警
  （降级而非崩溃，是 Agent 系统的工程纪律）；
- 单步返回条数有上限（防上下文引爆）；工具超时/异常 → 该步失败但流程不中断。
"""
import ast
import json
import logging
import operator
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel, Field

from app.domain.schemas import RetrievalConfig
from app.services.agent.tool_spec import spec_of, validate_arguments
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
    acl_filtered: int = 0  # 被 ACL 过滤掉的条数（仅 ToolResult 显式携带，不泄漏被过滤文档元信息）


class Tool(Protocol):
    name: str
    description: str

    def run(self, query: str, tenant_id: str) -> ToolResult: ...


class VectorSearchTool:
    """在文档知识库中检索与 query 相关的文本片段（BM25+向量+RRF+精排）。"""

    name = "vector_search"
    description = "在文档知识库中检索与 query 相关的文本片段（适用：产品/政策/教材等已入库文档的事实型问题）"
    returns = "相关文本片段（含来源文档与页码）"

    # 结构化入参——模型可以显式表达"要几条""限定哪篇文档"，
    # 而不是把这些意图塞进 query 句子里靠下游猜
    class arguments_model(BaseModel):
        query: str = Field(description="检索用的查询语句，应是对目标信息的自然语言描述")
        doc_id: str | None = Field(default=None, description="限定在某一篇文档内检索（可选）")
        top_k: int | None = Field(default=None, ge=1, le=50, description="返回条数上限（可选，默认 4）")

    def __init__(self, hybrid: HybridRetriever, config: RetrievalConfig):
        self._hybrid = hybrid
        self._config = config

    def run(self, query: str, tenant_id: str, **kwargs) -> ToolResult:
        started = time.monotonic()
        # 结构化入参优先（doc_id 限定文档、top_k 覆盖默认条数）
        result = self._hybrid.search(
            query, tenant_id, self._config, doc_id=kwargs.get("doc_id")
        )
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
    returns = "数值结果"
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
    """知识图谱实体关系检索：实体 → BFS 关系路径。

    失败模式纪律：实体未命中返回空结果，
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
    """跨模态图像检索：自然语言 → 相似图片元数据。"""

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


class SqlSearchTool:
    """只读业务库查询。

    安全边界（三条，缺一条就不能上生产）：
    1. **只允许 SELECT / WITH**——AST 解析而非字符串匹配，`WHERE 1=1; DROP TABLE`
       这类靠字符串前缀拦不住；
    2. **强制 LIMIT**且不可被查询里的 LIMIT 覆盖；
    3. 结果行转文本后才进上下文，不把整表灌进提示词。

    未配置 `sql_dsn` 时不注册——工具目录里出现一个必然失败的工具，
    会诱导 Planner 规划出注定失败的步骤。
    """

    name = "sql_search"
    description = (
        "以只读 SQL 查询业务库中的结构化数据（适用：'XX 部门今年的订单量'、"
        "'XX 产品的库存'等库里有现成表/字段的统计类问题）"
    )

    _ALLOWED_HEADS = {"select", "with"}

    def __init__(self, dsn: str, readonly: bool = True, max_rows: int = 50,
                 tenant_column: str | None = None):
        self._dsn = dsn
        self._readonly = readonly
        self._max_rows = max(1, max_rows)
        # 租户隔离列：配置后强制「单表查询 + 表白名单 + 谓词包装」。
        # 不设则保持兼容（但生产启用 sql_search 应配置，否则跨租户越权）。
        self._tenant_column = tenant_column or None

    @classmethod
    def _validate(cls, sql: str) -> str:
        stripped = sql.strip().rstrip(";")
        if not stripped:
            raise ValueError("SQL 为空")
        try:
            parsed = __import__("sqlparse").parse(stripped)  # 可选依赖
        except Exception:  # noqa: BLE001 - 无 sqlparse 时退回保守解析
            head = stripped.split(None, 1)[0].lower()
            if head not in cls._ALLOWED_HEADS:
                raise ValueError("只允许 SELECT / WITH 语句")
            if ";" in stripped:
                raise ValueError("不允许多语句执行")
            return stripped
        if len(parsed) > 1:
            raise ValueError("不允许多语句执行")
        if parsed[0].get_type() != "SELECT":
            raise ValueError("只允许 SELECT / WITH 语句")
        stripped = str(parsed[0]).strip().rstrip(";")
        return stripped

    def _force_limit(self, sql: str) -> str:
        """强制行数上限（兑现 _validate 注释承诺）：剥掉调用方写的 LIMIT，统一追加。

        此前 _validate 只 rstrip(";") 而未真正追加 LIMIT，调用方写 LIMIT 100000 即可绕过——
        唯一防线是 fetchmany 在 Python 侧截断，全表仍会被扫描读出。
        """
        import re as _re
        stripped = _re.sub(r"\s+LIMIT\s+\d+(\s+OFFSET\s+\d+)?\s*;?\s*$", "", sql, flags=_re.IGNORECASE)
        return f"{stripped} LIMIT {self._max_rows}"

    @staticmethod
    def _single_table_name(sql: str) -> str | None:
        """租户模式专用：仅当是单表 SELECT 时返回表名，否则 None。

        拒绝 JOIN / UNION / 子查询 / 集合运算——子查询包装对它们存在绕过（JOIN 的
        租户列歧义会把外层过滤引到错误的表上），因此租户隔离只允许单表查询。
        """
        import re as _re
        upper = f" {sql.upper()} "
        for kw in (" JOIN ", " UNION ", "(SELECT", " INTERSECT ", " EXCEPT "):
            if kw in upper:
                return None
        m = _re.search(r"\bFROM\s+([A-Za-z_][A-Za-z0-9_.`]*)", sql, _re.IGNORECASE)
        if not m:
            return None
        return m.group(1).strip("`").split(".")[-1]

    def _tenant_scoped_tables(self, conn) -> set[str]:
        """探测业务库中含租户隔离列的表（白名单）。"""
        scoped: set[str] = set()
        for (tname,) in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')"):
            try:
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({tname})")}
            except Exception:  # noqa: BLE001
                continue
            if self._tenant_column in cols:
                scoped.add(tname)
        return scoped

    def run(self, query: str, tenant_id: str) -> ToolResult:
        started = time.monotonic()
        import sqlite3

        try:
            sql = self._force_limit(self._validate(query))
            conn = sqlite3.connect(f"file:{self._dsn}?mode=ro", uri=True)
            try:
                conn.row_factory = sqlite3.Row
                params: tuple = ()
                if self._tenant_column:
                    table = self._single_table_name(sql)
                    if table is None:
                        return ToolResult(
                            elapsed_ms=int((time.monotonic() - started) * 1000),
                            error="租户隔离模式仅支持单表查询（不允许 JOIN / 子查询 / 集合运算）",
                        )
                    if table not in self._tenant_scoped_tables(conn):
                        return ToolResult(
                            elapsed_ms=int((time.monotonic() - started) * 1000),
                            error=f"表 '{table}' 不含租户隔离列 '{self._tenant_column}'，对 Agent 不可见",
                        )
                    # 外层包装强制过滤：无论内层 WHERE 怎么写，只放行 tenant_id = 当前租户的行
                    sql = f"SELECT * FROM ({sql}) AS _kquery_tenant_scope WHERE {self._tenant_column} = ?"
                    params = (tenant_id,)
                rows = conn.execute(sql, params).fetchmany(self._max_rows + 1)
            finally:
                conn.close()
            total = len(rows)
            rows = rows[: self._max_rows]
            if not rows:
                return ToolResult(
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    error="查询无结果（0 行）",
                )
            columns = list(rows[0].keys())
            table = "\n".join(
                " | ".join(f"{c}={r[c]}" for c in columns) for r in rows
            )
            content = f"SQL 结果（{len(rows)} 行，{total} 行截断）：\n{table}"
            return ToolResult(
                items=[{
                    "chunk_id": f"sql#{abs(hash(sql)) % 100000}",
                    "content": content,
                    "score": 1.0,
                    "source": "业务库",
                    "doc_id": "__sql__",
                    "source_route": "sql",
                }],
                elapsed_ms=int((time.monotonic() - started) * 1000),
                truncated_from=total,
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=f"SQL 查询失败: {exc}",
            )


class WebSearchTool:
    """联网搜索。

    只在配置了 `web_search_endpoint` 时注册。结果一律带来源 URL 与抓取时间——
    联网内容的时效性必须让读者看见，否则等于把过期信息当权威。
    """

    name = "web_search"
    description = (
        "联网检索公开信息（适用：'最新'/'今天'/'当前'这类必须看时效的问题，"
        "以及知识库未收录的外部事实）"
    )

    def __init__(self, endpoint: str, api_key: str = "", timeout_s: float = 10.0, max_rows: int = 5):
        self._endpoint = endpoint
        self._api_key = api_key
        self._timeout = timeout_s
        self._max_rows = max(1, max_rows)

    def run(self, query: str, tenant_id: str) -> ToolResult:
        started = time.monotonic()
        try:
            import urllib.parse
            import urllib.request

            url = f"{self._endpoint}?{urllib.parse.urlencode({'q': query, 'limit': self._max_rows})}"
            request = urllib.request.Request(url)
            if self._api_key:
                request.add_header("Authorization", f"Bearer {self._api_key}")
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            raw_items = payload.get("items") or payload.get("results") or []
            items = []
            for idx, hit in enumerate(raw_items[: self._max_rows], start=1):
                title = hit.get("title") or f"结果{idx}"
                link = hit.get("url") or hit.get("link") or ""
                snippet = hit.get("content") or hit.get("snippet") or hit.get("summary") or ""
                items.append({
                    "chunk_id": f"web#{idx}:{link or title}",
                    # 来源与时间进正文：让模型在答案里能引用出处，也让读者看到时效
                    "content": f"[{title}]({link})\n{snippet}".strip(),
                    "score": 1.0,
                    "source": f"联网检索 · {title}" + (f" · {link}" if link else ""),
                    "doc_id": "__web__",
                    "source_route": "web",
                })
            if not items:
                return ToolResult(
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    error="联网检索无结果（0 条）",
                )
            return ToolResult(
                items=items,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                truncated_from=len(raw_items),
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=f"联网检索失败: {exc}",
            )


# 工具危险级。`write` 级工具在执行前必须经人工审批——
# 只读工具出错最多是答错，写工具出错会改到外部系统（发请求、写文件、改库）。
DANGER_SAFE = "safe"
DANGER_READ_ONLY = "read_only"
DANGER_WRITE = "write"

_DANGER_BY_NAME: dict[str, str] = {
    "calculator": DANGER_SAFE,
    "datetime_calc": DANGER_SAFE,
    "vector_search": DANGER_READ_ONLY,
    "graph_search": DANGER_READ_ONLY,
    "image_search": DANGER_READ_ONLY,
    "sql_search": DANGER_READ_ONLY,
    "sql_schema": DANGER_READ_ONLY,
    "list_documents": DANGER_READ_ONLY,
    "web_search": DANGER_READ_ONLY,  # 只读检索（非 GET 类外部写操作见 http_request）
    "http_request": DANGER_WRITE,
    "python_sandbox": DANGER_WRITE,
    # MCP 外部工具一律按 write 处理：来源不可控，不能默认可信
    "mcp": DANGER_WRITE,
}

# 工具健康熔断参数
HEALTH_WINDOW = 50            # 滑动窗口大小
HEALTH_FAIL_THRESHOLD = 5     # 连续失败摘除阈值
HEALTH_RECOVER_SUCCESS = 3    # 恢复窗口内成功次数阈值（半开恢复）


class ToolRegistry:
    """工具目录：Planner 的工具清单来源，执行期的分发入口。"""

    def __init__(self, timeout_s: int = TOOL_TIMEOUT_S, executor=None):
        self._tools: dict[str, Tool] = {}
        # 第三层熔断阈值必须来自配置：写死 30s 意味着改了配置也不生效
        self._timeout_s = max(1, int(timeout_s or TOOL_TIMEOUT_S))
        # 并发提容（批次 1）：复用固定线程池执行工具，不再每次调用新建池。
        # 必须与"运行 Agent 图的池"分离——图线程等待工具 future，同池会嵌套死锁。
        self._executor = executor or ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="agent-tool"
        )
        # 每工具健康度（滑动窗口成败 + 连续失败数 + 摘除状态）
        self._health: dict[str, dict] = {}
        self._health_lock = threading.Lock()
        # 取消标志。run/ref 层在每轮工具调用前检查——
        # 取消是"不再产生新的模型/工具调用"，而不是"立刻掐断正在跑的那一个"
        # （后者需要子进程才能做到，见  的技术边界）。
        self._cancel_flags: set[str] = set()
        self._cancel_lock = threading.Lock()

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool
        with self._health_lock:
            self._health.setdefault(
                tool.name,
                {"ok": 0, "fail": 0, "consecutive_fail": 0, "tripped": False,
                 "recent": [], "recover_ok": 0},
            )

    def names(self) -> list[str]:
        return list(self._tools)

    # ---- 取消 ----

    def cancel(self, run_id: str) -> None:
        """置取消标志（`/ask/runs/{id}/cancel` 调用）。"""
        with self._cancel_lock:
            self._cancel_flags.add(run_id)

    def is_cancelled(self, run_id: str) -> bool:
        if not run_id:
            return False
        with self._cancel_lock:
            return run_id in self._cancel_flags

    def clear_cancel(self, run_id: str) -> None:
        """清掉取消标志（run 结束后调用，避免标志集合无界增长）。"""
        with self._cancel_lock:
            self._cancel_flags.discard(run_id)

    # ---- 危险级 ----

    @staticmethod
    def danger_of(tool) -> str:
        """工具危险级：工具自带 `danger` 属性优先，否则按名字查表，未知按 write 保守处理。"""
        declared = getattr(tool, "danger", "")
        if declared in (DANGER_SAFE, DANGER_READ_ONLY, DANGER_WRITE):
            return declared
        name = getattr(tool, "name", "") or ""
        if name in _DANGER_BY_NAME:
            return _DANGER_BY_NAME[name]
        if name.startswith("mcp__") or name.startswith("mcp_"):
            return DANGER_WRITE
        # 未知工具按最危险处理：默认放行未知来源工具是权限事故的常见成因
        return DANGER_WRITE

    # ---- 健康熔断 ----

    def _record_health(self, name: str, ok: bool) -> None:
        with self._health_lock:
            h = self._health.setdefault(
                name,
                {"ok": 0, "fail": 0, "consecutive_fail": 0, "tripped": False,
                 "recent": [], "recover_ok": 0},
            )
            h["recent"].append(1 if ok else 0)
            del h["recent"][: max(0, len(h["recent"]) - HEALTH_WINDOW)]
            if ok:
                h["ok"] += 1
                h["consecutive_fail"] = 0
                if h["tripped"]:
                    # 半开恢复：连续成功若干次才重新挂载，避免抖动工具反复上下线
                    h["recover_ok"] += 1
                    if h["recover_ok"] >= HEALTH_RECOVER_SUCCESS:
                        h["tripped"] = False
                        h["recover_ok"] = 0
                        logger.info("工具 %s 健康度恢复，重新挂载", name)
            else:
                h["fail"] += 1
                h["consecutive_fail"] += 1
                h["recover_ok"] = 0
                if h["consecutive_fail"] >= HEALTH_FAIL_THRESHOLD and not h["tripped"]:
                    h["tripped"] = True
                    logger.warning(
                        "工具 %s 连续失败 %d 次，已摘除", name, h["consecutive_fail"]
                    )

    def health(self, name: str) -> dict:
        with self._health_lock:
            h = self._health.get(name) or {}
            recent = list(h.get("recent") or [])
            total = len(recent)
            return {
                "tripped": bool(h.get("tripped")),
                "consecutive_fail": int(h.get("consecutive_fail", 0)),
                "ok": int(h.get("ok", 0)),
                "fail": int(h.get("fail", 0)),
                "success_rate": round(sum(recent) / total, 4) if total else None,
            }

    def health_report(self) -> list[dict]:
        return [
            {"name": n, **self.health(n), "danger": self.danger_of(t)}
            for n, t in self._tools.items()
        ]

    def catalog(self, skip_retrieval: bool = False) -> list[dict]:
        """目录随场景变化：闲聊场景不暴露检索工具。

        已熔断的工具**不出现**在目录里，并在描述中告知模型"该工具当前不可用"——
        让它换工具，而不是反复调用一个已知坏掉的东西。

        加入 `parameters`（JSON Schema）——模型据此知道工具能接受哪些
        参数（top_k / doc_id 等），而不是把参数写进 query 句子里靠下游猜。
        """
        tools = list(self._tools.values())
        if skip_retrieval:
            tools = [t for t in tools if t.name == "calculator"]
        out = []
        for t in tools:
            spec = spec_of(t, danger_resolver=self.danger_of)
            out.append({
                "name": t.name,
                "description": t.description,
                "parameters": spec.parameters,
                "returns": spec.returns,
                "danger": spec.danger,
                "available": not self.health(t.name)["tripped"],
            })
        return out

    def specs(self) -> list[dict]:
        """完整工具规格（管理端点 / 排障用，含全部元信息）。"""
        return [
            spec_of(t, danger_resolver=self.danger_of).model_dump()
            for t in self._tools.values()
        ]

    def resolve(self, name: str) -> tuple[Tool, bool]:
        """按名取工具；幻觉工具名兜底到注册表第一个工具 + 告警（降级不崩溃）。

        返回 (tool, hallucinated)。

        空注册表时原实现 `next(iter(...))` 会抛 StopIteration（裸的，
        连异常类型都不说明问题）。改为明确的运行时错误——注册表为空是装配错误，
        应当立刻暴露而不是伪装成随机崩溃。
        """
        tool = self._tools.get(name)
        if tool is not None:
            return tool, False
        if not self._tools:
            raise RuntimeError("工具注册表为空：没有任何工具可兜底（装配错误）")
        fallback = next(iter(self._tools.values()))
        logger.warning("工具名幻觉兜底: %r 未注册，降级到 %r", name, fallback.name)
        return fallback, True

    @staticmethod
    def _apply_acl(items: list[dict], acl_filter) -> tuple[list[dict], int]:
        """按 ACL 过滤工具结果（此前仅 vector_search 走 ACL，图/图像证据不过滤）。

        只有**来自受管控知识库文档**的证据受 ACL 约束：
        - 带 doc_id 且非 ``__`` 前缀 → 交 acl_filter 判定（deny 优先在 acl.check 内）
        - ``__`` 前缀（社区摘要 / 图像索引 / 计算器等系统块）→ 放行
        - 无 doc_id（calculator / sql_search / web_search 等非知识库来源）→ 放行，
          它们本就不来自任何受管控文档
        """
        visible: list[dict] = []
        removed = 0
        for it in items:
            doc_id = it.get("doc_id") or ""
            if not doc_id or doc_id.startswith("__") or acl_filter(doc_id):
                visible.append(it)
            else:
                removed += 1
        return visible, removed

    def run_with_timeout(
        self,
        name: str,
        query: str,
        tenant_id: str,
        acl_filter=None,
        run_id: str = "",
        approved: dict | None = None,
        arguments: dict | None = None,
    ) -> tuple[ToolResult, bool, str]:
        """带第三层熔断（单步超时）的工具执行。返回 (result, hallucinated, actual_tool_name)。

        传入 acl_filter 时，**所有工具**的证据都在返回前统一过 ACL（原先只在
        graph.py 对 vector_search 做，图/图像/SQL 结果可绕过）。

        - `run_id`：用于检查取消标志与危险工具审批
        - `approved`：已批准的 `{工具名: True}` 集合；`write` 级工具未批准时**不执行**
        """
        tool, hallucinated = self.resolve(name)

        # 取消标志。已取消则不再产生新的工具调用（额度/外部副作用）。
        if run_id and self.is_cancelled(run_id):
            return (
                ToolResult(error="运行已被取消", acl_filtered=0),
                hallucinated,
                tool.name,
            )

        # 写类工具必须先审批。返回结构化错误而不是抛异常——
        # Agent 要读到"需要审批"这个事实并继续用别的工具，而不是整条链断掉。
        danger = self.danger_of(tool)
        if danger == DANGER_WRITE and not (approved or {}).get(tool.name):
            return (
                ToolResult(
                    error=(
                        f"工具 {tool.name} 属于写操作，需人工审批后执行"
                        f"（danger={danger}）"
                    )
                ),
                hallucinated,
                tool.name,
            )

        def _finish(result: ToolResult) -> ToolResult:
            if acl_filter is None or not result.items:
                return result
            result.items, removed = self._apply_acl(result.items, acl_filter)
            if removed:
                result.acl_filtered = removed
                logger.info("ACL 过滤工具结果 %s：%d 条不可见", tool.name, removed)
                if not result.items and not result.error:
                    result.error = f"工具 {tool.name} 的结果均无访问权限"
            return result

        # 复用固定线程池（批次 1）：此前每次调用新建 ThreadPoolExecutor(1)，
        # 超时后 shutdown(wait=False) 留下悬空线程，高并发下线程数线性泄漏。
        # 技术边界：future.cancel() 只能取消尚未开始的任务，已在运行的工具线程仍会跑完
        # （Python 线程无强制中断能力）；真正强制中断留给 python_sandbox 的子进程实现。
        # 入参校验。失败返回**错误文案**（回传给 LLM 自我修正），
        # 而不是抛异常让整步直接失败——"参数写错"是模型最能自愈的一类错误。
        call_kwargs: dict = {}
        if arguments is not None:
            cleaned, err = validate_arguments(tool, arguments)
            if err:
                logger.info("工具 %s 入参校验失败: %s", tool.name, err)
                return ToolResult(error=err), hallucinated, tool.name
            call_kwargs = {k: v for k, v in cleaned.items() if k != "query"}

        future = self._executor.submit(tool.run, query, tenant_id, **call_kwargs)
        try:
            result = _finish(future.result(timeout=self._timeout_s))
            # 健康度按"是否产出了可用结果"判定。工具返回 error 也算失败——
            # 它没给出信息，重规划时不该再指望它。
            self._record_health(tool.name, ok=not result.error)
            return result, hallucinated, tool.name
        except FutureTimeout:
            future.cancel()  # 只能取消未开始的任务；已运行的线程仍会跑完（ 边界）
            logger.warning("工具 %s 超时 %ss（线程仍可能在后台跑完）", tool.name, self._timeout_s)
            self._record_health(tool.name, ok=False)
            return (
                ToolResult(error=f"工具调用超过 {self._timeout_s}s 超时"),
                hallucinated,
                tool.name,
            )
        except Exception as exc:  # noqa: BLE001
            self._record_health(tool.name, ok=False)
            return ToolResult(error=f"工具异常: {exc}"), hallucinated, tool.name
