"""运行时检索配置存取（阶段 2 前端规范 §4.2 / §4.3）。

变更不立即生效：PUT 先存为 pending 并给出 effective_at，
到达生效时刻后自动晋升为 active（异步生效，前端需明确提示）。
配置持久化到数据目录，重启后保留。
"""
import json
import threading
import time
from pathlib import Path

from app.domain.schemas import RetrievalConfig


class RetrievalConfigStore:
    def __init__(self, path: Path, apply_delay_s: int = 300):
        self._path = path
        self._delay_s = apply_delay_s
        self._lock = threading.RLock()  # view() 内会嵌套调用 get_active()
        self._active = RetrievalConfig()
        self._pending: RetrievalConfig | None = None
        self._effective_at: float | None = None
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._active = RetrievalConfig.model_validate(raw.get("active", {}))
            if raw.get("pending"):
                self._pending = RetrievalConfig.model_validate(raw["pending"])
                self._effective_at = raw.get("effective_at")
        except Exception:
            # 配置损坏不阻断启动：回退默认配置
            self._active = RetrievalConfig()
            self._pending = None
            self._effective_at = None

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "active": self._active.model_dump(),
                    "pending": self._pending.model_dump() if self._pending else None,
                    "effective_at": self._effective_at,
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
            self._pending = config
            self._effective_at = time.time() + self._delay_s
            self._save()
            return self._effective_at

    def reset(self) -> float:
        return self.update(RetrievalConfig())
