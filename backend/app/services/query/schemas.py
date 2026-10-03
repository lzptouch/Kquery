"""Query 预处理的数据模型（阶段 4 README §5）。

把"用户原话到检索输入"之间的全部加工过程固化为**可回放的查询分析记录**：
改写与子问题内嵌于分析记录（只能依附某次分析存在，不独立成表）。
记录带策略版本，保证离线回放的结论有意义。
"""
from pydantic import BaseModel, Field

# 意图类型（阶段 4 §3.1）
INTENT_CHITCHAT = "chitchat"
INTENT_FACT_QA = "fact_qa"
INTENT_COMPARE = "compare"
INTENT_SUMMARIZE = "summarize"
INTENT_CALC = "calc"
INTENT_CODE = "code"
INTENT_ROUTING = "routing"

# 需要拆解的意图
DECOMPOSE_INTENTS = {INTENT_COMPARE, INTENT_SUMMARIZE}

# 改写类型
REWRITE_ORIGINAL = "original"
REWRITE_REWRITE = "rewrite"
REWRITE_HYDE = "hyde"
REWRITE_STEPBACK = "stepback"

# 预处理策略版本（换策略后旧记录不可直接回放对比）
PREPROCESS_VERSION = "v1"


class IntentResult(BaseModel):
    intent: str = INTENT_FACT_QA
    needs_retrieval: bool = True
    confidence: float = 0.0
    reasoning: str = ""


class Rewrite(BaseModel):
    type: str  # original / rewrite / hyde / stepback
    text: str
    expander: str  # 产出组件标识
    used: bool = True  # 最终是否被真正用于检索（生成 ≠ 使用）
    skip_reason: str = ""  # 未使用原因（预算截断 / 生成失败等）


class SubQuestion(BaseModel):
    text: str
    expected_source: str = "vector"  # vector / sql / web / graph
    skipped: bool = False
    skip_reason: str = ""


class QueryAnalysis(BaseModel):
    """查询分析记录：一次提问的完整理解结果（可观测、可复盘、可回归）。"""

    analysis_id: str
    raw_query: str  # 用户原话，必须原样保留（纠错体现为改写，不覆盖原话）
    tenant_id: str
    intent: str = INTENT_FACT_QA
    intent_confidence: float = 0.0
    intent_reason: str = ""
    needs_retrieval: bool = True
    rewrites: list[Rewrite] = Field(default_factory=list)
    sub_questions: list[SubQuestion] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    degraded: list[str] = Field(default_factory=list)  # 哪一步降级了：intent / expansion / decompose
    elapsed_ms: int = 0
    llm_calls: int = 0  # 预处理的大模型调用次数（成本项）
    strategy_version: str = PREPROCESS_VERSION
    created_at: str = ""
