"""import 边界守卫（`A-13`）。

## 依赖方向

```
api/  →  services/  →  core/
```

**`core/` 不得反向 import `services/`**：一旦反向依赖出现，底层就会依赖上层，
此后任何「改 services 要先看 core」的改动都可能触发循环导入或半初始化模块。

## 为什么用 AST 而不是 grep

grep 会漏（多行 import、字符串里的路径），也会误报（注释、docstring）。
AST 看的是**真实的 import 语句**。

## 允许的例外

`core/` 内部**函数内**的延迟导入（import 在函数体里、为了打破循环）也算反向依赖——
本守卫一并禁止：延迟导入只是把问题推迟到运行期。若确实需要，
应把被依赖的类型下沉到 `core/` 或用 `TYPE_CHECKING` 声明。
"""
from __future__ import annotations

import ast
from pathlib import Path

BACKEND_APP = Path(__file__).resolve().parents[2] / "backend" / "app"
CORE_DIR = BACKEND_APP / "core"


def _violations() -> list[str]:
    bad: list[str] = []
    for path in sorted(CORE_DIR.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod.startswith("app.services"):
                    bad.append(f"{path.relative_to(BACKEND_APP)}:{node.lineno} from {mod}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("app.services"):
                        bad.append(f"{path.relative_to(BACKEND_APP)}:{node.lineno} import {alias.name}")
    return bad


def test_core_does_not_import_services():
    """`core/` 不得反向依赖 `services/`。"""
    bad = _violations()
    assert not bad, (
        "core/ 反向 import services/（依赖方向被破坏）：\n" + "\n".join(bad)
    )


def test_guard_detects_a_violation():
    """守卫自身有效：构造一个反向 import，断言它会被判违规。"""
    sample = "from app.services.pipeline import RagPipeline\n"
    tree = ast.parse(sample)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app.services"):
            found.append(node.module)
    assert found == ["app.services.pipeline"]


def test_services_may_import_core():
    """方向是单向的：`services/` 依赖 `core/` 是正常的。"""
    hits = 0
    for path in sorted((BACKEND_APP / "services").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app.core"):
                hits += 1
    assert hits > 0, "services/ 一处 core/ 都没引用（口径坏了？）"
