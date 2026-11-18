"""裸异常计数守卫（`A-07` 验收：裸异常 ≤ 12）。

裸异常（`raise RuntimeError` / `raise ValueError`）**不携带错误码**：
既映射不到统一信封，也没法在指标里按类别聚合，还会让调用方只能全接异常。

口径：`backend/app/core/` + `backend/app/services/`（与审计计划一致）。
**允许保留**的少数几处：
- `core/envelope.py` 的错误码唯一性自检（启动期断言，抛裸异常是对的）；
- 注释与 docstring 里出现的文字（不算 raise 语句）。

新增裸异常会被这个守卫挡住——要加就必须先想清楚「该用哪个领域异常」。
"""
from __future__ import annotations

import ast
from pathlib import Path

BACKEND_APP = Path(__file__).resolve().parents[2] / "backend" / "app"
SCOPES = ("core", "services")
LIMIT = 12

# 有意保留裸异常的位置（文件相对 backend/app 的路径）→ 理由
ALLOWED = {
    "core/envelope.py": "错误码唯一性自检：启动期断言，抛裸异常是对的（不是业务失败）",
}


def _bare_raise_sites() -> list[str]:
    sites: list[str] = []
    for scope in SCOPES:
        for path in sorted((BACKEND_APP / scope).rglob("*.py")):
            rel = str(path.relative_to(BACKEND_APP))
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Raise) or node.exc is None:
                    continue
                exc = node.exc
                if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                    if exc.func.id in ("RuntimeError", "ValueError"):
                        sites.append(f"{rel}:{node.lineno}")
    return sites


def test_bare_exception_count_within_limit():
    sites = _bare_raise_sites()
    unexpected = [s for s in sites if s.split(":")[0] not in ALLOWED]
    assert len(unexpected) <= LIMIT, (
        f"裸异常 {len(unexpected)} 处（上限 {LIMIT}）：{unexpected}。"
        "改用领域异常：backend/app/core/errors.py"
    )


def test_allowed_sites_still_exist():
    """登记的「有意保留」必须真的还在——否则说明它已被改掉，台账该更新了。"""
    sites = _bare_raise_sites()
    for rel in ALLOWED:
        assert any(s.startswith(rel + ":") for s in sites), (
            f"{rel} 已不再有裸异常，ALLOWED 台账该更新了"
        )


def test_domain_exceptions_are_used_in_those_places():
    """反向检查：核心服务里应当**真的**在用领域异常（不是只定义了不用）。"""
    names = (
        "ConfigError", "InvalidInputError", "UpstreamUnavailableError",
        "UpstreamTimeoutError", "ModelOutputError", "ToolExecutionError",
        "StorageCorruptedError",
    )
    hits = 0
    for path in (BACKEND_APP / "services").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in names:
            if f"raise {name}(" in text:
                hits += 1
    assert hits >= 10, f"领域异常的实际使用点太少（{hits}）：说明替换没做透"
