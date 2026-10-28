"""RAG 流水线：预处理 → 缓存 → 混合检索 → CRAG 校验 → 生成 → 幻觉自检。

- **校验发生在生成之前**：CRAG 先把证据筛干净（评分/精炼/重检索/拒答），
  模型只在证据合格后才被调用——从流程上排除"看到噪声证据后编造"的主要诱因；
- 拒答是一等公民：HTTP 200 + verdict=refused + refusal_reason（可归因），不走错误码；
- 降级是平行分支而非异常出口：评分器/自检器失效时请求继续走完，
  confidence 标为 None（未知）并如实提示"本次未做证据校验"；
- 受限重生成只一次：二次校验仍有无据声明 → 如实输出并下调置信度，不掩饰。
"""
import logging
import threading
import time
from typing import Callable, Iterator

from app.core.config import Settings
from app.core.envelope import ErrCode
from app.core.metrics import MetricsStore, PromMetrics
from app.domain.schemas import AskResult, Evidence, RetrievalConfig, SelfCheckRecord
from app.core.otel import span
from app.services.cache import SemanticCache, config_digest, make_cache_key
from app.services.context import ContextItem, assemble_context
from app.services.retrieval.filters import RetrievalFilters
from app.services.config_store import RetrievalConfigStore
from app.services.llm import LLM, LLMError
from app.services.retrieval.hybrid import HybridResult, HybridRetriever
from app.services.validation.crag import (
    BRANCH_AMBIGUOUS,
    BRANCH_CORRECT,
    BRANCH_INCORRECT,
    CragRouter,
)
from app.services.validation.history import CRAG_KIND, SELFCHECK_KIND
from app.services.validation.selfcheck import (
    HallucinationChecker,
    compute_confidence,
    estimate_claims,
)

logger = logging.getLogger(__name__)

NO_INFO_ANSWER = "知识库无相关内容"
REFUSAL_TEXT = "知识库中没有足够证据回答该问题"

# 通用知识兜底提示词（doc/analysis/通用知识兜底回答.md §6.2）。
# 红线：
# 1. 首句措辞必须与 NO_INFO_ANSWER 不同——runner 的拒答归一用 startswith(NO_INFO_ANSWER)
#    检测，含该串会被判回拒答；
# 2. 不得注入证据块——低分无关片段会干扰通用知识回答；
# 3. 禁止 [n] 引用标注——兜底回答没有可溯源证据，出现编号会让前端引用跳转指到无关片段。
GENERAL_KNOWLEDGE_PROMPT = """你是企业知识库助手。用户的问题在知识库中没有检索到相关内容。
请基于你自己的通用知识回答这个问题，并遵守：
1. 回答的第一句必须是"以下回答基于大模型通用知识，知识库中未收录相关内容"；
2. 不要编造企业内部数据、制度、人员信息、未公开的数字、具体文档条款；
3. 无法确定的部分如实说明"这一点我不确定"，不要猜测；
4. 不要添加 [数字] 形式的引用标注。

用户问题：{question}"""

# 拒答原因（可归因，三类对应完全不同的产品动作）
REASON_NO_CONTENT = "no_content"  # 知识库无相关内容 → 补知识
REASON_INSUFFICIENT = "insufficient_evidence"  # 证据不足（循环耗尽）→ 换问法
REASON_PERMISSION = "permission_denied"  # 权限不足→ 申请权限

PROMPT_TEMPLATE = """你是知识库问答助手。仅根据下面给出的证据片段回答用户问题，不允许使用证据之外的知识。
若证据不足以回答问题，请直接回答"{no_info}"，不要编造、不要自由发挥。

证据片段：
{evidence_block}
---
用户问题：{question}

回答要求：
1. 答案中的每个事实性陈述后标注引用编号，如 [#1]；
2. 不同证据之间是独立的，不要把不同文档的片段当成同一文档；
3. 答案末尾列出引用片段编号；
4. 证据不足时直接回答"{no_info}"。"""


def build_evidence_block(evidence: list[Evidence]) -> str:
    blocks = []
    prev_doc = None
    for ev in evidence:
        if not ev.used:
            continue
        if prev_doc is not None and ev.doc_id != prev_doc:
            blocks.append("---")
        blocks.append(f"[#{ev.rank}]（来源：{ev.source}）\n{ev.content}")
        prev_doc = ev.doc_id
    return "\n\n".join(blocks) if blocks else "（无证据）"


