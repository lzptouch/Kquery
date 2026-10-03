"""LLM 输出 JSON 的容错解析（阶段 4 README §4）。

LLM 输出经常"不干净"：markdown 围栏、前后杂文本、注释错位。
容错链：剥围栏 → 截取首个 { / [ 到末个 } / ] → json.loads → pydantic 校验。
"""
import json
import logging
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json_text(raw: str) -> str:
    """剥离 markdown 围栏并截取 JSON 主体。"""
    text = raw.strip()
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    # 截取第一个 { / [ 到最后一个 } / ]
    start_candidates = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not start_candidates:
        return text
    start = min(start_candidates)
    end = max(text.rfind("}"), text.rfind("]"))
    if end > start:
        return text[start : end + 1]
    return text[start:]


class JsonParseError(Exception):
    pass


def parse_json(raw: str) -> dict | list:
    """容错解析 LLM 输出的 JSON。失败抛 JsonParseError 由上层降级。"""
    text = extract_json_text(raw)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise JsonParseError(f"JSON 解析失败: {exc}（原文前 120 字: {raw[:120]!r}）") from exc


def parse_json_model(raw: str, model: type[T]) -> T:
    """解析并做 pydantic 校验。"""
    data = parse_json(raw)
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise JsonParseError(f"JSON 结构校验失败: {exc}") from exc
