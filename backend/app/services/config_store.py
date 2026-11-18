"""运行时检索配置存取。

变更不立即生效：PUT 先存为 pending 并给出 effective_at，
到达生效时刻后自动晋升为 active（异步生效，前端需明确提示）。
配置持久化到数据目录，重启后保留。

**灰度（批次 6）**：除了全局 active/pending，还支持**带作用域的覆盖层**：

| scope | 语义 |
|---|---|
| `tenant:<id>` | 只对该租户生效 |
| `percent:<0-100>` | 按**稳定哈希**分桶，命中比例内的用户生效 |

分桶用 `sha256(tenant:user)` 而不是随机数——随机分桶会让同一用户每次请求
命中不同组，实验数据无法归因（"这个用户到底看的是哪套参数"根本答不出来）。
"""
import hashlib
import json
import threading
import time
from pathlib import Path

from app.domain.schemas import RetrievalConfig
from app.services.external_state import ExternalDocStore
from app.core.errors import InvalidInputError
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

#: 作用域前缀
SCOPE_TENANT = "tenant:"
SCOPE_PERCENT = "percent:"


def bucket_of(tenant_id: str, user_id: str = "", percent: int = 0) -> bool:
    """稳定分桶：同一 (tenant, user) 永远落在同一桶。

    用 sha256 而不是 `hash()`：后者按进程随机种子生成，
    多副本/重启后同一用户会落到不同桶，灰度数据直接失真。
    """
    if percent <= 0:
        return False
    if percent >= 100:
        return True
    raw = f"{tenant_id or ''}:{user_id or ''}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    return int(digest[:8], 16) % 100 < percent


