"""DocStore：父块 / 表格 / 媒体资源的结构化存储（阶段 3 §2.3 / §4.3）。

父子映射是显式关系数据，不能塞进向量库（会污染相似度计算）——
结构化数据与向量化数据分开存储。接口与存储解耦：
当前为 JSON 文件持久化 + 内存索引；生产可换 Redis / 关系库，接口不变。
"""
import json
import logging
import threading
from pathlib import Path

from app.domain.schemas import Chunk, MediaAsset, TableRecord

logger = logging.getLogger(__name__)


class DocStore:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._parents: dict[str, Chunk] = {}  # parent_id → 父块
        self._tables: dict[str, TableRecord] = {}
        self._assets: dict[str, MediaAsset] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._parents = {k: Chunk.model_validate(v) for k, v in raw.get("parents", {}).items()}
            self._tables = {k: TableRecord.model_validate(v) for k, v in raw.get("tables", {}).items()}
            self._assets = {k: MediaAsset.model_validate(v) for k, v in raw.get("assets", {}).items()}
        except Exception as exc:
            raise RuntimeError(f"DocStore 数据损坏，拒绝启动: {exc}") from exc

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "parents": {k: v.model_dump() for k, v in self._parents.items()},
                    "tables": {k: v.model_dump() for k, v in self._tables.items()},
                    "assets": {k: v.model_dump() for k, v in self._assets.items()},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    # ---- 父块 ----

    def put_parents(self, parents: list[Chunk]) -> None:
        with self._lock:
            for p in parents:
                self._parents[p.chunk_id] = p
            self._save()

    def get_parent(self, parent_id: str) -> Chunk | None:
        return self._parents.get(parent_id)

    def parents_of_doc(self, doc_id: str) -> list[Chunk]:
        return [p for p in self._parents.values() if p.doc_id == doc_id]

    # ---- 表格 ----

    def put_tables(self, tables: list[TableRecord]) -> None:
        with self._lock:
            for t in tables:
                self._tables[t.table_id] = t
            self._save()

    def tables_of_doc(self, doc_id: str) -> list[TableRecord]:
        return sorted(
            [t for t in self._tables.values() if t.doc_id == doc_id],
            key=lambda t: (t.page, t.table_index),
        )

    # ---- 媒体资源 ----

    def put_assets(self, assets: list[MediaAsset]) -> None:
        with self._lock:
            for a in assets:
                self._assets[a.asset_id] = a
            self._save()

    def assets_of_doc(self, doc_id: str) -> list[MediaAsset]:
        return [a for a in self._assets.values() if a.doc_id == doc_id]

    def find_asset_by_hash(self, content_hash: str) -> MediaAsset | None:
        """跨文档去重：同一张图只入一次。"""
        for a in self._assets.values():
            if a.hash == content_hash:
                return a
        return None

    # ---- 清理 ----

    def delete_doc(self, doc_id: str) -> None:
        with self._lock:
            self._parents = {k: v for k, v in self._parents.items() if v.doc_id != doc_id}
            self._tables = {k: v for k, v in self._tables.items() if v.doc_id != doc_id}
            self._assets = {k: v for k, v in self._assets.items() if v.doc_id != doc_id}
            self._save()
