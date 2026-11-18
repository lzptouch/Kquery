"""索引别名：让"重建索引"可以不停机切换。

**问题**：改 chunk 策略或换 embedding 模型 → 必须重建整个索引。
若直接在生产集合上重建，期间检索结果是**新旧混合**的（脏读），
而且重建失败等于把线上索引搞坏了。

**解法**：物理索引与"名字"分离——
    物理：Milvus 集合 `kquery_chunks_v3` / FAISS 目录 `index_v3/`
    别名：`kquery_chunks`（逻辑名，检索层永远用它）
重建流程：新物理索引 → 后台重灌 → 指标比对 → **原子切换**（秒级，可回滚）。

本模块是别名的**真相源**：一个指针文件 `<data_dir>/index_alias.json`。

    {"alias": "kquery_chunks", "physical": "kquery_chunks_v3",
     "version": 3, "previous": "kquery_chunks_v2", "updated_at": "..."}

原子性靠"写临时文件 + `os.replace`"——替换是同文件系统内的原子操作，
不会出现"读到半个 JSON"的中间态。

**刻意不做的**：双写编排、重建调度、指标比对。这些属于运维流程（`scripts/`），
本模块只保证"切换这一步是原子的、可回滚的"。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from app.core.errors import InvalidInputError
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

POINTER_FILE = "index_alias.json"


class IndexAliasManager:
    """索引别名的真相源（指针文件）。"""

    def __init__(self, data_dir: Path, base_name: str = "index") -> None:
        self._dir = Path(data_dir)
        self._base = base_name
        self._path = self._dir / POINTER_FILE

    # ---- 内部 ----

    def _read(self) -> dict:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write(self, payload: dict) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)  # 原子替换：不会读到半个 JSON

    # ---- 对外 ----

    def ensure_initialized(self) -> dict:
        """首次使用时建立指针（幂等）：指向既有索引，不动任何数据。"""
        current = self._read()
        if current.get("physical"):
            return current
        payload = {
            "alias": self._base,
            "physical": self._base,
            "version": 1,
            "previous": "",
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._write(payload)
        logger.info("索引别名已初始化：%s → %s", self._base, self._base)
        return payload

    def status(self) -> dict:
        """当前别名状态（供管理端与探针展示）。"""
        current = self.ensure_initialized()
        return {
            "alias": current.get("alias", self._base),
            "physical": current.get("physical", self._base),
            "version": int(current.get("version", 1) or 1),
            "previous": current.get("previous", ""),
            "updated_at": current.get("updated_at", ""),
        }

    def version(self) -> int:
        """别名版本号（进 `data_version`：切换后前端据此失效缓存）。"""
        return int(self.status().get("version", 1) or 1)

    def physical_name(self) -> str:
        """当前应使用的物理索引名（目录名 / 集合名）。"""
        return str(self.status().get("physical") or self._base)

    def next_physical_name(self) -> str:
        """下一个物理索引名（重建时用它建新索引）。"""
        return f"{self._base}_v{self.version() + 1}"

    def switch(self, physical: str | None = None) -> dict:
        """原子切换别名指向的物理索引。

        `physical` 为空时切到"下一个"物理名（`{base}_v{version+1}`）。
        旧物理索引**不删**——回滚只需再切回去（秒级）。
        """
        current = self.ensure_initialized()
        target = (physical or self.next_physical_name()).strip()
        if not target:
            raise InvalidInputError("物理索引名不能为空")
        if target == current.get("physical"):
            return self.status()
        payload = {
            "alias": current.get("alias", self._base),
            "physical": target,
            "version": int(current.get("version", 1) or 1) + 1,
            "previous": current.get("physical", ""),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._write(payload)
        logger.info(
            "索引别名已切换：%s → %s（上一个：%s）",
            current.get("physical"), target, payload["previous"],
        )
        return self.status()

    def rollback(self) -> dict:
        """回滚到上一个物理索引（重建把线上搞坏了时的退路）。"""
        current = self.status()
        previous = str(current.get("previous") or "")
        if not previous:
            raise InvalidInputError("没有可回滚的上一个索引")
        payload = {
            "alias": current.get("alias", self._base),
            "physical": previous,
            "version": max(1, int(current.get("version", 1) or 1) - 1),
            "previous": current.get("physical", ""),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._write(payload)
        logger.info("索引别名已回滚：%s → %s", current.get("physical"), previous)
        return self.status()
