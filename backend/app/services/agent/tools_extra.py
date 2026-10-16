"""工具集扩展（TASK-108）。

补三个"当前最大盲区"的工具。**刻意不实现 `http_request` 与 `python_sandbox`**：
- 前者是 SSRF 与提示注入的高危面（需先有内网地址黑名单与出站白名单）；
- 后者要么在主进程 eval（不可接受），要么需要子进程 + 资源限制的独立实现。
这两个留给专门一轮，而不是在这里凑数——**半成品的安全工具比没有更危险**。

实现的三个：

| 工具 | 补的盲区 |
| --- | --- |
| `list_documents` | Agent 不知道"库里有什么"，只能靠检索碰运气 |
| `sql_schema` | `sql_search` 完全依赖 LLM 猜表结构 |
| `datetime_calc` | 「去年同期」「三个工作日后」这类日期推理无可依赖 |
"""
import logging
import re
import sqlite3
import time
from datetime import date, timedelta

from app.services.agent.tools import TOOL_RESULT_CAP, ToolResult

logger = logging.getLogger(__name__)


class ListDocumentsTool:
    """浏览知识库目录。

    这是当前最大的盲区：Agent 只能"检索"，无法回答"库里有哪些文档"，
    所以「上传的财报里有哪些指标」这类问题只能靠反复检索猜。
    """

    name = "list_documents"
    description = (
        "列出知识库中的**文档清单**（只有文件名/块数/状态，不含正文内容）。"
        "仅适用：'库里有哪些文档'、'有没有关于X的资料'这类**问有哪些资料**的问题。"
        "⚠️ 问**具体内容**（如'X 对 Y 有什么影响'）请用 vector_search，不要用本工具——"
        "它不返回正文，无法回答内容型问题。"
    )
    returns = "文档清单（文件名、块数、状态、版本、上传时间）"
    danger = "read_only"

    def __init__(self, knowledge, max_rows: int = 20):
        self._knowledge = knowledge
        self._max_rows = max(1, max_rows)

    @staticmethod
    def _field(doc, *names, default=""):
        """按字段名取值，**同时支持对象与 dict**。

        ⚠️ 这里必须兼容两种形态：`KnowledgeService.list_files()` 返回的是
        `DocumentRecord`（pydantic 模型，用属性访问），而单元测试的替身常用 dict。
        此前只写 `doc.get("filename")` → 线上每次调用都抛
        `'DocumentRecord' object has no attribute 'get'`，工具恒定失败；
        更糟的是 Planner 会反复挑这个"看起来很对口"的工具（问"列出知识库中…的
        文档"时几乎必选它），把迭代次数耗尽直达熔断，最终表现为**本可回答的问题
        被判拒答**。该缺陷由浏览器 E2E 发现（单元测试的替身形态与真实不符而漏过）。
        """
        for n in names:
            if isinstance(doc, dict):
                v = doc.get(n)
            else:
                v = getattr(doc, n, None)
            if v not in (None, ""):
                return v
        return default

    def run(self, query: str, tenant_id: str) -> ToolResult:
        started = time.monotonic()
        try:
            docs = list(self._knowledge.list_files() or [])
        except Exception as exc:  # noqa: BLE001
            return ToolResult(error=f"文档清单读取失败: {exc}")

        def name_of(d) -> str:
            return str(self._field(d, "filename", "name"))

        def line_of(d) -> str:
            return (
                f"{name_of(d)}｜块数：{self._field(d, 'chunk_count', default=0)}"
                f"｜状态：{self._field(d, 'status', default='unknown')}"
                f"｜版本：v{self._field(d, 'version', default=1)}"
                f"｜上传：{self._field(d, 'uploaded_at', 'updated_at')}"
            )

        keyword = (query or "").strip().lower()
        rows: list[dict] = []
        if keyword:
            # 关键词命中优先；但**不能只按文件名匹配**——"关于通货膨胀的文档"里
            # 关键词是主题而非文件名，只匹配文件名会全军覆没并回退全量，
            # 于是模型看不到"哪些文档与问题相关"这一关键判断依据。
            for d in docs:
                if keyword in name_of(d).lower():
                    rows.append({"content": line_of(d), "source": name_of(d), "score": 1.0})
        if not rows:
            # 无命中（或未给关键词）→ 回退全量清单，让模型知道库里到底有什么
            for d in docs[: self._max_rows]:
                rows.append({
                    "content": line_of(d),
                    "source": name_of(d),
                    "score": 0.5,
                })
        if not rows:
            return ToolResult(
                items=[],
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error="" if docs is not None else "文档清单为空",
            )
        return ToolResult(
            items=rows[: self._max_rows],
            elapsed_ms=int((time.monotonic() - started) * 1000),
            truncated_from=len(docs),
        )


