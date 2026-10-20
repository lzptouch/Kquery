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
import logging
import re
import time
from pathlib import Path
from typing import Protocol

from app.domain.schemas import Chunk, ImageIndexEntry
from app.services.embeddings import Embeddings
from app.services.vectorstore import VectorStore

logger = logging.getLogger(__name__)

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

    def __init__(self, api_key: str, model: str = "qwen-vl-max", max_retries: int = 3):
        self._api_key = api_key
        self._model = model
        self._max_retries = max_retries

    def caption(self, image_path: Path) -> str:
        import dashscope
        from urllib.parse import quote

        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
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
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == self._max_retries - 1:
                    break
                time.sleep(2**attempt)
        raise RuntimeError(f"图像描述生成失败: {last_error}")


class MockCaptioner:
    """测试用确定性描述：从文件名派生四段结构。"""

    def caption(self, image_path: Path) -> str:
        stem = image_path.stem
        return f"主体：{stem} 示意图\n属性：测试图像\n场景：文档插图\n图中文字：无"


def _parse_caption(raw: str) -> str:
    """清洗描述：剥围栏、压空行。"""
    text = re.sub(r"^```\w*\s*|\s*```$", "", raw.strip())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class ImageIndex:
    """图像向量索引：描述入统一向量库 + 元数据 JSON 存取（按 hash 去重）。"""

    def __init__(
        self,
        captioner: Captioner,
        embeddings: Embeddings,
        vector_store: VectorStore,
        index_path: Path,
        failure_counter=None,  # 描述生成失败率统计（监控指标）
    ):
        self._captioner = captioner
        self._embeddings = embeddings
        self._store = vector_store
        self._path = index_path
        self._entries: dict[str, ImageIndexEntry] = {}
        self._on_failure = failure_counter
        if index_path.exists():
            try:
                data = json.loads(index_path.read_text(encoding="utf-8"))
                self._entries = {k: ImageIndexEntry(**v) for k, v in data.items()}
            except Exception:  # noqa: BLE001
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
        except Exception as exc:  # noqa: BLE001
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
        self._entries[asset_id] = entry
        self._save()
        return entry

    def search(self, query: str, top_k: int = 4, tenant_id: str = "default") -> list[dict]:
        """以文搜图：查询向量在统一库中检索图像描述块，回填元数据。"""
        qvec = self._embeddings.embed_query(query)
        hits = self._store.search(qvec, top_k * 3, tenant_id, doc_id=IMAGE_DOC_ID)
        items = []
        for chunk, score in hits[:top_k]:
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
            })
        return items

    def stats(self) -> dict:
        return {
            "images": len(self._entries),
            "model_version": CAPTION_MODEL_VERSION,
        }

    def remove_doc(self, doc_id: str) -> int:
        """删除某文档的图像索引条目（文档删除时联动，KQ-08）。

        条目移除后 `search()` 会跳过对应结果，图像即不可达。
        已知残留：图像描述块存活在共享 doc_id（`__image__`）的统一向量库里，
        存储层目前不提供按 chunk_id 删除的接口——残留块不再被 image_search 返回，
        但理论上仍可能出现在通用向量检索中（后续应在存储层补 chunk 级删除）。
        """
        victims = [aid for aid, e in self._entries.items() if e.doc_id == doc_id]
        for asset_id in victims:
            self._entries.pop(asset_id)
        if victims:
            self._save()
        return len(victims)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({k: v.model_dump() for k, v in self._entries.items()}, ensure_ascii=False),
            encoding="utf-8",
        )
