"""问答端点。

网关顺序：认证 → Principal → 突发限流（令牌桶）→ Agent 执行
（检索前租户过滤 + 检索后 ACL）→ 响应。

并发模型（批次 1）：端点全部为 async，同步管线经 `PipelineExecutor`
（在飞 32 + 排队 200 + 503 背压）执行；SSE 连接不再占用线程池线程。
"""
import json
import asyncio
import threading
import time
from collections.abc import Iterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.config import Settings
from app.core.deadline import Deadline
from app.core.envelope import (
    ApiError,
    Envelope,
    ErrCode,
    error_envelope,
    ok_envelope,
    warn_envelope,
)
from app.core.tracing import elapsed_ms_since, get_trace_id, now_ms, set_trace_id
from app.dependencies import (
    enforce_rate_limit,
    get_app_settings,
    get_pipeline,
    get_principal,
)
from app.domain.schemas import AskRequest
from app.services.agent.runner import AgentRunner
from app.services.idempotency import DONE, INFLIGHT, IdempotencyStore, content_key
from app.services.llm import LLMError
from app.services.pipeline import PipelineCancelled, RagPipeline
from app.services.pipeline_executor import OverloadedError, PipelineExecutor, raise_overloaded
from app.services.query.intent import is_obvious_chitchat
from app.services.sse_replay import SseReplayStore, parse_last_event_id
from app.core.identity import Principal
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["ask"])


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _use_agent(req: AskRequest, settings: Settings) -> bool:
    return settings.agent_enabled and (req.mode == "agent" or req.session_id is not None)


def _agent_identity(req: AskRequest, principal: Principal) -> tuple[str, str, str]:
    # 身份以认证结果为准（凭证优先），请求体 user_id 仅在免认证模式兜底
    tenant, user = _principal_identity(principal, req.user_id or "")
    return tenant, user, req.session_id or ""


def _principal_identity(principal: Principal, fallback_user: str = "") -> tuple[str, str]:
    """主体 → (租户, 用户)；无请求体时的统一口径（续传端点也用它）。"""
    user = principal.user_id if principal.user_id != "anon" else (fallback_user or "anon")
    return principal.tenant_id, user


def _validate_question(req: AskRequest) -> None:
    """空问题兜底（AskRequest 有 min_length=1，这里防的是纯空白）。"""
    if not (req.question or "").strip():
        raise ApiError(ErrCode.QUESTION_EMPTY, "问题不能为空", http_status=400)


def _auto_title(request: Request, principal: Principal, session_id: str, question: str, answer: str) -> None:
    """首轮自动生成会话标题（F5）。

    只在**首轮**且标题仍是默认的「新对话」时改名——用户手动改过的标题不能被覆盖。
    任何失败都静默：会话标题是锦上添花，不能让问答失败。
    """
    if not session_id or not answer:
        return
    store = getattr(request.app.state, "session_store", None)
    llm = getattr(request.app.state, "llm", None)
    if store is None or llm is None:
        return
    try:
        session = store.get(session_id, principal.tenant_id, principal.user_id)
    except Exception:  # noqa: BLE001 - 读不到会话就不改名
        logger.debug("读不到会话就不改名")
        return
    if len(session.get("turns", [])) > 1:
        return  # 不是首轮
    if (session.get("title") or "新对话") != "新对话":
        return  # 用户已手动命名
    # 按需载入：仅自动命名 / 推荐追问用
    from app.services.llm import generate_title

    try:
        title = generate_title(llm, question, answer)
    except Exception as exc:  # noqa: BLE001 - 会话自动命名失败
        logger.warning("会话自动命名失败（忽略）: %s", exc)
        return
    if not title:
        return
    try:
        store.rename(session_id, principal.tenant_id, principal.user_id, title)
    except Exception as exc:  # noqa: BLE001 - 会话标题写入失败
        logger.warning("会话标题写入失败（忽略）: %s", exc)


# 预处理降级标记 → 错误码（`A-10` 接线 46xx~48xx）。
# 编排器把「哪一步降级了」记在 `analysis.degraded` 里（intent / expansion / decompose），
# 这里把它翻译成用户能看懂的轻提示 + 统一错误码。
_PREPROCESS_DEGRADE_CODES: dict[str, tuple[ErrCode, str]] = {
    "intent": (ErrCode.INTENT_FAILED, "本次未做意图识别，已按需要检索处理"),
    "expansion": (ErrCode.REWRITE_FAILED, "本次未做查询改写，已用原问题检索"),
    "decompose": (ErrCode.DECOMPOSE_FAILED, "本次未做多跳拆解，已按单跳处理"),
}


