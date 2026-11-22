#!/usr/bin/env python3
"""upload_eval 语料生成器（计划 §4.3）。

薄层：内容构造全部复用 ``tests/format/fixtures.py`` 的 ``make_*`` 夹具，
本脚本只负责 ① 落盘到 ``tests/data/upload_eval/corpus/<类型>/``、
② 产出 ``facts.jsonl``（金标事实表，与正文注入共用同一份常量）、
③ ``--self-check`` 用真实 ``ParserRouter`` 本地解析一遍（不依赖服务）。

幂等：事实常量固定，重复运行产物字节级一致（可 diff）。

用法::

    python scripts/eval/upload_corpus_gen.py                # 生成语料 + facts.jsonl
    python scripts/eval/upload_corpus_gen.py --self-check   # 生成后本地解析自检
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "format"))

from fixtures import (  # noqa: E402
    make_code,
    make_corrupt_pdf,
    make_doc_like_binary,
    make_docx,
    make_html,
    make_jpg,
    make_markdown,
    make_mixed_pdf,
    make_png,
    make_pptx,
    make_scanned_pdf,
    make_table_pdf,
    make_text,
    make_text_pdf,
    make_xlsx,
)

OUT = REPO / "tests" / "data" / "upload_eval"
CORPUS = OUT / "corpus"

# ---------------------------------------------------------------------------
# 金标事实：自造实体（澄川生物科技 / 猎隼计划 / 云杉港 / 苔原实验室 / 星桥…），
# 保证「答对只能来自上传文档」。负向问题用词与全部语料零交集（曲率引擎等）。
# ---------------------------------------------------------------------------

NEGATIVE_POOL = [
    "曲率引擎的冷却剂更换周期是多少天？",
    "暗物质库存台账的保管人是谁？",
    "跃迁阀门的出厂型号是什么？",
    "引力波天线的最佳仰角是多少度？",
    "反物质容器的额定压力是多少兆帕？",
    "虫洞稳定场的谐振频率是多少赫兹？",
]

_MD_GUIDE = """# 澄川生物科技 · 猎隼计划项目手册

## 第一章 项目概况

猎隼计划（Project-Falcon-2026）于 2026 年 3 月 17 日正式启动，由澄川生物科技研发中心负责。
项目目标是在 2026 年内完成冷链温控平台一期交付，覆盖云杉港与北辰两个仓储节点。

## 第二章 工程规范

项目代码仓库的分支保护规则编号为 BR-GUARD-042，任何合并请求必须通过双人评审。
主干分支只允许通过 squash 合入，禁止直接推送。

## 第三章 里程碑

| 里程碑 | 日期 | 负责人 |
| --- | --- | --- |
| M1 需求冻结 | 2026-05-30 | 陈北望 |
| M2 灰度上线 | 2026-08-15 | 林晚舟 |
| M3 正式发布 | 2026-09-30 | 陈北望 |
"""

_MD_NOTES = """# 云杉港运维周报（2026 年第 44 周）

- 云杉港冷链仓 7 号库的温控区间本周保持在 2℃ 至 6℃，未发生超限告警。
- 本周起例行巡检调整为每周五 07:42 开始，由值班工程师现场签字确认。
- 备件库存充足，无需追加采购。
"""

_TXT_FAQ = """澄川生物科技常见问题（FAQ）

问：如何联系澄川生物科技的客服？
答：澄川生物科技的客服邮箱是 support@chengchuan-bio.example，工作日上午九点至下午六点响应。

问：报销单编号规则是什么？
答：报销单编号一律以 REI-2026- 开头，后接六位顺序号，由财务系统自动分配。
"""

_CSV_METRICS = """指标,数值,统计周期
订单履约率,96.4%,2026-Q1
退货率,1.8%,2026-Q1
冷链准时率,93.7%,2026-Q1
"""

_YAML_CONFIG = """# 澄川生物科技生产环境配置
env: production
limits:
  max_concurrent_tasks: 37   # 生产环境的最大并发任务数
  queue_timeout_seconds: 120
