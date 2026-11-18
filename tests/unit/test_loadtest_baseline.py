"""压测基线留档（`C-02` / `C-03`）。

## 三条验收

1. 压测脚本能把结果产出成**基线表的 Markdown 行**（数字与压测同源，不手抄）
2. 基线文档存在，且表头与脚本产出的行**列数一致**（否则追加进去会破表）
3. 基线文档写明「**追加不覆盖**」的纪律
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "load_test_script", REPO_ROOT / "scripts" / "load_test.py"
)
load_test = importlib.util.module_from_spec(_spec)
sys.modules["load_test_script"] = load_test
_spec.loader.exec_module(load_test)

BASELINE_DOC = REPO_ROOT / "doc" / "analysis" / "并发容量基线.md"


def _stat(total: int, p50: float, p95: float, n429: int = 0, n5xx: int = 0, elapsed: float = 10.0):
    stat = load_test.Stat()
    for i in range(total):
        stat.add(200, p50)
    stat.lat = [p50] * (total - n429 - n5xx) + [p95] * (n429 + n5xx)
    stat.status[429] = n429
    if n5xx:
        stat.status[500] = n5xx
    stat.elapsed_s = elapsed
    return stat


def test_markdown_row_has_all_columns():
    """① 脚本产出的行包含基线表的全部列。"""
    row = load_test.markdown_row(
        "2026-11-13", "v2", "ask ×8", 8, _stat(100, 120.0, 300.0, n429=3, n5xx=0)
    )
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    assert len(cells) == 10, cells
    assert cells[0] == "2026-11-13"
    assert cells[2] == "ask ×8"
    assert cells[3] == "8"
    assert float(cells[4]) > 0  # 吞吐：成功 + 背压的总量 / 耗时
    assert cells[5] == "120"   # P50
    assert cells[7] == "3"     # 429
    assert cells[8] == "0"     # 5xx


def test_row_matches_document_table_columns():
    """② 产出的行与基线文档表头**列数一致**（否则追加进去会破表）。"""
    doc = BASELINE_DOC.read_text(encoding="utf-8")
    header = None
    for line in doc.splitlines():
        if line.strip().startswith("| 日期"):
            header = line
            break
    assert header, "基线文档缺少表头"
    header_cells = len([c for c in header.strip().strip("|").split("|")])
    row = load_test.markdown_row("2026-11-13", "v2", "ask", 8, _stat(10, 1.0, 2.0))
    row_cells = len([c for c in row.strip().strip("|").split("|")])
    assert row_cells == header_cells, (row_cells, header_cells)


def test_document_states_append_not_overwrite():
    """③ 基线文档写明「追加不覆盖」。"""
    doc = BASELINE_DOC.read_text(encoding="utf-8")
    assert "追加不覆盖" in doc or "不覆盖" in doc


def test_document_has_legacy_and_v2_rows():
    """旧口径与新口径**分段标注**（不可比的历史数字不能混在一张表里比）。"""
    doc = BASELINE_DOC.read_text(encoding="utf-8")
    assert "批次 1 前" in doc, "旧口径数字必须标注为「批次 1 前」"
    assert re.search(r"\|\s*日期\s*\|", doc), "缺少基线表"


def test_five_xx_excludes_backpressure():
    """503 属背压（设计内），不算 5xx 故障——口径写在脚本里。"""
    stat = load_test.Stat()
    stat.status[503] = 10
    stat.elapsed_s = 10.0
    row = load_test.markdown_row("2026-11-13", "v2", "ask", 8, stat)
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    assert cells[8] == "0", "503 是背压，不该计入 5xx"
