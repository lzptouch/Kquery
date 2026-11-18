"""兜底必须有痕迹（`A-08`）。

## 三档规范（设计文档 §四 3）

| 档 | 场景 | 义务 |
|---|---|---|
| **A 档 · 边界兜底** | 主循环、后台 worker、第三方 SDK 包装 | 记完整堆栈 + **写明为何能吞** + 不改变对外语义 |
| **B 档 · 降级兜底** | 单个阶段失败但整体可继续 | 记 warning + 上报降级指标 + 结果里显式标记「该阶段未生效」 |
| **C 档 · 静默兜底** | —— | **禁止** |

## 机器可判的规则

「C 档」= 捕获 `Exception` 之后**既不留日志、也不上报指标、也不重新抛出**，
并且**没写明为何能吞**——即故障被完全吃掉，事后查无对证。

本守卫要求每个宽兜底（`except Exception` / `except (Exception, ...)`）至少满足一条：

1. 有日志调用（`logger.*`）；
2. 有指标写入（`record_error` / `.inc()` / `.observe()`）；
3. 重新抛出（`raise`）；
4. 写了 `# noqa: BLE001 - <理由>`（"写明为何能吞" 这项义务）。

注意：规则 4 **不能**单独让 C 档复活——它只要求「说清楚为什么能吞」，
若同时没有任何痕迹，规则 1~3 仍需满足其一。理由是：有的路径**故意**不打日志
（如指标采集失败、日志系统自身），此时理由就是唯一的留痕。
"""
from __future__ import annotations

import ast
from pathlib import Path

BACKEND_APP = Path(__file__).resolve().parents[2] / "backend" / "app"

# 宽兜底的类型表达式：Exception，或元组里含 Exception
def _is_wide_except(node: ast.ExceptHandler) -> bool:
    t = node.type
    if isinstance(t, ast.Name):
        return t.id == "Exception"
    if isinstance(t, ast.Tuple):
        return any(isinstance(x, ast.Name) and x.id == "Exception" for x in t.elts)
    return False


def _has_log_call(node: ast.ExceptHandler) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in ("logger", "log"):
                return True
    return False


def _has_metric_call(node: ast.ExceptHandler) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Attribute) and f.attr in ("record_error", "inc", "observe", "set"):
                return True
    return False


def _has_raise(node: ast.ExceptHandler) -> bool:
    return any(isinstance(s, ast.Raise) for s in ast.walk(node))


def _has_reason(line: str) -> bool:
    """except 行上是否写了「为何能吞」的理由。"""
    tail = line.split("noqa: BLE001", 1)[1] if "noqa: BLE001" in line else ""
    return tail.strip().startswith("-")


def _violations() -> list[str]:
    bad: list[str] = []
    for path in sorted(BACKEND_APP.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        lines = src.splitlines()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler) or not _is_wide_except(node):
                continue
            line = lines[node.lineno - 1]
            has_trace = (
                _has_log_call(node)
                or _has_metric_call(node)
                or _has_raise(node)
                or _has_reason(line)
            )
            if not has_trace:
                bad.append(f"{path.relative_to(BACKEND_APP)}:{node.lineno}")
    return bad


def test_no_silent_except_in_production_code():
    """C 档（静默兜底）必须为零。"""
    bad = _violations()
    assert not bad, (
        "以下宽兜底既无日志、也无指标、也不重新抛出、也没写明为何能吞（C 档·静默兜底）：\n"
        + "\n".join(bad)
    )


def test_guard_actually_catches_violations():
    """守卫自身有效：构造一个 C 档样例，断言它会被判不合规。

    没有这一条，守卫有可能因为「解析规则写错」而永远绿。
    """
    sample = '''
def f():
    try:
        g()
    except Exception:
        return None
'''
    tree = ast.parse(sample)
    handler = next(n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler))
    line = "    except Exception:"
    assert not (
        _has_log_call(handler)
        or _has_metric_call(handler)
        or _has_raise(handler)
        or _has_reason(line)
    )


def test_wide_except_detection_covers_tuple_form():
    """`except (Exception, ValueError)` 也要算宽兜底。"""
    tree = ast.parse("try:\n    x()\nexcept (Exception, ValueError):\n    pass\n")
    handler = next(n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler))
    assert _is_wide_except(handler)
