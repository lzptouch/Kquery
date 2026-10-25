"""快问服务（F4 · 单文档「上传即问」通道）。

## 它解决什么

普通用户（无 admin 权限）想就**一个刚拿到的文件**提问，不想先入库、
不想污染知识库、也不想等异步任务。快问把这条路径压缩成：
上传 → 解析 → 只检索这一个文件 → 回答 → 到期自动清理。

## 四条不可让步的约束

1. **不入知识库**：不调 `create_record`（它会写 `_docs` 与 `metadata.json`），
   不写 DocStore，不出现在文件列表里。
2. **不 bump 索引版本**：`index_version` 进语义缓存键，每次快问都 bump
   会清空全站缓存。清理走 `purge_transient`（不 bump）。
3. **向量 + BM25 双写**：只写向量库会让 RRF 退化为单路，关键词精确命中零召回。
4. **缓存键带 scope**：快问与全库检索的问题文本可能完全一样，
   不区分就会互相命中（跨范围串答案）。

## 临时态存哪

优先 Redis（多副本下 B 副本要能看到 A 副本上传的文档），Redis 不可用时
**Fail-Open** 降级为进程内实现——快问不可用不应让整个服务不可用。
"""
import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from app.core.config import Settings
from app.core.envelope import ApiError, ErrCode
from app.domain.schemas import DOC_STATUS_PARSING, DocumentRecord
from app.services.knowledge import KnowledgeService
from app.services.parsers.router import SUPPORTED_EXTENSIONS

logger = logging.getLogger(__name__)

#: 临时文档 doc_id 前缀（启动清扫据此识别残留）
QUICK_PREFIX = "quick_"
#: 临时元数据的 key 前缀
QUICK_KEY_PREFIX = "quick:"


class QuickAskStore(Protocol):
    """快问临时元数据存储（进程内 / Redis 两种实现）。"""

    def put(self, doc_id: str, meta: dict, ttl_s: int) -> None: ...

    def get(self, doc_id: str) -> dict | None: ...

    def delete(self, doc_id: str) -> None: ...

    def list_doc_ids(self) -> list[str]: ...


class InMemoryQuickAskStore:
    """进程内实现（单副本 / Redis 不可用时的降级目标）。"""

    def __init__(self) -> None:
        self._items: dict[str, tuple[dict, float]] = {}  # doc_id → (meta, expire_at)

    def put(self, doc_id: str, meta: dict, ttl_s: int) -> None:
        # ttl_s <= 0 表示"立即过期"（0 与"不设过期"必须区分开，
        # 否则用 `if expire_at` 判断过期时间时，0 会被当成"没有过期时间"）
        expires_at = (time.time() + ttl_s) if ttl_s > 0 else 0.0
        self._items[doc_id] = (meta, expires_at)

    def get(self, doc_id: str) -> dict | None:
        item = self._items.get(doc_id)
        if item is None:
            return None
        meta, expires_at = item
        if expires_at <= 0 or expires_at < time.time():
            self._items.pop(doc_id, None)
            return None
        return meta

    def delete(self, doc_id: str) -> None:
        self._items.pop(doc_id, None)

    def list_doc_ids(self) -> list[str]:
        return list(self._items.keys())


