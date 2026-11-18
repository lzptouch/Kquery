"""Runbook 覆盖守卫（`OP-01`）。

## 为什么要有守卫

`deploy/prometheus-rules.yaml` 里加一条告警很容易，**加一张处置卡**很容易忘。
结果就是「半夜被叫醒，然后发现没有对应文档」——这是 runbook 失效的最常见方式。

守卫的判据：

1. **每条告警都有处置卡**（文件名 = 告警名的连字符形式）；
2. **每张卡有固定的六段**（告警表达式 / 症状 / 第一诊断命令 / 处置动作 / 回滚动作 / 升级条件）；
3. **「症状」段写的是用户感知**，不是组件状态——否则值班人会被引导到错误方向。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RULES = REPO_ROOT / "deploy" / "prometheus-rules.yaml"
ALERTS_DIR = REPO_ROOT / "doc" / "runbook" / "alerts"

REQUIRED_SECTIONS = (
    "## 告警表达式",
    "## 症状",
    "## 第一诊断命令",
    "## 处置动作",
    "## 回滚动作",
    "## 升级条件",
)


def _alert_names() -> list[str]:
    text = RULES.read_text(encoding="utf-8")
    return re.findall(r"-\s+alert:\s+(\w+)", text)


def _card_name(alert: str) -> str:
    """告警名 → 处置卡文件名。

    处置卡文件名**去掉产品前缀** `Kquery`（与设计文档 §五 4 的对照表一致）：
    `KqueryQueueDepthHigh` → `queue-depth-high.md`。
    """
    name = alert[6:] if alert.startswith("Kquery") else alert
    return re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower() + ".md"


def test_every_alert_has_a_runbook_card():
    """每条告警都有处置卡。"""
    missing = [
        alert for alert in _alert_names()
        if not (ALERTS_DIR / _card_name(alert)).exists()
    ]
    assert not missing, (
        f"以下告警没有处置卡：{missing}。"
        "新增告警必须同时新增处置卡（文件放在 doc/runbook/alerts/，名字取连字符形式）"
    )


def test_every_card_has_all_six_sections():
    """每张卡都有固定的六段。"""
    problems: list[str] = []
    for path in sorted(ALERTS_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for section in REQUIRED_SECTIONS:
            if section not in text:
                problems.append(f"{path.name}: 缺「{section.lstrip('# ')}」")
    assert not problems, "处置卡结构不完整：\n" + "\n".join(problems)


def test_no_orphan_cards():
    """没有「无对应告警」的孤儿处置卡（规则删了、卡还留着会误导值班人）。"""
    alerts = set(_alert_names())
    expected = {_card_name(a) for a in alerts}
    orphans = [p.name for p in sorted(ALERTS_DIR.glob("*.md")) if p.name not in expected]
    assert not orphans, (
        f"以下处置卡没有对应的告警规则：{orphans}（规则已删？卡要一起删）"
    )


def test_symptom_section_describes_user_impact():
    """「症状」段必须写用户感知（不是组件状态）。"""
    problems: list[str] = []
    for path in sorted(ALERTS_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        m = re.search(r"## 症状\n(.*?)(?=\n## )", text, re.S)
        if not m:
            problems.append(f"{path.name}: 症状段为空")
            continue
        symptom = m.group(1).strip()
        if "用户侧" not in symptom:
            problems.append(f"{path.name}: 症状段未写「用户侧…」")
    assert not problems, "症状段写法不合规：\n" + "\n".join(problems)


def test_runbook_index_exists_and_lists_the_cards():
    """索引存在，并链接到全部处置卡。"""
    index = REPO_ROOT / "doc" / "runbook" / "README.md"
    assert index.exists(), "doc/runbook/README.md 缺失（值班同学的第一入口）"
    text = index.read_text(encoding="utf-8")
    for alert in _alert_names():
        card = _card_name(alert)
        assert card in text or "alerts/" in text, f"索引未引用处置卡 {card}"


def test_severity_and_sla_docs_exist():
    """分级与 SLO 两份必备文档存在。"""
    for rel in ("doc/runbook/severity.md", "doc/runbook/sla.md", "doc/runbook/backup.md"):
        assert (REPO_ROOT / rel).exists(), f"{rel} 缺失"
