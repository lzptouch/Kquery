"""控制台断言报告（smoke_api / 浏览器端到端脚本共用）。

此前 `smoke_api.py` 与 `browser_helpers.py` 各有一份 `check` / `section`，
而且**行为不一致**：`check` 一个返回 None、一个返回 bool，
失败记录一个存元组、一个存拼好的字符串。
统一到这里：**一个实现、一处修**，调用方不必再判断"这个 check 返回什么"。
"""
from __future__ import annotations

#: 通过的用例名
PASSED: list[str] = []
#: 失败的用例：(名称, 详情)
FAILED: list[tuple[str, str]] = []


def reset() -> None:
    """清空统计（一个进程里跑多轮时用）。"""
    PASSED.clear()
    FAILED.clear()


def check(name: str, condition: bool, detail: str = "") -> bool:
    """记录并打印一条断言结果，返回该断言是否通过。

    返回 bool 是刻意的：调用方常写成
    ``if not check(...): return 1``，返回 None 会让"通过"和"失败"无法区分。
    """
    if condition:
        PASSED.append(name)
        print(f"  \033[32m✓\033[0m {name}")
        return True
    FAILED.append((name, detail))
    print(f"  \033[31m✗\033[0m {name} — {detail}")
    return False


def section(title: str) -> None:
    """打印一个分组标题。"""
    print(f"\n\033[1m{title}\033[0m")


def summary(total_extra: int = 0) -> int:
    """打印汇总，返回进程退出码（0=全过）。"""
    total = len(PASSED) + len(FAILED) + total_extra
    print(f"\n通过 {len(PASSED)} / {total}")
    if FAILED:
        print(f"\033[31m失败 {len(FAILED)} 项：\033[0m")
        for name, detail in FAILED:
            print(f"  - {name}: {detail}")
        return 1
    return 0
