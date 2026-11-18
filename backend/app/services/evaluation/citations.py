"""引用抽取与支持性判定（`EV-05` 生成侧归因的第一环）。

## 为什么需要

缺口归因此前**只有检索侧 3 桶**：答案块卡在哪一环能查，
但「召回到了却没答对」是黑盒——无法区分「没引用」「引用错了」「结论错了」。

## 两条能力

1. **抽取引用编号**：答案里的 `[#1]` / `[#1][#2]` → 引用编号集合
   （编号格式与 `services/pipeline.py` 的 `build_evidence_block` 对应）；
2. **逐条支持性判定**：引用了某个编号 → 该编号对应的证据**是否支持**答案里
   与它关联的陈述。

## 判定口径（子串 / 锚点，**不做语义判定**）

语义判定要靠模型（`EV-03` 的 judge）；这里做的是**确定性的机械判定**：
答案中引用编号附近的关键片段是否出现在对应证据里。
它的价值是**可复现**（同一份答案永远同一结论），
代价是**只能抓住明显的编造引用**（换个说法就抓不到）——
两者的分工在 `EV-05` 的归因矩阵里：`misquoted` 是下限估计。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 答案里的引用编号（`[#1]` / `[#1,#2]` / `[# 1]` 都认）
CITATION_RE = re.compile(r"\[#\s*(\d+)\s*\]")

#: 引用编号后面跟的一段文本（到下一个引用或段落结束）——用于支持性判定
_CLAIM_AFTER_CITATION = 60


@dataclass
class CitationCheck:
    """一条引用的判定结果。"""

    number: int
    supported: bool
    reason: str = ""


@dataclass
class CitationReport:
    """一份答案的引用检查结果。"""

    cited: list[int] = field(default_factory=list)
    uncited_evidence: list[int] = field(default_factory=list)
    checks: list[CitationCheck] = field(default_factory=list)
    fabricated: list[int] = field(default_factory=list)  # 引用了不存在的编号

    @property
    def all_supported(self) -> bool:
        return all(c.supported for c in self.checks) and not self.fabricated


def extract_citations(answer: str) -> list[int]:
    """抽取答案里的引用编号（去重且保序）。"""
    seen: set[int] = set()
    out: list[int] = []
    for m in CITATION_RE.finditer(answer or ""):
        n = int(m.group(1))
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def split_claims(answer: str) -> list[tuple[int, str]]:
    """把答案按引用编号切成「(编号, 与该编号关联的主张文本)」。

    主张文本优先取引用**之前**的一小段（从上一个引用 / 段首起）——
    模型的习惯是「结论 [#1]」「依据 [#2]」，引用标记跟在主张**后面**。
    「之前」为空时（「[#1] 结论」这种写法）回落到「之后」的一小段。
    """
    text = answer or ""
    marks = list(CITATION_RE.finditer(text))
    out: list[tuple[int, str]] = []
    for i, m in enumerate(marks):
        prev_end = marks[i - 1].end() if i > 0 else 0
        before = text[prev_end:m.start()].strip()
        if len(before) > _CLAIM_AFTER_CITATION:
            before = before[-_CLAIM_AFTER_CITATION:]
        if not before:
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            before = text[m.end():end].strip()[:_CLAIM_AFTER_CITATION]
        out.append((int(m.group(1)), before))
    return out


def check_citations(
    answer: str,
    evidence: dict[int, str],
    keywords: list[str] | None = None,
) -> CitationReport:
    """逐条判定引用是否被证据支持。

    - `evidence`：编号 → 证据文本；
    - `keywords`：若给出，则用这些「必须出现的要点」判定支持
      （取每条要点在证据里是否出现）；不给则退化为「主张文本是否出现在证据里」。

    **判定失败要说清原因**（`reason`）：只给 `supported=False` 没人知道该查什么。
    """
    report = CitationReport()
    claims = split_claims(answer or "")
    report.cited = extract_citations(answer or "")

    for number, claim in claims:
        if number not in evidence:
            report.fabricated.append(number)
            report.checks.append(
                CitationCheck(number, False, f"引用了不存在的证据编号 #{number}")
            )
            continue
        ev = evidence[number] or ""
        if keywords:
            missing = [k for k in keywords if k and k not in ev]
            supported = not missing
            reason = "" if supported else f"证据 #{number} 里缺少要点：{missing[:3]}"
        else:
            # 主张为空（答案只写了编号）→ 无法判定，按「未被证伪」处理但不加分
            probe = claim.strip()
            if not probe:
                supported = True
                reason = "引用后无主张文本，按未被证伪处理"
            else:
                supported = probe in ev
                reason = "" if supported else "引用附近的主张未在对应证据中出现"
        report.checks.append(CitationCheck(number, supported, reason))

    report.uncited_evidence = [n for n in evidence if n not in report.cited]
    return report
