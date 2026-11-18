"""生成侧归因（`EV-05`）：引用抽取 + 支持性判定 + 归因矩阵。

## 关键语义

- `not_cited`：证据进了上下文但答案没引用 → **引用是瓶颈**
- `misquoted`：引用了但与证据矛盾 → **忠实性是瓶颈**
- `answered_wrong`：引用一致但结论错 → **生成是瓶颈**
- `unjudged`：**没有 judge 结果** → 诚实标注，**不计入任何桶**
"""
from __future__ import annotations

from app.services.evaluation.citations import (
    check_citations,
    extract_citations,
    split_claims,
)
from app.services.evaluation.metrics import attribution_matrix, generation_attribution

EVIDENCE = {
    1: "单笔报销上限为 5000 元。超过上限需要双签。",
    2: "差旅住宿标准为每晚 600 元。",
}


def test_extract_citations():
    """抽取引用编号（去重保序）。"""
    assert extract_citations("上限 5000 元 [#1]，见 [#2] 与 [#1]") == [1, 2]
    assert extract_citations("没有引用") == []
    assert extract_citations("[# 3]") == [3]


def test_split_claims():
    """按引用切分主张文本。"""
    claims = split_claims("上限是 5000 元 [#1]，住宿是 600 元 [#2]")
    assert claims[0][0] == 1
    assert "5000" in claims[0][1]
    assert claims[1][0] == 2


def test_supported_citation():
    """主张在证据里 → 支持。"""
    report = check_citations("单笔报销上限为 5000 元 [#1]", EVIDENCE)
    assert report.cited == [1]
    assert report.checks[0].supported is True


def test_reworded_claim_is_not_supported_but_says_why():
    """**换个说法就抓不到**——子串判定的已知下限，必须诚实标出来。

    证据写「单笔报销上限为 5000 元」，答案写「报销上限是 5000 元」：
    语义相同但不是子串 → 判为「未被支持」并写明原因。
    这正是 `misquoted` 只是**下限估计**的原因（见模块 docstring）。
    """
    report = check_citations("报销上限是 5000 元 [#1]", EVIDENCE)
    assert report.checks[0].supported is False
    assert "未在对应证据中出现" in report.checks[0].reason


def test_fabricated_citation_is_flagged():
    """引用了不存在的编号 → 判为不支持（编造引用）。"""
    report = check_citations("答案 [#9]", EVIDENCE)
    assert report.fabricated == [9]
    assert report.checks[0].supported is False
    assert "不存在" in report.checks[0].reason


def test_uncited_evidence_is_reported():
    """没被引用的证据要列出来（前端据此提示「有证据没用上」）。"""
    report = check_citations("只引用了 [#1]", EVIDENCE)
    assert report.uncited_evidence == [2]


def test_keywords_based_support():
    """给了要点时：要点必须出现在对应证据里。"""
    report = check_citations(
        "报销上限 [#1]",
        EVIDENCE,
        keywords=["5000 元", "双签"],
    )
    assert report.checks[0].supported is True
    report2 = check_citations("报销上限 [#1]", EVIDENCE, keywords=["单笔报销上限为 5000 元。", "双签"])
    assert report2.checks[0].supported is True


def test_bucket_not_cited():
    """没引用 → `not_cited`。"""
    gen = generation_attribution("报销上限是 5000 元。", EVIDENCE)
    assert gen["bucket"] == "not_cited"


def test_bucket_misquoted():
    """引用但与证据矛盾 → `misquoted`。"""
    # 要点「8000 元」不在证据里 → 与证据矛盾
    gen = generation_attribution("报销上限是 8000 元 [#1]", EVIDENCE, keywords=["8000 元"])
    assert gen["bucket"] == "misquoted"


def test_bucket_unjudged_without_judge():
    """没有 judge 结果 → `unjudged`（**不假装答对**）。"""
    gen = generation_attribution("报销上限 [#1]", EVIDENCE)
    assert gen["bucket"] == "unjudged"


def test_bucket_correct_and_answered_wrong():
    """有 judge 结果时按「答案正确性」分桶。"""
    gen = generation_attribution("报销上限 [#1]", EVIDENCE, judge_scores={"answer_correctness": 1.0})
    assert gen["bucket"] == "correct"
    gen2 = generation_attribution("报销上限 [#1]", EVIDENCE, judge_scores={"answer_correctness": 0.2})
    assert gen2["bucket"] == "answered_wrong"


def test_attribution_matrix_conclusion():
    """归因矩阵给出可读结论 + 动作。"""
    gen = generation_attribution("报销上限 [#1]", EVIDENCE, judge_scores={"answer_correctness": 1.0})
    matrix = attribution_matrix({"bucket": "found"}, gen)
    assert matrix["conclusion"] == "通过"

    gen_bad = generation_attribution("报销上限 [#1]", EVIDENCE, judge_scores={"answer_correctness": 0.1})
    matrix_bad = attribution_matrix({"bucket": "found"}, gen_bad)
    assert matrix_bad["conclusion"] == "生成是瓶颈"
    assert "换模型" in matrix_bad["action"]


def test_attribution_matrix_without_generation():
    """没有生成结果 → 生成侧不产出，并写明原因。"""
    matrix = attribution_matrix({"bucket": "found"}, None)
    assert matrix["generation"] is None
    assert "未判定" in matrix["conclusion"]


def test_retrieval_side_wins_when_not_found():
    """检索没命中时，结论归检索侧（生成侧结论没有意义）。"""
    gen = generation_attribution("报销上限 [#1]", EVIDENCE, judge_scores={"answer_correctness": 1.0})
    matrix = attribution_matrix({"bucket": "not_recalled"}, gen)
    assert matrix["conclusion"] == "召回是瓶颈"
