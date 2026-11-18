"""缓存阶段：键 / 语义近似读写 / single-flight（`B0 X-27` 第二步）。

从 `services/pipeline.py` 抽出的**缓存与去重**职责，边界严格：

- 只做「算键、查、写、合并并发」四件事；
- **不组装 `AskResult`、不写请求指标**——那属于编排层（`core`），否则阶段会反向
  依赖 `core`，拆分就只是把耦合换了位置。

首参一律是 `pip`（被 `bind_principal` 绑定过的 pipeline 副本）。这是刻意的：
流水线是单例，`bind_principal` 通过 `__dict__.update` 产出**请求级副本**
（`_tenant_id` / `_experiment_label` / `_req_cache` 都挂在副本上），
如果把 pipeline 存进协作者对象，拆分后所有请求都会用回单例的那份状态。
"""
import threading

from ...core.envelope import ErrCode
from ...core.logging_setup import get_logger
from ...domain.schemas import RetrievalConfig
from ..cache import config_digest, make_cache_key

logger = get_logger(__name__)

# 等待者复用结果的上限；超时自行执行，避免等待者占满线程
SF_WAIT_S = 30.0


def model_version(pip, config: RetrievalConfig) -> str:
    """模型版本串：换生成 / 向量 / 重排模型都会让旧缓存失效。"""
    settings = pip._settings
    return (
        f"{settings.resolved_provider}:{settings.llm_model}"
        f"+{settings.embedding_model}+{config.rerank_model}"
    )


def cache_dims(pip, config: RetrievalConfig) -> tuple[str, str]:
    """返回 (config_digest, 维度签名)。同一次请求只算一次，键与签名共用。"""
    digest = config_digest(
        {
            "retrieval": config.model_dump(),
            "chunk_size": pip._settings.chunk_size,
            "temperature": pip._settings.temperature,
            "index_version": pip._index_version(),
            "crag": pip._crag is not None,
            # 缓存键必须携带身份——缓存命中路径跳过检索，
            # 不同主体共享缓存会把一个人的答案喂给另一个人
            "user": pip._principal_user,
        }
    )
    # KQ-04：语义近似命中只按键无法区分主体与版本，必须额外比对维度签名
    return digest, f"{model_version(pip, config)}:{digest}"


def cache_key(
    pip,
    question: str,
    tenant_id: str,
    config: RetrievalConfig,
    doc_id: str | None = None,
) -> str:
    digest, _ = cache_dims(pip, config)
    # F4 快问：scope 维度进键——快问与全库检索的问题文本可能完全一样，
    # 不区分就会互相命中（跨范围串答案）。
    return make_cache_key(
        tenant_id, question, model_version(pip, config), digest, scope=doc_id or ""
    )


def question_vector(pip, question: str) -> list:
    """问题向量（供语义近似命中）。编码失败返回空列表，退回精确匹配。"""
    embedder = getattr(pip._cache, "_embeddings", None)
    if embedder is None:
        return []
    try:
        vectors = embedder.embed([question])
        return list(vectors[0]) if vectors else []
    except Exception as exc:  # noqa: BLE001 - 缓存问题编码失败，退回精确匹配
        logger.warning("缓存问题编码失败，退回精确匹配: %s", exc)
        return []


def cache_lookup(pip, question: str, tenant_id: str, key: str, scope: str = "", signature: str = ""):
    """返回 (entry, hit_kind)。缓存不可用时静默 miss + 上报 45xx 计数。"""
    cache = pip._cache
    if not hasattr(cache, "lookup"):
        return cache.get(key), "exact"
    try:
        return cache.lookup(
            question, key, question_vector(pip, question),
            tenant_id=tenant_id, scope=scope, signature=signature,
        )
    except TypeError:
        # 旧实现没有 scope / signature 维度：只能退回精确命中
        #（宁可少命中，不可串范围或串主体）
        try:
            return cache.lookup(
                question, key, question_vector(pip, question), tenant_id=tenant_id
            )
        except Exception as exc:  # noqa: BLE001 - 缓存读取降级
            logger.warning("缓存读取降级（本次不走缓存）: %s", exc)
            pip._prom.cache_total.labels(result="error").inc()
            pip._prom.record_error(int(ErrCode.CACHE_UNAVAILABLE))
            return None, "miss"
    except Exception as exc:  # noqa: BLE001 - 缓存读取降级
        # 45xx：无感降级，只上报指标不回传用户
        logger.warning("缓存读取降级（本次不走缓存）: %s", exc)
        pip._prom.cache_total.labels(result="error").inc()
        # 45xx 降级码（`A-10` 接线）：缓存不可用是**无感降级**，
        # 用户看不到 warn 提示，所以码在这里上报，不走信封。
        pip._prom.record_error(int(ErrCode.CACHE_UNAVAILABLE))
        return None, "miss"


def cache_store(
    pip, key: str, question: str, payload: dict, ttl: float,
    tenant_id: str = "", scope: str = "", signature: str = "",
) -> None:
    """写入缓存。优先用支持"问题 + 向量 + 租户 + 范围 + 维度签名"的新签名，旧实现退回三参数。"""
    try:
        pip._cache.set(
            key, payload, ttl, question=question,
            vector=question_vector(pip, question), tenant_id=tenant_id,
            scope=scope, signature=signature,
        )
        return
    except TypeError:
        pass
    except Exception as exc:  # noqa: BLE001 - 缓存写入降级
        pip._prom.cache_total.labels(result="error").inc()
        pip._prom.record_error(int(ErrCode.CACHE_UNAVAILABLE))
        logger.warning("缓存写入降级（不影响本次返回）: %s", exc)
        return
    try:
        pip._cache.set(key, payload, ttl)
    except Exception as exc:  # noqa: BLE001 - 缓存写入降级
        pip._prom.cache_total.labels(result="error").inc()
        pip._prom.record_error(int(ErrCode.CACHE_UNAVAILABLE))
        logger.warning("缓存写入降级（不影响本次返回）: %s", exc)


# ---- single-flight（批次 2）：相同问题的并发请求合并为一次真实计算 ----


def sf_acquire(pip, cache_key_value: str) -> tuple[threading.Event, bool]:
    """返回 (event, 是否首个请求)。首个请求负责真实计算并在 finally 里 release。"""
    with pip._inflight_lock:
        ev = pip._inflight.get(cache_key_value)
        if ev is None:
            ev = threading.Event()
            pip._inflight[cache_key_value] = ev
            return ev, True
        return ev, False


def sf_release(pip, cache_key_value: str, ev: threading.Event) -> None:
    with pip._inflight_lock:
        if pip._inflight.get(cache_key_value) is ev:
            pip._inflight.pop(cache_key_value, None)
    ev.set()
