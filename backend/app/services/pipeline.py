"""RAG 流水线（阶段 5）：预处理 → 缓存 → 混合检索 → CRAG 校验 → 生成 → 幻觉自检。

- **校验发生在生成之前**：CRAG 先把证据筛干净（评分/精炼/重检索/拒答），
  模型只在证据合格后才被调用——从流程上排除"看到噪声证据后编造"的主要诱因；
- 拒答是一等公民：HTTP 200 + verdict=refused + refusal_reason（可归因），不走错误码；
- 降级是平行分支而非异常出口：评分器/自检器失效时请求继续走完，
  confidence 标为 None（未知）并如实提示"本次未做证据校验"；
- 受限重生成只一次：二次校验仍有无据声明 → 如实输出并下调置信度，不掩饰。
"""
import logging
import time
from typing import Callable, Iterator

from app.core.config import Settings
from app.core.envelope import ErrCode
from app.core.metrics import MetricsStore, PromMetrics
from app.domain.schemas import AskResult, Evidence, RetrievalConfig
from app.services.cache import SemanticCache, config_digest, make_cache_key
from app.services.config_store import RetrievalConfigStore
from app.services.llm import LLM, LLMError
from app.services.retrieval.hybrid import HybridResult, HybridRetriever
from app.services.validation.crag import (
    BRANCH_AMBIGUOUS,
    BRANCH_CORRECT,
    BRANCH_INCORRECT,
    CragRouter,
)
from app.services.validation.selfcheck import HallucinationChecker, compute_confidence

logger = logging.getLogger(__name__)

NO_INFO_ANSWER = "知识库无相关内容"
REFUSAL_TEXT = "知识库中没有足够证据回答该问题"

