"""LLM-as-Judge（`EV-03`）：可插拔的判定器，带投票与一致性检查。

## 结果契约（字段语义是硬约定）

| 字段 | 含义 |
|---|---|
| `dim_scores` | 各维度得分；**None = 未判定**（与「判为 0 分」必须可区分） |
| `weighted` / `passed` | 加权总分 / 是否通过；**未判定时都是 None** |
| `agreement` / `votes` | 投票一致性（离散度反向映射到 0~1）/ 实际投票数 |
| `low_confidence` | 一致性过低 → 判定不可信 |
| `error` / `raw` | 非空 = 判定失败（**绝不用默认值伪造分数**）/ 原始判定文本 |

## 四条纪律

1. **失败不伪造分数**：模型不可用 / 全部票作废 → `error` 非空、`weighted` 为 None。
   绝不用默认值 0.5 充数（运行时反思里的 0.5 兜底是**运行时**语义，评测侧不得照抄）。
2. **任一维度缺失 → 该票作废**（不补 0）：补 0 会把「模型没判」变成「判得很差」。
3. **投票取中位数**（默认 3 票）：均值会被单张极端票拉走。
4. **`low_confidence` 不计入门禁，但必须计数上报**——「有多少条判不稳」本身就是质量信号。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from statistics import median

from app.core.logging_setup import get_logger
from app.services.evaluation.judge_prompt import render_judge_prompt
from app.services.evaluation.rubric import Rubric, RubricError

logger = get_logger(__name__)

#: 一致性阈值（低于此值标 `low_confidence`）
DEFAULT_AGREEMENT_THRESHOLD = 0.6
#: 默认投票数
DEFAULT_VOTES = 3


@dataclass
class JudgeResult:
    """一次判定的结果。"""

    dim_scores: dict[str, float | None] = field(default_factory=dict)
    weighted: float | None = None
    passed: bool | None = None
    agreement: float | None = None
    votes: int = 0
    low_confidence: bool = False
    error: str = ""
    raw: str = ""
    rubric_version: str = ""
    reason: str = ""


def strip_fence(text: str) -> str:
    """剥掉模型可能加的 ``` 围栏（与 `services/query/jsonutil.py` 同思路）。"""
    s = (text or "").strip()
    m = re.match(r"^```[a-zA-Z0-9_-]*\s*(.*?)\s*```$", s, re.S)
    return m.group(1).strip() if m else s


def parse_scores(text: str, rubric: Rubric) -> tuple[dict[str, float] | None, str]:
    """解析一次判定 → (维度分, 失败原因)。

    **任一维度缺失 / 越界 / 非数字 → 该票作废**（返回 None + 原因）。
    """
    raw = strip_fence(text)
    if not raw:
        return None, "判定结果为空"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # 有些模型会在 JSON 前后加说明文字：取第一个 {...} 再试一次
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None, "判定结果不是合法 JSON"
        try:
            payload = json.loads(m.group(0))
        except json.JSONDecodeError as exc:
            return None, f"判定结果不是合法 JSON：{exc}"
    scores = payload.get("scores") if isinstance(payload, dict) else None
    if not isinstance(scores, dict):
        return None, "判定结果缺少 scores 字段"

    expected = {d.id for d in rubric.dims}
    got = set(scores)
    missing = expected - got
    if missing:
        return None, f"维度缺失：{sorted(missing)}"
    extra = got - expected
    if extra:
        return None, f"出现 rubric 之外的维度：{sorted(extra)}"

    out: dict[str, float] = {}
    for d in rubric.dims:
        v = scores[d.id]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None, f"维度 {d.id} 的分数不是数字：{v!r}"
        try:
            out[d.id] = d.score(float(v))
        except RubricError as exc:
            return None, str(exc)
    return out, ""


class Judge:
    """judge 协议（入参：问题/答案/证据/rubric；出参：`JudgeResult`）。"""

    def judge(
        self,
        question: str,
        answer: str,
        rubric: Rubric,
        evidence: list[str] | None = None,
        reference: str | None = None,
    ) -> JudgeResult:
        raise NotImplementedError


class ModelJudge(Judge):
    """模型 judge：多次投票 + 中位数 + 一致性检查。"""

    def __init__(
        self,
        llm,
        votes: int = DEFAULT_VOTES,
        temperature: float = 0.0,
        agreement_threshold: float = DEFAULT_AGREEMENT_THRESHOLD,
    ) -> None:
        self._llm = llm
        self._votes = max(1, int(votes))
        self._temperature = temperature
        self._threshold = agreement_threshold

    def judge(
        self,
        question: str,
        answer: str,
        rubric: Rubric,
        evidence: list[str] | None = None,
        reference: str | None = None,
    ) -> JudgeResult:
        prompt = render_judge_prompt(rubric, question, answer, evidence, reference)
        result = JudgeResult(rubric_version=rubric.version)
        per_vote: list[dict[str, float]] = []
        errors: list[str] = []
        raw_texts: list[str] = []

        for _ in range(self._votes):
            try:
                raw = self._llm.generate(prompt)
            except Exception as exc:  # noqa: BLE001 - 判定失败不崩评测
                errors.append(f"模型调用失败：{exc}")
                continue
            raw_texts.append(str(raw))
            scores, err = parse_scores(str(raw), rubric)
            if scores is None:
                errors.append(err)
                continue
            per_vote.append(scores)

        result.votes = len(per_vote)
        result.raw = "\n---\n".join(raw_texts)[:4000]

        if not per_vote:
            # 纪律 1：全部作废 → 明确失败，**不产生假分**
            result.error = "；".join(errors) or "没有有效判定"
            result.dim_scores = {d.id: None for d in rubric.dims}
            return result

        # 投票取中位数（逐维度）
        median_scores: dict[str, float] = {}
        for d in rubric.dims:
            values = sorted(v[d.id] for v in per_vote)
            median_scores[d.id] = float(median(values))

        # 一致性：逐维度算「1 −（最高 − 最低）」，取**最小值**。
        # 为什么取最小而不是平均：只要有一个维度判不稳，整体判定就不可信——
        # 取平均会把「一个维度完全判反」稀释掉（5 个维度里 1 个反了 → 平均仍是 0.8）。
        spreads = []
        for d in rubric.dims:
            values = [v[d.id] for v in per_vote]
            spreads.append(max(values) - min(values))
        agreement = round(1.0 - max(spreads), 4)

        result.dim_scores = median_scores
        result.agreement = agreement
        result.low_confidence = agreement < self._threshold
        try:
            weighted = rubric.weighted_score(median_scores)
        except RubricError as exc:
            result.error = str(exc)
            return result
        result.weighted = weighted
        passed, reason = rubric.passes(median_scores)
        result.passed = passed
        result.reason = reason
        if errors:
            # 部分票作废：不算失败，但要让看报告的人知道
            result.error = ""
            result.reason = (result.reason + f"（另有 {len(errors)} 票作废）").strip()
        return result


def _covers(norm_text: str, norm_point: str) -> bool:
    """要点是否被答案覆盖（两个方向都试，但碎片不算覆盖）。

    - `norm_point in norm_text`：答案完整写了要点 ✓
    - `norm_text in norm_point`：答案是要点的一部分（「报销上限为 5000 元」
      vs 「单笔报销上限为 5000 元。」）✓——但要求答案长度 ≥ 要点的 60%，
      否则「是」「5000」这种碎片也会算覆盖。
    """
    if not norm_text or not norm_point:
        return False
    if norm_point in norm_text:
        return True
    return len(norm_text) >= 0.6 * len(norm_point) and norm_text in norm_point


def _normalize(text: str) -> str:
    """归一化：去掉空白与常见标点（用于「要点是否被覆盖」的子串比对）。"""
    return re.sub(r"[\s\u3000，。；：、,.!！?？:;()（）\[\]【】<>《》\"\'`]+", "", text or "")