"""

_JSON_EVENTS = """[
  {"event_id": "E-7781", "severity": "P1", "note": "北辰仓储的温控传感器离线告警"},
  {"event_id": "E-7782", "severity": "P3", "note": "例行磁盘清理完成"}
]
"""

_LOG_APP = """2026-11-03 03:14:26 INFO 关键事务 TX-8842 提交完成
2026-11-03 03:14:27 INFO 事务日志已归档到冷存储
2026-11-03 03:15:02 INFO 心跳正常
"""

_HTML_PAGE = """<h1>澄川生物科技行政公告</h1>
<p>年度体检预约窗口为 2026 年 11 月 2 日至 11 月 20 日，请登录内网系统自助预约。</p>
<p>园区班车 S7 线自本月起首班车 06:55 发车，末班车 21:40 收车。</p>
"""

_PDF_REPORT = """澄川生物科技 2026 年第四季度经营报告

摘要：本季度公司整体经营平稳，冷链业务稳步增长。
苔原实验室 2026 年度研发预算核定为 47.3 万元，其中设备采购占三成。
报告期内无重大安全事件。
客户与交付

第四季度客户满意度调查得分为 91.2 分，环比上升 1.4 分。
云杉港节点准时交付率保持行业领先。
附录

本报告由澄川生物科技经营管理部编制，数据口径以财务月结为准。
如有疑问请联系经营管理部数据分析组。
"""

_PDF_TABLE_CELLS = [
    ["供应商", "月结算单价", "结算周期", "联系人"],
    ["星桥物流", "8.6 元/票", "月结", "赵启明"],
    ["北风冷链", "12.4 元/票", "半月结", "孙曼"],
    ["澄川自营", "5.2 元/票", "月结", "陈北望"],
]
_PDF_TABLE_NOTE = "澄川生物科技供应商结算价目表（2026 年度执行版），本表数据以财务系统为准，正文用于防止整页被判为扫描页。"

_SCAN_P1 = ["CHENGCHUAN BIOTECH", "SCAN-REF: TSC-2026-0317", "WAREHOUSE INSPECTION REPORT", "STATUS: PASSED"]
_SCAN_P2 = ["SECOND PAGE", "CHECKLIST CODE: CK-5540", "SIGNED BY: LIN WANZHOU"]

_MIXED_TEXT = """澄川生物科技设备校准单