# 拒答原因（可归因，三类对应完全不同的产品动作）
REASON_NO_CONTENT = "no_content"  # 知识库无相关内容 → 补知识
REASON_INSUFFICIENT = "insufficient_evidence"  # 证据不足（循环耗尽）→ 换问法
REASON_PERMISSION = "permission_denied"  # 权限不足（阶段 9 启用）→ 申请权限

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
        self._index_version = index_version_provider or (lambda: 0)
        self._preprocessor = preprocessor
        self._analysis_store = analysis_store
        self._crag = crag_router
        self._self_checker = self_checker

    # ---- 缓存键 ----

    def _model_version(self, config: RetrievalConfig) -> str:
        provider = self._settings.resolved_provider
        return (
            f"{provider}:{self._settings.llm_model}"
            f"+{self._settings.embedding_model}+{config.rerank_model}"
        )

    def _cache_key(self, question: str, tenant_id: str, config: RetrievalConfig) -> str:
        digest = config_digest(
            {
                "retrieval": config.model_dump(),
                "chunk_size": self._settings.chunk_size,
                "temperature": self._settings.temperature,
                "index_version": self._index_version(),
                "crag": self._crag is not None,
            }
        )
        return make_cache_key(tenant_id, question, self._model_version(config), digest)

    # ---- 预处理 ----

    def _preprocess(self, question: str, tenant_id: str) -> tuple[dict | None, list[tuple[str, str]], bool]:
        if self._preprocessor is None:
            return None, [("original", question)], False
        analysis_obj, query_items = self._preprocessor.process(question, tenant_id)
        if self._analysis_store is not None:
            self._analysis_store.save(analysis_obj)
        return analysis_obj.model_dump(), query_items, not analysis_obj.needs_retrieval

    # ---- 证据装配 ----

    def _to_evidence(self, hybrid_result: HybridResult) -> list[Evidence]:
        evidence: list[Evidence] = []
        for rank, cand in enumerate(hybrid_result.candidates, start=1):
            chunk = cand.chunk
            filename = self._doc_name_resolver(chunk.doc_id)
            score = cand.rerank_score if hybrid_result.reranked else cand.rrf_score
            content = chunk.content
            if chunk.parent_id and self._docstore is not None:
                parent = self._docstore.get_parent(chunk.parent_id)
                if parent is not None:
                    content = parent.content
            evidence.append(
                Evidence(
                    rank=rank,
                    chunk_id=chunk.chunk_id,
                    score=round(score, 4) if score is not None else None,
                    content=content,
                    source=f"{filename} · {chunk.source}",
                    doc_id=chunk.doc_id,
                    filename=filename,
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
            filename = self._doc_name_resolver(chunk.doc_id)
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
        return evidence

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
    ) -> None:
        self._prom.requests_total.labels(status="ok" if ok else "error").inc()
        self._prom.latency_seconds.labels(tenant=tenant_id).observe(latency_ms / 1000)
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

    # ---- 阶段 5 核心：CRAG 校验 → 生成 → 自检（事件流）----

    def _core_events(
        self, question: str, tenant_id: str, trace_id: str, config: RetrievalConfig
    ) -> Iterator[tuple[str, dict]]:
        """校验闭环主流程。产出的事件同时支撑 /ask（聚合）与 /ask/stream（透传）。"""
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
        max_rounds = 1 + (self._crag.max_iterations if self._crag else 0)

        for round_no in range(1, max_rounds + 1):
            hybrid_result = self._hybrid.search_multi(query_items, tenant_id, config, rerank_query=question)
            self._observe_retrieval(hybrid_result)

            if self._crag is None:
                break  # 未启用校验（阶段 4 行为）

            candidates = hybrid_result.candidates
            if not candidates:
                rounds.append({"round": round_no, "branch": BRANCH_INCORRECT, "avg_score": 0.0, "evidence_count": 0, "refined_query": ""})
                yield "crag_branch", {"branch": BRANCH_INCORRECT, "avg_score": 0.0, "round": round_no}
                final_branch = BRANCH_INCORRECT
                break

            # 证据评分：复用重排相关度分（与阶段 2 同口径）；重排降级 → 评分不可用 → 降级不校验
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
                query_items = [("refined", refined)]

        crag_payload = {"branch": final_branch if self._crag else "disabled", "rounds": rounds} if self._crag else None

        # ---- 拒答判定（一等公民：走成功状态码）----
        if self._crag is not None and final_branch == BRANCH_INCORRECT:
            yield "refused", {"reason": REASON_NO_CONTENT, "trace_id": trace_id}
            yield "_final", {
                "answer": REFUSAL_TEXT, "evidence": [], "analysis": analysis,
                "retrieval": hybrid_result.stats if hybrid_result else None,
                "reranked": True, "verdict": "refused",
                "refusal_reason": REASON_NO_CONTENT, "confidence": 0.0,
                "crag": crag_payload, "llm_calls": llm_calls,
            }
            return
        if self._crag is not None and final_branch == BRANCH_AMBIGUOUS and len(rounds) >= max_rounds:
            yield "refused", {"reason": REASON_INSUFFICIENT, "trace_id": trace_id}
            yield "_final", {
                "answer": REFUSAL_TEXT, "evidence": [], "analysis": analysis,
                "retrieval": hybrid_result.stats if hybrid_result else None,
                "reranked": True, "verdict": "refused",
                "refusal_reason": REASON_INSUFFICIENT, "confidence": 0.0,
                "crag": crag_payload, "llm_calls": llm_calls,
            }
            return

        # ---- 生成（证据已筛干净）----
        evidence = self._to_evidence(hybrid_result)
        used_evidence = [e for e in evidence if e.used]
        evidence_block = build_evidence_block(used_evidence)
        answer = self._llm.generate(self.build_prompt(question, used_evidence))
        llm_calls += 1

        # ---- 幻觉自检 + 受限重生成（只一次）----
        confidence: float | None = None
        regenerated = False
        unsupported_count = 0
        if self._self_checker is not None and used_evidence and self._settings.selfcheck_enabled:
            try:
                check = self._self_checker.check(answer, evidence_block)
                llm_calls += 1
                if check.is_hallucination and check.unsupported_claims:
                    regenerated = True
                    answer = self._self_checker.regenerate(question, evidence_block, check.unsupported_claims)
                    llm_calls += 1
                    # 二次校验：仍有无据声明 → 如实输出并下调置信度，不掩饰
                    recheck = self._self_checker.check(answer, evidence_block)
                    llm_calls += 1
                    unsupported_count = len(recheck.unsupported_claims)
                confidence = compute_confidence(unsupported_count)
            except Exception as exc:
                # 4Axx 降级：自检不可用不阻断答案返回，置信度标为未知
                logger.warning("幻觉自检降级（本次未做答案校验）: %s", exc)
                unvalidated = True
                confidence = None
        elif self._settings.selfcheck_enabled and used_evidence:
            unvalidated = True  # 未配置自检器但启用校验 → 标未知

        # 提示词级拒答归一：LLM 判定证据不足时，verdict 统一为 refused（一等公民）
        prompt_level_refusal = answer.strip().startswith(NO_INFO_ANSWER)
        if prompt_level_refusal:
            yield "refused", {"reason": REASON_NO_CONTENT, "trace_id": trace_id}
            yield "_final", {
                "answer": REFUSAL_TEXT, "evidence": [], "analysis": analysis,
                "retrieval": hybrid_result.stats if hybrid_result else None,
                "reranked": hybrid_result.reranked if hybrid_result else True,
                "verdict": "refused", "refusal_reason": REASON_NO_CONTENT,
                "confidence": 0.0, "crag": crag_payload, "llm_calls": llm_calls,
            }
            return

        for ev in evidence:
            yield "evidence", ev.model_dump()
        if hybrid_result is not None:
            yield "retrieval_stats", {
                "from_cache": False,
                "reranked": hybrid_result.reranked,
                **hybrid_result.stats.model_dump(),
            }
        yield "confidence", {
            "verdict": "answered",
            "score": confidence,
            "unsupported_claims_count": unsupported_count,
            "regenerated": regenerated,
            "unvalidated": unvalidated,
        }
        yield "_final", {
            "answer": answer, "evidence": evidence, "analysis": analysis,
            "retrieval": hybrid_result.stats if hybrid_result else None,
            "reranked": hybrid_result.reranked if hybrid_result else True,
            "verdict": "answered", "confidence": confidence,
            "regenerated": regenerated, "unsupported_claims_count": unsupported_count,
            "crag": crag_payload, "llm_calls": llm_calls,
        }

    def _assemble(self, trace_id: str, started: float, events: list[tuple[str, dict]]) -> AskResult:
        final = next(payload for kind, payload in events if kind == "_final")
        elapsed = int((time.perf_counter() - started) * 1000)
        return AskResult(
            answer=final["answer"],
            evidence=final["evidence"],
            trace_id=trace_id,
            elapsed_ms=elapsed,
            from_cache=False,
            reranked=final.get("reranked", True),
            retrieval=final.get("retrieval"),
            analysis=final.get("analysis"),
            verdict=final.get("verdict", "answered"),
            refusal_reason=final.get("refusal_reason", ""),
            confidence=final.get("confidence"),
            regenerated=final.get("regenerated", False),
            unsupported_claims_count=final.get("unsupported_claims_count", 0),
            crag=final.get("crag"),
        )

    def _record_quality(self, result: AskResult) -> None:
        if hasattr(self._metrics_store, "record_quality"):
            self._metrics_store.record_quality(
                verdict=result.verdict,
                confidence=result.confidence,
                branch=(result.crag or {}).get("branch"),
                regenerated=result.regenerated,
            )

    # ---- 同步问答 ----

    def ask(self, question: str, tenant_id: str, trace_id: str, top_k: int | None = None) -> AskResult:
        started = time.perf_counter()
        config = self._config_store.get_active()
        if top_k is not None:
            config = config.model_copy(update={"top_k": top_k})
        cache_key = self._cache_key(question, tenant_id, config)

        entry = self._cache.get(cache_key)
        if entry is not None:
            self._prom.cache_total.labels(result="hit").inc()
            elapsed = int((time.perf_counter() - started) * 1000)
            payload = {**entry.payload, "trace_id": trace_id, "elapsed_ms": elapsed, "from_cache": True}
            result = AskResult.model_validate(payload)
            self._record(question, tenant_id, True, elapsed, from_cache=True, llm_called=False)
            logger.info("ask 命中缓存 trace_id=%s elapsed_ms=%d", trace_id, elapsed)
            return result

        self._prom.cache_total.labels(result="miss").inc()
        try:
            events = list(self._core_events(question, tenant_id, trace_id, config))
            final = next(p for k, p in events if k == "_final")
            result = self._assemble(trace_id, started, events)
            self._cache.set(cache_key, result.model_dump(mode="json"), ttl_seconds=config.cache_ttl_s)
            self._prom.llm_calls_total.inc(final.get("llm_calls", 1))
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

    # ---- 流式问答 ----

    def ask_stream(
        self, question: str, tenant_id: str, trace_id: str, top_k: int | None = None
    ) -> Iterator[tuple[str, dict]]:
        started = time.perf_counter()
        config = self._config_store.get_active()
        if top_k is not None:
            config = config.model_copy(update={"top_k": top_k})
        cache_key = self._cache_key(question, tenant_id, config)

        entry = self._cache.get(cache_key)
        if entry is not None:
            self._prom.cache_total.labels(result="hit").inc()
            payload = entry.payload
            if payload.get("analysis"):
                yield "query_analysis", payload["analysis"]
            if payload.get("verdict") == "refused":
                yield "refused", {"reason": payload.get("refusal_reason", REASON_NO_CONTENT), "trace_id": trace_id}
            for ev in payload.get("evidence", []):
                yield "evidence", ev
            yield "retrieval_stats", {"from_cache": True, "reranked": payload.get("reranked", True)}
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
            elapsed = int((time.perf_counter() - started) * 1000)
            self._record(question, tenant_id, True, elapsed, from_cache=True, llm_called=False)
            yield "done", {"trace_id": trace_id, "elapsed_ms": elapsed, "from_cache": True}
            return

        self._prom.cache_total.labels(result="miss").inc()
        try:
            events: list[tuple[str, dict]] = []
            for kind, payload in self._core_events(question, tenant_id, trace_id, config):
                events.append((kind, payload))
                if kind == "_final":
                    # 最终答案在自检完成后统一流式吐出（保证 token 是终稿）
                    text = payload["answer"]
                    step = 12
                    for i in range(0, len(text), step):
                        yield "token", {"content": text[i : i + step]}
                    continue
                yield kind, payload

            result = self._assemble(trace_id, started, events)
            self._cache.set(cache_key, result.model_dump(mode="json"), ttl_seconds=config.cache_ttl_s)
            final = next(p for k, p in events if k == "_final")
            self._prom.llm_calls_total.inc(final.get("llm_calls", 1))
            self._record(question, tenant_id, True, result.elapsed_ms, from_cache=False, llm_called=True)
            self._record_quality(result)
            logger.info(
                "ask_stream 完成 trace_id=%s verdict=%s confidence=%s elapsed_ms=%d",
                trace_id, result.verdict, result.confidence, result.elapsed_ms,
            )
            yield "done", {"trace_id": trace_id, "elapsed_ms": result.elapsed_ms, "from_cache": False}
        except LLMError as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            self._record(question, tenant_id, False, elapsed, from_cache=False, llm_called=True)
            logger.warning("ask_stream 失败 trace_id=%s code=%s: %s", trace_id, exc.code, exc.message)
            yield "error", {"code": int(exc.code), "message": exc.message}
        except Exception:
            elapsed = int((time.perf_counter() - started) * 1000)
            self._record(question, tenant_id, False, elapsed, from_cache=False, llm_called=False)
            logger.exception("ask_stream 异常 trace_id=%s", trace_id)
            yield "error", {"code": int(ErrCode.INTERNAL_ERROR), "message": "服务暂时不可用，请稍后再试"}