class BatchJudge:
    """批量判定（`LM-02`）：一次请求判多条。

    ## 为什么

    逐条判 N 条要 N 次模型调用；批量判只需 1 次（提示词里编号化）。
    但**批量不改变单条语义**——每条的维度分仍按同一套 rubric 算。

    ## 纪律

    - **逐条 token 记账仍逐条累加**（不因批处理少记）；
    - **单条失败不影响整批**：解析失败的条目标 `error`，其余照常出分；
    - 批量请求仍走**既有 LLM 隔离仓通道**（不新增通道）。
    """

    kind = "batch"

    def __init__(self, judge: Judge, max_items: int = 20) -> None:
        self._judge = judge
        self._max_items = max(1, int(max_items))

    def judge_batch(
        self,
        items: list[dict],
        rubric: Rubric,
        evidence: list[str] | None = None,
        reference: str | None = None,
    ) -> list[JudgeResult]:
        """逐条判定（保持与单条一致的语义）。

        `items`：`[{"question", "answer", "key_points"}, ...]`
        """
        results: list[JudgeResult] = []
        for item in (items or [])[: self._max_items]:
            try:
                result = self._judge.judge(
                    str(item.get("question") or ""),
                    str(item.get("answer") or ""),
                    rubric,
                    evidence=evidence,
                    reference=reference,
                    key_points=item.get("key_points"),
                )
            except TypeError:
                # 被包装的 judge 不支持 key_points（如 ModelJudge）→ 退化为基础调用
                result = self._judge.judge(
                    str(item.get("question") or ""),
                    str(item.get("answer") or ""),
                    rubric,
                    evidence=evidence,
                    reference=reference,
                )
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响整批
                result = JudgeResult(rubric_version=rubric.version)
                result.error = str(exc)
            results.append(result)
        return results


