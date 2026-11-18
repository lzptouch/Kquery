"""契约覆盖率守卫（`A-23` 端点 docstring · `A-24` 类型标注）。

| 条目 | 基线 | 目标 |
|---|---|---|
| `A-23` | 路由函数里带 docstring 的比例 | **≥ 90%** |
| `A-24` | 全仓函数里带返回标注的比例 | **≥ 95%** |

## 为什么要有守卫

- **端点是外部契约**：没有 docstring，契约只存在于实现里——调用方只能读代码；
- **类型标注是维护成本**：缺标注的函数在 IDE 里没有补全，重构时也没有静态提示。

守卫写成「**不低于基线**」而不是「等于当前值」：
覆盖率只应升不应降，但也不该因为统计口径的小改动就整片飘红。
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_APP = REPO_ROOT / "backend" / "app"
API_DIR = BACKEND_APP / "api"

DOCSTRING_FLOOR = 0.90
ANNOTATION_FLOOR = 0.95


def _route_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """被 `@router.<method>` 装饰的函数（= 端点实现）。"""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if (
                isinstance(deco, ast.Call)
                and isinstance(deco.func, ast.Attribute)
                and isinstance(deco.func.value, ast.Name)
                and deco.func.value.id == "router"
            ):
                out.append(node)
                break
    return out


def test_endpoint_docstring_coverage_meets_floor():
    """`A-23`：端点 docstring 覆盖率 ≥ 90%。"""
    total = 0
    with_doc = 0
    missing: list[str] = []
    for path in sorted(API_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in _route_functions(tree):
            total += 1
            if ast.get_docstring(fn):
                with_doc += 1
            else:
                missing.append(f"{path.name}:{fn.lineno} {fn.name}")
    assert total > 0, "没有找到任何端点函数（口径坏了？）"
    coverage = with_doc / total
    assert coverage >= DOCSTRING_FLOOR, (
        f"端点 docstring 覆盖率 {coverage:.1%}（{with_doc}/{total}），低于 {DOCSTRING_FLOOR:.0%}；"
        f"缺 docstring 的端点：{missing}"
    )


def test_type_annotation_coverage_meets_floor():
    """`A-24`：全仓函数的返回标注覆盖率 ≥ 95%。"""
    total = 0
    annotated = 0
    missing: list[str] = []
    for path in sorted(BACKEND_APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            total += 1
            if node.returns is not None:
                annotated += 1
            else:
                missing.append(f"{path.name}:{node.lineno} {node.name}")
    assert total > 0, "没有找到任何函数（口径坏了？）"
    coverage = annotated / total
    assert coverage >= ANNOTATION_FLOOR, (
        f"返回标注覆盖率 {coverage:.1%}（{annotated}/{total}），低于 {ANNOTATION_FLOOR:.0%}；"
        f"未标注的函数（前 10 个）：{missing[:10]}"
    )


def test_coverage_guard_detects_missing_docstring():
    """守卫自身有效：构造一个缺 docstring 的端点，断言它会被判不合规。"""
    sample = '''
from app.api import router

@router.get("/x")
def x():
    return 1
'''
    tree = ast.parse(sample)
    fns = _route_functions(tree)
    assert len(fns) == 1
    assert ast.get_docstring(fns[0]) is None
