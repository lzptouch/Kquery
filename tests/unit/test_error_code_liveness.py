"""错误码活性不变量（`A-10`）。

## 纪律

每个错误码必须满足**二者之一**：

1. **有生产引用**——在 `backend/app/`（枚举定义文件 `core/envelope.py` 自身除外）
   出现过 `ErrCode.<NAME>`；
2. **登记保留理由**——在 `core/envelope.py` 的 `RETAINED_CODES` 里写明「为何保留」。

两者都没有 = **死位**：枚举里占着一个号，前端按码分流却永远收不到它，
留下一条死分支；而后来者还会以为「这个码有人在用」。

## 为什么排除 `core/envelope.py`

`RETAINED_CODES` 本身就写在 `envelope.py` 里，若把它算作「生产引用」，
登记理由就等于自证——不变量会永远成立，也就永远测不出问题。
"""
from __future__ import annotations

import re
from pathlib import Path

from app.core.envelope import RETAINED_CODES, ErrCode

BACKEND_APP = Path(__file__).resolve().parents[2] / "backend" / "app"
_ENUM_FILE = "envelope.py"
_REF_RE = re.compile(r"ErrCode\.([A-Z_]+)")


def _production_references() -> set[str]:
    """生产代码里引用过的错误码名（排除枚举定义文件自身）。"""
    names: set[str] = set()
    for path in BACKEND_APP.rglob("*.py"):
        if path.name == _ENUM_FILE:
            continue
        names |= set(_REF_RE.findall(path.read_text(encoding="utf-8")))
    return names


def test_every_error_code_is_wired_or_explicitly_retained():
    """核心不变量：既无生产引用又无保留理由的码 = 死位，必须为零。"""
    referenced = _production_references()
    dead = [
        code.name
        for code in ErrCode
        if code.name not in referenced
        and not (RETAINED_CODES.get(code) or "").strip()
    ]
    assert not dead, (
        f"以下错误码既无生产引用、也无保留理由（死位）：{dead}。"
        "裁决方式：接线到真实场景，或写进 core/envelope.py 的 RETAINED_CODES"
    )


def test_retained_codes_have_non_empty_reason():
    """保留码必须写清「为何保留」——空理由等于没裁决。"""
    for code, reason in RETAINED_CODES.items():
        assert code in ErrCode, f"{code} 不在 ErrCode 里"
        assert reason and reason.strip(), f"{code.name} 缺少保留理由"


def test_retained_codes_are_actually_unwired():
    """登记了保留理由的码，若已经有生产引用，说明理由该删了（避免过期登记）。"""
    referenced = _production_references()
    for code in RETAINED_CODES:
        assert code.name not in referenced, (
            f"{code.name} 已接入生产路径，RETAINED_CODES 里的保留理由已过期"
        )


def test_no_duplicate_error_code_values():
    """数值唯一（IntEnum 重复值会产生别名，按码分流会失效）。"""
    values = [int(code) for code in ErrCode]
    assert len(values) == len(set(values))
