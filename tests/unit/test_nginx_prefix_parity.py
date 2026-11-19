"""前端 dev / 生产代理前缀一致性守卫（`A-25`）。

## 为什么要有这个守卫

`frontend/vite.config.ts`（dev）与 `deploy/docker/frontend.nginx.conf`（容器）各写了一份
「哪些路径转发给后端」的清单。两份清单**手写且无校验**，历史上漏过 `/readyz`：
dev 下探针请求落到 SPA 的 HTML —— **HTTP 200 但内容不是 JSON**，
排查时极难看出是代理层搞错了。

## 为什么不能按行 grep

两份文件的组织方式**不同**：

- vite：`proxy: { "/ask": apiProxy, "/knowledge": apiProxy, ... }` —— 16 个独立键；
- nginx：两条正则 —— 运维端点 `^/(healthz|readyz|livez|health|metrics)$`
  与 API 前缀 `^/(ask|knowledge|...|quickask)(/|$)`。

所以守卫必须**解析正则**（把 `|` 拆成前缀集合），而不是按行比对文本。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VITE_CONFIG = REPO_ROOT / "frontend" / "vite.config.ts"
NGINX_CONF = REPO_ROOT / "deploy" / "docker" / "frontend.nginx.conf"

# vite 的 proxy 键：`"/xxx": apiProxy`
_VITE_KEY_RE = re.compile(r'"(?P<prefix>/[A-Za-z0-9_\-/]*)"\s*:\s*apiProxy')
# nginx 的 location 正则：`location ~ ^/(a|b|c)$ {`
_NGINX_LOCATION_RE = re.compile(r"location\s+~\s+\^/\((?P<alts>[^)]*)\)")


def _vite_prefixes() -> set[str]:
    text = VITE_CONFIG.read_text(encoding="utf-8")
    return {m.group("prefix") for m in _VITE_KEY_RE.finditer(text)}


def _nginx_prefixes() -> set[str]:
    text = NGINX_CONF.read_text(encoding="utf-8")
    out: set[str] = set()
    for m in _NGINX_LOCATION_RE.finditer(text):
        for alt in m.group("alts").split("|"):
            alt = alt.strip()
            if alt:
                out.add("/" + alt)
    return out


def test_vite_and_nginx_prefixes_are_identical():
    """两份清单必须完全一致（多一个少一个都算不一致）。"""
    vite = _vite_prefixes()
    nginx = _nginx_prefixes()
    assert vite, "vite 配置里没解析出任何代理前缀（口径坏了？）"
    assert nginx, "nginx 配置里没解析出任何 location 前缀（口径坏了？）"
    assert vite == nginx, (
        "dev 与生产的代理前缀不一致："
        f"仅 vite 有 {sorted(vite - nginx)}；仅 nginx 有 {sorted(nginx - vite)}"
    )


def test_probe_prefixes_present_in_both():
    """探针前缀必须在两份清单里（漏了就会出现「200 但不是 JSON」）。"""
    for prefix in ("/healthz", "/readyz", "/livez"):
        assert prefix in _vite_prefixes(), f"vite 缺少探针前缀 {prefix}"
        assert prefix in _nginx_prefixes(), f"nginx 缺少探针前缀 {prefix}"


def test_quickask_present_in_both():
    """快问是后加的能力，两边都漏过（dev 能用、prod 404）。"""
    for prefix in ("/quickask",):
        assert prefix in _vite_prefixes(), f"vite 缺少 {prefix}"
        assert prefix in _nginx_prefixes(), f"nginx 缺少 {prefix}"


def test_guard_detects_mismatch():
    """守卫自身有效：构造一份少一个前缀的 nginx 配置，断言会被判不一致。"""
    vite = _vite_prefixes()
    nginx = _nginx_prefixes()
    simulated = nginx - {"/quickask"}
    assert vite != simulated, "守卫逻辑写错了：少一个前缀竟然判定为一致"