资产编号 ZC-5561 的离心机已于 2026 年 10 月 28 日完成年度校准，
校准结果为合格，下次校准日期为 2027 年 10 月。
本单据第二页为现场扫描件，随附存档。
"""
_MIXED_SCAN = ["MIXED-SCAN ATTACHMENT", "FREEZER F-09 TEMP -18C", "CHECKED 2026-10-28"]

_DOCX_SECTIONS = [
    ("第一章 合同主体", "本合同编号 HT-2026-0442，甲方为澄川生物科技有限公司，乙方为星桥物流有限公司。"),
    ("第二章 服务内容", "乙方为甲方提供华东区域冷链运输服务，服务期自 2026 年 4 月 1 日起一年。"),
    ("第三章 违约责任", "任何一方违约，违约金为合同总额的 5%，守约方有权解除合同。"),
]
_DOCX_TABLE = [["条款", "数值"], ["违约金比例", "5%"], ["账期天数", "45 天"]]

_XLSX_SHEETS = {
    "汇总": [["科目", "年度预算(万元)"], ["猎隼计划-研发", "32.5"], ["猎隼计划-差旅", "6.8"], ["猎隼计划-设备", "8.0"], ["合计", "47.3"]],
    "明细": [["月份", "差旅支出(万元)"], ["1月", "0.42"], ["2月", "0.51"], ["3月", "0.63"]],
    "备注": [["说明"], ["猎隼计划的年度预算上限为 47.3 万元，预算负责人为陈北望。"]],
}

_PPTX_SECTIONS = [
    {"title": "星桥平台 V2.0 路线图", "body": "澄川生物科技研发中心 · 2026 年度规划"},
    {"title": "发布窗口", "body": "星桥平台 V2.0 的正式发布窗口为 2026 年 9 月，灰度从 8 月 15 日开始。"},
    {"title": "里程碑", "body": "三个关键里程碑的完成标准如下表。",
     "table": [["里程碑", "完成标准"], ["M1", "联调通过率 100%"], ["M2", "压测达标"], ["M3", "准确率不低于 95%"]]},
    {"title": "资源投入", "body": "核心研发 12 人，测试 4 人，运维 2 人。"},
    {"title": "风险", "body": "供应商交付延迟为主要风险。", "notes": "风险负责人是林晚舟，每周五复盘一次。"},
    {"title": "总结", "body": "按路线图推进，季度末复盘。"},
]

_IMG_POSTER = ["FALCON LAUNCH", "DATE 2026-09-30", "HOTLINE 400-8123-456"]
_IMG_PHOTO = ["WAREHOUSE YS-07", "OPEN 08:30", "MANAGER SUN MAN"]

_CODE_PY = '''"""苔原实验室批处理模块。"""

# 苔原实验室的批处理上限由运维委员会于 2026 年核定，请勿随意调大。
MAX_BATCH_SIZE = 512
SECRET_TAG = "tundra-lab-2077"


def chunk_tasks(tasks: list, size: int = MAX_BATCH_SIZE) -> list:
    return [tasks[i : i + size] for i in range(0, len(tasks), size)]
'''

_CODE_TS = '''/**
 * 星桥网关 SLA 配置。
 * 星桥网关的重试上限是 3 次，超过即熔断。
 */
export const SLA_WINDOW_MINUTES = 45;
export const RETRY_LIMIT = 3;
'''

_CODE_JAVA = '''public class Main {
    // 澄川主库的只读副本地址：ro.chengchuan.internal
    static final int CACHE_TTL_SECONDS = 900;
    static final String READONLY_REPLICA = "ro.chengchuan.internal";
}
'''


def _f(fid: str, fact_text: str, question: str, contains: list[str], l0: bool = True) -> dict:
    return {
        "fact_id": fid,
        "fact_text": fact_text,
        "question": question,
        "expected_contains": contains,
        "expect_verdict": "answered",
        "l0": l0,
    }


def _neg(question: str) -> dict:
    # L0 下 mock embedding 无相关度下限、mock LLM 永远从 top-k 摘句，
    # 拒答判定无意义 → 负向问题只在 L1 断言。
    return {"question": question, "expect_verdict": "refused_or_parametric", "l0": False}


#: 语料清单：目录 / 文件名 / 造件器 / 事实 / l0 模式 / 负向问题
SPECS: list[dict] = [
    {
        "dir": "markdown", "name": "guide.md", "l0_mode": "full",
        "build": lambda: make_markdown(body=_MD_GUIDE),
        "facts": [
            _f("F-MD-01", "猎隼计划（Project-Falcon-2026）于 2026 年 3 月 17 日正式启动", "猎隼计划的启动日期是哪一天？", ["2026年3月17日"]),
            _f("F-MD-02", "分支保护规则编号为 BR-GUARD-042", "项目代码仓库的分支保护规则编号是什么？", ["BR-GUARD-042"]),
            _f("F-MD-03", "M1 需求冻结的负责人是陈北望", "里程碑 M1 需求冻结的负责人是谁？", ["陈北望"]),
        ],
        "negative_questions": [_neg(NEGATIVE_POOL[0])],
    },
    {
        "dir": "markdown", "name": "notes.markdown", "l0_mode": "full",
        "build": lambda: make_markdown(body=_MD_NOTES),
        "facts": [
            _f("F-NOTE-01", "云杉港冷链仓 7 号库的温控区间保持在 2℃ 至 6℃", "云杉港冷链仓七号库的温控区间是多少？", ["2℃至6℃", "2℃~6℃"]),
            _f("F-NOTE-02", "例行巡检调整为每周五 07:42 开始", "云杉港冷链仓的巡检每周几几点开始？", ["07:42"]),
        ],
        "negative_questions": [_neg(NEGATIVE_POOL[1])],
    },
    {
        "dir": "text", "name": "faq.txt", "l0_mode": "full",
        "build": lambda: make_text(text=_TXT_FAQ),
        "facts": [
            _f("F-FAQ-01", "客服邮箱是 support@chengchuan-bio.example", "澄川生物科技的客服邮箱是什么？", ["support@chengchuan-bio.example"]),
            _f("F-FAQ-02", "报销单编号一律以 REI-2026- 开头", "报销单编号以什么开头？", ["REI-2026-"]),
        ],
        "negative_questions": [],
    },
    {
        "dir": "text", "name": "metrics.csv", "l0_mode": "full",
        "build": lambda: _CSV_METRICS.encode("utf-8"),
        "facts": [
            _f("F-CSV-01", "2026-Q1 订单履约率 96.4%", "2026年第一季度的订单履约率是多少？", ["96.4%"]),
            _f("F-CSV-02", "2026-Q1 冷链准时率 93.7%", "2026年第一季度的冷链准时率是多少？", ["93.7%"]),
        ],
        "negative_questions": [],
    },
    {
        "dir": "text", "name": "config.yaml", "l0_mode": "full",
        "build": lambda: _YAML_CONFIG.encode("utf-8"),
        "facts": [
            _f("F-YAML-01", "生产环境的最大并发任务数（max_concurrent_tasks）为 37", "生产环境的最大并发任务数是多少？", ["37"]),
        ],
        "negative_questions": [],
    },
    {
        "dir": "text", "name": "events.json", "l0_mode": "full",
        "build": lambda: _JSON_EVENTS.encode("utf-8"),
        "facts": [
            _f("F-JSON-01", "事件 E-7781 的严重级别是 P1", "事件 E-7781 的严重级别是什么？", ["P1"]),
        ],
        "negative_questions": [],
    },
    {
        "dir": "text", "name": "app.log", "l0_mode": "full",
        "build": lambda: _LOG_APP.encode("utf-8"),
        "facts": [
            _f("F-LOG-01", "关键事务 TX-8842 于 03:14:26 提交完成", "事务 TX-8842 在什么时间提交完成？", ["03:14:26"]),
        ],
        "negative_questions": [_neg(NEGATIVE_POOL[2])],
    },
    {
        "dir": "html", "name": "page.html", "l0_mode": "full",
        "build": lambda: make_html(body=_HTML_PAGE),
        "facts": [
            _f("F-HTML-01", "年度体检预约窗口为 2026 年 11 月 2 日至 11 月 20 日", "年度体检预约窗口是哪段时间？", ["11月2日至11月20日", "11月2日"]),
            _f("F-HTML-02", "班车 S7 线首班车 06:55 发车", "S7 班车线首班车几点发车？", ["06:55"]),
        ],
        "negative_questions": [],
    },
    {
        "dir": "pdf", "name": "report.pdf", "l0_mode": "full",
        "build": lambda: make_text_pdf(body=_PDF_REPORT, cjk=True),
        "facts": [
            _f("F-PDF-01", "苔原实验室 2026 年度研发预算核定为 47.3 万元", "苔原实验室2026年度研发预算是多少？", ["47.3万", "47.3 万"]),
            _f("F-PDF-02", "第四季度客户满意度得分为 91.2 分", "第四季度客户满意度得分是多少？", ["91.2"]),
        ],
        "negative_questions": [_neg(NEGATIVE_POOL[3])],
    },
    {
        "dir": "pdf", "name": "table.pdf", "l0_mode": "full",
        "build": lambda: make_table_pdf(rows=4, cols=4, cells=_PDF_TABLE_CELLS, note=_PDF_TABLE_NOTE),
        "facts": [
            _f("F-TBL-01", "星桥物流的月结算单价是 8.6 元/票", "星桥物流的月结算单价是多少？", ["8.6"]),
            _f("F-TBL-02", "北风冷链的联系人是孙曼", "北风冷链的联系人是谁？", ["孙曼"]),
        ],
        "negative_questions": [],
    },
    {
        "dir": "pdf", "name": "scan.pdf", "l0_mode": "link_only",
        "build": lambda: make_scanned_pdf(pages=2, page_lines=[_SCAN_P1, _SCAN_P2]),
        "facts": [
            _f("F-SCAN-01", "SCAN-REF: TSC-2026-0317", "扫描件上的 SCAN-REF 编号是什么？", ["TSC-2026-0317"], l0=False),
            _f("F-SCAN-02", "CHECKLIST CODE: CK-5540", "扫描件第二页的 CHECKLIST CODE 是什么？", ["CK-5540"], l0=False),
        ],
        "negative_questions": [],
    },
    {
        "dir": "pdf", "name": "mixed.pdf", "l0_mode": "full",
        "build": lambda: make_mixed_pdf(text_body=_MIXED_TEXT, scan_lines=_MIXED_SCAN),
        "facts": [
            _f("F-MIX-01", "资产编号 ZC-5561 的离心机已完成年度校准", "资产编号 ZC-5561 的设备完成了什么操作？", ["校准"]),
            _f("F-MIX-02", "FREEZER F-09 TEMP -18C", "混合文档扫描页中 FREEZER F-09 的温度是多少？", ["-18C", "-18"], l0=False),
        ],
        "negative_questions": [],
    },
    {
        "dir": "word", "name": "contract.docx", "l0_mode": "full",
        "build": lambda: make_docx(sections=_DOCX_SECTIONS, table_rows=_DOCX_TABLE),
        "facts": [
            _f("F-DOC-01", "本合同编号 HT-2026-0442", "这份合同的合同编号是什么？", ["HT-2026-0442"]),
            _f("F-DOC-02", "违约金为合同总额的 5%", "违约金是合同总额的百分之几？", ["5%"]),
            _f("F-DOC-03", "账期天数为 45 天", "合同约定的账期是多少天？", ["45"]),
        ],
        "negative_questions": [_neg(NEGATIVE_POOL[4])],
    },
    {
        "dir": "excel", "name": "budget.xlsx", "l0_mode": "full",
        "build": lambda: make_xlsx(sheets=_XLSX_SHEETS, include_default=False),
        "facts": [
            _f("F-XLS-01", "猎隼计划的年度预算上限为 47.3 万元", "猎隼计划的年度预算上限是多少万元？", ["47.3"]),
            _f("F-XLS-02", "猎隼计划差旅科目的年度预算为 6.8 万元", "猎隼计划差旅科目的年度预算是多少万元？", ["6.8"]),
            _f("F-XLS-03", "猎隼计划的预算负责人为陈北望", "猎隼计划的预算负责人是谁？", ["陈北望"]),
        ],
        "negative_questions": [],
    },
    {
        "dir": "ppt", "name": "roadmap.pptx", "l0_mode": "full",
        "build": lambda: make_pptx(sections=_PPTX_SECTIONS),
        "facts": [
            _f("F-PPT-01", "星桥平台 V2.0 的正式发布窗口为 2026 年 9 月", "星桥平台 V2.0 的正式发布窗口是什么时候？", ["2026年9月", "2026 年 9 月"]),
            _f("F-PPT-02", "里程碑 M3 的完成标准是准确率不低于 95%", "里程碑 M3 的完成标准是什么？", ["95%"]),
            _f("F-PPT-03", "风险负责人是林晚舟", "路线图中的风险负责人是谁？", ["林晚舟"]),
        ],
        "negative_questions": [],
    },
    {
        "dir": "image", "name": "poster.png", "l0_mode": "link_only",
        "build": lambda: make_png(lines=_IMG_POSTER),
        "facts": [
            _f("F-IMG-01", "海报上的发布日期是 2026-09-30", "海报上 DATE 一行的发布日期是什么？", ["2026-09-30"], l0=False),
            _f("F-IMG-02", "海报上的热线电话是 400-8123-456", "海报上 HOTLINE 一行的热线电话是多少？", ["400-8123-456"], l0=False),
        ],
        "negative_questions": [],
    },
    {
        "dir": "image", "name": "photo.jpg", "l0_mode": "link_only",
        "build": lambda: make_jpg(lines=_IMG_PHOTO),
        "facts": [
            _f("F-IMG-03", "YS-07 仓库 08:30 开放", "照片中 YS-07 仓库几点开放？", ["08:30"], l0=False),
        ],
        "negative_questions": [],
    },
    {
        "dir": "code", "name": "app.py", "l0_mode": "full",
        "build": lambda: make_code(".py", body=_CODE_PY),
        "facts": [
            _f("F-PY-01", "MAX_BATCH_SIZE = 512", "MAX_BATCH_SIZE 的值是多少？", ["512"]),
            _f("F-PY-02", 'SECRET_TAG = "tundra-lab-2077"', "SECRET_TAG 的值是什么？", ["tundra-lab-2077"]),
        ],
        "negative_questions": [],
    },
    {
        "dir": "code", "name": "service.ts", "l0_mode": "full",
        "build": lambda: make_code(".ts", body=_CODE_TS),
        "facts": [
            _f("F-TS-01", "SLA_WINDOW_MINUTES = 45", "SLA_WINDOW_MINUTES 的值是多少？", ["45"]),
            _f("F-TS-02", "星桥网关的重试上限是 3 次", "星桥网关的重试上限是几次？", ["3"]),
        ],
        "negative_questions": [],
    },
    {
        "dir": "code", "name": "Main.java", "l0_mode": "full",
        "build": lambda: make_code(".java", body=_CODE_JAVA),
        "facts": [
            _f("F-JAVA-01", "CACHE_TTL_SECONDS = 900", "CACHE_TTL_SECONDS 的值是多少秒？", ["900"]),
            _f("F-JAVA-02", "只读副本地址 ro.chengchuan.internal", "澄川主库的只读副本地址是什么？", ["ro.chengchuan.internal"]),
        ],
        "negative_questions": [],
    },
]

#: 反向用例（期望 400 + 3101 / 明确失败）
NEGATIVE_SPECS: list[dict] = [
    {"dir": "negative", "name": "legacy.xls", "build": lambda: b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 128, "expect_code": 3101, "hint_contains": ".xlsx"},
    {"dir": "negative", "name": "legacy.ppt", "build": lambda: b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 128, "expect_code": 3101, "hint_contains": ".pptx"},
    {"dir": "negative", "name": "song.mp3", "build": lambda: b"ID3" + b"\x00" * 128, "expect_code": 3101, "hint_contains": "语音转写"},
    {"dir": "negative", "name": "archive.zip", "build": lambda: b"PK\x03\x04" + b"\x00" * 128, "expect_code": 3101, "hint_contains": ".zip"},
    {"dir": "negative", "name": "corrupt.pdf", "build": make_corrupt_pdf, "expect_code": None, "hint_contains": None},
    {"dir": "negative", "name": "legacy.doc", "build": make_doc_like_binary, "expect_code": None, "hint_contains": None},
]


def build_all() -> list[dict]:
    """落盘语料 + 返回 facts.jsonl 的记录列表。"""
    if CORPUS.exists():
        shutil.rmtree(CORPUS)
    records: list[dict] = []
    manifest: list[dict] = []
    for spec in SPECS:
        payload = spec["build"]()
        path = CORPUS / spec["dir"] / spec["name"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        manifest.append({"file": f"{spec['dir']}/{spec['name']}", "bytes": len(payload), "sha256": digest[:12]})
        records.append({
            "file": f"{spec['dir']}/{spec['name']}",
            "l0_mode": spec["l0_mode"],
            "facts": spec["facts"],
            "negative_questions": spec["negative_questions"],
        })
    neg_dir = CORPUS / "negative"
    neg_dir.mkdir(parents=True, exist_ok=True)
    for spec in NEGATIVE_SPECS:
        payload = spec["build"]()
        (neg_dir / spec["name"]).write_bytes(payload)
        manifest.append({"file": f"negative/{spec['name']}", "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()[:12]})
    facts_path = OUT / "facts.jsonl"
    with facts_path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    neg_cases = [{"file": f"negative/{s['name']}", "expect_code": s["expect_code"], "hint_contains": s["hint_contains"]} for s in NEGATIVE_SPECS]
    (OUT / "negative_cases.json").write_text(json.dumps(neg_cases, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"语料已生成：{len(records)} 个正向文件 + {len(NEGATIVE_SPECS)} 个反向用例")
    for m in manifest:
        print(f"  {m['file']:<28} {m['bytes']:>7} B  sha256:{m['sha256']}")
    n_facts = sum(len(r["facts"]) for r in records)
    n_neg = sum(len(r["negative_questions"]) for r in records)
    print(f"金标事实 {n_facts} 条，负向问题 {n_neg} 条 → {facts_path}")
    return records


def self_check() -> None:
    """用真实 ParserRouter 本地解析全部正向语料（不依赖服务）。"""
    sys.path.insert(0, str(REPO / "backend"))
    from app.services.parsers.ocr import MockOcrEngine, OcrParser
    from app.services.parsers.pdf import PdfParser
    from app.services.parsers.docx import DocxParser
    from app.services.parsers.router import ParserRouter
    from app.services.parsers.spreadsheet import SpreadsheetParser

    assets = OUT / ".selfcheck_assets"
    assets.mkdir(parents=True, exist_ok=True)
    router = ParserRouter(
        ocr_parser=OcrParser(MockOcrEngine(), assets),
        docx_parser=DocxParser(assets),
        pdf_parser=PdfParser(ocr_engine=MockOcrEngine(), assets_dir=assets),
        spreadsheet_parser=SpreadsheetParser(),
    )
    failures = 0
    for path in sorted(CORPUS.rglob("*")):
        if not path.is_file() or path.parent.name == "negative":
            continue
        try:
            result = router.parse(path, doc_id=path.stem, tenant_id="selfcheck", policy={})
            print(f"  {path.parent.name}/{path.name:<18} parser={router.route(path):<12} segments={len(result.segments)}")
            if not result.segments:
                print(f"    !! 无段落")
                failures += 1
        except Exception as exc:  # noqa: BLE001 - 自检要打印所有失败
            print(f"  {path.parent.name}/{path.name:<18} 解析失败: {exc}")
            failures += 1
    print(f"自检完成：失败 {failures} 个")
    if failures:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-check", action="store_true", help="生成后用 ParserRouter 本地解析自检")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    build_all()
    if args.self_check:
        self_check()


if __name__ == "__main__":
    main()
