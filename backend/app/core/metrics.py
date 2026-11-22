"""可观测设施：Prometheus 指标 + 进程内聚合。

- Prometheus 指标经 /metrics 端点暴露（运营侧拉取）；
- 进程内聚合（MetricsStore）支撑 /admin/metrics/* JSON 端点（监控大盘首屏）；
- label 纪律：tenant 高基数、status 低基数，绝不把 request 内容当 label（会爆炸）；
- 四段耗时分开上报：只报总耗时会让"召回慢/融合慢/重排慢"无从排查。
"""
import threading
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from app.core.envelope import kind_of


class PromMetrics:
    """Prometheus 指标集（每个 app 实例独立 Registry，测试可反复建 app）。"""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests_total = Counter(
            "rag_requests_total", "请求总数（按状态分组）", ["status"], registry=self.registry
        )
        self.latency_seconds = Histogram(
            "rag_latency_seconds",
            "端到端延迟",
            # 原 label 为 ["tenant"]，是多租户下的高基数反模式——
            # 租户数随客户线性增长会让时间序列数量同步膨胀，最终打爆监控存储。
            # 租户维度改由**日志**承载（结构化日志的 tenant_id 字段），
            # 指标侧只保留低基数标签；按租户的 P95 应由日志后端聚合而非 Prometheus。
            ["path"],  # ask / ask_stream
            # 桶上限必须覆盖**业务真实时长**：SSE 问答可跑 60s+，
            # 桶止于 10s 时 30~60s 的请求全部落进 +Inf 桶，
            # P95/P99 被系统性低估（X-16）。
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0),
            registry=self.registry,
        )
        self.cache_total = Counter(
            "rag_cache_total", "缓存命中/未命中", ["result"], registry=self.registry
        )
        self.retrieval_top_score = Histogram(
            "rag_retrieval_top_score",
            "Top-1 融合分数分布",
            # 桶上限对齐 CRAG 阈值口径（0.14 / 0.08）：桶止于 0.1 时
            # 所有"合格分数"都落进最后一桶，分布完全看不出差别（X-16）。
            buckets=(0.005, 0.01, 0.0164, 0.02, 0.03, 0.05, 0.08, 0.1, 0.14, 0.2, 0.3, 0.5),
            registry=self.registry,
        )
        self.llm_calls_total = Counter(
            "rag_llm_calls_total", "LLM 调用次数", registry=self.registry
        )
        # HTTP 访问指标（X-16）：此前只有 /ask 的业务指标，
        # 管理面与探针的流量完全不可见——"哪个端点在刷屏"无从查起。
        # route 用**模板**（/knowledge/{doc_id}）而非原始路径，避免高基数。
        self.http_requests_total = Counter(
            "kquery_http_requests_total", "HTTP 请求总数（按路由模板与状态分组）",
            ["method", "route", "status"], registry=self.registry,
        )
        self.http_latency_seconds = Histogram(
            "kquery_http_latency_seconds", "HTTP 端到端延迟（按路由模板）",
            ["method", "route"],
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
            registry=self.registry,
        )
        self.retrieval_stage_ms = Histogram(
            "rag_retrieval_stage_ms",
            "检索分阶段耗时",
            ["stage"],  # keyword / vector / fusion / rerank
            buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000),
            registry=self.registry,
        )

        # ---- Agent 路径----
        # 此前 6 个指标全部只在 pipeline.py 上报，Agent 与多智能体路径等于裸奔：
        # 成功率、工具失败率、熔断率、迭代轮数在监控大盘上完全看不到。
        # 标签一律低基数：status / mode / tool / result / action / reason 都是有限枚举，
        # **不放 tenant 或 user**（高基数，见 latency_seconds 的注释）。
        self.agent_runs_total = Counter(
            "rag_agent_runs_total", "Agent 运行总数（按终态分组）",
            ["status", "mode"], registry=self.registry,
        )
        self.agent_iterations = Histogram(
            "rag_agent_iterations", "Agent 迭代轮数分布（验收指标：平均 ≤ 3）",
            ["mode"],
            buckets=(1, 2, 3, 4, 6, 8, 12, 20),
            registry=self.registry,
        )
        self.agent_tool_calls_total = Counter(
            "rag_agent_tool_calls_total", "Agent 工具调用（按工具与结果分组）",
            ["tool", "result"],  # result: ok / empty / error / timeout / skipped
            registry=self.registry,
        )
        # 失败归因（新增指标，**不改**既有标签集）：
        # 给 agent_tool_calls_total 加 error_class 标签会让历史序列整体改名、
        # 看板与告警一次性全断；这里用独立计数器承载"失败原因分布"，
        # 标签仍是有限枚举（timeout / param_invalid / tripped / cancelled /
        # approval_required / permission / exception / upstream_error），
        # 不放 tenant/user 等高基数维度。
        self.agent_tool_failures_total = Counter(
            "rag_agent_tool_failures_total", "Agent 工具失败（按工具与失败分类）",
            ["tool", "error_class"],
            registry=self.registry,
        )
        self.agent_tool_duration_seconds = Histogram(
            "rag_agent_tool_duration_seconds", "单次工具调用耗时",
            ["tool"],
            buckets=(0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30),
            registry=self.registry,
        )
        # 这个指标是  的验收口：此前 replan 分支不可达，熔断率恒为 0，
        # 监控上看"一切正常"实际是分支从未执行。
        self.agent_fuse_total = Counter(
            "rag_agent_fuse_total", "Agent 熔断次数（按原因）",
            ["reason"],  # iteration_limit / no_progress / loop_detected / budget_*
            registry=self.registry,
        )
        self.agent_next_action_total = Counter(
            "rag_agent_next_action_total", "反思决策分布（验收反思是否真在起作用）",
            ["action"],  # answer / continue / replan
            registry=self.registry,
        )
        self.agent_tokens_total = Counter(
            "rag_agent_tokens_total", "Agent 路径 token 用量（按阶段）",
            ["phase"],  # think / answer / reflect
            registry=self.registry,
        )

        # ---- 并发水位（批次 2）：背压与降级是否生效，只能靠这些指标判断 ----
        self.inflight_requests = Gauge(
            "kquery_inflight_requests", "在飞问答请求数", registry=self.registry
        )
        self.queue_depth = Gauge(
            "kquery_queue_depth", "问答排队深度", registry=self.registry
        )
        self.upstream_in_use = Gauge(
            "kquery_upstream_in_use", "上游许可占用", ["upstream"], registry=self.registry
        )
        # 分母（`C-07`）：只有 in_use 时外部算不出「是否打满」——
        # 占用率 = in_use / max_permits，容量调参与降级判据都要它。
        self.upstream_permits = Gauge(
            "kquery_upstream_permits", "上游许可总数（占用率的分母）", ["upstream"],
            registry=self.registry,
        )
        # 上游耗时 P95（`C-10`）：降级判据的数据源（毫秒）。
        self.upstream_latency_p95 = Gauge(
            "kquery_upstream_latency_p95_ms", "上游调用耗时 P95（毫秒）", ["upstream"],
            registry=self.registry,
        )
        # 图存储锁等待 / 持有耗时（`C-08`）：判定「图写入是不是瓶颈」的唯一依据。
        # 判据：锁等待 P95 > 锁持有 P95 的 1/3 → 图写入成为瓶颈（触发 `C-09`）。
        # 标签只按存储类型（sqlite / memory）——基数只有 2，可控。
        self.graph_lock_wait_seconds = Histogram(
            "kquery_graph_lock_wait_seconds", "图存储锁等待耗时（秒）", ["store"],
            registry=self.registry,
        )
        self.graph_lock_hold_seconds = Histogram(
            "kquery_graph_lock_hold_seconds", "图存储锁持有耗时（秒）", ["store"],
            registry=self.registry,
        )
        # 模型路由降级计数（`LM-01`）：**独立指标**，不给既有的 LLM 调用计数
        # 加模型标签（高基数纪律）。reason ∈ {timeout, rate_limited, server_error}。
        self.llm_degrade_total = Counter(
            "kquery_llm_degrade_total", "模型路由降级次数（按原因）", ["reason"],
            registry=self.registry,
        )
        # 历史摘要压缩（`ST-02`）：压缩次数与省下的 token。
        # `saved_tokens` 只在真压缩（llm / truncate）时累计——跳过（skipped）不算。
        self.history_compactions = Counter(
            "kquery_history_compactions", "历史摘要压缩次数（按方式）", ["method"],
            registry=self.registry,
        )
        self.history_compaction_saved_tokens = Counter(
            "kquery_history_compaction_saved_tokens", "历史摘要压缩节省的 token（按方式）",
            ["method"], registry=self.registry,
        )
        self.upstream_circuit_open = Gauge(
            "kquery_upstream_circuit_open", "上游熔断状态（1=open）", ["upstream"],
            registry=self.registry,
        )
        self.shed_total = Counter(
            "kquery_shed_total", "背压/降级拒绝数（按原因）", ["reason"], registry=self.registry
        )
        # ---- 错误指标（`A-09`）----
        # 此前**没有任何按错误码聚合的指标**：出问题时只能翻日志，
        # 而日志按 trace_id 组织、按码聚合这件事没人做得了。
        # 标签只有 code（有限枚举）与 kind（6 类码段映射）——**不放 trace_id /
        # 路径 / 用户 / 租户**（高基数，见 latency_seconds 的注释）。
        #
        # 注意：44xx~49xx 是**降级告警**（warn 信封），它们计入本指标时表示
        # 「发生过降级」而非「请求失败」。看板里要区分二者，看 `status` 标签
        # 或直接按码段过滤。
        self.errors_total = Counter(
            "kquery_errors_total", "错误 / 降级告警计数（按错误码与类别）",
            ["code", "kind"], registry=self.registry,
        )
        self._stats_provider = None

    def record_error(self, code: int) -> None:
        """记录一次错误 / 降级告警。

        **唯一计数入口**：所有错误响应都从这里过，避免「每个抛错点各数一次」
        导致的漏计与重复计数。`kind` 由码段映射得出，调用方不必自己分类。
        """
        value = int(code)
        self.errors_total.labels(code=str(value), kind=kind_of(value)).inc()

    def set_stats_provider(self, fn) -> None:
        """装配期注入水位采集函数（render 时调用，避免定时线程）。"""
        self._stats_provider = fn

    def render(self) -> bytes:
        if self._stats_provider is not None:
            try:
                self._stats_provider(self)
            except Exception:  # noqa: BLE001 - 指标采集失败不得影响抓取
                pass
        return generate_latest(self.registry)