def _degraded_warning(result: dict) -> tuple[int, str] | None:
    """降级语义 → warn 信封。

    重排不可用是用户该知道的事（44xx 轻提示）；缓存不可用不是——它无感降级，
    只上报指标（ §错误码）。
    """
    # 通用知识兜底是用户该知道的事：答案不来自知识库（warn 信封，仍返回结果）
    if result.get("answer_source") == "parametric":
        return (int(ErrCode.PARAMETRIC_FALLBACK), "知识库未收录相关内容，以下回答来自模型自身知识")
    if result.get("reranked") is False:
        return (int(ErrCode.RERANK_DEGRADED), "本次未进行精排，结果可能不够精确")
    # 46xx~48xx：预处理各步降级（意图 / 改写 / 拆解）。
    # 优先级低于上面两条：它们不影响答案本身，只影响「这次检索怎么问的」。
    degraded = (result.get("analysis") or {}).get("degraded") or []
    for marker, (code, message) in _PREPROCESS_DEGRADE_CODES.items():
        if marker in degraded:
            return (int(code), message)
    return None


def _envelope_for(result: dict, trace_id: str, elapsed_ms: int, prom=None) -> Envelope:
    """按降级语义选择 ok / warn 信封。

    `prom` 用于给 warn 码计数（`A-09`）：warn 也是「出过问题」，只是没失败。
    没有 prom 时（单测直接调用）跳过计数，不影响返回。
    """
    warning = _degraded_warning(result)
    if warning:
        code, message = warning
        if not ErrCode.is_degraded_code(code):
            # 不变量：warn 信封**只**用于降级语义（失败一律走 error 信封）。
            # 出现非降级码说明 `_degraded_warning` 的映射写错了——记一条日志，
            # 但不改变响应（用户已经拿到结果，别在这里改语义）。
            logger.warning("warn 信封使用了非降级码 code=%s", code)
        if prom is not None:
            try:
                prom.record_error(code)
            except Exception:  # noqa: BLE001 - 指标失败不该影响返回
                pass
        return warn_envelope(result, message, trace_id, elapsed_ms, code=code)
    return ok_envelope(result, trace_id, elapsed_ms)


