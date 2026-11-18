"""`C-05` 压测判据闭环：指标标签组合数不得随请求量膨胀。

## 判据是什么

Prometheus 的高基数事故不是「指标多」，而是「**label 取值由请求方决定**」。
一旦某个 label 的取值来自 URL 原文、文档 ID、查询串或错误消息，
每来一个新值就多一条时间序列，序列数随时间无界增长，最终把采集端打挂。

所以判据不是「序列数小于某个数」，而是**两条可判定的性质**：

1. **不随量增长**：同样的请求形状，跑 5 倍流量后序列集合**完全不变**；
2. **取值有界**：每个 label 的取值 ⊆ 路由表 / 枚举值 / 固定占位符。

这条判据此前挂在「需独立压测环境」上没落地。其实它不需要压测环境——
它是**基数性质**，不是吞吐性质，在进程内发几十个请求就能证明。
真正的压测（`scripts/metrics_cardinality.py --base-url`）只是把同样的判据
在真实流量下再跑一遍，用来抓「只有并发才出现」的路径。

## 这条用例抓到的真实缺陷

未匹配路由（404）的 `route` label 曾回退到 `request.url.path`：
`/wp-login.php`、随机 UUID 每次都会新建一条序列。修复点是
`bootstrap.py` 里把未匹配路由收敛到 `<unmatched>`（日志仍保留原始路径）。
"""
from __future__ import annotations

import re

import pytest

#: `name{label="value",...}` 形式的样本行；`name` 带 `_bucket/_count/_sum` 后缀也吃进来
_SAMPLE = re.compile(r'^(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)\{(?P<labels>[^}]*)\}\s')

#: 允许出现在任何 label 名里的敏感/高基数字段（出现即判失败）
FORBIDDEN_LABELS = ("tenant", "tenant_id", "user", "user_id", "doc_id", "query", "trace_id", "path")


def _series(metrics_text: str) -> dict[str, set[tuple[str, ...]]]:
    """把 `/metrics` 文本解析成 {指标名: {label 组合}}。"""
    out: dict[str, set[tuple[str, ...]]] = {}
    for line in metrics_text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        m = _SAMPLE.match(line)
        if not m:
            continue
        labels = tuple(
            sorted(
                f"{k}={v}"
                for k, v in re.findall(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"', m.group("labels"))
            )
        )
        out.setdefault(m.group("name"), set()).add(labels)
    return out


def _label_names(metrics_text: str) -> set[str]:
    names: set[str] = set()
    for line in metrics_text.splitlines():
        m = _SAMPLE.match(line)
        if m:
            names.update(re.findall(r'([a-zA-Z_][a-zA-Z0-9_]*)="', m.group("labels")))
    return names


def _crack_requests(client, paths: list[str]) -> None:
    """按给定路径形状打一轮请求（状态码不重要，重要的是**形状**）。"""
    for path in paths:
        client.get(path)


def _unknown_paths(n: int, tag: str) -> list[str]:
    """模拟扫描器/随机打点：每个路径都不同，正是高基数的来源。"""
    return [f"/{tag}-{i}" for i in range(n)]


def _doc_id_paths(n: int, tag: str) -> list[str]:
    return [f"/knowledge/doc-{tag}-{i}" for i in range(n)]


@pytest.mark.timeout(120)
def test_series_do_not_grow_with_traffic(client):
    """核心判据：请求量 ×5，序列集合不变。"""
    shapes = [
        "/healthz",
        "/readyz",
        "/metrics",
        "/api/nonexistent-endpoint",
        "/knowledge?limit=999999",
    ]
    # 预热：先把五种形状各打一次，让「第一次遇见某路由」也计入基线
    _crack_requests(client, shapes)
    _crack_requests(client, _unknown_paths(3, "warm"))
    baseline = _series(client.get("/metrics").text)

    # 同样的形状 ×5，且未知路径**全部换新值**——这是最容易漏的膨胀源
    for _ in range(5):
        _crack_requests(client, shapes)
        _crack_requests(client, _unknown_paths(3, "burst"))
        _crack_requests(client, _doc_id_paths(3, "burst"))

    after = _series(client.get("/metrics").text)

    for name, series in after.items():
        new = series - baseline.get(name, set())
        assert not new, (
            f"{name} 在 5 倍流量下新增了 {len(new)} 条时间序列（高基数风险）："
            f"{sorted(new)[:3]}"
        )


def test_unknown_routes_collapse_to_placeholder(client):
    """未匹配路由必须收敛到 `<unmatched>`，不能把原始路径当 label。"""
    _crack_requests(client, _unknown_paths(4, "scan"))
    text = client.get("/metrics").text
    series = _series(text).get("kquery_http_requests_total", set())
    assert series, "访问指标没有样本，埋点可能没接上"
    joined = "\n".join("|".join(s) for s in series)
    assert "route=<unmatched>" in joined
    assert "scan-0" not in joined, "原始路径泄漏进指标 label（无界基数）"


def test_doc_id_never_becomes_label(client):
    """文档 ID 走路径参数时，label 必须是模板（`/knowledge/{doc_id}`）而不是具体 ID。"""
    _crack_requests(client, _doc_id_paths(3, "x"))
    text = client.get("/metrics").text
    http_part = "".join(
        line for line in text.splitlines() if line.startswith("kquery_http_")
    )
    assert "doc-x-" not in http_part, "doc_id 泄漏进指标 label"


def test_no_forbidden_label_names(client):
    """任何指标都不得带租户/用户/文档/查询串/trace 这类高基数或敏感 label。"""
    client.get("/healthz")
    names = _label_names(client.get("/metrics").text)
    leaked = sorted(names & set(FORBIDDEN_LABELS))
    assert not leaked, f"指标里出现了不该有的 label：{leaked}"


def test_route_label_values_are_bounded(client):
    """`route` 的取值必须是有限闭集：路由模板 + `<unmatched>`。"""
    _crack_requests(client, ["/healthz", "/api/nope-1", "/api/nope-2", "/knowledge?limit=1"])
    text = client.get("/metrics").text
    routes = set()
    for line in text.splitlines():
        m = _SAMPLE.match(line)
        if not m or not m.group("name").startswith("kquery_http"):
            continue
        found = dict(re.findall(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"', m.group("labels")))
        if "route" in found:
            routes.add(found["route"])
    assert routes, "没有采到 route label"
    for route in routes:
        assert route.startswith("/") or route == "<unmatched>", f"非法 route label：{route}"
        assert "nope-" not in route
    # 上界：应用注册的路由数量 + 1（占位符）。超了说明有动态值混进来。
    app_routes = len({getattr(r, "path", "") for r in client.app.routes})
    assert len(routes) <= app_routes + 1, f"route 取值数 {len(routes)} 超过路由表上界 {app_routes}"