@dataclass
class _MinuteBucket:
    ok: int = 0
    error: int = 0
    cache_hit: int = 0
    cache_miss: int = 0
    llm_calls: int = 0
    latencies: list[float] = field(default_factory=list)


class MetricsStore:
    """进程内时序聚合：为 /admin/metrics/* 提供 summary / timeseries / top-queries。

    生产可替换为 Prometheus 查询API；此处保证单实例下看板开箱可用。
    """

    def __init__(self, retention_minutes: int = 60 * 24) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[int, _MinuteBucket] = defaultdict(_MinuteBucket)
        self._retention = retention_minutes
        # 键改为**问题哈希**而非原文。
        # 进程内指标是长期驻留的聚合结构，把用户原话（可能含姓名、工号、病情等）
        # 明文留在内存里既违反最小化原则，也会在 /metrics 或排障导出时外泄。
        # 分析"哪些问题高频"用哈希足够（同一问题哈希一致，可计数、可聚合）。
        # top queries **有界**：此前是无界 dict —— 每个不同问题占一条，
        # 长跑进程里只增不减（内存泄漏 + 排序开销随问题数线性增长）。
        # 改为固定容量 LRU：只保留最近/最热的 N 个问题指纹。
        self._top_queries: "OrderedDict[str, dict]" = OrderedDict()
        self._top_queries_capacity = 500
        self._agent = {
            "runs": 0,
            "iteration_sum": 0,
            "fuse_count": 0,  # 熔断触发次数（先于答案质量下降的预警信号）
            "tool_calls": {},  # {tool_name: count}
            # `MP-01`：工具失败计数——「工具失败率」是工具可用性的**先行指标**：
            # 工具开始大面积失败时，答案质量还没掉，但已经不该继续用这条链路了。
            "tool_failures": 0,
            "tool_error_names": {},  # {tool_name: 失败次数}
            "abnormal": deque(maxlen=20),  # 异常执行列表（熔断/工具失败）
        }
        self._quality = {
            "total": 0, "answered": 0, "refused": 0, "parametric": 0,
            "confidence_sum": 0.0, "confidence_n": 0,
            "regenerated": 0, "branches": {},
        }
        # `ST-01` 阶段 A：上下文构成聚合（五个来源的计数 / token + 丢弃原因 Top）。
        # **有界**：来源是固定 5 个；丢弃原因用固定容量 LRU（不存用户原文）。
        self._context = {
            "requests": 0,
            "sources": {},            # {source: {"count": n, "tokens": n}}
            "total_tokens": 0,
            "dropped": OrderedDict(),  # {reason: count}（LRU，容量 50）
        }
        self._context_dropped_capacity = 50
        # 批次 1：latency 采样。此前每请求 append 一条到分钟桶，500 QPS × 24h
        # 会在进程内驻留数百万个 float（数百 MB 且只增不删直到桶过期）。
        # 改为计数 + 每 10 个留 1 个 + 每桶上限 500 条，分位数精度损失可控。
        self._latency_seen = 0

    @staticmethod
    def _minute(ts: float) -> int:
        return int(ts // 60)

    def record_history_compaction(self, method: str, saved_tokens: int = 0) -> None:
        """记录一次历史摘要压缩（`ST-02`）。

        `method` ∈ {llm, truncate_fallback, skipped}——**低基数**（3 个取值）。
        `saved_tokens` 只在真压缩时 > 0（跳过不省 token）。
        """
        try:
            self.history_compactions.labels(method=method).inc()
            if saved_tokens > 0:
                self.history_compaction_saved_tokens.labels(method=method).inc(saved_tokens)
        except Exception:  # noqa: BLE001 - 指标失败不得影响问答
            pass

    def record_request(
        self,
        query: str,
        ok: bool,
        latency_ms: float,
        from_cache: bool,
        llm_called: bool,
    ) -> None:
        now = time.time()
        minute = self._minute(now)
        with self._lock:
            bucket = self._buckets[minute]
            if ok:
                bucket.ok += 1
            else:
                bucket.error += 1
            self._latency_seen += 1
            if self._latency_seen % 10 == 1 and len(bucket.latencies) < 500:
                bucket.latencies.append(latency_ms)
            if from_cache:
                bucket.cache_hit += 1
            else:
                bucket.cache_miss += 1
            if llm_called:
                bucket.llm_calls += 1
            # 超保留期的桶滚动清理
            cutoff = minute - self._retention
            for key in [k for k in self._buckets if k < cutoff]:
                del self._buckets[key]
            # top queries（只存哈希，不存用户原话——见 _top_queries 初始化处）
            fingerprint = self._query_fingerprint(query)
            tq = self._top_queries.get(fingerprint)
            if tq is None:
                tq = {"count": 0, "hits": 0, "latency_sum": 0.0, "errors": 0}
                self._top_queries[fingerprint] = tq
            else:
                # 命中即移到队尾（LRU：热问题留在容量内）
                self._top_queries.move_to_end(fingerprint)
            tq["count"] += 1
            tq["latency_sum"] += latency_ms
            if from_cache:
                tq["hits"] += 1
            if not ok:
                tq["errors"] += 1
            # 超出容量：淘汰最久未命中的（队首）
            while len(self._top_queries) > self._top_queries_capacity:
                self._top_queries.popitem(last=False)

    @staticmethod
    def _percentile(values: list[float], p: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = min(len(ordered) - 1, int(len(ordered) * p))
        return ordered[idx]

    def _range_buckets(self, range_s: int) -> list[tuple[int, _MinuteBucket]]:
        cutoff = self._minute(time.time()) - (range_s // 60 + 1)
        with self._lock:
            return sorted((k, b) for k, b in self._buckets.items() if k >= cutoff)

    def summary(self, range_s: int = 3600) -> dict:
        buckets = self._range_buckets(range_s)
        ok = sum(b.ok for _, b in buckets)
        error = sum(b.error for _, b in buckets)
        hits = sum(b.cache_hit for _, b in buckets)
        miss = sum(b.cache_miss for _, b in buckets)
        llm = sum(b.llm_calls for _, b in buckets)
        latencies = [v for _, b in buckets for v in b.latencies]
        total = ok + error
        minutes = max(len(buckets), 1)
        return {
            "qps": round(total / (minutes * 60), 3),
            "total_requests": total,
            "error_rate": round(error / total, 4) if total else 0.0,
            "p50_latency_ms": round(self._percentile(latencies, 0.5), 1),
            "p95_latency_ms": round(self._percentile(latencies, 0.95), 1),
            "p99_latency_ms": round(self._percentile(latencies, 0.99), 1),
            "cache_hit_rate": round(hits / (hits + miss), 4) if (hits + miss) else 0.0,
            "llm_calls": llm,
        }

    def timeseries(self, metric: str, range_s: int = 3600) -> list[dict]:
        points = []
        for minute, b in self._range_buckets(range_s):
            total = b.ok + b.error
            if metric == "requests":
                value = total
            elif metric == "errors":
                value = b.error
            elif metric == "cache_hit_rate":
                value = round(b.cache_hit / (b.cache_hit + b.cache_miss), 4) if (b.cache_hit + b.cache_miss) else 0.0
            elif metric == "p95_latency_ms":
                value = round(self._percentile(b.latencies, 0.95), 1)
            elif metric == "llm_calls":
                value = b.llm_calls
            else:
                value = total
            points.append({"ts": minute * 60, "value": value})
        return points

    def record_context(self, breakdown: dict) -> None:
        """记录一次请求的上下文构成（`ST-01` 阶段 A）。

        `breakdown` 是 `ContextEnvelope.breakdown()` 的产物：
        `{"sources": {name: {"count", "tokens"}}, "dropped": [{"reason"}], ...}`。
        空字典 → 忽略（未装配上下文的路径，如缓存命中）。
        """
        if not breakdown:
            return
        sources = breakdown.get("sources") or {}
        dropped = breakdown.get("dropped") or []
        with self._lock:
            self._context["requests"] += 1
            self._context["total_tokens"] += int(breakdown.get("total_tokens") or 0)
            for name, info in sources.items():
                row = self._context["sources"].setdefault(name, {"count": 0, "tokens": 0})
                row["count"] += int(info.get("count") or 0)
                row["tokens"] += int(info.get("tokens") or 0)
            for item in dropped:
                reason = item.get("reason") or "unknown"
                self._context["dropped"][reason] = self._context["dropped"].get(reason, 0) + 1
                while len(self._context["dropped"]) > self._context_dropped_capacity:
                    self._context["dropped"].popitem(last=False)

    def context_summary(self) -> dict:
        """上下文构成聚合结果（`/admin/metrics/context` 的数据源）。"""
        with self._lock:
            requests = self._context["requests"]
            sources = {
                name: {
                    "count": row["count"],
                    "tokens": row["tokens"],
                    "avg_count": round(row["count"] / requests, 2) if requests else 0.0,
                    "avg_tokens": round(row["tokens"] / requests, 1) if requests else 0.0,
                }
                for name, row in self._context["sources"].items()
            }
            dropped = sorted(
                ({"reason": k, "count": v} for k, v in self._context["dropped"].items()),
                key=lambda d: d["count"],
                reverse=True,
            )[:10]
            return {
                "requests": requests,
                "avg_total_tokens": (
                    round(self._context["total_tokens"] / requests, 1) if requests else 0.0
                ),
                "sources": sources,
                "dropped_top": dropped,
            }

    @staticmethod
    def _query_fingerprint(query: str) -> str:
        """问题指纹。

        同一问题在任何时刻得到同一个哈希，因此"高频问题"仍然可统计、可排序；
        但聚合结构里不再持有用户原话。原话的追溯需求由审计（同样只存哈希）
        与业务库承担，指标层不该是第二份明文副本。
        """
        import hashlib

        return hashlib.sha256((query or "").strip().encode("utf-8")).hexdigest()[:16]

    def top_queries(self, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = [
                {
                    # 键已是哈希；字段名同步改为 query_hash，避免调用方
                    # 误以为还能拿到原文
                    "query_hash": q,
                    "count": v["count"],
                    "cache_hit_rate": round(v["hits"] / v["count"], 4) if v["count"] else 0.0,
                    "avg_latency_ms": round(v["latency_sum"] / v["count"], 1) if v["count"] else 0.0,
                    "error_rate": round(v["errors"] / v["count"], 4) if v["count"] else 0.0,
                }
                for q, v in self._top_queries.items()
            ]
        return sorted(rows, key=lambda r: r["count"], reverse=True)[:limit]

    # ---- 质量指标（Faithfulness / 拒答率 / 重生成 / CRAG 分支占比）----

    def record_quality(self, verdict: str, confidence: float | None, branch: str | None,
                       regenerated: bool, answer_source: str = "") -> None:
        with self._lock:
            q = self._quality
            q["total"] += 1
            if verdict == "refused":
                q["refused"] += 1
            else:
                q["answered"] += 1
                # 兜底单独计数：拒答率下降要能区分"兜底生效"与"检索退化"（看板不混口径）
                if answer_source == "parametric":
                    q["parametric"] += 1
                if confidence is not None:
                    q["confidence_sum"] += confidence
                    q["confidence_n"] += 1
                if regenerated:
                    q["regenerated"] += 1
            if branch:
                q["branches"][branch] = q["branches"].get(branch, 0) + 1

    def quality_summary(self) -> dict:
        with self._lock:
            q = dict(self._quality)
            q["branches"] = dict(self._quality["branches"])
        total = max(q["total"], 1)
        answered = max(q["answered"], 1)
        branch_total = max(sum(q["branches"].values()), 1)
        avg_confidence = (
            round(q["confidence_sum"] / q["confidence_n"], 4) if q["confidence_n"] else None
        )
        return {
            # 这个值本来是**置信度均值**，却叫 faithfulness（忠实度），
            # 语义不符会让看板读者以为它是 RAGAS 那种"答案是否有据"的指标。
            # 现在用准确的名字，并保留 `faithfulness` 作为**兼容别名**（旧前端/看板
            # 直接读这个键，改名会静默显示为空），标记为 deprecated。
            "avg_confidence": avg_confidence,
            "faithfulness": avg_confidence,  # deprecated：请改用 avg_confidence
            "refusal_rate": round(q["refused"] / total, 4),
            "avg_regeneration": round(q["regenerated"] / answered, 4),
            "crag_branch_ratio": {b: round(c / branch_total, 4) for b, c in q["branches"].items()},
            "answered": q["answered"],
            "refused": q["refused"],
            "parametric": q["parametric"],
            "parametric_rate": round(q["parametric"] / total, 4),
        }

    # ---- Agent 执行指标 ----

    def record_agent_run(
        self,
        iterations: int,
        fused: bool,
        fuse_reason: str,
        tool_calls: list[dict],
        session_id: str = "",
    ) -> None:
        with self._lock:
            a = self._agent
            a["runs"] += 1
            a["iteration_sum"] += iterations
            if fused:
                a["fuse_count"] += 1
                a["abnormal"].appendleft({
                    "ts": time.time(), "session_id": session_id[-12:],
                    "kind": "circuit_breaker", "detail": fuse_reason,
                })
            for tc in tool_calls:
                name = tc.get("tool_name", "unknown")
                a["tool_calls"][name] = a["tool_calls"].get(name, 0) + 1
                # `MP-01`：失败判定 = 有 error 文本（空结果也算失败，见 graph.py 的记录口径）
                if tc.get("error"):
                    a["tool_failures"] += 1
                    a["tool_error_names"][name] = a["tool_error_names"].get(name, 0) + 1
                if tc.get("error"):
                    a["abnormal"].appendleft({
                        "ts": time.time(), "session_id": session_id[-12:],
                        "kind": "tool_error", "detail": f"{name}: {tc['error'][:50]}",
                    })

    def agent_summary(self) -> dict:
        with self._lock:
            a = {
                "runs": self._agent["runs"],
                "iteration_sum": self._agent["iteration_sum"],
                "fuse_count": self._agent["fuse_count"],
                "tool_calls": dict(self._agent["tool_calls"]),
                # `MP-01`：工具失败计数（分母 = 工具调用总数）
                "tool_failures": self._agent["tool_failures"],
                "tool_error_names": dict(self._agent["tool_error_names"]),
                "abnormal": list(self._agent["abnormal"]),
            }
        runs = max(a["runs"], 1)
        tool_total = sum(a["tool_calls"].values())
        failures = a["tool_failures"]
        failure_by_tool = a["tool_error_names"]
        return {
            "runs": a["runs"],
            "avg_iterations": round(a["iteration_sum"] / runs, 2),
            "fuse_rate": round(a["fuse_count"] / runs, 4),
            "tool_distribution": a["tool_calls"],
            # `MP-01`：工具失败率（分母 = 工具调用总数；没有调用时为 0 而不是 None）
            "tool_calls_total": tool_total,
            "tool_failures": failures,
            "tool_failure_rate": round(failures / tool_total, 4) if tool_total else 0.0,
            "tool_failure_distribution": failure_by_tool,
            "abnormal": a["abnormal"],
        }
