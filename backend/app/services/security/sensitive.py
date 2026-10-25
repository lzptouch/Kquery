"""敏感词扫描：输出侧扫描。

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

# PII 正则：此前只有词表匹配，无租户配词时身份证/手机/邮箱/卡号
# 完全不检测——而 PII 泄露与"是否配置了敏感词"无关，必须内置兜底。
# 顺序有意义：身份证优先，避免被后面的银行卡规则重复/误判。
PII_PATTERNS: dict[str, re.Pattern] = {
    "pii_id_card": re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    "pii_phone": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "pii_email": re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    "pii_bank_card": re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
}


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

    @staticmethod
    def _scan_pii(text: str) -> list[tuple[str, re.Pattern]]:
        """内置 PII 命中检测（与词表无关）。"""
        if not text:
            return []
        return [(cat, pat) for cat, pat in PII_PATTERNS.items() if pat.search(text)]

    def scan(self, text: str, tenant_id: str = "default") -> dict:
        """扫描输出文本。返回 {safe, categories, action, masked_text}。

        PII 与词表是**两条独立的检测线**：即使租户未配置任何敏感词，
        PII 仍然会被掩码（否则「没配词表 = PII 全放行」是个致命缺口）。
        """
        self._reload()  # 热更新
        with self._lock:
            words = list(self._words.get(tenant_id, []))

        if not text:
            return {"safe": True, "categories": [], "action": "none", "masked_text": text}

        normalized = self._normalize(text)
        hits: list[dict] = []
        for w in words:
            pattern = self._normalize(w["word"])
            if pattern and pattern in normalized:
                hits.append(w)

        pii_hits = self._scan_pii(text)
        categories = sorted({w["category"] for w in hits} | {cat for cat, _ in pii_hits})
        if not categories:
            return {"safe": True, "categories": [], "action": "none", "masked_text": text}

        # 动作取最高强度：refuse > mask > tag
        actions = {w.get("action", ACTION_MASK) for w in hits}
        if ACTION_REFUSE in actions:
            return {
                "safe": False, "categories": categories, "action": ACTION_REFUSE,
                "masked_text": "该回答涉及受限内容，无法展示",
            }
        # PII 恒掩码（不因词表是 tag 而放行）
        if ACTION_TAG in actions and ACTION_MASK not in actions and not pii_hits:
            return {"safe": True, "categories": categories, "action": ACTION_TAG, "masked_text": text}

        # 掩码：先 PII（按声明顺序，身份证先于银行卡）再词表
        masked = text
        for _, pattern in pii_hits:
            masked = pattern.sub(lambda m: "*" * len(m.group()), masked)
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