@router.post("/ask", response_model=Envelope, dependencies=[Depends(enforce_rate_limit)])
async def ask(
    req: AskRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    pipeline: RagPipeline = Depends(get_pipeline),
    settings: Settings = Depends(get_app_settings),
) -> Envelope:
    """同步问答：返回答案 + 证据 + 判定结果。

    **拒答是一等公民**：证据不足时返回 200 + `verdict=refused` + `refusal_reason`，
    不走错误码——「没答上来」不是系统故障。
    命中语义缓存时 `from_cache=true`，未调用模型。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    _validate_question(req)
    executor: PipelineExecutor = request.app.state.pipeline_executor
    tenant, user, session_id = _agent_identity(req, principal)
    idem: IdempotencyStore = request.app.state.idempotency
    # ① 在飞去重（按问题内容）：前端在 429/503 后会自动重试一次，
    #    同一问题会被算两次钱，还可能拿到两份不一致的答案。
    #    拿到占位就执行，否则立刻 409——不排队（排队等于"再等一轮"，更贵更慢）。
    inflight_key = content_key(tenant, req.question, session_id or "")
    # ② 幂等键（结果重放）：调用方显式声明"这是同一次提交"，成功后可直接回放。
    replay_key = (request.headers.get("Idempotency-Key") or "").strip()
    replay_scope = f"{tenant}:{replay_key}" if replay_key else ""
    replay_done = False
    if replay_scope:
        state, cached = idem.begin(replay_scope)
        if state == DONE and cached:
            return JSONResponse(cached, headers={"X-Idempotent-Replay": "true"})
        if state == INFLIGHT:
            raise ApiError(
                ErrCode.REQUEST_IN_PROGRESS, "同一请求正在处理中，请稍候再试", http_status=409
            )
    if not idem.acquire(inflight_key):
        # 回滚上面为幂等键占的位置：本次没拿到执行权，不能留下"在飞"的假象
        if replay_scope:
            idem.fail(replay_scope)
        raise ApiError(
            ErrCode.REQUEST_IN_PROGRESS, "同一问题正在处理中，请稍候再试", http_status=409
        )
    try:
        if _use_agent(req, settings):
            runner: AgentRunner = request.app.state.agent_runner

            def _collect() -> dict | None:
                final: dict | None = None
                for event, payload in runner.run(
                    req.question, tenant, user, session_id or None,
                    doc_name_resolver=request.app.state.knowledge.filename_of,
                    trace_id=trace_id,
                    # 闲聊零成本短路。此前该参数在 Agent 路径**从无生产调用点**，
                    # 导致"你好"也白跑 plan→tool→reflect 全循环并计费。
                    skip_retrieval=is_obvious_chitchat(req.question),
                ):
                    if event == "_final":
                        final = payload
                return final

            final = await executor.run_async(_collect)
            result = {
                **(final or {"answer": "", "evidence": [], "verdict": "refused"}),
                "trace_id": trace_id,
                "elapsed_ms": elapsed_ms_since(started),
                "session_id": session_id or None,
            }
            _auto_title(request, principal, session_id, req.question, result.get("answer", ""))
            envelope = _envelope_for(
                result, trace_id, result["elapsed_ms"], prom=request.app.state.prom
            )
            if replay_scope:
                idem.complete(replay_scope, envelope.model_dump(mode="json"))
                replay_done = True
            return envelope
        # 线性路径同样挂主体（租户/用户是数据隔离维度）
        scoped = pipeline.bind_principal(tenant, user, principal)
        # 请求级墙钟预算：此前线性路径没有任何整体上限（单次 LLM 超时 60s × 3 次重试
        # + 阻塞退避 ≈183s，网关会先超时而服务端还在烧 token）。
        deadline = Deadline(settings.ask_deadline_ms)
        # `C-04`：阻塞端点也要能被打断。SSE 那边靠「生成器被关闭」间接发现断开，
        # 这里没有生成器，只能像流式一样主动轮询 `is_disconnected()`；否则用户
        # 关掉页面后，检索 + CRAG + 生成仍会跑完（白烧上游 token，还占着在飞槽位）。
        cancel = threading.Event()
        watcher = asyncio.create_task(_watch_disconnect(request, cancel))
        try:
            result = await executor.run_async(
                scoped.ask, req.question, tenant, trace_id, req.top_k,
                deadline=deadline, cancel=cancel,
            )
        finally:
            watcher.cancel()
        payload = result.model_dump()
        envelope = _envelope_for(
            payload, trace_id, result.elapsed_ms, prom=request.app.state.prom
        )
        if replay_scope:
            idem.complete(replay_scope, envelope.model_dump(mode="json"))
            replay_done = True
        return envelope
    except OverloadedError:
        raise_overloaded()
    except PipelineCancelled as exc:
        # 客户端已断开：返回 499 让网关把它从 5xx 错误率里摘出去，
        # 而**不计数**（取消不是故障；计入错误率会污染告警阈值）。
        # 响应大概率写不出去（连接已关），这里只是把语义表达完整。
        logger.info("ask 客户端断开，已中止阶段 %s trace_id=%s", exc.stage, trace_id)
        return JSONResponse(
            status_code=499,
            content=error_envelope(
                ErrCode.REQUEST_CANCELLED, "客户端已关闭请求", trace_id, 0
            ).model_dump(),
        )
    except LLMError as exc:
        # X-04：失败语义必须是**正确的 HTTP 状态**。
        # 此前把 LLM 失败装进 200 的错误信封：网关按 5xx 统计时全部漏掉，
        # 前端也拿不到"该重试"的信号（200 会被当成成功）。
        raise ApiError(
            exc.code,
            exc.message,
            http_status=_llm_error_status(exc.code),
            retry_after=5 if int(exc.code) == int(ErrCode.RATE_LIMITED) else None,
        ) from exc
    except ApiError:
        raise  # 业务错误码（如 4301 会话不存在）透传给全局异常处理器
    except Exception:
        logger.exception("ask 异常 trace_id=%s", trace_id)
        # 失败语义必须是**正确的 HTTP 状态**（同 X-04 对 LLM 失败的处理）：
        # 此前这里返回的是「200 + 错误信封」，网关按 5xx 统计时全部漏掉，
        # 前端也拿不到「该重试」的信号（200 会被当成成功）。
        # 抛 ApiError 由全局异常处理器统一转成 500 信封（并在那里计数）。
        raise ApiError(
            ErrCode.INTERNAL_ERROR,
            "服务暂时不可用，请稍后再试",
            http_status=500,
        )
    finally:
        # 在飞占位必须释放（成功/失败/异常都要）；幂等键只有成功才留下可回放结果
        idem.release(inflight_key)
        if replay_scope and not replay_done:
            idem.fail(replay_scope)


def _llm_error_status(code: int) -> int:
    """LLM 错误码 → HTTP 状态码（X-04）。

    - 超时 → 504（网关/客户端知道该重试或换降级）
    - 生成失败 / 其它上游错误 → 502（上游的问题，不是我们的）
    - 限流 → 429（带 Retry-After）
    - 依赖不可用 → 503
    """
    return {
        int(ErrCode.GENERATION_TIMEOUT): 504,
        int(ErrCode.GENERATION_FAILED): 502,
        int(ErrCode.RATE_LIMITED): 429,
        int(ErrCode.DEPENDENCY_UNAVAILABLE): 503,
    }.get(int(code), 502)


async def _watch_disconnect(
    request: Request,
    cancel: threading.Event,
    interval_s: float = 0.5,
    *,
    grace_s: float = 0.0,
    replay: "SseReplayStore | None" = None,
    trace_id: str = "",
) -> None:
    """主动探测客户端断开（`C-04`）。

    ## 为什么需要主动探测

    事件流的消费者退出时（生成器被关闭）会置位取消事件——但那要等到
    **下一次写事件**才会被发现。模型正在生成、几秒内没有新事件时，
    服务端会**继续把答案算完**（白烧上游 token 与线程）。

    轮询 `request.is_disconnected()` 让断开**在 ≤ interval_s 内**被发现，
    管线随即在阶段边界 / 生成循环里停止（`C-04`）。

    ## 宽限期（`grace_s > 0`，仅在开启续传时）

    开启续传后，断开不再等于"不要了"——手机锁屏、代理闪断都会让连接断开，
    而人还在。`grace_s` 内只要有人带着 `Last-Event-ID` 接上（`mark_resumed`
    时间戳晚于断开时刻），生成继续；窗口内没人接，仍然立刻取消。
    `grace_s = 0` 就是旧行为：断开即取消。
    """
    try:
        while not cancel.is_set():
            if await request.is_disconnected():
                if grace_s <= 0 or replay is None:
                    cancel.set()
                    return
                disconnected_at = time.monotonic()
                deadline = disconnected_at + grace_s
                while time.monotonic() < deadline and not cancel.is_set():
                    run = replay.get(trace_id)
                    if run is None or run.done:
                        return  # 生成已结束 / 缓冲已过期：没有可取消的东西
                    if run.resume_attached_at >= disconnected_at:
                        return  # 有人接上了，继续生成
                    await asyncio.sleep(0.2)
                cancel.set()
                return
            await asyncio.sleep(interval_s)
    except Exception:  # noqa: BLE001 - 探测失败不该影响正常流式
        return


# 泵任务必须有强引用：`asyncio.create_task` 只持弱引用，中间没有 await 的窗口里
# 任务可能被 GC 回收（表现为"偶尔什么都不发"）。挂在模块级集合里，做完即摘。
_PUMPS: set[asyncio.Task] = set()

# 尾随循环的轮询间隔：帧是内存拷贝，20ms 足够让"逐 token 回放"看起来仍是流式
_TAIL_POLL_S = 0.02
# 空闲时的心跳间隔（客户端看门狗是 60s，5s 一条足够）
_TAIL_PING_S = 5.0


def _stream_response(
    executor: PipelineExecutor,
    factory,
    trace_id: str,
    on_value_error: bool = False,
    request: Request | None = None,
    cancel: threading.Event | None = None,
    replay: "SseReplayStore | None" = None,
    tenant_id: str = "",
    user_id: str = "",
    grace_s: float = 0.0,
) -> StreamingResponse:
    """把同步事件工厂包装成带背压/心跳/断线取消的 SSE 响应（批次 1）。

    事件名与顺序保持不变（契约零破坏）；新增的只有 `: ping` 心跳注释行，
    SSE 规范下客户端会忽略未知注释行，不影响既有前端解析。

    `cancel`（`C-04`）：与 `factory` 共用同一个取消事件——这样看门狗
    探测到断开时，**正在跑的管线**能立刻看到。
    `request`：给了就启动**断开看门狗**（主动探测）。
    `replay`：给了就把生成与连接**解耦**——一个泵任务负责生成并写缓冲，
    连接只从缓冲尾随。这是断点续传（`Last-Event-ID`）的前提：
    连接断了生成还在，重连才能接着拿。
    """
    if cancel is None:
        cancel = threading.Event()

    async def _event_items():
        """执行器事件 → (event, payload)；异常一律变成 `error` 事件。

        抽出来的原因：直连模式与泵模式要用**同一份**错误映射，
        两份各写一遍必然会漂移（此前非流式路径缺背压分支就是这么来的）。
        """
        try:
            async for event, payload in executor.stream_events(factory, cancel):
                yield event, payload
        except OverloadedError:
            yield "error", {
                "code": int(ErrCode.RATE_LIMITED), "message": "服务繁忙，请稍后重试",
                "trace_id": trace_id,
            }
        except ValueError as exc:
            if on_value_error:
                yield "error", {
                    "code": int(ErrCode.INVALID_INPUT), "message": str(exc), "trace_id": trace_id,
                }
            else:
                logger.exception("SSE 参数异常 trace_id=%s", trace_id)
                yield "error", {
                    "code": int(ErrCode.INTERNAL_ERROR), "message": "服务暂时不可用",
                    "trace_id": trace_id,
                }
        except ApiError as exc:
            yield "error", {"code": int(exc.code), "message": exc.message, "trace_id": trace_id}
        except Exception:
            logger.exception("SSE 异常 trace_id=%s", trace_id)
            yield "error", {
                "code": int(ErrCode.INTERNAL_ERROR), "message": "服务暂时不可用",
                "trace_id": trace_id,
            }

    async def _pump():
        """独立于连接的生成任务：写缓冲，不写 socket。"""
        try:
            async for event, payload in _event_items():
                if event == "__heartbeat__":
                    replay.touch(trace_id)
                    continue
                replay.append(trace_id, event, payload)
        finally:
            cancel.set()
            replay.finish(trace_id)

    async def _tail():
        """只读缓冲：原连接与重连连接走同一段代码。"""
        cursor = 0
        last_ping = time.monotonic()
        while True:
            run = replay.get(trace_id)
            if run is None:
                # 缓冲被回收（TTL/上限）：流不能"装作继续"，明确告知不可续传
                yield _sse("error", {
                    "code": int(ErrCode.STREAM_NOT_RESUMABLE),
                    "message": "流式记录已过期，请重新提问", "trace_id": trace_id,
                })
                return
            pending = run.frames_after(cursor)
            if pending:
                for frame in pending:
                    cursor = frame.seq
                    yield frame.line
                continue  # 立刻再排一次，避免每帧多等一个轮询间隔
            if run.done:
                return
            now = time.monotonic()
            if now - last_ping >= _TAIL_PING_S:
                last_ping = now
                yield ": ping\n\n"
            await asyncio.sleep(_TAIL_POLL_S)

    async def event_source():
        watcher = None
        if request is not None:
            # `C-04`：主动探测客户端断开（模型生成期间没有新事件时也能及时发现）
            watcher = asyncio.create_task(
                _watch_disconnect(
                    request, cancel, grace_s=grace_s, replay=replay, trace_id=trace_id,
                )
            )
        try:
            if replay is None:
                async for event, payload in _event_items():
                    if event == "__heartbeat__":
                        yield ": ping\n\n"
                    else:
                        yield _sse(event, payload)
                return
            pump = asyncio.create_task(_pump())
            _PUMPS.add(pump)
            pump.add_done_callback(_PUMPS.discard)
            async for line in _tail():
                yield line
        finally:
            if watcher is not None:
                watcher.cancel()

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
        "X-Trace-Id": trace_id,
    }
    if replay is not None:
        # 能力声明：前端**只在看到这个头时**自动重连。没有它就重连等于重发问题
        # （换一条新 stream，重复计费且丢掉已看到的内容）。
        headers["X-SSE-Resume"] = "enabled"
        replay.open(trace_id, tenant_id=tenant_id, user_id=user_id)
    return StreamingResponse(event_source(), media_type="text/event-stream", headers=headers)


@router.post("/ask/stream", dependencies=[Depends(enforce_rate_limit)])
async def ask_stream(
    req: AskRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    pipeline: RagPipeline = Depends(get_pipeline),
    settings: Settings = Depends(get_app_settings),
) -> StreamingResponse:
    """流式问答（SSE）：事件序列见 `doc/analysis/03-接口与契约.md` 的 SSE 字典。

    token 在**终稿**确定后按 12 字切片重放（保证引用与终稿一致），
    因此它不是「首 token 延迟」意义上的真流式（真流式口径见 `doc/analysis/04-问答链路.md`）。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    _validate_question(req)
    executor: PipelineExecutor = request.app.state.pipeline_executor
    use_agent = _use_agent(req, settings)
    runner: AgentRunner | None = request.app.state.agent_runner if use_agent else None
    tenant, user, session_id = _agent_identity(req, principal)
    # 线性路径的主体绑定与 Agent 路径对齐
    scoped = None if runner is not None else pipeline.bind_principal(tenant, user, principal)

    # 请求级墙钟预算（与 /ask 同口径）：SSE 长连接同样必须有整体上限，
    # 否则上游一慢，连接会一直挂着直到网关掐断。
    deadline = Deadline(settings.ask_deadline_ms)
    # 在飞去重：前端在 SSE 失败后会自动重试一次 → 同一问题被算两次钱。
    # SSE 尤其危险——两次都"成功"，用户只会觉得答得慢，不会察觉被算了两次。
    idem: IdempotencyStore = request.app.state.idempotency
    inflight_key = content_key(tenant, req.question, session_id or "")
    if not idem.acquire(inflight_key):
        raise ApiError(
            ErrCode.REQUEST_IN_PROGRESS, "同一问题正在处理中，请稍候再试", http_status=409
        )

    # `C-04`：取消事件在**端点层**创建，factory 与看门狗共用同一个——
    # 这样「客户端断开」能被正在跑的管线立刻看到（而不是等下一次写事件）。
    cancel = threading.Event()

    def factory() -> Iterator[tuple[str, dict]]:
        try:
            if runner is not None:
                last_answer = ""
                for event, payload in runner.run(
                    req.question, tenant, user, session_id or None,
                    doc_name_resolver=request.app.state.knowledge.filename_of,
                    trace_id=trace_id,
                ):
                    if event == "_final":
                        last_answer = payload.get("answer", "")
                        yield "done", {**payload, "trace_id": trace_id, "session_id": session_id or None}
                    else:
                        yield event, payload
                # F5：首轮自动命名（流式结束后补写标题）
                _auto_title(request, principal, session_id, req.question, last_answer)
            else:
                yield from scoped.ask_stream(
                    req.question, tenant, trace_id, req.top_k,
                    deadline=deadline, cancel=cancel,
                )
        finally:
            # 流结束（含中断/异常）都要释放在飞占位，
            # 否则用户稍后再问同一个问题会被自己的 409 挡住
            idem.release(inflight_key)

    # 断点续传：开启后生成与连接解耦（泵任务写缓冲），断开有宽限期；
    # 关闭时行为与之前完全一致（断开即取消，不额外占内存）。
    replay_store: SseReplayStore | None = (
        request.app.state.sse_replay if settings.sse_resume_enabled else None
    )
    return _stream_response(
        executor, factory, trace_id, request=request, cancel=cancel,
        replay=replay_store, tenant_id=tenant, user_id=user,
        grace_s=settings.sse_resume_grace_s if replay_store is not None else 0.0,
    )