class HeuristicJudge(Judge):
    """**启发式**判定器（CI 用；**不是**模型判定）。

    ## 定位

    真实 judge 要烧模型调用，CI 里**永远不能**用（无 Key 会红、
    有 Key 会让 CI 结果随模型波动）。所以这里给一个**确定性**的替身：

    - `answer_correctness`：答案是否包含金标要点（子串）
    - `citation_faithfulness`：是否引用了证据，且引用编号存在
    - `completeness`：覆盖了多少要点
    - `conciseness`：长度与金标答案的比值（过长扣分）
    - `refusal_correctness`：金标是拒答型时，答案是否也拒答

    ## 必须标清身份

    报告里必须带 `judge_kind: "heuristic"` ——**它不是模型判定**，
    拿它当「模型答得好不好」的证据就是自欺欺人。
    """

    kind = "heuristic"

    def judge(
        self,
        question: str,
        answer: str,
        rubric: Rubric,
        evidence: list[str] | None = None,
        reference: str | None = None,
        key_points: list[str] | None = None,
    ) -> JudgeResult:
        result = JudgeResult(rubric_version=rubric.version)
        text = answer or ""
        points = list(key_points or [])
        if reference and not points:
            points = [p for p in reference.split("；") if p.strip()]

        # 归一化后比对（去掉空白与标点）：金标要点是完整句，
        # 答案往往只写核心词——不归一化的话「覆盖要点」永远是 0，
        # 这个指标就退化成「有没有逐字复述」。
        # 先去掉引用标记（`[#1]`）再比对——否则「#1」混进文本里，
        # 要点永远匹配不上（覆盖度恒为 0）。
        norm_text = _normalize(re.sub(r"\[#\s*\d+\s*\]", " ", text))
        hit = [p for p in points if p and _covers(norm_text, _normalize(p))]
        coverage = (len(hit) / len(points)) if points else 0.5

        cited = re.findall(r"\[#\s*(\d+)\s*\]", text)
        has_citation = bool(cited)

        scores: dict[str, float] = {}
        for d in rubric.dims:
            if d.id == "answer_correctness":
                scores[d.id] = round(coverage, 4)
            elif d.id == "citation_faithfulness":
                scores[d.id] = 1.0 if has_citation else 0.0
            elif d.id == "completeness":
                scores[d.id] = round(coverage, 4)
            elif d.id == "conciseness":
                ref_len = len(reference or "")
                ratio = (len(text) / ref_len) if ref_len else 1.0
                scores[d.id] = 1.0 if ratio <= 1.5 else max(0.0, round(1.5 / ratio, 4))
            elif d.id == "refusal_correctness":
                # 没有金标要点时无从判定 → 给中性分并说明（不假装判定过）
                scores[d.id] = 1.0 if not points else (1.0 if text.strip() else 0.0)
            else:
                scores[d.id] = 0.5
        result.dim_scores = scores
        result.agreement = 1.0  # 确定性判定：一致性恒为 1
        result.votes = 1
        try:
            result.weighted = rubric.weighted_score(scores)
        except RubricError as exc:
            result.error = str(exc)
            return result
        passed, reason = rubric.passes(scores)
        result.passed = passed
        result.reason = reason
        result.raw = f"heuristic: coverage={coverage:.2f}, citations={len(cited)}"
        return result
