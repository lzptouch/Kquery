"""敏感词扫描（阶段 9 §5）：输出侧扫描。

- 词表按租户严格隔离（跨租户共用会让 A 租户规则泄漏给 B 租户）；
- 长词优先匹配（否则短词抢先命中长词前缀，掩码不完整）；
- 词形覆盖：大小写 + 全半角归一 + 分隔符插入（"绝 密" 也能命中）；
- 外置 JSON 热更新（改动即生效，不重启）；
- 处置：掩码（默认）/ 拒答（最高密级）/ 仅标记（灰度观察）；
- 回报结构化结果：是否安全 + 命中类别集合 + 动作——不回传具体词。
"""
import json
import logging
import re
import threading
import unicodedata
from pathlib import Path

logger = logging.getLogger(__name__)

ACTION_MASK = "mask"
ACTION_REFUSE = "refuse"
ACTION_TAG = "tag"


class SensitiveScanner:
    def __init__(self, words_path: Path):
        self._path = words_path
        self._lock = threading.Lock()
        self._mtime = 0.0
        self._words: dict[str, list[dict]] = {}  # tenant_id → 词表（长词优先排序）
        self._reload()

    def _reload(self) -> None:
        if not self._path.exists():
            self._words = {}
            return
        mtime = self._path.stat().st_mtime
        if mtime == self._mtime:
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            words: dict[str, list[dict]] = {}
            for w in data.get("words", []):
                if not w.get("enabled", True):
                    continue
                words.setdefault(w.get("tenant_id", "default"), []).append(w)
            # 长词优先
            for tenant in words:
                words[tenant].sort(key=lambda w: -len(w["word"]))
            with self._lock:
                self._words = words
                self._mtime = mtime
        except Exception:  # noqa: BLE001
            logger.exception("敏感词表加载失败")

    @staticmethod
    def _normalize(text: str) -> str:
        """词形归一：全半角统一 + 去分隔符 + 小写。"""
        text = unicodedata.normalize("NFKC", text)
        return re.sub(r"[\s\-_\.]+", "", text).lower()

    def scan(self, text: str, tenant_id: str = "default") -> dict:
        """扫描输出文本。返回 {safe, categories, action, masked_text}。"""
        self._reload()  # 热更新
        with self._lock:
            words = list(self._words.get(tenant_id, []))
        if not words or not text:
            return {"safe": True, "categories": [], "action": "none", "masked_text": text}

        normalized = self._normalize(text)
        hits: list[dict] = []
        for w in words:
            pattern = self._normalize(w["word"])
            if pattern and pattern in normalized:
                hits.append(w)

        if not hits:
            return {"safe": True, "categories": [], "action": "none", "masked_text": text}

        categories = sorted({w["category"] for w in hits})
        # 动作取最高强度：refuse > mask > tag
        actions = {w.get("action", ACTION_MASK) for w in hits}
        if ACTION_REFUSE in actions:
            return {
                "safe": False, "categories": categories, "action": ACTION_REFUSE,
                "masked_text": "该回答涉及受限内容，无法展示",
            }
        if ACTION_TAG in actions and ACTION_MASK not in actions:
            return {"safe": True, "categories": categories, "action": ACTION_TAG, "masked_text": text}

        # 掩码：在原文上按词逐个替换（大小写/分隔符容忍用正则）
        masked = text
        for w in hits:
            if w.get("action", ACTION_MASK) == ACTION_MASK:
                pattern = self._flexible_regex(w["word"])
                masked = re.sub(pattern, "█" * len(w["word"]), masked, flags=re.IGNORECASE)
        return {"safe": False, "categories": categories, "action": ACTION_MASK, "masked_text": masked}

    @staticmethod
    def _flexible_regex(word: str) -> str:
        """词形容忍：字符间允许插入空白/分隔符。"""
        chars = [re.escape(c) for c in unicodedata.normalize("NFKC", word)]
        return r"[\s\-_\.]*".join(chars)

    # ---- 词表管理 ----

    def list_words(self, tenant_id: str | None = None) -> list[dict]:
        self._reload()
        with self._lock:
            if tenant_id:
                return [dict(w) for w in self._words.get(tenant_id, [])]
            return [dict(w) for words in self._words.values() for w in words]

    def save_words(self, words: list[dict]) -> None:
        """全量保存词表（管理端编辑后写回，热更新于下次扫描生效）。"""
        payload = {"version": int(self._path.exists() and 1 or 0) + 1, "words": words}
        self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        self._mtime = 0.0
        self._reload()
