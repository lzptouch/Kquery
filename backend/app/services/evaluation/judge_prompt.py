"""judge 提示词（`EV-03`）：**按 rubric 维度动态渲染**。

## 纪律：维度只在 rubric 文件里定义一次

维度 / 权重 / 评分说明**全部来自 rubric 文件**，代码里**不二次定义**——
否则两份定义必然漂移（改了 rubric 的 criteria，提示词还在按旧的问，
judge 打出来的分与 rubric 对不上，且没人发现）。

## 输出契约

要求返回**纯 JSON**（不要围栏、不要解释文字）：
```json
{"scores": {"<dim_id>": 0.0~1.0, ...}}
```
维度集合必须**等于** rubric 的维度集合——少一个就说明模型没按格式判，
该票作废（不补 0）。
"""
from __future__ import annotations

from app.services.evaluation.rubric import Rubric

#: 输出格式说明（模型必须照此返回）
OUTPUT_CONTRACT = """严格输出一个 JSON 对象，不要输出任何解释文字或代码围栏：
{"scores": {"<维度 id>": <0 到 1 之间的数字>, ...}}
必须**恰好**包含下列全部维度，一个不多一个不少：
{dims}"""


def render_judge_prompt(
    rubric: Rubric,
    question: str,
    answer: str,
    evidence: list[str] | None = None,
    reference: str | None = None,
) -> str:
    """渲染 judge 提示词。

    - `question`：用户问题
    - `answer`：待判定的答案
    - `evidence`：证据块文本（可选；给了才能判「引用忠实性」）
    - `reference`：标准答案（可选；给了才能判「答案正确性」）
    """
    dim_lines = []
    for d in rubric.dims:
        critical = "（关键维度）" if d.critical else ""
        dim_lines.append(
            f"- `{d.id}`（{d.name}，权重 {d.weight}）{critical}：{d.criteria}\n"
            f"  取值区间 {d.scale[0]}~{d.scale[1]}，越高越好。"
        )
    dims_block = "\n".join(dim_lines)

    parts = [
        "你是生成质量的评测员（judge）。请按下列维度给**待判定答案**打分。",
        "",
        "## 评分维度（来自 rubric 文件，勿增删维度）",
        dims_block,
        "",
        "## 用户问题",
        question or "（无）",
        "",
        "## 待判定答案",
        answer or "（空答案）",
    ]
    if evidence:
        parts += ["", "## 证据（判断引用是否有据可依时用）", "\n".join(evidence)]
    if reference:
        parts += ["", "## 标准答案（判断结论是否一致时用）", reference]
    parts += [
        "",
        "## 判分原则",
        "1. 只依据上面给出的材料判定，不要引入外部知识；",
        "2. 证据不足时给低分并说明，不要凭猜测给高分；",
        "3. 分数是 0~1 的连续值，不是整数档位。",
        "",
        "## 输出",
        # 用 replace 而不是 format：契约里有**字面花括号**（JSON 示例），
        # `.format()` 会把它们当占位符 → KeyError: '"scores"'。
        OUTPUT_CONTRACT.replace("{dims}", ", ".join(f"`{d.id}`" for d in rubric.dims)),
    ]
    return "\n".join(parts)