class SqlSchemaTool:
    """返回业务库表结构与样例行。

    `sql_search` 之前完全依赖 LLM 猜表名与字段名——猜错就是一次失败的工具调用。
    有 schema 之后模型可以"先看表再写 SQL"。

    安全：**只返回含租户隔离列的表**（与 TASK-143 的白名单口径一致），
    样例行也只取当前租户——否则这个工具本身就成了跨租户数据泄漏通道。
    """

    name = "sql_schema"
    description = (
        "查看业务库的表结构与样例行（适用：需要写 SQL 但不确定表名/字段名时）。"
        "返回可用表清单及其列定义。"
    )
    danger = "read_only"

    def __init__(self, dsn: str, tenant_column: str | None = None, sample_rows: int = 2):
        self._dsn = dsn
        self._tenant_column = tenant_column or None
        self._sample_rows = max(0, sample_rows)

    def run(self, query: str, tenant_id: str) -> ToolResult:
        started = time.monotonic()
        try:
            conn = sqlite3.connect(f"file:{self._dsn}?mode=ro", uri=True)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(error=f"业务库不可用: {exc}")
        try:
            conn.row_factory = sqlite3.Row
            tables = [
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            keyword = (query or "").strip().lower()
            items: list[dict] = []
            for tname in tables:
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tname})")]
                # 与 TASK-143 同一白名单口径：无租户列的表对 Agent 不可见
                if self._tenant_column and self._tenant_column not in cols:
                    continue
                if keyword and keyword not in tname.lower() and not any(
                    keyword in c.lower() for c in cols
                ):
                    continue
                lines = [f"表 {tname}（列：{', '.join(cols)}）"]
                if self._sample_rows:
                    sql = f"SELECT * FROM {tname}"
                    params: tuple = ()
                    if self._tenant_column:
                        sql += f" WHERE {self._tenant_column} = ?"
                        params = (tenant_id,)
                    sql += f" LIMIT {self._sample_rows}"
                    for row in conn.execute(sql, params):
                        pairs = ", ".join(
                            f"{k}={row[k]}" for k in row.keys()
                        )
                        lines.append(f"  样例：{pairs}")
                items.append({"content": "\n".join(lines), "source": tname, "score": 1.0})
        finally:
            conn.close()
        return ToolResult(
            items=items[:TOOL_RESULT_CAP],
            elapsed_ms=int((time.monotonic() - started) * 1000),
            truncated_from=len(items),
        )


# 「N 天/周/月/年后」「N 个工作日后」——中文数字也支持
_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_REL_RE = re.compile(
    r"(?P<n>\d+|[一两二三四五六七八九十])\s*个?\s*(?P<unit>天|日|周|星期|月|年|工作日)"
    r"\s*(?P<dir>后|前|之后|之前)?"
)


class DateTimeCalcTool:
    """日期推理（比 calculator 更贴合 RAG 场景）。

    「去年同期的销售数据」「三个工作日之后」这类问题需要的不是算术，
    而是**日历**推理（月长不等、周末跳过）。这些用 LLM 直接算错误率很高。
    """

    name = "datetime_calc"
    description = (
        "日期计算（适用：'N 天后/前是什么日期'、'去年同期'、'N 个工作日后'、"
        "'两个日期相差多少天'）。不要用它做数值算术（那用 calculator）。"
    )
    danger = "safe"

    def __init__(self, today: date | None = None):
        self._today = today  # 便于测试注入固定"今天"

    def _base(self) -> date:
        return self._today or date.today()

    def run(self, query: str, tenant_id: str) -> ToolResult:
        started = time.monotonic()
        base = self._base()
        q = query or ""

        # 「相差多少天」：需要两个日期
        dates = re.findall(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", q)
        if len(dates) >= 2 and ("差" in q or "间隔" in q or "多少天" in q):
            try:
                d1 = date(int(dates[0][0]), int(dates[0][1]), int(dates[0][2]))
                d2 = date(int(dates[1][0]), int(dates[1][1]), int(dates[1][2]))
            except ValueError as exc:
                return ToolResult(error=f"日期非法: {exc}")
            days = abs((d2 - d1).days)
            return self._ok(started, f"{d1.isoformat()} 与 {d2.isoformat()} 相差 {days} 天")

        # 「去年同期」
        if "同期" in q or "去年" in q or "今年" in q:
            target = base.replace(year=base.year - 1)
            return self._ok(
                started,
                f"今天 {base.isoformat()}；去年同期为 {target.isoformat()}"
                f"（{-1 if '去' in q else 0} 年）",
            )

        # 相对偏移
        m = _REL_RE.search(q)
        if not m:
            return ToolResult(
                error="未能识别日期表达式（支持：N天后/前、N个工作日后、去年同期、两日期相差天数）"
            )
        raw_n = m.group("n")
        n = int(raw_n) if raw_n.isdigit() else _CN_NUM.get(raw_n, 1)
        unit = m.group("unit")
        back = m.group("dir") in ("前", "之前")
        sign = -1 if back else 1

        if unit == "工作日":
            target = self._add_workdays(base, sign * n)
            return self._ok(
                started,
                f"{base.isoformat()} 起 {n} 个工作日{'前' if back else '后'}是 "
                f"{target.isoformat()}（已跳过周末）",
            )
        if unit in ("天", "日"):
            target = base + timedelta(days=sign * n)
        elif unit in ("周", "星期"):
            target = base + timedelta(weeks=sign * n)
        elif unit == "月":
            target = self._add_months(base, sign * n)
        else:  # 年
            target = self._add_months(base, sign * n * 12)
        return self._ok(started, f"{base.isoformat()} {n}{unit}{'前' if back else '后'}是 {target.isoformat()}")

    @staticmethod
    def _ok(started: float, text: str) -> ToolResult:
        return ToolResult(
            items=[{"content": text, "source": "datetime", "score": 1.0}],
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    @staticmethod
    def _add_workdays(base: date, n: int) -> date:
        step = 1 if n >= 0 else -1
        remaining = abs(n)
        cur = base
        while remaining > 0:
            cur += timedelta(days=step)
            if cur.weekday() < 5:  # 周一~周五
                remaining -= 1
        return cur

    @staticmethod
    def _add_months(base: date, months: int) -> date:
        """月份加减（处理月末：1/31 + 1 月 → 2/28，而不是抛异常）。"""
        total = base.year * 12 + (base.month - 1) + months
        year, month = divmod(total, 12)
        month += 1
        # 目标月没有这一天时取该月最后一天
        day = base.day
        while day > 0:
            try:
                return date(year, month, day)
            except ValueError:
                day -= 1
        return date(year, month, 1)
