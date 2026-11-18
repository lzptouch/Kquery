"""提示词注入检测（`KQ-27` 第二层 · 入库侧打标）。

## 为什么入库侧要打标

提示词加固（第一层）是**结构提示**，不是安全边界——模型仍可能被文档里的
「忽略以上指令」带跑。OWASP LLM01 的结论是**分层防御**：除了加固提示词，
还要在**入库侧**识别出「这段文本在试图指挥模型」，并让检索侧对它降权。

## 为什么是「降权」而不是「排除」

排除会变成攻击者的工具：只要在文档里写一句「忽略以上指令」，
这段内容就被系统「保护」起来不再被检索到——**攻击者可以靠触发拦截来隐藏内容**
（把自己的违规内容藏在「被判为注入」的文档里）。
降权只是让它排后面，正常内容仍可被检索到。

## 变形绕过

检测前必须做 Unicode 规范化（NFKC）+ 去零宽字符 + 去控制字符，
否则「忽 略 上 一 条 指 令」「ｉｇｎｏｒｅ ａｌｌ」这类变形会绕过字面匹配。
"""
from __future__ import annotations

import re
import unicodedata

from app.core.logging_setup import get_logger

logger = get_logger(__name__)

# 高风险指令模式（命中即打标）。
# 覆盖的是「让模型忽略既有指令 / 扮演别的角色 / 调用工具 / 泄露秘密」四类。
_RISK_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 忽略 / 覆盖既有指令
    re.compile(r"忽略(?:掉)?(?:以上|上面|之前|前面|此前|所有)?(?:的)?(?:所有)?(?:指令|命令|提示|规则|要求)"),
    re.compile(r" disregard\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions|prompts|rules)", re.I),
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions|prompts|rules)", re.I),
    # 角色改写 / 系统消息伪造
    re.compile(r"(?:你现在是|从现在起你是|扮演|假装你是|你必须是)[^\n。]{0,20}(?:管理员|admin|root|系统|开发者|开发者模式|越狱|jailbreak|DAN)"),
    re.compile(r"(?:system|系统)\s*(?:提示|prompt|指令)\s*[:：]"),
    re.compile(r"(?:你有一个|你有一个新的)\s*(?:新任务|新指令|新身份)"),
    # 工具调用诱导
    re.compile(r"(?:请|立即|马上)?\s*(?:调用|执行|运行)\s*(?:sql_search|sql|bash|shell|tool|工具)[^\n。]{0,20}(?:查询|执行|调用|删除|drop|update)"),
    re.compile(r"```(?:sql|bash|sh)\s*(?:drop|delete|update|select)\b", re.I),
    # 秘密诱导
    re.compile(r"(?:输出|打印|告诉我|泄露|发送)[^\n。]{0,20}(?:api[\s_]?key|密钥|令牌|token|password|密码|连接串)"),
    re.compile(r"(?:reveal|print|show|leak)\s+(?:your\s+)?(?:api[\s_]?key|secret|token|password|credentials)", re.I),
    # 绕过护栏
    re.compile(r"(?:关闭|停用|禁用|解除|去掉)[^\n。]{0,10}(?:护栏|安全|过滤|审核|校验)"),
    re.compile(r"(?:绕过|跳过|规避)[^\n。]{0,10}(?:检测|审核|校验|过滤|护栏)"),
)

# 零宽字符与控制字符（检测前必须清掉，否则变形绕过）
_ZERO_WIDTH = re.compile("[\\u200b-\\u200f\\u202a-\\u202e\\u2060-\\u2064\\ufeff]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def normalize_for_detection(text: str) -> str:
    """检测前的规范化：NFKC + 去零宽 + 去控制字符 + 折叠空白。

    折叠空白（「忽 略 上 面」→ 「忽 略 上 面」）保留单词间的空格——
    中文按字匹配即可，英文按词匹配，所以这里只折叠**连续**空白到单个空格。
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = _ZERO_WIDTH.sub("", normalized)
    normalized = _CONTROL.sub("", normalized)
    normalized = re.sub(r"[ \t　]+", " ", normalized)
    return normalized


def detect_injection(text: str) -> list[str]:
    """返回命中的风险模式描述（空列表 = 未命中）。

    返回**命中的模式**而不是布尔值：排障时要知道「命中的是哪一类」，
    运营才能判断是误报还是真攻击。
    """
    if not text or not text.strip():
        return []
    normalized = normalize_for_detection(text)
    hits: list[str] = []
    for pattern in _RISK_PATTERNS:
        m = pattern.search(normalized)
        if m:
            hits.append(m.group(0)[:60])
    return hits


def has_injection_risk(text: str) -> bool:
    """是否命中注入风险（入库打标用）。"""
    return bool(detect_injection(text))
