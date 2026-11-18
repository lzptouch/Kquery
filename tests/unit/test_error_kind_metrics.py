"""错误指标与错误类别（`A-09`）。

## 为什么要有这个指标

此前**没有任何按错误码聚合的指标**：出问题时只能翻日志，而日志按 trace_id 组织，
「上周 4001 出现过几次」这种问题没人答得上来。

## 标签纪律

只有 `code`（有限枚举）与 `kind`（6 类码段映射）两个标签——**不放 trace_id /
路径 / 用户 / 租户**（高基数会让时间序列数量爆炸，见 `core/metrics.py` 的注释）。
"""
from __future__ import annotations

from app.core.envelope import CODE_KINDS, ErrCode, kind_of
from app.core.metrics import PromMetrics


def test_kind_of_maps_code_ranges():
    """码段 → 类别。六个分区各取一个代表码。"""
    assert kind_of(int(ErrCode.UNAUTHORIZED)) == "admission"       # 1xxx
    assert kind_of(int(ErrCode.INVALID_INPUT)) == "input"           # 2xxx
    assert kind_of(int(ErrCode.DOCUMENT_NOT_FOUND)) == "resource"   # 3xxx
    assert kind_of(int(ErrCode.RETRIEVAL_FAILED)) == "upstream"     # 4xxx
    assert kind_of(int(ErrCode.INTERNAL_ERROR)) == "internal"        # 5xxx
    assert kind_of(int(ErrCode.RATE_LIMITED)) == "ratelimit"        # 6xxx


def test_degrade_codes_belong_to_upstream_kind():
    """44xx~49xx 是降级告警，但类别仍属上游（4xxx 段）。"""
    for code in (
        ErrCode.RERANK_DEGRADED,
        ErrCode.PARAMETRIC_FALLBACK,
        ErrCode.CACHE_UNAVAILABLE,
        ErrCode.SCORER_UNAVAILABLE,
        ErrCode.SELFCHECK_UNAVAILABLE,
        ErrCode.INTENT_FAILED,
        ErrCode.REWRITE_FAILED,
        ErrCode.DECOMPOSE_FAILED,
    ):
        assert kind_of(int(code)) == "upstream", code.name


def test_unknown_code_falls_back_to_internal():
    """未知码段归 internal：不凭空造新类别（指标基数会跟着涨）。"""
    assert kind_of(99999) == "internal"
    assert kind_of(0) == "internal"


def test_every_error_code_has_a_known_kind():
    """不变量：每个枚举成员都能落到已知类别（防止新增码漏配）。"""
    for code in ErrCode:
        assert kind_of(int(code)) in CODE_KINDS, code.name


def test_record_error_counts_by_code_and_kind():
    """计数入口：同码累加，标签是 code + kind。"""
    prom = PromMetrics()
    prom.record_error(int(ErrCode.INVALID_INPUT))
    prom.record_error(int(ErrCode.INVALID_INPUT))
    prom.record_error(int(ErrCode.DEPENDENCY_UNAVAILABLE))

    text = prom.render().decode("utf-8")
    assert 'kquery_errors_total{code="2001",kind="input"} 2.0' in text
    assert 'kquery_errors_total{code="5201",kind="internal"} 1.0' in text


def test_record_error_accepts_enum_member():
    """直接传枚举成员也行（内部会转成 int）。"""
    prom = PromMetrics()
    prom.record_error(ErrCode.RATE_LIMITED)
    text = prom.render().decode("utf-8")
    assert 'kquery_errors_total{code="6101",kind="ratelimit"} 1.0' in text
