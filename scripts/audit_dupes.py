#!/usr/bin/env python3
"""重复实现 / 死代码审计（结构整理用）。

三项检查：
1. **同名顶层定义**：同一函数/类名在多个文件里各有一份 → 典型的"复制粘贴分叉"；
2. **从未被引用的模块**：没有任何地方 import 它 → 死代码候选；
3. **重复的字面量常量**：同一 magic number/字符串在多处各写一遍。

只报事实，不改代码（改动由人决定）。
"""
from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ["backend/app", "scripts"]
SKIP_DIRS = {"__pycache__", ".venv", "node_modules", ".ruff_cache", ".pytest_cache"}
#: 允许重名的通用小工具（各文件自己实现是有意的，不算重复实现）
ALLOW_DUPLICATE_NAMES = {
    "main", "get", "post", "put", "delete", "patch", "router",
    "logger", "client", "response", "request", "app", "config",
}


def _py_files() -> list[Path]:
    out: list[Path] = []
    for d in SCAN_DIRS:
        for p in (REPO_ROOT / d).rglob("*.py"):
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            out.append(p)
    return out


def _top_defs(path: Path) -> dict[str, str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return {}
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[node.name] = node.__class__.__name__
    return out


def _decorated_names(path: Path) -> set[str]:
    """带装饰器的顶层定义（路由、fixture、注册函数……）。

    它们**按名字不被引用**是正常的（FastAPI 用装饰器注册），
    所以死代码检查必须排除，否则整层 api/ 都会被误报。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return set()
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.decorator_list:
                out.add(node.name)
    return out


def _imported_modules() -> set[str]:
    """收集所有 import 过的模块名（app.* / scripts.*）。"""
    names: set[str] = set()
    for p in list(_py_files()) + list((REPO_ROOT / "tests").rglob("*.py")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    names.add(a.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
                for a in node.names:
                    names.add(f"{node.module}.{a.name}")
    return names


def main() -> int:
    files = _py_files()
    by_name: dict[str, list[str]] = defaultdict(list)
    for p in files:
        for name, kind in _top_defs(p).items():
            if name in ALLOW_DUPLICATE_NAMES or name.startswith("_"):
                continue
            by_name[name].append(f"{p.relative_to(REPO_ROOT)}[{kind}]")

    print("=== 1. 同名顶层定义（潜在重复实现）===")
    dupes = {k: v for k, v in by_name.items() if len(v) > 1}
    if not dupes:
        print("（无）")
    for name, places in sorted(dupes.items()):
        print(f"  {name}: {', '.join(places)}")

    print("\n=== 2. 从未被 import 的模块（死代码候选）===")
    imported = _imported_modules()
    orphans: list[str] = []
    for p in files:
        rel = p.relative_to(REPO_ROOT)
        parts = rel.with_suffix("").parts
        # import 名不带 "backend/" 前缀（app.* 而不是 backend.app.*）
        if parts and parts[0] == "backend":
            parts = parts[1:]
        mod = ".".join(parts)
        if mod.startswith("scripts.") or mod.endswith("__init__"):
            continue
        # 模块自身被引用（app.services.x）或其顶层符号被引用都算活着
        if mod in imported:
            continue
        if any(m.startswith(mod + ".") for m in imported):
            continue
        symbols = set(_top_defs(p))
        if symbols and any(f"{mod}.{s}" in imported for s in symbols):
            continue
        # 名字唯一出现也可能是被动态引用（__all__ / 入口脚本）
        if len(symbols) <= 1 and symbols and next(iter(symbols)) == "main":
            continue
        orphans.append(str(rel))
    for o in sorted(orphans):
        print(f"  {o}")
    if not orphans:
        print("（无）")

    print("\n=== 3. 从未被引用的顶层定义（死代码）===")
    # 收集全仓出现过的标识符（粗但有效：宁可多报，不可漏报）
    counts: dict[str, int] = defaultdict(int)
    for p in list(_py_files()) + list((REPO_ROOT / "tests").rglob("*.py")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        text = p.read_text(encoding="utf-8")
        for tok in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", text):
            counts[tok] += 1
    dead: list[str] = []
    for p in files:
        rel = p.relative_to(REPO_ROOT)
        decorated = _decorated_names(p)
        for name, kind in _top_defs(p).items():
            if name.startswith("_") or name in ALLOW_DUPLICATE_NAMES:
                continue
            if name in decorated:
                continue  # 路由 / fixture：按名字不被引用是正常的
            # 出现次数 <= 1（只有定义处那一次）→ 从未被引用
            if counts.get(name, 0) <= 1:
                dead.append(f"{rel}:{name}[{kind}]")
    for d in sorted(dead):
        print(f"  {d}")
    if not dead:
        print("（无）")

    print("\n=== 4. 重复的魔法常量（同值出现在多处）===")
    pattern = re.compile(r"^\s*([A-Z_]{3,})\s*[:=]\s*(.+)$", re.M)
    consts: dict[str, set[str]] = defaultdict(set)
    for p in files:
        text = p.read_text(encoding="utf-8")
        for m in pattern.finditer(text):
            name, value = m.group(1), m.group(2).strip().rstrip(",")
            if len(value) > 60 or value.startswith(("#", "\"\"\"")):
                continue
            consts[value].add(name)
    for value, names in sorted(consts.items(), key=lambda kv: -len(kv[1])):
        if len(names) > 1:
            print(f"  {value}  ←  {sorted(names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