class RedisQuickAskStore:
    """Redis 实现（多副本共享）。Redis 不可用时**降级**为进程内。

    Fail-Open 是硬约束：Redis 挂了不能变成"快问不可用"。
    """

    def __init__(self, state_store, fallback: InMemoryQuickAskStore | None = None):
        self._state = state_store
        self._fallback = fallback or InMemoryQuickAskStore()

    def _key(self, doc_id: str) -> str:
        return f"{QUICK_KEY_PREFIX}{doc_id}"

    def put(self, doc_id: str, meta: dict, ttl_s: int) -> None:
        try:
            self._state.set(self._key(doc_id), json.dumps(meta, ensure_ascii=False), ttl_s)
        except Exception as exc:  # noqa: BLE001 - 降级而非报错
            logger.warning("快问元数据写入 Redis 失败（降级为进程内）: %s", exc)
            self._fallback.put(doc_id, meta, ttl_s)

    def get(self, doc_id: str) -> dict | None:
        try:
            raw = self._state.get(self._key(doc_id))
            if raw:
                return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("快问元数据读取 Redis 失败（降级为进程内）: %s", exc)
        return self._fallback.get(doc_id)

    def delete(self, doc_id: str) -> None:
        try:
            self._state.delete(self._key(doc_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("快问元数据删除 Redis 失败: %s", exc)
        self._fallback.delete(doc_id)

    def list_doc_ids(self) -> list[str]:
        """列出已登记的快问文档（Redis 侧用 SCAN 前缀，避免 KEYS 阻塞）。"""
        doc_ids: list[str] = []
        redis = getattr(self._state, "_redis", None)
        client = getattr(redis, "_client", None) if redis is not None else None
        if client is not None:
            try:
                # key 前缀在 RedisStore 上（SharedState 自己没有 _prefix）。
                # 用错属性会抛 AttributeError，被 except 吞掉 → 多副本下永远扫不到
                # 别人的快问文档（启动清扫形同虚设），且只在日志里留一行警告。
                prefix = getattr(redis, "_prefix", "")
                for key in client.scan_iter(match=f"{prefix}{QUICK_KEY_PREFIX}*"):
                    doc_ids.append(str(key).split(QUICK_KEY_PREFIX, 1)[-1])
            except Exception as exc:  # noqa: BLE001
                logger.warning("快问元数据扫描失败: %s", exc)
        doc_ids.extend(self._fallback.list_doc_ids())
        return doc_ids


class QuickAskService:
    """快问服务：解析编排 + 临时态登记 + 过期清扫。"""

    def __init__(
        self,
        settings: Settings,
        knowledge: KnowledgeService,
        store: QuickAskStore | None = None,
    ):
        self._settings = settings
        self._knowledge = knowledge
        self._store = store or InMemoryQuickAskStore()

    # ---- 属性 ----

    @property
    def ttl_s(self) -> int:
        return int(self._settings.quick_ask_ttl_s)

    @property
    def max_bytes(self) -> int:
        limit = int(self._settings.quick_ask_max_mb)
        return min(limit, int(self._settings.max_upload_mb)) * 1024 * 1024

    @property
    def max_pages(self) -> int:
        return int(self._settings.quick_ask_max_pages)

    def _temp_dir(self) -> Path:
        return self._settings.data_dir / "uploads" / "quick"

    # ---- 上传与解析 ----

    async def upload(
        self, filename: str, content: bytes, principal, session_id: str | None = None
    ) -> dict:
        """上传并解析一个文件，返回临时文档信息。"""
        ext = Path(filename).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            raise ApiError(
                ErrCode.UNSUPPORTED_FILE_TYPE,
                f"不支持的文件类型 {ext}（支持：{sorted(SUPPORTED_EXTENSIONS)}）",
            )
        if ext == ".xls":
            raise ApiError(
                ErrCode.UNSUPPORTED_FILE_TYPE,
                "不支持旧版 Excel 格式（.xls），请另存为 .xlsx 后重试",
            )
        if not content:
            raise ApiError(ErrCode.INVALID_INPUT, "文件内容为空")
        if len(content) > self.max_bytes:
            raise ApiError(
                ErrCode.FILE_TOO_LARGE,
                f"快问文件超过 {self.max_bytes // (1024 * 1024)}MB 限制，请改用知识库入库",
            )

        doc_id = f"{QUICK_PREFIX}{uuid4().hex[:16]}"
        path = self._write_temp(doc_id, filename, content)

        record = DocumentRecord(
            doc_id=doc_id,
            file_id=doc_id,
            filename=filename,
            tenant_id=principal.tenant_id,
            source_path=str(path),
            content_hash=hashlib.sha256(content).hexdigest(),
            status=DOC_STATUS_PARSING,
            size=len(content),
        )
        try:
            parsed = await asyncio.to_thread(self._knowledge.parse_document, record)
        except ApiError:
            path.unlink(missing_ok=True)
            raise
        if len(parsed.segments) == 0:
            path.unlink(missing_ok=True)
            raise ApiError(ErrCode.PARSE_FAILED, "未解析到有效内容")
        if len(parsed.segments) > self.max_pages:
            path.unlink(missing_ok=True)
            raise ApiError(
                ErrCode.FILE_TOO_LARGE,
                f"快问仅支持 {self.max_pages} 页/段以内的文件，请改用知识库入库",
            )

        try:
            chunks = await asyncio.to_thread(self._knowledge.index_transient, record, parsed)
        except ApiError:
            path.unlink(missing_ok=True)
            raise

        self._remember(doc_id, principal, filename, chunks, path, session_id=session_id)
        return {
            "doc_id": doc_id,
            "filename": filename,
            "chunks": chunks,
            "ttl_s": self.ttl_s,
            "expires_in_s": self.ttl_s,
            "session_id": session_id or "",
        }

    def _write_temp(self, doc_id: str, filename: str, content: bytes) -> Path:
        directory = self._temp_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{doc_id}{Path(filename).suffix.lower()}"
        path.write_bytes(content)
        return path

    def _remember(
        self,
        doc_id: str,
        principal,
        filename: str,
        chunks: int,
        path: Path,
        session_id: str | None = None,
    ) -> None:
        self._store.put(
            doc_id,
            {
                "doc_id": doc_id,
                "filename": filename,
                "chunks": chunks,
                "tenant_id": principal.tenant_id,
                "user_id": principal.user_id,
                # 会话维度：临时文档归属到某次对话（对齐 doc-qa 的"会话内快速解析"）。
                # 空串 = 未绑定会话（仍可跨会话使用，只是不参与会话维度查询）。
                "session_id": session_id or "",
                "path": str(path),
                "created_at": time.time(),
            },
            self.ttl_s,
        )

    # ---- 读取 / 校验 ----

    def get_meta(self, doc_id: str, principal, session_id: str | None = None) -> dict:
        """取快问文档元信息并校验归属、有效期与会话维度。

        过期/不存在/越权/不属于该会话，一律返回同一个错误码（3004），
        避免通过错误码差异探测他人文档是否存在。

        `session_id` 只在**文档绑定了会话**时才校验：未绑定（空串）的文档
        可以在任意会话里使用——那正是"上传即问"的用法。
        """
        meta = self._store.get(doc_id)
        if not meta:
            raise ApiError(
                ErrCode.QUICK_DOC_EXPIRED, "临时文档已过期或不存在，请重新上传", http_status=404
            )
        if meta.get("user_id") != principal.user_id or meta.get("tenant_id") != principal.tenant_id:
            raise ApiError(
                ErrCode.QUICK_DOC_EXPIRED, "临时文档已过期或不存在，请重新上传", http_status=404
            )
        bound_session = meta.get("session_id") or ""
        if bound_session and session_id is not None and bound_session != session_id:
            raise ApiError(
                ErrCode.QUICK_DOC_EXPIRED,
                "临时文档不属于当前会话，请重新上传",
                http_status=404,
            )
        return meta

    def list_session_docs(self, session_id: str, principal) -> list[dict]:
        """列出某会话下的临时文档（会话内快速解析的语义）。

        只返回**本人**且仍有效的文档；过期条目直接顺手清掉。
        """
        out: list[dict] = []
        for doc_id in list(self._store.list_doc_ids()):
            if not doc_id.startswith(QUICK_PREFIX):
                continue
            meta = self._store.get(doc_id)
            if not meta:
                continue
            if meta.get("user_id") != principal.user_id:
                continue
            if meta.get("tenant_id") != principal.tenant_id:
                continue
            if (meta.get("session_id") or "") != session_id:
                continue
            out.append(
                {
                    "doc_id": doc_id,
                    "filename": meta.get("filename", ""),
                    "chunks": meta.get("chunks", 0),
                    "created_at": meta.get("created_at", 0),
                    "ttl_s": self.ttl_s,
                }
            )
        out.sort(key=lambda x: x.get("created_at", 0))
        return out

    def status(self, doc_id: str, principal) -> dict:
        meta = self.get_meta(doc_id, principal)
        return {
            "doc_id": doc_id,
            "filename": meta.get("filename", ""),
            "chunks": meta.get("chunks", 0),
            "ttl_s": self.ttl_s,
            "session_id": meta.get("session_id", ""),
        }

    # ---- 删除 ----

    def delete(self, doc_id: str, principal) -> None:
        meta = self.get_meta(doc_id, principal)
        self._knowledge.purge_transient(doc_id)
        self._store.delete(doc_id)
        path = Path(meta.get("path", ""))
        if str(path) and path.exists():
            try:
                path.unlink()
            except OSError as exc:  # noqa: BLE001 - 删文件失败不影响索引已清理
                logger.warning("快问临时文件删除失败 %s: %s", path, exc)

    # ---- 过期清扫 ----

    def sweep_expired(self, limit: int = 50, budget_s: float = 5.0) -> int:
        """清理已过期的快问文档（启动时调用一次）。

        **为什么不能只靠 Redis 的 TTL**：TTL 到期后临时元数据就没了，
        我们再也拿不到那个 doc_id，对应的向量却还留在索引里——存储膨胀 +
        检索噪声。启动清扫用**磁盘临时目录**兜住这类残留：
        目录里还在、但登记信息已失效的 `quick_*` 文件一律清理。

        **必须限量**：清理一个文档要打两次 Milvus（query + delete），
        实测每个约 10s。不限量的话，几十个残留就能把启动拖成几分钟——
        容器在启动期就被判 unhealthy，重启策略还会把它打进 crash loop。
        所以这里同时限**个数**与**时间预算**，剩下的留给下次启动/后台线程。
        """
        started = time.perf_counter()
        removed = 0
        skipped = 0

        def _budget_exhausted() -> bool:
            return removed >= limit or (time.perf_counter() - started) >= budget_s

        # ① 登记信息已过期（进程内存储的僵尸条目）
        for doc_id in list(self._store.list_doc_ids()):
            if not doc_id.startswith(QUICK_PREFIX):
                continue
            if _budget_exhausted():
                skipped += 1
                continue
            if self._store.get(doc_id) is not None:
                continue  # 还没到期
            try:
                self._knowledge.purge_transient(doc_id)
            except Exception as exc:  # noqa: BLE001 - 单个清理失败不阻塞启动
                logger.warning("快问过期清理失败 %s: %s", doc_id, exc)
            self._store.delete(doc_id)
            removed += 1

        # ② 磁盘残留（TTL 已过 / 进程崩溃）
        directory = self._temp_dir()
        if directory.exists():
            for path in directory.glob(f"{QUICK_PREFIX}*"):
                if _budget_exhausted():
                    skipped += 1
                    continue
                doc_id = path.name.split(".")[0]
                if self._store.get(doc_id) is not None:
                    continue  # 仍然有效
                try:
                    self._knowledge.purge_transient(doc_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("快问残留清理失败 %s: %s", doc_id, exc)
                self._store.delete(doc_id)
                try:
                    path.unlink()
                except OSError:
                    pass
                removed += 1
        if removed:
            logger.info("快问过期清扫：清理 %d 个残留临时文档", removed)
        if skipped:
            logger.warning(
                "快问过期清扫：还有 %d 个残留未清理（达到本次预算 %d 个 / %.1fs），"
                "留给下次清扫", skipped, limit, budget_s,
            )
        return removed