class RagPipeline:
    def __init__(
        self,
        settings: Settings,
        llm: LLM,
        hybrid: HybridRetriever,
        cache: SemanticCache,
        config_store: RetrievalConfigStore,
        prom: PromMetrics,
        metrics_store: MetricsStore,
        doc_name_resolver: Callable[[str], str] | None = None,
        docstore=None,
        index_version_provider: Callable[[], int] | None = None,
        preprocessor=None,
        analysis_store=None,
        crag_router: CragRouter | None = None,
        self_checker: HallucinationChecker | None = None,
        # ---- 企业护栏在**线性路径**同样生效（此前只有 Agent 路径接入）----
        acl_filter: Callable[[str], bool] | None = None,
        audit=None,  # AuditStore：三事件审计
        scanner=None,  # SensitiveScanner：输出侧扫描
        history_store=None,  # ValidationHistoryStore：CRAG 轮次 / 自检明细落库
        # ACL 全局版本提供者：缓存键维度之一，权限变更后旧缓存失效
        acl_version_provider: Callable[[], int] | None = None,
        quota=None,  # SlidingWindowQuota：token 用量结算，装配期晚于构造故回填
        # 当前主体可见文档清单提供者：把 ACL 下推到召回阶段。
        # 返回 None 表示"无法低成本枚举"→ 自动降级为后过滤。
        visible_docs_provider: Callable[[], set[str] | None] | None = None,
    ):
        self._settings = settings
        self._llm = llm
        self._hybrid = hybrid
        self._cache = cache
        self._config_store = config_store
        self._prom = prom
        self._metrics_store = metrics_store
        self._doc_name_resolver = doc_name_resolver or (lambda doc_id: doc_id)
        self._docstore = docstore
        # ACL 下推所需的两个依赖（装配期回填，见 bind_principal）
        self._acl_service = None
        self._knowledge = None
        self._index_version = index_version_provider or (lambda: 0)
        self._preprocessor = preprocessor
        self._analysis_store = analysis_store
        self._crag = crag_router
        self._self_checker = self_checker
        self._acl_filter = acl_filter
        self._audit = audit
        self._scanner = scanner
        self._history = history_store
        self._acl_version = acl_version_provider or (lambda: 0)
        # 当前主体可见文档清单提供者（装配期回填）。为 None 时只做后过滤。
        self._visible_docs_provider = visible_docs_provider
        # 本轮被上下文组装丢弃的候选项（前端据此解释"为什么没进上下文"）
        self._context_dropped: list[dict] = []
        self._quota = quota
        # 批次 2 single-flight：相同 cache key 的并发请求合并为一次真实计算，
        # 等待者带 deadline 复用结果（bind_principal 的副本共享同一 dict 引用）
        self._inflight: dict[str, threading.Event] = {}
        self._inflight_lock = threading.Lock()

    # ---- 护栏挂载点（同一请求内固定，便于审计三事件复用同一主体）----

    def bind_principal(
        self, tenant_id: str, user_id: str = "anon", principal=None
    ) -> "RagPipeline":
        """绑定认证主体（副本语义，不改共享实例——流水线是单例）。

        同时绑定"可见文档清单提供者"。可见清单是**主体相关**的，
        只能在这里绑定——放在装配期会退化成"所有人共用一份清单"（比后过滤更糟）。
        提供者本身惰性求值，未被检索用到时不会产生枚举开销。
        """
        clone = object.__new__(RagPipeline)
        clone.__dict__.update(self.__dict__)
        clone._tenant_id = tenant_id
        clone._user_id = user_id
        if principal is not None and self._acl_service is not None and self._knowledge is not None:
            acl, knowledge = self._acl_service, self._knowledge

            def _provider() -> set[str] | None:
                try:
                    docs = knowledge.list_files()
                except Exception as exc:  # noqa: BLE001 - 拿不到就不下推
                    logger.debug("文档清单读取失败（不下推 ACL）: %s", exc)
                    return None
                # 与后过滤同源：优先用请求级闭包（含 ACL + KB 两道闸门）枚举，
                # 否则下推清单会含 KB 不可见文档、白占 top-k 名额。
                # 时序依赖：本 provider 惰性求值，首次检索时才执行——
                # 调用方在 bind_principal 之后才赋值 scoped._acl_filter
                # （api/ask.py），故执行时闭包必已就绪；切勿把本 provider 改成立即求值。
                judge = clone._acl_filter
                if judge is not None:
                    if len(docs) > 5000:
                        return None
                    return {d.doc_id for d in docs if judge(d.doc_id)}
                return acl.visible_doc_ids(principal, docs)

            clone._visible_docs_provider = _provider
        else:
            clone._visible_docs_provider = None
        return clone

    def attach_security(self, audit=None, scanner=None, history_store=None) -> None:
        """装配期回填 护栏。

        这些组件在装配顺序上晚于流水线创建（审计库依赖认证配置、扫描器依赖词表），
        因此不能在构造参数里传——否则只能挂到 Agent 路径上，线性路径永远拿不到。
        """
        self._audit = audit
        self._scanner = scanner
        self._history = history_store

    @property
    def _principal_tenant(self) -> str:
        return getattr(self, "_tenant_id", "")

    @property
    def _principal_user(self) -> str:
        return getattr(self, "_user_id", "anon")

    def _log_request(self, trace_id: str, tenant_id: str, question: str) -> None:
        if self._audit and trace_id:
            self._audit.log_request(trace_id, tenant_id, self._principal_user, question)

    def _log_retrieve(self, trace_id: str, tenant_id: str, retrieved: int, visible: int) -> None:
        if self._audit and trace_id:
            self._audit.log_retrieve(trace_id, tenant_id, self._principal_user, retrieved, visible)

    # ---- 缓存键 ----

    def _model_version(self, config: RetrievalConfig) -> str:
        provider = self._settings.resolved_provider
        return (
            f"{provider}:{self._settings.llm_model}"
            f"+{self._settings.embedding_model}+{config.rerank_model}"
        )

    def _settle_tokens(self, tenant_id: str, llm_calls: int) -> dict:
        """把本次链路的 LLM token 用量结算到配额。

        此前 `quota.record_tokens` 在生产代码中零调用，导致 `dependencies.py` 的
        `token_daily` 配额分支永不触发——配额体系实际少了 token 这一个维度。
        配额服务由装配期注入（不设全局单例，与 platform_quota 的处理保持一致的显式依赖）。
        口径说明：LLM 实例只保留"最近一次"usage，故按调用次数线性外推；
        返回值里的 estimated 让调用方能区分真值与估算。
        """
        usage = getattr(self._llm, "last_usage", None)
        if usage is None or self._quota is None:
            return {}
        total = int(getattr(usage, "total", 0) or 0) * max(1, llm_calls)
        if total <= 0:
            return {}
        try:
            self._quota.record_tokens(tenant_id, total)
        except Exception as exc:  # noqa: BLE001 - 结算失败不得影响主流程
            logger.debug("token 用量结算失败（忽略）: %s", exc)
        return {"tokens": total, "estimated": bool(getattr(usage, "estimated", True))}

    def _cache_dims(self, config: RetrievalConfig) -> tuple[str, str]:
        """返回 (config_digest, 维度签名)。同一次请求只算一次，键与签名共用。"""
        digest = config_digest(
            {
                "retrieval": config.model_dump(),
                "chunk_size": self._settings.chunk_size,
                "temperature": self._settings.temperature,
                "index_version": self._index_version(),
                "crag": self._crag is not None,
                # 缓存键必须携带身份与 ACL 版本——缓存命中路径跳过检索与
                # ACL 过滤，否则不同可见性的用户共享缓存、权限撤销后旧缓存仍命中
                "user": self._principal_user,
                "acl_version": self._acl_version(),
            }
        )
        # KQ-04：语义近似命中只按键无法区分主体与版本，必须额外比对维度签名
        return digest, f"{self._model_version(config)}:{digest}"

    def _cache_key(
        self, question: str, tenant_id: str, config: RetrievalConfig, doc_id: str | None = None
    ) -> str:
        digest, _ = self._cache_dims(config)
        # F4 快问：scope 维度进键——快问与全库检索的问题文本可能完全一样，
        # 不区分就会互相命中（跨范围串答案）。
        return make_cache_key(
            tenant_id, question, self._model_version(config), digest, scope=doc_id or ""
        )

    # ---- 缓存读写（45xx 无感降级：缓存故障不得让用户看到错误）----

    def _question_vector(self, question: str) -> list:
        """问题向量（供语义近似命中）。编码失败返回空列表，退回精确匹配。"""
        embedder = getattr(self._cache, "_embeddings", None)
        if embedder is None:
            return []
        try:
            vectors = embedder.embed([question])
            return list(vectors[0]) if vectors else []
        except Exception as exc:  # noqa: BLE001
            logger.warning("缓存问题编码失败，退回精确匹配: %s", exc)
            return []

    def _cache_lookup(
        self, question: str, tenant_id: str, key: str, scope: str = "", signature: str = ""
    ):
        """返回 (entry, hit_kind)。缓存不可用时静默 miss + 上报 45xx 计数。"""
        if not hasattr(self._cache, "lookup"):
            return self._cache.get(key), "exact"
        try:
            return self._cache.lookup(
                question, key, self._question_vector(question),
                tenant_id=tenant_id, scope=scope, signature=signature,
            )
        except TypeError:
            # 旧实现没有 scope / signature 维度：只能退回精确命中
            #（宁可少命中，不可串范围或串主体）
            try:
                return self._cache.lookup(
                    question, key, self._question_vector(question), tenant_id=tenant_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("缓存读取降级（本次不走缓存）: %s", exc)
                self._prom.cache_total.labels(result="error").inc()
                return None, "miss"
        except Exception as exc:  # noqa: BLE001
            # 45xx：无感降级，只上报指标不回传用户
            logger.warning("缓存读取降级（本次不走缓存）: %s", exc)
            self._prom.cache_total.labels(result="error").inc()
            return None, "miss"

    def _cache_store(
        self, key: str, question: str, payload: dict, ttl: float,
        tenant_id: str = "", scope: str = "", signature: str = "",
    ) -> None:
        """写入缓存。优先用支持"问题 + 向量 + 租户 + 范围 + 维度签名"的新签名，旧实现退回三参数。"""
        try:
            self._cache.set(
                key, payload, ttl, question=question,
                vector=self._question_vector(question), tenant_id=tenant_id,
                scope=scope, signature=signature,

            )
            return
        except TypeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._prom.cache_total.labels(result="error").inc()
            logger.warning("缓存写入降级（不影响本次返回）: %s", exc)
            return
        try:
            self._cache.set(key, payload, ttl)
        except Exception as exc:  # noqa: BLE001
            self._prom.cache_total.labels(result="error").inc()
            logger.warning("缓存写入降级（不影响本次返回）: %s", exc)

    # ---- 预处理 ----

    def _degrade_level(self) -> int:
        """降级阶梯水位（批次 4，默认 0=正常）。1=上游熔断，收缩预处理与 CRAG。"""
        if not getattr(self._settings, "degrade_ladder_enabled", False):
            return 0
        try:
            from app.services.upstream import bulkhead

            if bulkhead("rerank").stats().get("state") == "open":
                return 1
        except Exception:  # noqa: BLE001 - 观测失败不降级
            pass
        return 0

    def _preprocess(self, question: str, tenant_id: str) -> tuple[dict | None, list[tuple[str, str]], bool]:
        if self._preprocessor is None:
            return None, [("original", question)], False
        analysis_obj, query_items = self._preprocessor.process(question, tenant_id)
        if self._analysis_store is not None:
            self._analysis_store.save(analysis_obj)
        return analysis_obj.model_dump(), query_items, not analysis_obj.needs_retrieval

    # ---- ACL 过滤（检索后强制校验， 双保险的应用层一侧）----

    def _build_filters(self, tenant_id: str, doc_id: str | None = None):
        """构造检索过滤条件（TASK-142 + F4 快问）。

        只有在**能低成本算出可见清单**时才带上 `visible_doc_ids`：判定是
        "按 ACL 表枚举该主体的可见文档"，文档量很大时这个集合会很大，此时
        `to_milvus_expr` 会自动降级为 tenant 粗过滤（避免超长表达式报错），
        精确性由后过滤保证。

        拿不到清单（未启用 ACL / 无访问器）时返回 None，行为与改造前一致——
        降级而非中断。

        **F4 关键修正**：`doc_id`（快问单文档范围）**必须**独立于 ACL 生效。
        原实现 ACL 未启用时直接 `return None`，于是"只检索这一个文件"的要求
        会被整个丢掉——快问会检索全库，答案里混进无关文档的引用。
        默认部署与测试环境（`AUTH_ENABLED=false`）恰恰都不启用 ACL，
        所以这不是边界情况，而是**主路径**。
        """

        if self._acl_filter is None:
            # 未启用 ACL：不做文档级过滤，但快问的 doc_id 过滤必须保留
            return RetrievalFilters(tenant_id=tenant_id, doc_id=doc_id) if doc_id else None
        if doc_id:
            # 快问：范围已由 doc_id 锁定，**不再叠加可见清单**——
            # 快问文档不在知识库里，可见清单里根本没有它，
            # 两个条件叠加会让快问永远检索不到任何东西。
            # 归属由 QuickAskService 校验（principal.user_id），ACL 不适用于临时文档。
            return RetrievalFilters(tenant_id=tenant_id, doc_id=doc_id)
        visible = None
        provider = self._visible_docs_provider
        if provider is not None:
            try:
                visible = provider()
            except Exception as exc:  # noqa: BLE001 - 清单拿不到就退回后过滤
                logger.debug("可见文档清单获取失败（退回后过滤）: %s", exc)
        return RetrievalFilters(tenant_id=tenant_id, visible_doc_ids=visible, doc_id=doc_id)

    def _probe_denied_content(self, query_items, tenant_id, config, filters) -> bool:
        """探测"存在匹配内容但当前主体不可见"。

        只在**检索结果为空**时调用（冷路径），且只返回布尔值——被探测到的内容
        绝不进入响应、日志或指标，避免把探测本身变成信息泄露通道。

        任何异常都视为"探不到"（返回 False）：宁可把权限拒答误报成"无内容"，
        也不要因探测失败把整个请求变成 500。
        """
        try:
            probe = RetrievalFilters(tenant_id=tenant_id)  # 不带允许清单 = 不过滤
            result = self._hybrid.search_multi(
                query_items, tenant_id, config, rerank_query="", filters=probe
            )
            return bool(result.candidates)
        except Exception as exc:  # noqa: BLE001
            logger.debug("权限探测失败（按无内容处理）: %s", exc)
            return False

    # ---- 请求级缓存（批次 2 / KQ-12）：同一请求内消除重复查询 ----

    def _rcache(self) -> dict:
        """请求级缓存（bind_principal 的副本天然按请求隔离，无需 contextvar）。"""
        cache = getattr(self, "_req_cache", None)
        if cache is None:
            cache = {"parents": {}, "names": {}}
            self._req_cache = cache
        return cache

    def _reset_rcache(self) -> None:
        self._req_cache = None

    def _parent_of(self, parent_id: str):
        cache = self._rcache()["parents"]
        if parent_id not in cache:
            cache[parent_id] = self._docstore.get_parent(parent_id)
        return cache[parent_id]

    def _name_of(self, doc_id: str) -> str:
        cache = self._rcache()["names"]
        if doc_id not in cache:
            cache[doc_id] = self._doc_name_resolver(doc_id)
        return cache[doc_id]

    def _visible_candidates(self, hybrid_result: HybridResult) -> tuple[list, int]:
        """ACL 过滤后的可见候选 + 被过滤条数。

        向量库 filter 只有"完全等于"语义，deny 优先 + 多身份 ACL 必须在应用层做。
        被过滤条目既不进证据，也不参与充分度聚合——否则会虚高置信度。
        """
        acl_filter = self._acl_filter
        if not acl_filter:
            return list(hybrid_result.candidates), 0
        visible, filtered = [], 0
        for cand in hybrid_result.candidates:
            if acl_filter(cand.chunk.doc_id):
                visible.append(cand)
            else:
                filtered += 1
        if filtered:
            logger.info("ACL 检索后过滤：%d 条不可见", filtered)
        return visible, filtered

    # ---- 证据装配 ----

    def _parametric_fallback_events(
        self, question: str, tenant_id: str, trace_id: str, analysis: dict | None,
        hybrid_result, llm_calls: int, acl_filtered: int, crag_payload: dict | None,
        refusal_reason: str,
    ) -> Iterator[tuple[str, dict]]:
        """通用知识兜底（doc/analysis/通用知识兜底回答.md §6.7）。

        CRAG 判定「无相关内容/证据耗尽」时，改由模型自身知识回答：
        - 重生成仍输出拒答话术 → 维持原拒答（兜底失败不硬撑）；
        - 兜底成功 → verdict=answered + answer_source=parametric，不发 refused 事件。
        token 由调用方在 _final 后按终稿切块重放，本方法不直接产 token。
        """
        answer = self._llm.generate(GENERAL_KNOWLEDGE_PROMPT.format(question=question))
        llm_calls += 1
        if answer.strip().startswith(NO_INFO_ANSWER):
            yield "refused", {"reason": refusal_reason, "trace_id": trace_id,
                              "acl_filtered": acl_filtered}
            yield "_final", {
                "answer": REFUSAL_TEXT, "evidence": [], "analysis": analysis,
                "retrieval": hybrid_result.stats if hybrid_result else None,
                "reranked": bool(hybrid_result.reranked) if hybrid_result else False,
                "verdict": "refused", "refusal_reason": refusal_reason,
                "answer_source": "", "confidence": 0.0,
                "crag": crag_payload, "llm_calls": llm_calls,
                "acl_filtered": acl_filtered,
            }
            return
        confidence = round(float(self._settings.parametric_fallback_confidence), 4)
        yield "confidence", {
            "verdict": "answered", "score": confidence,
            "unsupported_claims_count": 0, "regenerated": False,
            "unvalidated": True, "self_check": None, "removed_claims": [],
            "answer_source": "parametric",
        }
        self._log_answer(trace_id, tenant_id, answer, 0)
        yield "_final", {
            "answer": answer, "evidence": [], "analysis": analysis,
            "retrieval": hybrid_result.stats if hybrid_result else None,
            "reranked": bool(hybrid_result.reranked) if hybrid_result else False,
            "verdict": "answered", "refusal_reason": "",
            "answer_source": "parametric", "confidence": confidence,
            "crag": crag_payload, "llm_calls": llm_calls,
            "acl_filtered": acl_filtered,
        }

    def _to_evidence(self, hybrid_result: HybridResult) -> tuple[list[Evidence], int]:
        """装配证据。返回 (证据列表, 因权限被过滤的条数)。

        ACL 在检索后二次校验（）：向量库 filter 只有"完全等于"语义，
        deny 优先 + 多身份 ACL 必须在应用层做。被过滤条目**不进证据**，
        只把数量交给调用方报给前端——不暴露被过滤文档的任何元信息。
        """
        acl_filter = self._acl_filter
        visible, acl_filtered = self._visible_candidates(hybrid_result)

        # 先构造候选（含父块展开），再统一做
        # 去重 → MMR → token 预算裁剪 → 硬截断。
        # 此前父块展开后**不重排不截断**：多个命中子块被替换成同一段父块全文，
        # 同一篇长文可能进提示词 N 次（既浪费预算又挤掉其他文档的证据）。
        candidates: list[ContextItem] = []
        # key → 原始候选（保留 chunk 元信息，避免组装后再反查）
        meta: dict[str, tuple] = {}
        for rank, cand in enumerate(visible, start=1):
            chunk = cand.chunk
            filename = self._name_of(chunk.doc_id)
            score = cand.rerank_score if hybrid_result.reranked else cand.rrf_score
            content = chunk.content
            # 父块展开：用 parent_id 作为去重键，同一父块只保留最高分的那次命中
            dedupe_key = chunk.chunk_id
            if chunk.parent_id and self._docstore is not None:
                parent = self._parent_of(chunk.parent_id)
                if parent is not None:
                    content = parent.content
                    dedupe_key = f"parent:{chunk.parent_id}"
            if dedupe_key in meta:
                continue  # 同一父块已收录：保留先出现（分数更高）的那次
            meta[dedupe_key] = (chunk, cand, f"{filename} · {chunk.source}")
            candidates.append(
                ContextItem(
                    key=dedupe_key,
                    doc_id=chunk.doc_id,
                    content=content,
                    score=float(score or 0.0),
                    source=f"{filename} · {chunk.source}",
                    rank=rank,
                )
            )

        assembled = assemble_context(
            candidates,
            token_budget=int(getattr(self._settings, "context_token_budget", 0) or 0),
            mmr_lambda=float(getattr(self._settings, "context_mmr_lambda", 1.0) or 1.0),
        )
        if assembled.dropped:
            logger.info(
                "上下文组装：%d 条候选 → %d 条（约 %d token，丢弃 %d 条）",
                len(candidates), len(assembled.items), assembled.tokens, len(assembled.dropped),
            )
        # 供前端解释"为什么这条没进上下文"
        self._context_dropped = [
            {"key": d.key, "reason": d.reason, "doc_id": d.doc_id} for d in assembled.dropped
        ]

        evidence: list[Evidence] = []
        for rank, item in enumerate(sorted(assembled.items, key=lambda c: c.rank), start=1):
            chunk, cand, source_label = meta[item.key]
            evidence.append(
                Evidence(
                    rank=rank,
                    chunk_id=chunk.chunk_id,
                    score=round(item.score, 4) if item.score else None,
                    content=item.content,
                    source=source_label,
                    doc_id=chunk.doc_id,
                    filename=self._name_of(chunk.doc_id),
                    source_route=cand.source_route,
                    modality=chunk.modality,
                    chunker=chunk.chunker,
                    origin=cand.origin,
                    used=True,
                )
            )
        # 未采纳证据也必须返回（用户需要看到"检索到了但没用"），原因可归类
        for cand in hybrid_result.rejected:
            chunk = cand.chunk
            if acl_filter and not acl_filter(chunk.doc_id):
                continue
            filename = self._name_of(chunk.doc_id)
            score = cand.rerank_score if hybrid_result.reranked else cand.rrf_score
            evidence.append(
                Evidence(
                    rank=len(evidence) + 1,
                    chunk_id=chunk.chunk_id,
                    score=round(score, 4) if score is not None else None,
                    content=chunk.content,
                    source=f"{filename} · {chunk.source}",
                    doc_id=chunk.doc_id,
                    filename=filename,
                    source_route=cand.source_route,
                    modality=chunk.modality,
                    chunker=chunk.chunker,
                    origin=cand.origin,
                    used=False,
                    rejected_reason="相关度分不足（未进 Top-K）",
                )
            )
        return evidence, acl_filtered

    @staticmethod
    def _avg_score(evidence: list[Evidence], top_n: int) -> tuple[float, int]:
        """Top-N 重排分均值与样本数（以 ACL 过滤后的可见证据为口径）。"""
        scores = sorted((e.score for e in evidence if e.score is not None), reverse=True)
        scores = scores[:top_n]
        if not scores:
            return 0.0, 0
        return sum(scores) / len(scores), len(scores)

    def build_prompt(self, question: str, evidence: list[Evidence]) -> str:
        return PROMPT_TEMPLATE.format(
            no_info=NO_INFO_ANSWER,
            evidence_block=build_evidence_block(evidence),
            question=question,
        )

    # ---- 指标 ----

    def _record(
        self,
        question: str,
        tenant_id: str,
        ok: bool,
        latency_ms: float,
        from_cache: bool,
        llm_called: bool,
        path: str = "ask",
    ) -> None:
        self._prom.requests_total.labels(status="ok" if ok else "error").inc()
        # label 由 tenant 改为 path（低基数）。按租户的延迟请看结构化日志。
        self._prom.latency_seconds.labels(path=path).observe(latency_ms / 1000)
        self._metrics_store.record_request(question, ok, latency_ms, from_cache, llm_called)

    def _observe_retrieval(self, hybrid_result: HybridResult) -> None:
        stats = hybrid_result.stats
        for stage in ("keyword", "vector", "fusion", "rerank"):
            ms = getattr(stats, f"{stage}_ms", 0)
            if ms:
                self._prom.retrieval_stage_ms.labels(stage=stage).observe(ms)
        if hybrid_result.candidates:
            top = hybrid_result.candidates[0]
            self._prom.retrieval_top_score.observe(top.rerank_score or top.rrf_score)

    # ----  核心：CRAG 校验 → 生成 → 自检（事件流）----

    def _core_events(
        self, question: str, tenant_id: str, trace_id: str, config: RetrievalConfig,
        doc_id: str | None = None,
    ) -> Iterator[tuple[str, dict]]:
        """校验闭环主流程。产出的事件同时支撑 /ask（聚合）与 /ask/stream（透传）。"""
        self._log_request(trace_id, tenant_id, question)

        # ---- 输入侧 PII 探测----
        # 策略是**探测 + 留痕，不阻断**：用户问"我的订单 13800138000 怎么处理"是正常诉求，
        # 直接拒答会误伤。真正需要硬拦的是"把含 PII 的文档灌进知识库"（见 ingest 侧）。
        # 这里把命中的类别作为事件与审计线索暴露出来，便于事后追溯与提示用户。
        if self._scanner:
            q_scan = self._scanner.scan(question, tenant_id)
            if not q_scan["safe"]:
                logger.info(
                    "输入侧 PII 探测：trace_id=%s categories=%s", trace_id, q_scan["categories"]
                )
                yield "input_pii_detected", {
                    "categories": q_scan["categories"],
                    "trace_id": trace_id,
                }

        degrade = self._degrade_level()
        if degrade >= 1:
            # 降级阶梯（批次 4，默认关）：上游慢/熔断时跳过预处理（意图/改写/拆解
            # 的多次 LLM 调用），用原查询直接检索——降级期保可用性，不保预处理质量。
            analysis = {"degraded": ["preprocess_skipped"], "llm_calls": 0}
            query_items = [("original", question)]
            is_chitchat = False
            self._prom.shed_total.labels(reason="degrade_preprocess").inc()
        else:
            analysis, query_items, is_chitchat = self._preprocess(question, tenant_id)
        preprocess_calls = (analysis or {}).get("llm_calls", 0)
        llm_calls = preprocess_calls
        if analysis is not None:
            yield "query_analysis", analysis

        # 闲聊短路：直接回答，不校验
        if is_chitchat:
            answer = self._llm.generate(
                "用户在闲聊。请友好、简洁地回应（一两句话），不要编造知识库内容。\n用户说：" + question
            )
            llm_calls += 1
            yield "_final", {
                "answer": answer, "evidence": [], "analysis": analysis,
                "retrieval": None, "reranked": True,
                "verdict": "answered", "confidence": 1.0,
                "llm_calls": llm_calls,
            }
            return

        # ---- CRAG 循环（校验在生成之前全部完成）----
        rounds: list[dict] = []
        hybrid_result: HybridResult | None = None
        final_branch = "correct"
        unvalidated = False
        acl_filtered = 0
        retrieved_total = 0
        refusal_reason = REASON_NO_CONTENT
        # 降级时 CRAG 轮次收缩为 1（不精炼不重检索）
        max_rounds = 1 + (0 if degrade >= 1 else (self._crag.max_iterations if self._crag else 0))

        # 构造检索过滤条件并**下推**。此前 ACL 只在召回之后做后过滤，
        # 不可见文档会占满 top-k 名额（用户有权看 7 份，却因 3 份不可见
        # 排在前列而拿不到任何证据）。下推后 Milvus 在召回阶段就排除不可见文档，
        # FAISS 则由迭代扩取兜住。
        filters = self._build_filters(tenant_id, doc_id)

        for round_no in range(1, max_rounds + 1):
            # 检索阶段 span（含查询数、最终召回的统计在下面挂属性）
            with span("retrieve", **{"kquery.queries": len(query_items),
                                     "kquery.round": round_no}) as sp:
                hybrid_result = self._hybrid.search_multi(
                    query_items, tenant_id, config, rerank_query=question, filters=filters
                )
                sp.set_attribute("kquery.candidates", len(hybrid_result.candidates))
                sp.set_attribute("kquery.reranked", bool(hybrid_result.reranked))
            self._observe_retrieval(hybrid_result)
            # 后过滤保留为**第二道防线**：即使下推被降级（清单过大）或后端不支持
            # 表达式，应用层仍会拦一次。deny 与多身份判定只能在这里做。
            candidates, acl_filtered = self._visible_candidates(hybrid_result)
            retrieved_total += len(hybrid_result.candidates)

            if self._crag is None:
                break  # 未启用校验

            if not candidates:
                # 有召回但全被权限过滤 → 权限类拒答（与"知识库没有"是不同产品动作）
                reason = REASON_PERMISSION if acl_filtered and retrieved_total else REASON_NO_CONTENT
                # ACL 下推后，"不可见文档"根本不会出现在召回结果里，
                # 于是 acl_filtered 恒为 0，permission_denied 又变不可达。
                # 这里做一次**只读探测**（仅空结果路径，不在热路径上）：绕过 ACL 再查一次，
                # 若能查到内容则说明"库里有但你没权限"，据此区分两种拒答。
                # 注意只回传原因，不回传任何被过滤文档的元信息。
                if reason == REASON_NO_CONTENT and filters is not None and self._acl_filter:
                    if self._probe_denied_content(query_items, tenant_id, config, filters):
                        reason = REASON_PERMISSION
                        acl_filtered += 1
                rounds.append({"round": round_no, "branch": BRANCH_INCORRECT, "avg_score": 0.0, "evidence_count": 0, "refined_query": ""})
                self._persist_round(trace_id, question, tenant_id, rounds[-1])
                yield "crag_branch", {"branch": BRANCH_INCORRECT, "avg_score": 0.0, "round": round_no, "acl_filtered": acl_filtered}
                final_branch = BRANCH_INCORRECT
                refusal_reason = reason
                break

            # 证据评分：复用重排相关度分（与 同口径）；重排降级 → 评分不可用 → 降级不校验
            if not hybrid_result.reranked:
                unvalidated = True
                logger.warning("评分器降级（重排不可用）：跳过证据校验 trace_id=%s", trace_id)
                break

            scores = sorted(
                (c.rerank_score for c in candidates if c.rerank_score is not None),
                reverse=True,
            )
            # Top-N 均值聚合：全量 Top-K 均值会被长尾无关候选稀释（配置项，随阈值一同标定）
            top_scores = scores[: self._settings.crag_score_top_n]
            avg = sum(top_scores) / len(top_scores) if top_scores else 0.0
            branch = self._crag.decide(avg)
            rounds.append({"round": round_no, "branch": branch, "avg_score": round(avg, 4), "evidence_count": len(candidates), "refined_query": ""})
            self._persist_round(trace_id, question, tenant_id, rounds[-1])
            yield "crag_branch", {"branch": branch, "avg_score": round(avg, 4), "round": round_no}
            final_branch = branch

            if branch == BRANCH_CORRECT:
                break
            if branch == BRANCH_INCORRECT:
                break
            # ambiguous：精炼查询 → 重检索（改变提问角度，不是重复提问）
            if round_no < max_rounds:
                hints = "；".join((c.chunk.content[:40] for c in candidates[:3]))
                refined = self._crag.refine_query(self._llm, question, hints)
                llm_calls += 1
                rounds[-1]["refined_query"] = refined
                yield "refine", {"before": question if round_no == 1 else rounds[-2].get("refined_query") or question, "after": refined}
                # **追加**而非替换。此前 `query_items = [("refined", refined)]`
                # 把 original + Fusion/HyDE/Step-Back 改写 + 全部子问题一次性丢弃 ——
                # 结果是"CRAG 认为证据不足 → 反而把召回面收窄到一条查询"，
                # 修复动作让情况更糟。正确语义是"在原召回面上再补一个角度"。
                existing = {q for _, q in query_items}
                if refined and refined not in existing:
                    query_items = [*query_items, ("refined", refined)]

        self._log_retrieve(trace_id, tenant_id, retrieved_total, retrieved_total - acl_filtered)
        crag_payload = {"branch": final_branch if self._crag else "disabled", "rounds": rounds} if self._crag else None

        # ---- 拒答判定（一等公民：走成功状态码）----
        # 通用知识兜底：无相关内容 / 证据耗尽时改由模型自身知识回答。
        # 红线：权限类拒答（库里有但你没权限）不兜底——通用知识回答可能暗示文档存在。
        if self._crag is not None and final_branch == BRANCH_INCORRECT:
            if self._settings.parametric_fallback_enabled and refusal_reason != REASON_PERMISSION:
                yield from self._parametric_fallback_events(
                    question, tenant_id, trace_id, analysis, hybrid_result,
                    llm_calls, acl_filtered, crag_payload, refusal_reason,
                )
                return
            yield "refused", {"reason": refusal_reason, "trace_id": trace_id,
                              "acl_filtered": acl_filtered}
            yield "_final", {
                "answer": REFUSAL_TEXT, "evidence": [], "analysis": analysis,
                "retrieval": hybrid_result.stats if hybrid_result else None,
                "reranked": bool(hybrid_result.reranked) if hybrid_result else False,
                "verdict": "refused",
                "refusal_reason": refusal_reason, "confidence": 0.0,
                "crag": crag_payload, "llm_calls": llm_calls,
                "acl_filtered": acl_filtered,
            }
            return
        if self._crag is not None and final_branch == BRANCH_AMBIGUOUS and len(rounds) >= max_rounds:
            if self._settings.parametric_fallback_enabled:
                yield from self._parametric_fallback_events(
                    question, tenant_id, trace_id, analysis, hybrid_result,
                    llm_calls, acl_filtered, crag_payload, REASON_INSUFFICIENT,
                )
                return
            yield "refused", {"reason": REASON_INSUFFICIENT, "trace_id": trace_id,
                              "acl_filtered": acl_filtered}
            yield "_final", {
                "answer": REFUSAL_TEXT, "evidence": [], "analysis": analysis,
                "retrieval": hybrid_result.stats if hybrid_result else None,
                "reranked": bool(hybrid_result.reranked) if hybrid_result else False,
                "verdict": "refused",
                "refusal_reason": REASON_INSUFFICIENT, "confidence": 0.0,
                "crag": crag_payload, "llm_calls": llm_calls,
                "acl_filtered": acl_filtered,
            }
            return

        # ---- 生成（证据已筛干净）----
        evidence, evidence_acl_filtered = self._to_evidence(hybrid_result)
        acl_filtered = max(acl_filtered, evidence_acl_filtered)
        answer_source = "evidence"  # 通用知识兜底命中后改为 parametric
        used_evidence = [e for e in evidence if e.used]
        evidence_block = build_evidence_block(used_evidence)
        # 生成阶段 span（含证据条数；首 token 延迟由 runner 侧记录）
        with span("generate", **{"kquery.evidence": len(used_evidence)}) as sp:
            answer = self._llm.generate(self.build_prompt(question, used_evidence))
            usage = getattr(self._llm, "last_usage", None)
            if usage is not None:
                sp.set_attribute("gen_ai.usage.total_tokens", int(getattr(usage, "total", 0) or 0))
        llm_calls += 1

        # ---- 幻觉自检 + 受限重生成（只一次）----
        confidence: float | None = None
        regenerated = False
        unsupported_count = 0
        self_check_record = None
        if self._self_checker is not None and used_evidence and self._settings.selfcheck_enabled:
            check_started = time.perf_counter()
            try:
                check = self._self_checker.check(answer, evidence_block)
                llm_calls += 1
                removed = 0
                recheck = check
                if check.is_hallucination and check.unsupported_claims:
                    regenerated = True
                    removed = len(check.unsupported_claims)
                    answer = self._self_checker.regenerate(question, evidence_block, check.unsupported_claims)
                    llm_calls += 1
                    # 二次校验：仍有无据声明 → 如实输出并下调置信度，不掩饰
                    recheck = self._self_checker.check(answer, evidence_block)
                    llm_calls += 1
                    unsupported_count = len(recheck.unsupported_claims)
                confidence = compute_confidence(unsupported_count)
                self_check_record = self._to_self_check_record(
                    self._self_checker, recheck, answer, removed, unsupported_count
                )
                self_check_record.elapsed_ms = int((time.perf_counter() - check_started) * 1000)
                self._persist_self_check(trace_id, tenant_id, self_check_record)
            except Exception as exc:
                # 4Axx 降级：自检不可用不阻断答案返回，置信度标为未知
                logger.warning("幻觉自检降级（本次未做答案校验）: %s", exc)
                unvalidated = True
                confidence = None
        elif self._settings.selfcheck_enabled and used_evidence:
            unvalidated = True  # 未配置自检器但启用校验 → 标未知

        # 提示词级拒答归一：LLM 判定证据不足时，verdict 统一为 refused（一等公民）
        prompt_level_refusal = answer.strip().startswith(NO_INFO_ANSWER)
        if prompt_level_refusal and self._settings.parametric_fallback_enabled:
            # 通用知识兜底：模型判断证据不足 → 改由模型自身知识重答（仅一次）。
            # 自检已在上方跑过；兜底答案本就无据可核，不回自检/regenerate。
            fallback_answer = self._llm.generate(GENERAL_KNOWLEDGE_PROMPT.format(question=question))
            llm_calls += 1
            if not fallback_answer.strip().startswith(NO_INFO_ANSWER):
                answer = fallback_answer
                answer_source = "parametric"
                prompt_level_refusal = False
                confidence = round(float(self._settings.parametric_fallback_confidence), 4)
                self_check_record = None
                unvalidated = True
                regenerated = False
                unsupported_count = 0
                evidence = []  # 兜底回答不引用证据，前端不展示无关片段
        if prompt_level_refusal:
            yield "refused", {"reason": REASON_NO_CONTENT, "trace_id": trace_id}
            yield "_final", {
                "answer": REFUSAL_TEXT, "evidence": [], "analysis": analysis,
                "retrieval": hybrid_result.stats if hybrid_result else None,
                "reranked": hybrid_result.reranked if hybrid_result else True,
                "verdict": "refused", "refusal_reason": REASON_NO_CONTENT,
                "confidence": 0.0, "crag": crag_payload, "llm_calls": llm_calls,
                "acl_filtered": acl_filtered,
            }
            return

        # ---- 输出侧敏感词扫描----
        sensitive = {"safe": True, "categories": [], "action": "none"}
        verdict_out, refusal_reason_out = "answered", ""
        if self._scanner:
            sensitive = self._scanner.scan(answer, tenant_id)
            if sensitive["action"] == "refuse":
                # 命中最高密级：整体拒答，缓冲的证据也不外带
                answer = ""
                verdict_out, refusal_reason_out = "refused", REASON_PERMISSION
                yield "refused", {"reason": refusal_reason_out, "trace_id": trace_id}
            elif sensitive["action"] == "mask":
                answer = sensitive["masked_text"]

        for ev in evidence:
            yield "evidence", ev.model_dump()
        if hybrid_result is not None:
            yield "retrieval_stats", {
                "from_cache": False,
                "reranked": hybrid_result.reranked,
                "acl_filtered": acl_filtered,
                **hybrid_result.stats.model_dump(),
            }
        yield "confidence", {
            "verdict": verdict_out,
            "score": confidence,
            "unsupported_claims_count": unsupported_count,
            "regenerated": regenerated,
            "unvalidated": unvalidated,
            "answer_source": answer_source if verdict_out == "answered" else "",
            "self_check": self_check_record.model_dump() if self_check_record else None,
            "removed_claims": self_check_record.unsupported_claims if self_check_record else [],
        }
        self._log_answer(trace_id, tenant_id, answer, len(sensitive["categories"]))
        # F5：推荐追问（失败不伤主答案）
        followups = self._generate_followups(answer)
        yield "_final", {
            "answer": answer, "evidence": evidence if verdict_out == "answered" else [],
            "answer_source": answer_source if verdict_out == "answered" else "",
            "analysis": analysis,
            "retrieval": hybrid_result.stats if hybrid_result else None,
            "reranked": hybrid_result.reranked if hybrid_result else True,
            "verdict": verdict_out, "refusal_reason": refusal_reason_out,
            "confidence": confidence,
            "regenerated": regenerated, "unsupported_claims_count": unsupported_count,
            "crag": crag_payload, "llm_calls": llm_calls,
            "self_check": self_check_record.model_dump() if self_check_record else None,
            "acl_filtered": acl_filtered,
            "followups": followups,
        }

    def _generate_followups(self, answer: str) -> list[str]:
        """生成推荐追问（**任何失败都吞掉**）。

        追问是增强项：LLM 不可用、超时、返回格式不对，都只该表现为
        "没有追问"，绝不能让已经生成好的答案失败。
        """
        if not answer or not answer.strip():
            return []
        try:
            generator = getattr(self._llm, "generate_followups", None)
            if callable(generator):
                items = generator(answer, 3)
            else:
                from app.services.llm import generate_followups as _fallback

                items = _fallback(self._llm, answer, 3)
        except Exception as exc:  # noqa: BLE001 - 追问失败不影响主答案
            logger.warning("生成推荐追问失败（本次不展示追问）: %s", exc)
            return []
        if not isinstance(items, list):
            return []
        cleaned = [str(i).strip() for i in items if str(i).strip()]
        return cleaned[:3]

    # ---- 阶段 5 历史落库 ----

    @staticmethod
    def _to_self_check_record(checker, result, answer: str, removed: int, remaining: int) -> SelfCheckRecord:
        """自检明细落库记录。

        `to_record` 是可选能力：自检器替身（或第三方实现）只实现 check/regenerate 时
        不应因为缺它而把置信度降级成"未知"——那会让"做没做校验"与"校验失败"混淆。
        """
        if hasattr(checker, "to_record"):
            return checker.to_record(result, answer, removed, passed=remaining == 0)
        claims = list(getattr(result, "unsupported_claims", []) or [])
        checked = getattr(result, "checked_claims", 0) or estimate_claims(answer)
        return SelfCheckRecord(
            method="llm_claim_review",
            checked_claims=int(checked),
            removed_claims=removed,
            passed=remaining == 0,
            unsupported_claims=claims,
            reasoning=getattr(result, "reasoning", ""),
            model_version=getattr(checker, "model_version", ""),
        )

    def _persist_round(self, trace_id: str, question: str, tenant_id: str, round_data: dict) -> None:
        if self._history is None:
            return
        self._history.append(CRAG_KIND, {
            "trace_id": trace_id,
            "question": question,
            "tenant_id": tenant_id,
            **round_data,
        })

    def _persist_self_check(self, trace_id: str, tenant_id: str, record) -> None:
        if self._history is None:
            return
        self._history.append(SELFCHECK_KIND, {
            "trace_id": trace_id,
            "tenant_id": tenant_id,
            **record.model_dump(),
        })

    def _log_answer(self, trace_id: str, tenant_id: str, answer: str, sensitive_hits: int) -> None:
        if self._audit and trace_id:
            self._audit.log_answer(trace_id, tenant_id, self._principal_user, answer, sensitive_hits)

    def _assemble(self, trace_id: str, started: float, events: list[tuple[str, dict]]) -> AskResult:
        final = next(payload for kind, payload in events if kind == "_final")
        elapsed = int((time.perf_counter() - started) * 1000)
        return AskResult(
            answer=final["answer"],
            evidence=final["evidence"],
            trace_id=trace_id,
            elapsed_ms=elapsed,
            from_cache=final.get("from_cache", False),
            reranked=final.get("reranked", True),
            retrieval=final.get("retrieval"),
            analysis=final.get("analysis"),
            verdict=final.get("verdict", "answered"),
            refusal_reason=final.get("refusal_reason", ""),
            answer_source=final.get("answer_source", ""),
            confidence=final.get("confidence"),
            regenerated=final.get("regenerated", False),
            unsupported_claims_count=final.get("unsupported_claims_count", 0),
            crag=final.get("crag"),
            self_check=final.get("self_check"),
            acl_filtered=final.get("acl_filtered", 0),
            followups=final.get("followups") or [],
        )

    def _record_quality(self, result: AskResult) -> None:
        if hasattr(self._metrics_store, "record_quality"):
            self._metrics_store.record_quality(
                verdict=result.verdict,
                confidence=result.confidence,
                branch=(result.crag or {}).get("branch"),
                regenerated=result.regenerated,
                answer_source=result.answer_source,
            )

    # ---- single-flight（批次 2）：相同问题的并发请求合并为一次真实计算 ----

    _SF_WAIT_S = 30.0  # 等待者复用结果的上限；超时自行执行，避免等待者占满线程

    def _sf_acquire(self, cache_key: str) -> tuple[threading.Event, bool]:
        """返回 (event, 是否首个请求)。首个请求负责真实计算并在 finally 里 release。"""
        with self._inflight_lock:
            ev = self._inflight.get(cache_key)
            if ev is None:
                ev = threading.Event()
                self._inflight[cache_key] = ev
                return ev, True
            return ev, False

    def _sf_release(self, cache_key: str, ev: threading.Event) -> None:
        with self._inflight_lock:
            if self._inflight.get(cache_key) is ev:
                self._inflight.pop(cache_key, None)
        ev.set()

    def _cached_result(self, entry, hit_kind: str, question: str, tenant_id: str,
                       trace_id: str, started: float) -> AskResult:
        self._prom.cache_total.labels(result=hit_kind if hit_kind != "miss" else "hit").inc()
        elapsed = int((time.perf_counter() - started) * 1000)
        payload = {**entry.payload, "trace_id": trace_id, "elapsed_ms": elapsed, "from_cache": True}
        result = AskResult.model_validate(payload)
        self._record(question, tenant_id, True, elapsed, from_cache=True, llm_called=False)
        logger.info("ask 命中缓存 trace_id=%s elapsed_ms=%d", trace_id, elapsed)
        return result

    # ---- 同步问答 ----

    def ask(
        self, question: str, tenant_id: str, trace_id: str, top_k: int | None = None,
        doc_id: str | None = None,
    ) -> AskResult:
        started = time.perf_counter()
        self._reset_rcache()
        config = self._config_store.get_active()
        if top_k is not None:
            config = config.model_copy(update={"top_k": top_k})
        digest, signature = self._cache_dims(config)
        cache_key = self._cache_key(question, tenant_id, config, doc_id)
        scope = doc_id or ""

        entry, hit_kind = self._cache_lookup(
            question, tenant_id, cache_key, scope=scope, signature=signature
        )
        if entry is not None:
            return self._cached_result(entry, hit_kind, question, tenant_id, trace_id, started)

        ev, owner = self._sf_acquire(cache_key)
        if not owner and ev.wait(timeout=self._SF_WAIT_S):
            # 首个请求已算完并写入缓存：复用结果，不再打上游
            entry, hit_kind = self._cache_lookup(question, tenant_id, cache_key, signature)
            if entry is not None:
                return self._cached_result(entry, hit_kind, question, tenant_id, trace_id, started)

        self._prom.cache_total.labels(result="miss").inc()
        try:
            events = list(self._core_events(question, tenant_id, trace_id, config, doc_id))
            final = next(p for k, p in events if k == "_final")
            result = self._assemble(trace_id, started, events)
            self._cache_store(
                cache_key, question, result.model_dump(mode="json"), config.cache_ttl_s,
                tenant_id=tenant_id, scope=scope, signature=signature,

            )
            self._prom.llm_calls_total.inc(final.get("llm_calls", 1))
            # token 用量结算（激活此前永不触发的 token_daily 配额维度）
            self._settle_tokens(tenant_id, int(final.get("llm_calls", 1)))
            self._record(question, tenant_id, True, result.elapsed_ms, from_cache=False, llm_called=True)
            self._record_quality(result)
            logger.info(
                "ask 完成 trace_id=%s verdict=%s confidence=%s elapsed_ms=%d",
                trace_id, result.verdict, result.confidence, result.elapsed_ms,
            )
            return result
        except LLMError:
            elapsed = int((time.perf_counter() - started) * 1000)
            self._record(question, tenant_id, False, elapsed, from_cache=False, llm_called=True)
            raise
        except Exception:
            elapsed = int((time.perf_counter() - started) * 1000)
            self._record(question, tenant_id, False, elapsed, from_cache=False, llm_called=False)
            raise
        finally:
            if owner:
                self._sf_release(cache_key, ev)

    # ---- 流式问答 ----

    def _cached_stream_events(self, entry, hit_kind: str, question: str, tenant_id: str,
                              trace_id: str, started: float) -> Iterator[tuple[str, dict]]:
        """缓存命中（含 single-flight 等待者）的事件重放。"""
        self._prom.cache_total.labels(result=hit_kind if hit_kind != "miss" else "hit").inc()
        payload = entry.payload
        if payload.get("analysis"):
            yield "query_analysis", payload["analysis"]
        if payload.get("verdict") == "refused":
            yield "refused", {"reason": payload.get("refusal_reason", REASON_NO_CONTENT), "trace_id": trace_id}
        for ev in payload.get("evidence", []):
            yield "evidence", ev
        yield "retrieval_stats", {
            "from_cache": True,
            "hit_kind": hit_kind,
            "reranked": payload.get("reranked", True),
        }
        yield "confidence", {
            "verdict": payload.get("verdict", "answered"),
            "score": payload.get("confidence"),
            "unsupported_claims_count": payload.get("unsupported_claims_count", 0),
            "regenerated": payload.get("regenerated", False),
            "unvalidated": payload.get("confidence") is None,
            "crag": payload.get("crag"),
        }
        text = payload.get("answer", "")
        step = 12
        for i in range(0, len(text), step):
            yield "token", {"content": text[i : i + step]}
        # F5：推荐追问随答案一起缓存——缓存命中也必须能展示追问，
        # 否则"第二次问同一问题"就看不到追问按钮（用户会以为功能坏了）。
        cached_followups = payload.get("followups") or []
        if cached_followups:
            yield "followups", {"items": cached_followups}
        elapsed = int((time.perf_counter() - started) * 1000)
        self._record(question, tenant_id, True, elapsed, from_cache=True, llm_called=False)
        yield "done", {"trace_id": trace_id, "elapsed_ms": elapsed, "from_cache": True,
                       "answer_source": payload.get("answer_source", "")}

    def ask_stream(
        self, question: str, tenant_id: str, trace_id: str, top_k: int | None = None,
        doc_id: str | None = None,
    ) -> Iterator[tuple[str, dict]]:
        started = time.perf_counter()
        self._reset_rcache()
        config = self._config_store.get_active()
        if top_k is not None:
            config = config.model_copy(update={"top_k": top_k})
        digest, signature = self._cache_dims(config)
        cache_key = self._cache_key(question, tenant_id, config, doc_id)
        scope = doc_id or ""

        entry, hit_kind = self._cache_lookup(
            question, tenant_id, cache_key, scope=scope, signature=signature
        )
        if entry is not None:
            yield from self._cached_stream_events(entry, hit_kind, question, tenant_id, trace_id, started)

            return

        ev, owner = self._sf_acquire(cache_key)
        if not owner and ev.wait(timeout=self._SF_WAIT_S):
            entry, hit_kind = self._cache_lookup(question, tenant_id, cache_key, signature)
            if entry is not None:
                yield from self._cached_stream_events(entry, hit_kind, question, tenant_id, trace_id, started)
                return

        self._prom.cache_total.labels(result="miss").inc()
        try:
            events: list[tuple[str, dict]] = []
            for kind, payload in self._core_events(question, tenant_id, trace_id, config, doc_id):
                events.append((kind, payload))
                if kind == "_final":
                    # 最终答案在自检完成后统一流式吐出（保证 token 是终稿）
                    text = payload["answer"]
                    step = 12
                    for i in range(0, len(text), step):
                        yield "token", {"content": text[i : i + step]}
                    # F5：推荐追问必须在 done 之前发出（前端白名单按事件名分发）
                    followups = payload.get("followups") or []
                    if followups:
                        yield "followups", {"items": followups}
                    continue
                yield kind, payload

            result = self._assemble(trace_id, started, events)
            self._cache_store(
                cache_key, question, result.model_dump(mode="json"), config.cache_ttl_s,
                tenant_id=tenant_id, scope=scope, signature=signature,

            )
            final = next(p for k, p in events if k == "_final")
            self._prom.llm_calls_total.inc(final.get("llm_calls", 1))
            # token 用量结算（激活此前永不触发的 token_daily 配额维度）
            self._settle_tokens(tenant_id, int(final.get("llm_calls", 1)))
            self._record(question, tenant_id, True, result.elapsed_ms, from_cache=False,
                         llm_called=True, path="ask_stream")
            self._record_quality(result)
            logger.info(
                "ask_stream 完成 trace_id=%s verdict=%s confidence=%s elapsed_ms=%d",
                trace_id, result.verdict, result.confidence, result.elapsed_ms,
            )
            yield "done", {"trace_id": trace_id, "elapsed_ms": result.elapsed_ms,
                           "from_cache": False, "answer_source": result.answer_source}
        except LLMError as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            self._record(question, tenant_id, False, elapsed, from_cache=False, llm_called=True, path="ask_stream")
            logger.warning("ask_stream 失败 trace_id=%s code=%s: %s", trace_id, exc.code, exc.message)
            yield "error", {"code": int(exc.code), "message": exc.message}
        except Exception:
            elapsed = int((time.perf_counter() - started) * 1000)
            self._record(question, tenant_id, False, elapsed, from_cache=False, llm_called=False)
            logger.exception("ask_stream 异常 trace_id=%s", trace_id)
            yield "error", {"code": int(ErrCode.INTERNAL_ERROR), "message": "服务暂时不可用，请稍后再试"}
        finally:
            if owner:
                self._sf_release(cache_key, ev)
