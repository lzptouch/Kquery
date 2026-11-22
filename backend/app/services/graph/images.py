"""多模态图像索引。

路线：生成式描述 + 文本向量（不为图像另起向量空间）——
qwen-vl-max 生成四段结构化描述（主体/属性/场景/图中文字），
text-embedding-v3 编码后与正文向量同库存放（doc_id=__images__）。

三条硬约束：
1. 描述由同一模型生成（换模型需全量重编码，元数据记录 model_version）；
2. 描述用与正文相同的向量模型；
3. 描述结构化（自由格式会让命中率不可控）。
"""
import hashlib
import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from app.core.envelope import ApiError, ErrCode
from app.services.upstream import bulkhead
from app.domain.schemas import Chunk, ImageIndexEntry
from app.services.embeddings import Embeddings
from app.services.vectorstore import VectorStore
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

IMAGE_DOC_ID = "__images__"
CAPTION_MODEL_VERSION = "qwen-vl-max-2025-08"  # 描述生成模型版本（重建判据）

CAPTION_PROMPT = """为这张图片生成结构化描述，固定四段格式（每段一行）：
主体：图片的核心对象是什么
属性：对象的关键属性（颜色/形态/状态/数量）
场景：图片所处的场景或用途
图中文字：图中出现的所有文字（没有则写"无"）

只输出这四段，不要输出其他内容。"""


class Captioner(Protocol):
    def caption(self, image_path: Path) -> str: ...


