"""死代码守卫（`A-21`）。

## 纪律

一个符号在**生产路径**有调用点才算「有」；只在测试里被引用、或只有定义没有读取，
一律判死代码。死代码不是「不优雅」，是**负债**——它让读者以为该能力存在、
让 grep 结果失真、让「到底有没有实现」无法回答。

## 守卫的判定

对本清单里的每个符号，只允许两种状态：

1. **已删除**——代码里找不到它的定义；
2. **已接线**——定义还在，且在 `backend/app/`（生产代码）里有真实使用点。

两者都不是 → 红。
"""
from __future__ import annotations

import ast
from pathlib import Path

BACKEND_APP = Path(__file__).resolve().parents[2] / "backend" / "app"

# 符号 → 说明（说明要写清「它是死代码时为什么是负债」）
SYMBOLS: dict[str, str] = {
    "BackgroundQueue": "通用后台队列：曾只有测试引用；现由入库服务消费（并带来上下文传播）",
    "current_version_only": "检索过滤的死字段：版本过滤实际由 hybrid.py 的 is_current_version 驱动",
    "overfetch_limit": "检索过滤的死方法：无任何调用点",
    "max_items": "上下文组装的死参数：生产从不传 → MMR 上限恒等于候选数（多样性形同虚设）",
    "reset_clock": "预算的死方法：只有定义，没有调用点",
    "LINEAR_THRESHOLD": "向量索引的死常量：无读取点，注释却承诺了不存在的分支",
    "_context_dropped": "被丢弃的上下文条目：曾只写不读 → 前端拿不到「为什么没进上下文」",
    "specs": "工具注册表的规格方法：曾无生产调用点 → 现由 /admin/tools 消费",
}


def _definitions() -> set[str]:
    """生产代码里定义过的函数 / 类名（含方法名）。"""
    names: set[str] = set()
    for path in BACKEND_APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
    return names


def _production_usages(name: str) -> int:
    """生产代码里对 `name` 的引用次数（属性访问 / 调用 / 名字引用）。"""
    hits = 0
    for path in BACKEND_APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == name:
                hits += 1
            elif isinstance(node, ast.Name) and node.id == name:
                hits += 1
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == name:
                    hits += 1
    return hits


def test_no_symbol_is_defined_without_production_use():
    """每个符号要么已删除，要么在生产路径上有真实使用点。"""
    definitions = _definitions()
    problems: list[str] = []
    for symbol, why in SYMBOLS.items():
        if symbol not in definitions:
            continue  # 已删除 ✓
        uses = _production_usages(symbol)
        # 定义本身算 1 次引用（ast.Name 命中 def 名），所以阈值是 > 1
        if uses <= 1:
            problems.append(f"{symbol}（{why}）：只有定义，没有生产调用点")
    assert not problems, "以下符号仍是死代码：\n" + "\n".join(problems)


def test_dead_code_list_is_not_empty_but_each_entry_is_resolved():
    """清单本身不能为空（否则守卫会因为「没有条目」而永远绿）。"""
    assert SYMBOLS, "死代码清单空了：要么是全修完了（该删清单），要么是误删"
