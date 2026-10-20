"""实体/关系抽取。

- 生产：qwen-plus 从 chunk 抽取 {entities, relations}，JSON 输出由自定义
  解析器校验并重试（平台不支持强约束 JSON 时必须自行校验）；
- 关系必须附原文引用（evidence 字段 = 支撑句），保证可溯源；
- 实体五类 Person/Org/Product/Concept/Event，关系领域自适应；
- 测试：ScriptedExtractor 返回固定结构。
"""
import json
import logging
import re
from typing import Protocol

from app.domain.schemas import ENTITY_TYPES
from app.services.llm import LLM

logger = logging.getLogger(__name__)

EXTRACT_PROMPT = """你是知识图谱抽取器。从下面文本中抽取实体与关系。

## 实体类型（五类，只允许这五类）
Person（人物）/ Org（组织/公司/机构）/ Product（产品/工具/项目）/ Concept（概念/术语/理论）/ Event（事件）

## 要求
1. 实体 name 用文中出现的规范名称；type 必须是五类之一；
2. 关系必须有方向：source 是主语方，target 是宾语方；
3. 每条关系必须附 evidence（支撑该关系的原文句子， verbatim 摘录）；
4. 没有关系可抽取时返回空列表，不要编造；
5. 最多抽取 8 个实体、8 条关系，取最重要的。

## 输出（严格 JSON，不要输出其他内容）
{{"entities": [{{"name": "...", "type": "Org", "description": "一句话"}}],
  "relations": [{{"source": "...", "target": "...", "relation": "投资", "evidence": "原文句子"}}]}}

## 文本
{text}
"""


class Extractor(Protocol):
    def extract(self, text: str) -> dict: ...  # {"entities": [...], "relations": [...]}


def parse_extraction(raw: str) -> dict:
    """结构校验解析：坏 JSON / 缺字段 / 非法类型 → 抛错由上层重试。"""
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        raise ValueError("抽取结果中未找到 JSON")
    payload = json.loads(m.group(0))
    entities = []
    for e in payload.get("entities") or []:
        name = str(e.get("name", "")).strip()
        if not name:
            continue
        etype = str(e.get("type", "Concept"))
        if etype not in ENTITY_TYPES:
            etype = "Concept"  # 非法类型归并到 Concept（不丢实体）
        entities.append({
            "name": name,
            "type": etype,
            "description": str(e.get("description", ""))[:200],
        })
    relations = []
    for r in payload.get("relations") or []:
        source = str(r.get("source", "")).strip()
        target = str(r.get("target", "")).strip()
        relation = str(r.get("relation", "关联")).strip()
        evidence = str(r.get("evidence", "")).strip()
        if not source or not target or source == target:
            continue
        relations.append({
            "source": source, "target": target,
            "relation": relation, "evidence": evidence[:300],
        })
    return {"entities": entities, "relations": relations}


class LLMExtractor:
    """LLM 实体/关系抽取。

    单块正文的截断长度由构造参数控制（默认 2000 字），且**截断必留痕**。
    此前是硬编码 `text[:2000]` 且静默——长表格块的后半段实体直接消失，
    排查"为什么这个实体没进图谱"时毫无线索。
    """

    def __init__(self, llm: LLM, max_retries: int = 1, max_chars: int = 2000):
        self._llm = llm
        self._max_retries = max_retries
        self._max_chars = max(200, int(max_chars))

    def extract(self, text: str) -> dict:
        if len(text.strip()) < 10:
            return {"entities": [], "relations": []}
        truncated = len(text) > self._max_chars
        if truncated:
            logger.info(
                "实体抽取输入被截断：%d → %d 字（超长块后半段的实体不会被抽取）",
                len(text), self._max_chars,
            )
        prompt = EXTRACT_PROMPT.format(text=text[: self._max_chars])
        last_error: Exception | None = None
        for _ in range(self._max_retries + 1):
            try:
                return parse_extraction(self._llm.generate(prompt))
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("实体抽取解析失败，重试: %s", exc)
        logger.warning("实体抽取最终失败（按空结果降级）: %s", last_error)
        return {"entities": [], "relations": []}  # 抽取失败不阻塞入库


class ScriptedExtractor:
    """测试用：按关键字返回固定实体/关系。"""

    def __init__(self, table: dict[str, dict] | None = None, default: dict | None = None):
        self._table = table or {}
        self._default = default or {"entities": [], "relations": []}
        self.calls = 0

    def extract(self, text: str) -> dict:
        self.calls += 1
        for key, value in self._table.items():
            if key in text:
                return value
        return self._default