@router.get("/ask/stream/{trace_id}", dependencies=[Depends(enforce_rate_limit)])
async def resume_stream(
    trace_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    settings: Settings = Depends(get_app_settings),
) -> StreamingResponse:
    """按 `Last-Event-ID` 续传某次流式回答（断点续传）。

    ## 为什么不复用 POST /ask/stream

    续传**不能重新生成**——同一问题再跑一次既重复计费，回答也会和用户已经看到的
    前半段不一致。所以这里只读缓冲：从 `Last-Event-ID` 之后的事件开始发，
    直到该 run 的 `done`。

    客户端视角：`id` 是 `{trace_id}:{seq}`，把最后一个 `id` 原样塞进
    `Last-Event-ID` 请求头（或 `?last_event_id=`）即可。规范里 `Last-Event-ID`
    是头，查询参数是给不方便设头的客户端留的口子。

    三种拿不到的情况显式区分：run 不存在/过期/被挤出 → `STREAM_NOT_RESUMABLE`
    （4304，前端**不要**自动重试）；归属不符 → 404（不泄露他人 trace 是否存在）；
    能力关闭 → 404（这个端点根本没开）。
    """
    if not settings.sse_resume_enabled:
        raise ApiError(ErrCode.STREAM_NOT_RESUMABLE, "当前未开启流式续传", http_status=404)
    store: SseReplayStore = request.app.state.sse_replay
    tenant, user = _principal_identity(principal)
    if not store.is_resumable(trace_id, tenant_id=tenant, user_id=user):
        # 归属不符与"不存在"共用同一个 404：否则等于给了一个探测他人 trace 是否存在的接口
        raise ApiError(ErrCode.STREAM_NOT_RESUMABLE, "流式记录不存在或已过期", http_status=404)
    header_raw = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    _trace, start_seq = parse_last_event_id(header_raw)
    store.mark_resumed(trace_id)

    async def event_source():
        cursor = start_seq
        last_ping = time.monotonic()
        while True:
            run = store.get(trace_id)
            if run is None:
                yield _sse("error", {
                    "code": int(ErrCode.STREAM_NOT_RESUMABLE),
                    "message": "流式记录已过期，请重新提问", "trace_id": trace_id,
                })
                return
            pending = run.frames_after(cursor)
            if pending:
                for frame in pending:
                    cursor = frame.seq
                    yield frame.line
                continue
            if run.done:
                return
            now = time.monotonic()
            if now - last_ping >= _TAIL_PING_S:
                last_ping = now
                yield ": ping\n\n"
            await asyncio.sleep(_TAIL_POLL_S)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Trace-Id": trace_id,
            "X-SSE-Resume": "enabled",
        },
    )


