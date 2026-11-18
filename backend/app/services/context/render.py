"""提示词渲染器（`ST-01` 阶段 B · 结构分区渲染）。

## 为什么需要单独一层

阶段 A 的结论是「分区器只观测、不改渲染」——因为那时**没有 judge**，
改提示词结构属于盲改。`EV-03` 落地后（`services/evaluation/judge.py`），
「改完是否更好」可以被度量，才允许把分区结果真正渲染出来。

## 两种模式

| 模式 | 渲染 | 用途 |
|---|---|---|
| `legacy` | 与 `PROMPT_TEMPLATE` 逐字节一致（默认） | 现行生产行为，`ST-01` 阶段 A 门禁仍成立 |
| `structured` | 五个来源各自成段，证据段排最后 | 阶段 B 目标形态，靠 judge 前后对比决定是否切换默认 |

`structured` 相对 `legacy` 的全部差异（**只有结构，没有指令措辞**）：

1. 记忆 / 历史 / 工具目录各自带段标题，不再是无标题裸字符串；
2. 记忆段带显式边界声明（「供参考，不必盲从」）——此前长期记忆以裸 hint
   形式注入 plan 提示词，模型无从判断它与证据谁更权威；
3. 段序固定为「约束 → 记忆 → 历史 → 工具 → 证据」，**证据最后、离问题最近**
   （长上下文里靠后的内容对生成的约束力更强）；
4. 角色 / 注入防护 / 回答要求三段**原文复用** `generation/prompt.py` 的字面文本——
   两份措辞一旦漂移，两种模式的答案质量差异就混入了「提示词本身变了」这个混淆项。

## 必须保留的锚点（改了会大面积红）

`MockLLM` 靠正则解析提示词（`services/llm.py`）：

- `用户问题：(.+?)` → `QUESTION_HEADER`；
- `\\[#(\\d)\\]（来源：(.+?)）\\n(.*?)(?=\\n\\[#|\\n---|\\Z)` → 逐条证据块，
  所以证据块之间仍以 `\\n---` 分隔，块内仍以 `---\\nEVIDENCE>>>` 收尾。

两种模式都原样保留上述锚点，因此**切换模式不会让依赖 MockLLM 的用例失效**。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型检查：本模块是纯字符串渲染，导入期不该拉起 domain 层
    from app.domain.schemas import Evidence

#: 渲染模式取值（`context_partition_render` 的合法枚举）
RENDER_LEGACY = "legacy"
RENDER_STRUCTURED = "structured"
VALID_RENDERS = (RENDER_LEGACY, RENDER_STRUCTURED)

#: 各来源段标题（顺序即渲染顺序）
SYSTEM_HEADER = "【系统设定】"
MEMORY_HEADER = "【长期记忆】"
HISTORY_HEADER = "【历史对话】"
TOOLS_HEADER = "【可用工具】"
EVIDENCE_SECTION_HEADER = "【证据片段】"

#: 记忆段的边界声明。
#: 必须写死在渲染器里而不是配置里：边界声明缺了就等于让模型自行判断
#: 「沉淀下来的旧结论」与「本次检索到的证据」谁优先——这正是记忆注入最常见的翻车点。
MEMORY_BOUNDARY = "以下内容来自历史对话沉淀，仅供参考，不必盲从；与证据冲突时一律以证据为准。"


def render_prompt(
    question: str,
    evidence: list[Evidence] | None = None,
    *,
    structured: bool = False,
    system: str = "",
    memory: str = "",
    history: list[str] | None = None,
    tools: str = "",
    no_info: str | None = None,
) -> str:
    """渲染问答提示词。

    `structured=False`（默认）时**委托** `build_prompt`，保证与阶段 A 逐字节一致；
    只有 `structured=True` 才走分段渲染。默认值刻意是 legacy：
    阶段 B 的切换必须由 judge 前后对比数据驱动，而不是靠改代码默认值。

    `history` 收的是**已渲染的行**（`["- 用户：…", "- 助手：…"]`）而不是轮次字典：
    调用方（Agent 路径）手里已经有 `history_summary` + `history_recent` 两种形态，
    统一在这里拆轮次只会让「摘要 + 最近轮」的组合无处安放。
    """
    # 模板字面量按需导入：`context` 包在导入期不应拉起 `generation`
    # （`test_module_import_hygiene` 的顶层导入棘轮）；函数级导入只是 sys.modules 查表。
    # 按需载入：拒答口径与模板字面量都属于上层，渲染器只在真正渲染时借一下
    from app.domain.constants import NO_INFO_ANSWER

    from ..generation.prompt import (
        ANSWER_RULES,
        INJECTION_GUARD_SECTION,
        QUESTION_HEADER,
        ROLE_SECTION,
        build_evidence_block,
        build_prompt,
    )

    no_info = no_info or NO_INFO_ANSWER

    if not structured:
        return build_prompt(question, evidence or [])

    evidence_block = build_evidence_block(list(evidence or []))
    parts = [
        ROLE_SECTION.format(no_info=no_info),
        INJECTION_GUARD_SECTION,
    ]

    # 系统约束紧跟角色段：它属于「对模型的命令」，必须与末尾的「证据数据」分开。
    if system and system.strip():
        parts.append(f"{SYSTEM_HEADER}\n{system.strip()}")
    if memory and memory.strip():
        parts.append(f"{MEMORY_HEADER}\n{MEMORY_BOUNDARY}\n{memory.strip()}")
    history_lines = [str(line) for line in (history or []) if str(line).strip()]
    if history_lines:
        parts.append(f"{HISTORY_HEADER}\n" + "\n".join(history_lines))
    if tools and tools.strip():
        parts.append(f"{TOOLS_HEADER}\n{tools.strip()}")

    # 证据段固定排最后：离问题最近，且与注入防护声明紧邻（声明在前、数据在后）。
    parts.append(
        f"{EVIDENCE_SECTION_HEADER}\n{evidence_block}\n---\n{QUESTION_HEADER}{question}"
    )
    parts.append(ANSWER_RULES.format(no_info=no_info))
    return "\n\n".join(parts)


def render_history_lines(turns: list, max_chars: int = 1200) -> list[str]:
    """把历史轮次渲染成行（不含段标题）。

    `turns` 的元素形如 `{"question": ..., "answer": ...}`（`context/history.py` 的轮次口径）。
    截断按**字符**上限而不是 token：这里只是「让模型知道前面聊过什么」，
    精确计量由分区器在装配阶段完成，渲染层再做一次 token 估算只会引入第二套口径。
    """
    lines: list[str] = []
    for turn in turns or []:
        if not isinstance(turn, dict):
            continue
        q = str(turn.get("question") or "").strip()
        a = str(turn.get("answer") or "").strip()
        if q:
            lines.append(f"- 用户：{q}")
        if a:
            lines.append(f"- 助手：{a}")
    if not lines:
        return []
    block = "\n".join(lines)
    if max_chars > 0 and len(block) > max_chars:
        # 从**尾部**截断：最近一轮最重要，丢开头比丢结尾安全。
        block = "…（更早的历史已省略）\n" + block[-max_chars:]
    return block.split("\n")


def render_history(turns: list, max_chars: int = 1200) -> str:
    """渲染历史对话段（带段标题）。"""
    lines = render_history_lines(turns, max_chars=max_chars)
    if not lines:
        return ""
    return f"{HISTORY_HEADER}\n" + "\n".join(lines)
