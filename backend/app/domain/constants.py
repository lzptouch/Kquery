"""**协议级常量**（跨模块共享的字面量）。

放这里而不是各自定义的原因：这些字符串是**模块之间的约定**，
不是某个模块的实现细节。

- `NO_INFO_ANSWER`：模型表达"检索不到"的固定措辞。
  `pipeline` 用它**检测**拒答（`answer.strip().startswith(NO_INFO_ANSWER)`），
  `MockLLM` 用它**产出**拒答。两边各写一份字面量，一旦漂移，
  mock 的拒答就检测不出来——测试会"莫名其妙地通过"。
- `REFUSAL_TEXT`：对外展示的拒答文案（Agent 与线性路径共用）。

注意：这两个常量**不依赖 services 层**（否则 pipeline ↔ llm 会循环 import）。
"""

#: 模型表达"知识库没有相关内容"的固定措辞（检测与产出必须一致）
NO_INFO_ANSWER = "知识库无相关内容"

#: 对外展示的拒答文案
REFUSAL_TEXT = "知识库中没有足够证据回答该问题"

#: 上游不可用时的**降级文案**（`C-12` 生成降级模板）。
#: 预设文案，不是模板变量填充——上游挂了的时候不该再指望模板引擎。
#: 语义与「拒答」不同：拒答是「知识库里没有」，这里是「服务暂时不可用」。
UPSTREAM_UNAVAILABLE_TEXT = "生成服务暂时不可用，请稍后重试"

#: 上游不可用对应的拒答原因（`C-12`）。
#: 与 `no_content` / `insufficient_evidence` 并列，前端可据此给不同的提示。
REFUSAL_REASON_UPSTREAM_UNAVAILABLE = "upstream_unavailable"
