"""注入样本集的回归门禁（`KQ-27` 验收的最后一条）。

## 为什么样本集要单独一份

检索金标集（`queries.jsonl`）衡量的是**召回质量**（recall / nDCG）；
注入样本衡量的是**安全行为**（「答案不执行文档里的指令」）——
两者口径不同，**不能混进同一个指标**（注入样本不该影响检索质量分）。

但样本集必须**可执行**，否则会腐烂：写完没人跑，半年后规则改了、样本还是旧的。
本测试把样本集与检测逻辑绑在一起：

1. 每条样本都**必须**被检测器命中（规则改了导致漏检 → 红）；
2. 每条样本里的「正常内容」部分**不能**被误报（整段被当成注入 → 红）；
3. 每条样本的正常内容必须能提取出来（保证样本本身没写坏）。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.services.injection_guard import detect_injection, normalize_for_detection

EVAL_DIR = Path(__file__).resolve().parents[2] / "tests" / "data" / "eval"
CASES_FILE = EVAL_DIR / "injection_cases.jsonl"


def _load_cases() -> list[dict]:
    if not CASES_FILE.exists():
        return []
    out = []
    for line in CASES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def test_injection_case_dataset_exists():
    assert CASES_FILE.exists(), f"注入样本集缺失：{CASES_FILE}"
    cases = _load_cases()
    assert len(cases) >= 5, f"注入样本至少 5 条，当前 {len(cases)} 条"


def test_every_injection_case_is_detected():
    """每条样本都必须被检测器命中（漏检 = 防护失效）。"""
    missed = []
    for case in _load_cases():
        if not detect_injection(case["doc_content"]):
            missed.append(case["case_id"])
    assert not missed, f"以下注入样本未被检测：{missed}"


def test_normal_content_in_cases_is_not_flagged():
    """样本里的「正常内容」部分不能被误报（否则整段文档都会被降权）。"""
    problems = []
    for case in _load_cases():
        # 正常内容 = 最后一段（注入句在前面）
        lines = [l for l in case["doc_content"].splitlines() if l.strip()]
        normal = lines[-1]
        if detect_injection(normal):
            problems.append(f"{case['case_id']}: {normal[:40]}")
    assert not problems, "正常内容被误报：\n" + "\n".join(problems)


def test_cases_cover_four_injection_kinds():
    """样本要覆盖四类攻击手法（忽略指令 / 角色改写 / 工具诱导 / 秘密诱导）。"""
    kinds = {c["kind"] for c in _load_cases()}
    for required in ("ignore_instructions", "role_hijack", "tool_induction", "secret_induction"):
        assert required in kinds, f"样本集缺少「{required}」类样本（现有：{sorted(kinds)}）"


def test_zero_width_case_is_normalized_before_detection():
    """变形样本（零宽字符）必须被规范化后命中。"""
    cases = [c for c in _load_cases() if c["kind"] == "zero_width_bypass"]
    assert cases, "缺少变形绕过样本"
    raw = cases[0]["doc_content"]
    normalized = normalize_for_detection(raw)
    assert "\u200b" not in normalized, "零宽字符未被清除"
    assert detect_injection(raw), "变形样本未被命中"