class RetrievalConfigStore:
    """运行时检索配置。

    `docs` 不为空时配置（含**灰度覆盖层**）存在共享状态里 → 多副本一致。
    这条很关键：灰度"写在副本 A 的磁盘上、副本 B 看不到"会让实验数据无法归因。
    未配置 Redis 时仍落本地文件（单副本语义），行为与从前一致。
    """

    def __init__(
        self,
        path: Path,
        apply_delay_s: int = 300,
        settings=None,
        docs: "ExternalDocStore | None" = None,
    ) -> None:
        self._path = path
        self._delay_s = apply_delay_s
        self._settings = settings
        self._docs = docs
        self._lock = threading.RLock()  # view() 内会嵌套调用 get_active()
        self._active = self._defaults()
        self._pending: RetrievalConfig | None = None
        self._effective_at: float | None = None
        # 灰度覆盖层：scope → {config, enabled, experiment_id, percent}
        self._scoped: dict[str, dict] = {}
        self._load()

    def _defaults(self) -> RetrievalConfig:
        """默认配置以全局设置为基线。

        此前用的是 `RetrievalConfig()` 的字面默认，导致 `cache_ttl_s` /
        `ratelimit_*` 这三项环境变量无论怎么设都不生效。
        """
        if self._settings is None:
            return RetrievalConfig()
        return RetrievalConfig(
            top_k=self._settings.top_k,
            # 用 resolved：`deepseek` 重排通道下 `rerank_model` 的原始值可能是
            # 千问重排模型名（gte-*），原样下发会让 LLM 重排 400
            rerank_model=self._settings.resolved_rerank_model,
            cache_ttl_s=self._settings.cache_ttl_s,
            ratelimit_per_min=self._settings.ratelimit_refill_per_min,
            ratelimit_capacity=self._settings.ratelimit_capacity,
            rrf_k=self._settings.rrf_k,
            per_route_top_n=self._settings.retrieval_per_route_top_n,
            fused_top_n=self._settings.retrieval_fused_top_n,
        )

    def _normalize(self, config: RetrievalConfig) -> RetrievalConfig:
        """零值回落全局配置（存量 JSON 缺新字段时不会静默把召回数清零）。"""
        return config.with_fallbacks(self._settings) if self._settings else config

    # ---- 外置路径的键 ----

    _DOC_KEY = "config"
    _SCOPE_INDEX = "scopes"

    def _scope_key(self, scope: str) -> str:
        """scope 的文档 id。

        **直接用 scope 本身**（不再加 "scoped:" 前缀）：文档 id 会再被
        ExternalDocStore 拼上顶层前缀，若这里再加一次前缀，
        读回来时会被拼成 "retrieval:scoped:scoped:tenant:t1" → 永远读不到。
        """
        return scope

    def _external(self) -> bool:
        return self._docs is not None

    def _load_external(self) -> None:
        assert self._docs is not None
        doc = self._docs.get(self._DOC_KEY)
        if doc:
            self._active = self._normalize(RetrievalConfig.model_validate(doc.get("active", {})))
            if doc.get("pending"):
                self._pending = self._normalize(
                    RetrievalConfig.model_validate(doc["pending"])
                )
                self._effective_at = doc.get("effective_at")
        self._load_external_scoped()

    def _load_external_scoped(self) -> None:
        """读共享状态里的灰度覆盖层（覆盖当前内存副本）。"""
        assert self._docs is not None
        scoped: dict[str, dict] = {}
        for scope in self._docs.list_ids(self._SCOPE_INDEX):
            item = self._docs.get(scope)
            if not item:
                continue
            try:
                scoped[scope] = {
                    "config": self._normalize(
                        RetrievalConfig.model_validate(item.get("config") or {})
                    ),
                    "enabled": bool(item.get("enabled", True)),
                    "experiment_id": str(item.get("experiment_id") or ""),
                }
            except Exception:  # noqa: BLE001 - 灰度覆盖项解析失败：跳过该项
                logger.debug("灰度覆盖项解析失败：跳过该项")
                continue
        self._scoped = scoped

    def _refresh_external(self) -> None:
        """外置模式：**每次读都刷新**（读穿透）。

        只在启动时加载一次是不够的：副本 A 改了灰度作用域，副本 B 的内存快照
        还是旧的 → 灰度"时灵时不灵"，实验数据无法归因。
        """
        if not self._external():
            return
        self._load_external_scoped()

    def _save_external(self) -> None:
        assert self._docs is not None
        self._docs.put(
            self._DOC_KEY,
            {
                "active": self._active.model_dump(),
                "pending": self._pending.model_dump() if self._pending else None,
                "effective_at": self._effective_at,
            },
        )
        for scope, item in list(self._scoped.items()):
            self._docs.put(
                self._scope_key(scope),
                {
                    "config": item["config"].model_dump(),
                    "enabled": item["enabled"],
                    "experiment_id": item["experiment_id"],
                },
                index=self._SCOPE_INDEX,
                score=0.0,
            )

    def _load(self) -> None:
        if self._external():
            self._load_external()
            return
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._active = self._normalize(RetrievalConfig.model_validate(raw.get("active", {})))
            if raw.get("pending"):
                self._pending = self._normalize(RetrievalConfig.model_validate(raw["pending"]))
                self._effective_at = raw.get("effective_at")
            for scope, item in (raw.get("scoped") or {}).items():
                try:
                    self._scoped[scope] = {
                        "config": self._normalize(
                            RetrievalConfig.model_validate(item.get("config") or {})
                        ),
                        "enabled": bool(item.get("enabled", True)),
                        "experiment_id": str(item.get("experiment_id") or ""),
                    }
                except Exception:  # noqa: BLE001 - 灰度覆盖项解析失败：跳过该项
                    logger.warning("灰度覆盖项解析失败：跳过该项")
                    continue  # 单条灰度配置损坏不影响其余
        except Exception:  # noqa: BLE001 - 配置损坏不阻断启动：回退默认配置（配置项已丢失，需人工确认）
            logger.warning("配置损坏不阻断启动：回退默认配置（配置项已丢失，需人工确认）")
            # 配置损坏不阻断启动：回退默认配置
            self._active = self._defaults()
            self._pending = None
            self._effective_at = None
            self._scoped = {}

    def _save(self) -> None:
        if self._external():
            self._save_external()
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "active": self._active.model_dump(),
                    "pending": self._pending.model_dump() if self._pending else None,
                    "effective_at": self._effective_at,
                    "scoped": {
                        scope: {
                            "config": item["config"].model_dump(),
                            "enabled": item["enabled"],
                            "experiment_id": item["experiment_id"],
                        }
                        for scope, item in self._scoped.items()
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    def get_active(self) -> RetrievalConfig:
        """返回当前生效配置；pending 到达生效时刻即晋升。"""
        with self._lock:
            if self._pending is not None and self._effective_at is not None:
                if time.time() >= self._effective_at:
                    self._active = self._pending
                    self._pending = None
                    self._effective_at = None
                    self._save()
            return self._active

    def view(self) -> dict:
        with self._lock:
            return {
                "active": self.get_active().model_dump(),
                "pending": self._pending.model_dump() if self._pending else None,
                "effective_at": self._effective_at,
            }

    def update(self, config: RetrievalConfig) -> float:
        """存为 pending，返回生效时刻（epoch 秒）。"""
        with self._lock:
            self._pending = self._normalize(config)
            self._effective_at = time.time() + self._delay_s
            self._save()
            return self._effective_at

    def reset(self) -> float:
        return self.update(self._defaults())

    # ---- 灰度覆盖层 ----

    def set_scoped(
        self,
        scope: str,
        config: RetrievalConfig,
        enabled: bool = True,
        experiment_id: str = "",
    ) -> dict:
        """登记（或覆盖）一个作用域的覆盖配置。

        scope 形如 `tenant:<id>` / `percent:<0-100>`。
        `experiment_id` 只进日志与响应——**不进指标 label**（高基数）。
        """
        scope = (scope or "").strip()
        if not scope:
            raise InvalidInputError("scope 不能为空")
        if scope.startswith(SCOPE_TENANT) and not scope[len(SCOPE_TENANT):]:
            raise InvalidInputError("tenant scope 缺少租户 id")
        if scope.startswith(SCOPE_PERCENT):
            pct = _percent_of(scope)
            if pct < 0 or pct > 100:
                raise InvalidInputError("percent scope 必须在 0~100 之间")
        with self._lock:
            self._scoped[scope] = {
                "config": self._normalize(config),
                "enabled": bool(enabled),
                "experiment_id": str(experiment_id or ""),
            }
            self._save()
            if self._external():
                assert self._docs is not None
                self._docs.put(
                    self._scope_key(scope),
                    {
                        "config": self._scoped[scope]["config"].model_dump(),
                        "enabled": self._scoped[scope]["enabled"],
                        "experiment_id": self._scoped[scope]["experiment_id"],
                    },
                    index=self._SCOPE_INDEX,
                    score=0.0,
                )
            return dict(self._scoped[scope]["config"].model_dump())

    def clear_scoped(self, scope: str) -> bool:
        """移除一个作用域的覆盖（返回是否真的删掉了）。"""
        with self._lock:
            existed = self._scoped.pop(scope, None) is not None
            if existed:
                self._save()
                if self._external():
                    assert self._docs is not None
                    self._docs.delete(self._scope_key(scope), index=self._SCOPE_INDEX)
            return existed

    def scoped_view(self) -> list[dict]:
        """所有灰度覆盖的当前状态（管理端展示）。"""
        self._refresh_external()
        with self._lock:
            return [
                {
                    "scope": scope,
                    "enabled": item["enabled"],
                    "experiment_id": item["experiment_id"],
                    "config": item["config"].model_dump(),
                }
                for scope, item in sorted(self._scoped.items())
            ]

    def resolve(self, tenant_id: str, user_id: str = "") -> tuple[RetrievalConfig, str]:
        """解析某主体当前应使用的配置 → (配置, 实验标签)。

        优先级：**租户定向 > 百分比分桶 > 全局**。
        租户定向优先于分桶：定向是人为指定的，分桶是抽样，两者的意图不同时
        以更具体的那个为准。
        """
        base = self.get_active()
        self._refresh_external()
        with self._lock:
            tenant_scope = f"{SCOPE_TENANT}{tenant_id or ''}"
            item = self._scoped.get(tenant_scope)
            if item and item["enabled"]:
                label = tenant_scope
                if item["experiment_id"]:
                    label = f"{tenant_scope}#{item['experiment_id']}"
                return item["config"], label
            for scope, item in sorted(self._scoped.items()):
                if not scope.startswith(SCOPE_PERCENT) or not item["enabled"]:
                    continue
                pct = _percent_of(scope)
                if pct <= 0 or not bucket_of(tenant_id, user_id, pct):
                    continue
                label = f"{scope}#{item['experiment_id']}" if item["experiment_id"] else scope
                return item["config"], label
        return base, ""


def _percent_of(scope: str) -> int:
    """从 `percent:10` 里取比例（非法返回 -1）。"""
    try:
        return int(scope.split(SCOPE_PERCENT, 1)[1])
    except (IndexError, ValueError):
        return -1