class QwenVlCaptioner:
    """qwen-vl-max 结构化描述生成（限流重试与 OCR 引擎同策略）。"""

    def __init__(
        self,
        api_key: str,
        model: str = "qwen-vl-max",
        max_retries: int = 3,
        timeout_s: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._max_retries = max_retries
        # `LM-03`：等待许可的上限（不许无限排队——要快速失败）
        self._timeout_s = timeout_s

    def caption(self, image_path: Path) -> str:
        import dashscope
        from urllib.parse import quote

        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                # `LM-03`：多模态调用纳入隔离仓（持许可 + 等待有上限）。
                # 未配置 `upstream_multimodal_permits`（配置默认 **2**）→ 直通，行为不变。
                with bulkhead("multimodal").acquire(timeout_s=self._timeout_s):
                    resp = dashscope.MultiModalConversation.call(
                    model=self._model,
                    messages=[{
                        "role": "user",
                        "content": [
                            # 文件名含 # 等保留字符会截断 file:// URL，必须百分号编码
                            {"image": f"file://{quote(str(image_path.resolve()))}"},
                            {"text": CAPTION_PROMPT},
                        ],
                    }],
                    api_key=self._api_key,
                )
                if resp.status_code == 200:
                    return resp.output.choices[0].message.content[0]["text"].strip()
                last_error = RuntimeError(f"VL {resp.status_code}: {resp.message}")
                if "Throttl" not in str(resp.message) and resp.status_code not in (429, 500, 503):
                    raise last_error
            except Exception as exc:  # noqa: BLE001 - 降级继续（异常已在本处被隔离，不影响对外语义）
                last_error = exc
                if attempt == self._max_retries - 1:
                    break
                time.sleep(2**attempt)
        # 图片解析不可用（`A-10` 接线 3801）：此前是裸 RuntimeError，
        # 调用方只能按字符串猜「是不是模型挂了」。
        raise ApiError(
            ErrCode.VISION_UNAVAILABLE,
            f"图像描述生成失败: {last_error}",
            http_status=502,
        )


class MockCaptioner:
    """测试用确定性描述：从文件名派生四段结构。"""

    def caption(self, image_path: Path) -> str:
        stem = image_path.stem
        return f"主体：{stem} 示意图\n属性：测试图像\n场景：文档插图\n图中文字：无"


# 四段的标签（顺序即渲染顺序）与内部键名。
# 标签是提示词里写死的四个词；内部键名用于结构化承载，避免下游靠中文匹配。
CAPTION_SECTIONS: tuple[tuple[str, str], ...] = (
    ("subject", "主体"),
    ("attributes", "属性"),
    ("scene", "场景"),
    ("text_in_image", "图中文字"),
)

# 段标签行：允许 `- ` / `* ` 列表符、`**加粗**`、全角或半角冒号。
# 模型输出这四种变体的概率不低，收紧成正则比事后人工清洗省事。
_SECTION_RE = re.compile(
    r"^\s*(?:[-*•·]\s*)?(?:\*\*)?\s*(主体|属性|场景|图中文字)\s*(?:\*\*)?\s*[:：]\s*(.*)$"
)


def _strip_fence(raw: str) -> str:
    """剥掉整段外层的 ``` 围栏（模型爱包一层）。"""
    text = re.sub(r"^```\w*\s*|\s*```$", "", raw.strip())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_caption_sections(raw: str) -> dict[str, str]:
    """把模型输出解析成四段结构（`LM-04`）。

    为什么必须解析而不是直接入库：提示词要求"固定四段"，但这是**请求**不是
    保证——模型会改用半角冒号、加粗标签、列表符，也会漏段、还会整段跑题。
    描述原样入库的后果是命中率不可解释：同一张图换一次描述就可能搜不到。
    解析后统一渲染，至少形状可预期；解析不出来则**原文兜底**（宁可格式乱，
    不能丢信息）。

    返回：`{"subject":..., "attributes":..., "scene":..., "text_in_image":...}`
    （缺的段为空串，不编造）。
    """
    text = _strip_fence(raw)
    sections = {key: "" for key, _ in CAPTION_SECTIONS}
    if not text:
        return sections

    # 变体一：模型直接给 JSON（少但真实存在）
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            hit = False
            for key, label in CAPTION_SECTIONS:
                value = payload.get(key) or payload.get(label)
                if isinstance(value, str) and value.strip():
                    sections[key] = re.sub(r"\s+", " ", value).strip()
                    hit = True
            if hit:
                return sections

    # 变体二（主路径）：逐行按标签切段，段内续行归并到当前段
    label_to_key = {label: key for key, label in CAPTION_SECTIONS}
    current: str | None = None
    for line in text.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            current = label_to_key[m.group(1)]
            body = m.group(2).strip()
            if body:
                sections[current] = body
            continue
        if current and line.strip():
            # 段内换行（长描述）合并成一行：向量化前不需要保留排版
            sections[current] = (sections[current] + " " + line.strip()).strip()

    return sections


def render_caption(sections: dict[str, str]) -> str:
    """把四段结构渲染回标准文本（缺失段不输出，留白比编造诚实）。"""
    lines = [
        f"{label}：{sections[key]}"
        for key, label in CAPTION_SECTIONS
        if str(sections.get(key) or "").strip()
    ]
    return "\n".join(lines)


def _parse_caption(raw: str) -> str:
    """清洗 + 结构化：能解析出至少两段就按标准格式重排，否则原文兜底。

    阈值取 2 而不是 4：只命中一段多半是"正文里恰好有一行像标签"，
    此时重排会把其余内容整段丢掉；命中两段以上才认为模型确实在按格式答。
    """
    text = _strip_fence(raw)
    sections = parse_caption_sections(text)
    hit = sum(1 for value in sections.values() if value)
    if hit >= 2:
        if hit < len(CAPTION_SECTIONS):
            # 缺段是可观测信号：描述质量会直接影响以文搜图的召回
            missing = [label for key, label in CAPTION_SECTIONS if not sections[key]]
            logger.info("图像描述缺段 %s（缺 %s）", hit, "/".join(missing))
        return render_caption(sections)
    return text


if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from app.services.graph.image_embedder import ImageEmbedder


class ImageIndex:
    """图像向量索引：描述入统一向量库 + 元数据 JSON 存取（按 hash 去重）。"""

    def __init__(
        self,
        captioner: Captioner,
        embeddings: Embeddings,
        vector_store: VectorStore,
        index_path: Path,
        failure_counter=None,  # 描述生成失败率统计（监控指标）
        image_embedder: "ImageEmbedder | None" = None,  # M1：图像向量编码器（None = 只走 caption 路）
        image_store=None,      # M1：图像向量的**独立**存储（维度不同，不能混库）
    ) -> None:
        self._captioner = captioner
        self._embeddings = embeddings
        self._store = vector_store
        self._path = index_path
        # M1 双路：caption 路（文本向量）+ 图像向量路（多模态向量）。
        # 两者分数不可比（不同空间）→ 用 **RRF** 按排名融合，与主检索同一套口径。
        self._image_embedder: "ImageEmbedder | None" = image_embedder
        self._image_store = image_store
        self._entries: dict[str, ImageIndexEntry] = {}
        self._on_failure = failure_counter
        if index_path.exists():
            try:
                data = json.loads(index_path.read_text(encoding="utf-8"))
                self._entries = {k: ImageIndexEntry(**v) for k, v in data.items()}
            except Exception:  # noqa: BLE001 - 图像索引加载失败，从空开始
                logger.warning("图像索引加载失败，从空开始")

    def index_image(
        self, image_path: Path, asset_id: str, doc_id: str = "", page: int = 0, tenant_id: str = "default"
    ) -> ImageIndexEntry | None:
        """生成描述 → 编码 → 入统一向量库。同图（hash 相同）去重跳过。"""
        content_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
        for entry in self._entries.values():
            if entry.hash == content_hash and entry.tenant_id == tenant_id:
                return entry  # 同图多处出现 → 去重入库
        try:
            caption = _parse_caption(self._captioner.caption(image_path))
        except Exception as exc:  # noqa: BLE001 - 图像描述生成失败 %s
            if self._on_failure:
                self._on_failure()
            logger.warning("图像描述生成失败 %s: %s", asset_id, exc)
            return None

        entry = ImageIndexEntry(
            asset_id=asset_id, caption=caption, uri=str(image_path),
            page=page, doc_id=doc_id, tenant_id=tenant_id,
            model_version=CAPTION_MODEL_VERSION, hash=content_hash,
        )
        # 描述文本作为块入统一向量空间（以文搜图的桥梁）
        chunk = Chunk(
            chunk_id=f"{IMAGE_DOC_ID}#{asset_id}",
            doc_id=IMAGE_DOC_ID,
            seq=0,
            content=caption,
            source=f"图像 · {image_path.name}",
            tenant_id=tenant_id,
        )
        vector = self._embeddings.embed_texts([caption])
        self._store.add([chunk], vector)
        self._store.save()
        # M1：图像向量路（独立 collection/目录）。失败**不影响** caption 路
        # （caption 路已入库，图像路只是多一路召回）。
        if self._image_embedder is not None and self._image_store is not None:
            try:
                img_vec = self._image_embedder.embed_images([image_path])
                self._image_store.add(
                    [
                        Chunk(
                            chunk_id=chunk.chunk_id,
                            doc_id=IMAGE_DOC_ID,
                            seq=0,
                            content=caption,
                            source=f"图像向量 · {image_path.name}",
                            tenant_id=tenant_id,
                        )
                    ],
                    img_vec,
                )
                self._image_store.save()
            except Exception as exc:  # noqa: BLE001 - 图像向量路失败
                logger.warning("图像向量路失败（不影响 caption 路）: %s", exc)
        self._entries[asset_id] = entry
        self._save()
        return entry

    def index_images_batch(
        self,
        items: list[tuple[Path, str, str, int, str]],
        concurrency: int = 4,
    ) -> list[dict]:
        """批量索引（`LM-02`）：逐张成败独立返回，**部分失败不回滚整批**。

        `items`：`[(image_path, asset_id, doc_id, page, tenant_id), ...]`

        返回：每项一条 `{"asset_id", "ok", "error"}`——**成功的项已经入库**，
        失败的项只带错误信息（沿用「抽取失败不影响入库主链路」的纪律）。

        并发度受**上游许可**约束（`LM-03` 的多模态通道）：许可耗尽时等待，
        不会无限并发地压上游。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results: list[dict] = []
        if not items:
            return results

        def _one(item) -> dict:
            image_path, asset_id, doc_id, page, tenant_id = item
            try:
                entry = self.index_image(image_path, asset_id, doc_id, page, tenant_id)
            except Exception as exc:  # noqa: BLE001 - 单张失败不影响整批
                return {"asset_id": asset_id, "ok": False, "error": str(exc)}
            if entry is None:
                return {"asset_id": asset_id, "ok": False, "error": "描述生成失败"}
            return {"asset_id": asset_id, "ok": True, "error": ""}

        workers = max(1, min(int(concurrency), len(items)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="img-index") as pool:
            futures = {pool.submit(_one, item): item for item in items}
            for future in as_completed(futures):
                results.append(future.result())
        # 稳定顺序（按入参顺序），便于调用方对齐
        order = {item[1]: i for i, item in enumerate(items)}
        results.sort(key=lambda r: order.get(r["asset_id"], 0))
        return results

    def search(self, query: str, top_k: int = 4, tenant_id: str = "default") -> list[dict]:
        """以文搜图：**caption 路 + 图像向量路**（M1），RRF 融合。

        只配了 caption 路时，行为与改造前完全一致（单路，无融合开销）。
        """
        caption_items = self._search_caption_route(query, top_k * 3, tenant_id)
        image_items = self._search_image_route(query, top_k * 3, tenant_id)
        if not image_items:
            return caption_items[:top_k]
        return self._fuse(caption_items, image_items)[:top_k]

    def _search_caption_route(self, query: str, limit: int, tenant_id: str) -> list[dict]:
        """caption 路：文本向量检索图像描述块（以文搜图的桥梁）。"""
        qvec = self._embeddings.embed_query(query)
        hits = self._store.search(qvec, limit, tenant_id, doc_id=IMAGE_DOC_ID)
        items = []
        for chunk, score in hits:
            asset_id = chunk.chunk_id.split("#", 1)[1]
            entry = self._entries.get(asset_id)
            if not entry:
                continue
            items.append({
                "asset_id": asset_id,
                "caption": entry.caption,
                "uri": entry.uri,
                "page": entry.page,
                "doc_id": entry.doc_id,
                "score": round(score, 4),
                "source_route": "image_caption",
            })
        return items

    def _search_image_route(self, query: str, limit: int, tenant_id: str) -> list[dict]:
        """图像向量路：文本查询编码到图像空间后检索（真跨模态）。"""
        if self._image_embedder is None or self._image_store is None:
            return []
        try:
            qvec = self._image_embedder.embed_query(query)
            hits = self._image_store.search(qvec, limit, tenant_id, doc_id=IMAGE_DOC_ID)
        except Exception as exc:  # noqa: BLE001 - 图像路失败不影响 caption 路
            logger.warning("图像向量路检索失败（回落 caption 路）: %s", exc)
            return []
        items = []
        for chunk, score in hits:
            asset_id = chunk.chunk_id.split("#", 1)[1]
            entry = self._entries.get(asset_id)
            if not entry:
                continue
            items.append({
                "asset_id": asset_id,
                "caption": entry.caption,
                "uri": entry.uri,
                "page": entry.page,
                "doc_id": entry.doc_id,
                "score": round(score, 4),
                "source_route": "image_vector",
            })
        return items

    @staticmethod
    def _fuse(*routes: list[dict], k: int = 60) -> list[dict]:
        """RRF 融合（与主检索同一口径：k=60，按排名不按分数）。

        分数不可比（文本向量 vs 多模态向量），所以只能用排名。
        """
        scores: dict[str, float] = {}
        best: dict[str, dict] = {}
        for items in routes:
            for rank, item in enumerate(items, start=1):
                aid = item["asset_id"]
                scores[aid] = scores.get(aid, 0.0) + 1.0 / (k + rank)
                if aid not in best:
                    best[aid] = dict(item)
        fused = []
        for aid, score in sorted(scores.items(), key=lambda kv: -kv[1]):
            row = dict(best[aid])
            row["score"] = round(score, 6)
            row["source_route"] = "+".join(
                sorted({
                    i["source_route"]
                    for r in routes
                    for i in r
                    if i["asset_id"] == aid
                })
            )
            fused.append(row)
        return fused

    def stats(self) -> dict:
        return {
            "images": len(self._entries),
            "model_version": CAPTION_MODEL_VERSION,
        }

    def remove_doc(self, doc_id: str) -> int:
        """删除某文档的图像索引条目（文档删除时联动，KQ-08）。

        条目移除后 `search()` 会跳过对应结果，图像即不可达。

        图像描述块住在共享 doc_id（`__images__`）下，`delete_doc` 碰不到它们，
        所以调用方必须先用 `chunk_ids_of()` 取清单、再 `store.delete_chunks()`。
        这两步由 `services/knowledge.py` 的删除流程串起来（M2），
        已删文档的图像描述不会再被通用向量检索召回。
        """
        victims = [aid for aid, e in self._entries.items() if e.doc_id == doc_id]
        for asset_id in victims:
            self._entries.pop(asset_id)
        if victims:
            self._save()
        return len(victims)

    def chunk_ids_of(self, doc_id: str) -> set[str]:
        """某文档的图像条目在向量库里占用的 chunk_id 集合（M2 的删除清单）。"""
        return {
            f"{IMAGE_DOC_ID}#{asset_id}"
            for asset_id, e in self._entries.items()
            if e.doc_id == doc_id
        }

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({k: v.model_dump() for k, v in self._entries.items()}, ensure_ascii=False),
            encoding="utf-8",
        )