def _assert_run_owner(run_id: str, principal: Principal, settings: Settings) -> None:
    """run_id 归属校验（ 纪律：跨主体的 run_id 一律 404，不用 403 避免存在性探测）。

    thread_id 约定为 `{tenant}:{user}:{seq}:{turn}`（见 `_agent_identity` 与
    `thread_id_for`），因此前缀即归属凭证。
    """
    expected = f"{principal.tenant_id}:{principal.user_id}:"
    if not run_id.startswith(expected):
        # 复用 SESSION_NOT_FOUND(4301)：run_id 由 session_id 派生，"归属不符"与
        # "会话不存在"是同一类错误，且 4301 语义已被前端识别。
        raise ApiError(ErrCode.SESSION_NOT_FOUND, "运行不存在或不属于当前用户", http_status=404)


@router.post("/ask/runs/{run_id}/cancel", dependencies=[Depends(enforce_rate_limit)])
def cancel_run(
    run_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    settings: Settings = Depends(get_app_settings),
) -> Envelope:
    """中止一个进行中的 Agent run。

    语义是**停止产生新的工具/模型调用**，而不是掐断正在执行的那一个——
    Python 线程无法被强制中断（见  的技术边界）。因此取消后
    最迟在"当前这一步跑完"时停止，`incomplete=True`。
    """
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    _assert_run_owner(run_id, principal, settings)
    registry = request.app.state.tool_registry
    registry.cancel(run_id)
    return ok_envelope(
        {"run_id": run_id, "cancelled": True}, trace_id, elapsed_ms_since(started)
    )


@router.get("/ask/{trace_id}/cost", response_model=Envelope)
def ask_cost(
    trace_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> Envelope:
    """单次问答的成本账单。

    按阶段与模型归集 token 与金额。`priced=False` 表示价格表未覆盖某个模型，
    此时金额**不可信**（显示为未知而非免费），前端应据此提示。
    """
    _tid = get_trace_id()
    set_trace_id(_tid)
    started = now_ms()
    # 按需载入：成本台账仅在展示时用
    from app.services.agent.cost import get_ledger

    # KQ-02：成本账单含模型与金额信息，必须按租户收敛（跨租户读取即越权）
    return ok_envelope(
        get_ledger().for_trace(trace_id, tenant_id=principal.tenant_id),
        _tid, elapsed_ms_since(started),
    )
