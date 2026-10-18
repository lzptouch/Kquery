"""口令哈希（阶段 9 §8.1 账号密码登录配套）。

## 为什么不用 bcrypt / argon2

`auth.py` 的 JWT 实现已经确立了一条约束：**安全关键逻辑自己写，把"接受哪些参数、
拒绝哪些输入"留在代码里而不是藏进依赖**（见该文件模块级说明）。本模块沿用同一取向，
只用标准库 `hashlib.pbkdf2_hmac`——PBKDF2 是 NIST 认可的口令派生函数，
参数（迭代轮数）显式可见可调，出问题时能直接定位到"轮数不够"而不是"库的默认值变了"。

## 参数选择

`_ITERATIONS = 600_000` 对应 OWASP 2023 对 PBKDF2-HMAC-SHA256 的建议下限。
实测本机单次派生耗时见 `scripts/` 中的基准（百毫秒量级），对登录场景可接受；
轮数只增不减，**已在库中的口令不会被自动重新哈希**（见 `needs_rehash`）。

## 存储格式

    pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>

单行自描述，含算法名与轮数，因此未来调高轮数时旧口令仍能校验通过
（`verify_password` 读取口令自带的轮数，而不是当前常量）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_ALGO = "pbkdf2_sha256"
_SALT_BYTES = 16
#: OWASP 2023 对 PBKDF2-HMAC-SHA256 的建议下限；调高不影响存量口令
_ITERATIONS = 600_000

#: 最小长度与"必须同时含字母和数字"——纯数字或纯字母在 6 位以上都极易爆破
MIN_LENGTH = 8


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str) -> str:
    """生成一条自描述的哈希记录（含随机盐）。相同口令每次结果不同。"""
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, encoded: str | None) -> bool:
    """常量时间校验。

    `encoded` 为 `None`（账号从未设过口令）或格式非法时返回 `False`：
    登录端点对这两种情况必须返回**完全相同**的错误码与文案，
    否则"该账号没有口令"就成了一条可用于批量探测账号是否存在的侧信道。
    """
    if not encoded:
        return False
    parts = encoded.split("$")
    if len(parts) != 4:
        return False
    algo, iterations, salt_b64, hash_b64 = parts
    if algo != _ALGO:
        return False
    try:
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
        rounds = int(iterations)
    except (ValueError, TypeError):
        return False
    if rounds <= 0:
        return False
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(derived, expected)


def validate_password_strength(password: str) -> str | None:
    """返回不合规的原因；`None` 表示通过。口令规范只有两条，避免"什么算强口令"变成玄学。"""
    if len(password) < MIN_LENGTH:
        return f"口令至少 {MIN_LENGTH} 位"
    has_letter = any(c.isalpha() for c in password)
    has_digit = any(c.isdigit() for c in password)
    if not (has_letter and has_digit):
        return "口令需同时包含字母和数字"
    return None


def needs_rehash(encoded: str | None) -> bool:
    """轮数低于当前常量时为真——用于登录成功后顺带升密，不打扰用户。"""
    if not encoded:
        return True
    parts = encoded.split("$")
    if len(parts) != 4 or parts[0] != _ALGO:
        return True
    try:
        return int(parts[1]) < _ITERATIONS
    except (ValueError, TypeError):
        return True


def burn(password: str) -> None:
    """对不存在的账号执行一次等价的派生运算。

    没有这一步时，"账号不存在"会因为少一次 PBKDF2 而比"口令错误"快几十到几百倍，
    攻击者据此就能枚举出哪些账号存在——而这恰好是登录接口最需要避免的信息泄漏。
    """
    hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), b"\x00" * _SALT_BYTES, _ITERATIONS)
