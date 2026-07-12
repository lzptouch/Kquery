"""智能问答增强服务。

基于GraphRAG的智能问答系统，提供：
- 问题分类与意图理解
- 多策略答案检索
- 增强式答案生成
- 证据追溯和来源引用
- 对话上下文管理

核心能力：
1. 智能问题理解：自动判断问题类型并选择最优检索策略
2. 多源证据整合：融合向量检索、图谱查询、结构化知识
3. 高质量答案生成：基于LLM的连贯、准确、有据可查的回答
4. 追问支持：维护对话上下文，支持多轮交互

使用示例：
    from app.services.graphrag.qa_service import QAService

    qa = QAService()

    # 单轮问答
    result = await qa.ask(
        question="深度学习和机器学习有什么区别？",
        collection_name="ai_kb",
        kb_id=1
    )
    print(result.answer)

    # 多轮对话
    session = await qa.create_session()
    
    response1 = await qa.follow_up(
        session_id=session.session_id,
        question="那它们各自适合什么场景？"
    )
    print(response1.answer)
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from app.core.logger import get_logger
from app.services.graphrag.graphrag_engine import (
    GraphRAGEngine,
    QuestionType,
    HybridSearchResult,
    FusedSearchResult,
    classify_question,
)

logger = get_logger(__name__)


class LLMAnswerGenerator:
    """LLM答案生成器（用于替换模板化答案）。"""
    
    def __init__(self):
        from langchain_openai import ChatOpenAI
        from app.core.llm_client import is_llm_configured
        from app.core.settings import get_settings
        
        settings = get_settings()
        
        self._llm_configured = is_llm_configured("agent")
        self.llm = ChatOpenAI(
            model=settings.llm.agent_model_name,
            temperature=0.3,
            openai_api_base=settings.llm.agent_api_base,
            openai_api_key=settings.llm.agent_api_key or "sk-placeholder",
            request_timeout=120,
        )
        logger.info("LLM答案生成器初始化完成")
    
    async def generate_answer(
        self,
        prompt: str,
        question: str,
        question_type: QuestionType,
    ) -> str:
        """使用LLM生成答案（带 Mock 降级）。"""
        # 如果 LLM 未配置，使用 Mock 数据
        if not self._llm_configured:
            logger.warning("Agent API 未配置，LLM 答案生成使用 Mock 数据")
            from app.core.mock_data import get_mock_data
            mock_result = get_mock_data("qa_answer", question=question)
            return mock_result

        try:
            from app.core.llm_client import call_langchain_chat
            from langchain_core.messages import HumanMessage
            from app.core.settings import get_settings

            settings = get_settings()
            return await call_langchain_chat(
                model=settings.llm.agent_model_name,
                messages=[HumanMessage(content=prompt)],
                api_key=settings.llm.agent_api_key,
                base_url=settings.llm.agent_api_base,
                temperature=0.3,
                timeout=120,
                mock_module="qa_answer",
            )
        except Exception as e:
            logger.error(f"LLM答案生成失败: {e}", exc_info=True)
            raise


# ============================================================
# 数据模型
# ============================================================


@dataclass
class Evidence:
    """证据片段。
    
    Attributes:
        content: 证据内容
        source_type: 来源类型（"vector", "graph", "knowledge_base"）
        source_id: 来源ID（文档ID或实体ID）
        relevance: 相关性分数 (0-1)
        entity_mentioned: 提及的关键实体
        confidence: 可信度 (0-1)
    """
    content: str
    source_type: str = "vector"
    source_id: Optional[str] = None
    relevance: float = 0.8
    entity_mentioned: Optional[str] = None
    confidence: float = 0.8
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "content": self.content[:300],
            "source_type": self.source_type,
            "source_id": self.source_id,
            "relevance": round(self.relevance, 4),
            "entity": self.entity_mentioned,
            "confidence": round(self.confidence, 4),
        }


@dataclass
class Answer:
    """生成的答案。
    
    Attributes:
        text: 答案文本
        question_type: 问题类型
        strategy_used: 使用的策略名称
        evidences: 支持该答案的证据列表
        sources: 溯源信息列表（包含文档位置）
        entities_involved: 涉及的实体列表
        graph_context_used: 是否使用了图谱上下文
        confidence_score: 整体置信度 (0-1)
        generation_time_sec: 生成耗时（秒）
        follow_up_suggestions: 推荐的追问问题
    """
    text: str
    question_type: QuestionType = QuestionType.UNKNOWN
    strategy_used: str = ""
    evidences: List[Evidence] = field(default_factory=list)
    sources: List[Dict[str, Any]] = field(default_factory=list)
    entities_involved: List[str] = field(default_factory=list)
    graph_context_used: bool = False
    confidence_score: float = 0.7
    generation_time_sec: float = 0.0
    follow_up_suggestions: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "question_type": self.question_type.value,
            "strategy": self.strategy_used,
            "evidence_count": len(self.evidences),
            "evidences": [e.to_dict() for e in self.evidences[:5]],
            "sources": self.sources[:5],
            "entities": self.entities_involved[:10],
            "graph_enhanced": self.graph_context_used,
            "confidence": round(self.confidence_score, 4),
            "generation_time_ms": round(self.generation_time_sec * 1000, 2),
            "suggested_followups": self.follow_up_suggestions[:3],
        }


@dataclass 
class ConversationTurn:
    """一轮对话。
    
    Attributes:
        turn_id: 轮次ID
        question: 用户问题
        answer: 生成的答案
        timestamp: 时间戳
        search_result: 检索结果（用于调试）
    """
    turn_id: str
    question: str
    answer: Optional[Answer] = None
    timestamp: datetime = field(default_factory=datetime.now)
    search_result: Optional[HybridSearchResult] = None


@dataclass
class ConversationSession:
    """会话。
    
    Attributes:
        session_id: 会话ID
        turns: 对话轮次列表
        created_at: 创建时间
        last_active: 最后活跃时间
        context_summary: 上下文摘要
        metadata: 元数据
    """
    session_id: str
    turns: List[ConversationTurn] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    last_active: datetime = field(default_factory=datetime.now)
    context_summary: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def turn_count(self) -> int:
        return len(self.turns)


# ============================================================
# 答案策略管理器
# ============================================================


class AnswerStrategyManager:
    """根据问题类型选择最优的答案生成策略。
    
    不同类型的问题需要不同的处理方式：
    - 事实型：直接回答 + 实体属性展示
    - 关系型：关系路径 + 解释说明
    - 推理型：子图分析 + 逐步推理过程
    - 列举型：结构化列表 + 分类汇总
    - 比较型：对比表格 + 异同点总结
    """
    
    STRATEGY_PROMPTS = {
        QuestionType.FACTOID: """你是一个精确的事实回答助手。

任务：基于提供的上下文，直接、准确地回答用户的问题。

要求：
1. 直接给出答案，不要过多铺垫
2. 如果有具体的数据、时间、数字，必须明确指出
3. 如果信息不足，明确说明缺少什么
4. 引用来源时使用 [来源N] 标记

格式：
【答案】
{你的答案}

【来源】
- [来源1] ...
""",

        QuestionType.RELATION: """你是一个擅长分析实体关系的助手。

任务：解释两个或多个实体之间的关系。

要求：
1. 明确指出实体间的具体关系
2. 说明关系的性质（层级/关联/因果等）
3. 如有时间线，按时间顺序描述
4. 引用图谱中的路径作为证据

格式：
【关系概述】
{关系的总体描述}

【具体关系】
1. {关系1} - {说明}
2. {关系2} - {说明}

【证据】
{来自知识图谱的证据}
""",

        QuestionType.REASONING: """你是一个逻辑推理能力强的助手。

任务：基于提供的信息进行推理，回答"为什么"或"如何"类问题。

要求：
1. 给出清晰的推理链条（步骤1→步骤2→步骤3）
2. 区分事实和推断
3. 考虑多种可能性
4. 明确标注推理依据

格式：
【结论】
{最终结论}

【推理过程】
步骤1：{...} → 依据：[来源N]
步骤2：{...} → 依据：[来源N]
步骤3：{...} → 依据：[来源N]

【支撑事实】
- 事实1：...
- 事实2：...
""",

        QuestionType.LISTING: """你是一个善于整理信息的助手。

任务：列出所有符合条件的项目。

要求：
1. 使用结构化列表（编号）
2. 按类别或重要性排序
3. 每项附带简要说明
4. 注明总数和可能的遗漏

格式：
【完整列表】（共N项）

**{分类1}**
1. {项目A} - {简要说明}
2. {项目B} - {简要说明}

**{分类2}**
3. ...

【备注】
{补充说明}
""",

        QuestionType.COMPARISON: """你是一个善于对比分析的助手。

任务：对多个对象进行全面比较。

要求：
1. 从多个维度进行比较
2. 使用表格或结构化对比
3. 明确指出异同点
4. 给出客观的评价

格式：
【对比总览】

| 维度 | 对象A | 对象B | 相似度 |
|------|-------|-------|--------|
| {维度1} | ... | ... | 高/中/低 |

【详细差异】
1. {差异1}：A是..., B是...
2. {差异2}：...

【共同点】
- 共同点1：...
- 共同点2：...

【总结建议】
{基于比较的建议}
""",

        QuestionType.DEFINITION: """你是一个清晰的概念解释者。

任务：解释某个概念或术语的定义。

要求：
1. 给出简洁明确的定义
2. 用通俗的语言解释
3. 提供实例帮助理解
4. 说明相关概念的区别

格式：
【定义】
{标准定义}

【通俗解释】
{用简单语言重新表述}

【关键特征】
1. 特征1：...
2. 特征2：...

【示例】
{实际应用示例}

【易混淆概念】
与{其他概念}的区别：...
""",

        QuestionType.PROCEDURAL: """你是一个步骤清晰的指导者。

任务：详细说明如何完成某项操作或流程。

要求：
1. 分步骤说明（带序号）
2. 每步包含必要的细节
3. 标注注意事项和常见错误
4. 提供验证方法

格式：
【操作指南】

**前置条件**
- 条件1：...
- 条件2：...

**详细步骤**

步骤1：{标题}
- 操作：...
- 注意事项：...

步骤2：{标题}
...

**验证方法**
如何确认成功：...

**常见问题**
Q: {可能的问题}
A: {解决方案}
""",
    }
    
    def __init__(self):
        logger.info("答案策略管理器初始化完成")
    
    def get_strategy_prompt(
        self,
        question_type: QuestionType,
        custom_prompt: Optional[str] = None,
    ) -> str:
        """获取对应问题类型的Prompt模板。"""
        
        if custom_prompt:
            return custom_prompt
        
        return self.STRATEGY_PROMPTS.get(
            question_type,
            self.STRATEGY_PROMPTS[QuestionType.FACTOID]
        )
    
    def generate_followup_suggestions(
        self,
        question: str,
        question_type: QuestionType,
        entities: List[str],
    ) -> List[str]:
        """基于当前问题和实体生成推荐的追问问题。"""
        suggestions = []
        
        if not entities:
            return [
                f"能详细说明一下吗？",
                f"有哪些具体的例子？",
                f"在实际应用中如何实现？",
            ]
        
        main_entity = entities[0]
        
        type_specific = {
            QuestionType.FACTOID: [
                f"{main_entity}的发展历程是什么？",
                f"{main_entity}的主要特点有哪些？",
                f"{main_entity}与其他同类相比如何？",
            ],
            QuestionType.RELATION: [
                f"除了上述关系，{main_entity}还有哪些关联？",
                f"这种关系的强度如何量化？",
                f"未来这种关系会如何发展？",
            ],
            QuestionType.REASONING: [
                f"有哪些因素会影响这个结论？",
                f"有没有反例或例外情况？",
                f"从另一个角度看这个问题呢？",
            ],
            QuestionType.LISTING: [
                f"这些项目中哪个最重要？为什么？",
                f"有没有遗漏的重要项？",
                f"如何对这些项目进行评估？",
            ],
            QuestionType.COMPARISON: [
                f"在性能方面谁更优？",
                f"成本差异有多大？",
                f"哪个更适合初学者？",
            ],
            QuestionType.DEFINITION: [
                f"能举个具体的例子吗？",
                f"{main_entity}的应用场景有哪些？",
                f"学习{main_entity}需要哪些基础知识？",
            ],
            QuestionType.PROCEDURAL: [
                f"执行过程中可能遇到什么困难？",
                f"有没有更简便的方法？",
                f"如何验证每一步都正确执行了？",
            ],
        }
        
        suggestions = type_specific.get(question_type, suggestions[:3])
        return suggestions[:3]


# ============================================================
# 主服务类
# ============================================================


class QAService:
    """智能问答服务主入口。
    
    集成了完整的问答流程：
    1. 接收用户问题
    2. 问题理解和分类
    3. 执行GraphRAG混合检索
    4. 选择最优答案策略
    5. 生成高质量答案（带证据引用）
    6. 管理多轮对话会话
    
    设计为高级封装层，内部调用GraphRAGEngine，
    并增加了对话管理、答案优化等功能。
    
    使用示例：
        qa = QAService()
        
        # 快速问答
        answer = await qa.ask(
            question="什么是知识图谱？",
            collection_name="tech_docs"
        )
        print(answer.text)
        print(f"置信度: {answer.confidence_score}")
        print(f"证据数: {len(answer.evidences)}")
        
        # 带会话管理的多轮对话
        session = await qa.create_session(kb_id=1)
        
        ans1 = await qa.follow_up(session_id=session.session_id, question="它有什么优势？")
        ans2 = await qa.follow_up(session_id=session.session_id, question="如何构建？")
    """
    
    # 会话存储（生产环境应使用Redis或数据库）
    _sessions: Dict[str, ConversationSession] = {}
    SESSION_TIMEOUT_MINUTES = 30
    
    def __init__(
        self,
        default_collection_name: str = "",
        default_kb_id: Optional[int] = None,
        enable_followup: bool = True,
        max_evidence_count: int = 10,
    ):
        """
        Args:
            default_collection_name: 默认的Milvus集合名
            default_kb_id: 默认的知识库ID
            enable_followup: 是否启用追问建议
            max_evidence_count: 最大证据数量
        """
        self.default_collection_name = default_collection_name
        self.default_kb_id = default_kb_id
        self.enable_followup = enable_followup
        self.max_evidence_count = max_evidence_count
        
        # 核心组件
        self._engine: Optional[GraphRAGEngine] = None
        self._llm_generator: Optional[LLMAnswerGenerator] = None
        self.strategy_manager = AnswerStrategyManager()
        
        logger.info("智能问答服务初始化完成")
    
    @property
    def engine(self) -> GraphRAGEngine:
        """获取GraphRAG引擎实例（延迟初始化）。"""
        if self._engine is None:
            self._engine = GraphRAGEngine()
        return self._engine
    
    @property
    def llm_generator(self) -> LLMAnswerGenerator:
        """获取LLM答案生成器实例（延迟初始化）。"""
        if self._llm_generator is None:
            self._llm_generator = LLMAnswerGenerator()
        return self._llm_generator
    
    async def ask(
        self,
        question: str,
        collection_name: Optional[str] = None,
        kb_id: Optional[int] = None,
        conversation_history: Optional[List[Dict]] = None,
        system_prompt: Optional[str] = None,
        top_k: int = 8,
    ) -> Answer:
        """执行单次问答。
        
        这是QAService的核心方法，端到端完成整个问答流程。
        
        Args:
            question: 用户问题
            collection_name: Milvus集合名称（可使用默认值）
            kb_id: 知识库ID
            conversation_history: 对话历史 [{"role": "user", "content": "..."}]
            system_prompt: 自定义系统提示词
            top_k: 检索结果数量
            
        Returns:
            Answer: 包含答案文本、证据、置信度的完整答案对象
            
        Example:
            qa = QAService()
            
            answer = await qa.ask(
                question="张三在腾讯公司负责什么工作？",
                collection_name="company_kb",
                kb_id=1
            )
            
            print(answer.text)           # 答案文本
            print(answer.confidence_score) # 置信度
            print(len(answer.evidences))  # 证据数量
        """
        import time
        start_time = time.time()
        
        try:
            collection = collection_name or self.default_collection_name
            kb = kb_id or self.default_kb_id
            
            if not collection:
                raise ValueError("未指定collection_name且无默认值")
            
            logger.info(f"🤔 开始问答: '{question}'")
            
            # Step 1: 问题分类
            q_type = classify_question(question)
            logger.info(f"  ✓ 问题类型: {q_type.value}")
            
            # Step 2: 执行GraphRAG混合检索
            search_result = await self.engine.hybrid_search(
                query=question,
                collection_name=collection,
                kb_id=kb,
                top_k=top_k,
            )
            
            # Step 3: 构建证据列表
            evidences = self._build_evidence_list(search_result.fused_results)
            
            # Step 4: 获取策略Prompt
            strategy_prompt = self.strategy_manager.get_strategy_prompt(
                q_type, system_prompt
            )
            
            # Step 5: 构建完整Prompt
            full_prompt = self._build_full_prompt(
                question=question,
                search_result=search_result,
                strategy_prompt=strategy_prompt,
                conversation_history=conversation_history,
            )
            
            # Step 6: 使用LLM生成答案
            try:
                answer_text = await self.llm_generator.generate_answer(
                    prompt=full_prompt,
                    question=question,
                    question_type=q_type,
                )
            except Exception as e:
                logger.warning(f"LLM答案生成失败，使用模板回退: {e}")
                answer_text = self._generate_answer_from_template(
                    q_type=q_type,
                    search_result=search_result,
                )
            
            # Step 7: 生成追问建议
            followups = []
            if self.enable_followup:
                entity_names = [e.name for e in search_result.extracted_entities]
                followups = self.strategy_manager.generate_followup_suggestions(
                    question, q_type, entity_names
                )
            
            # 组装溯源信息
            sources = []
            for r in search_result.fused_results[:5]:
                if r.metadata:
                    sources.append({
                        "file_name": r.metadata.get("file_name", ""),
                        "page_number": r.metadata.get("page_number"),
                        "char_offset": r.metadata.get("char_offset", 0),
                        "content": r.content[:200],
                        "score": r.final_score,
                        "source": r.source,
                    })
            
            # 组装答案对象
            elapsed = time.time() - start_time
            
            answer = Answer(
                text=answer_text,
                question_type=q_type,
                strategy_used=f"graphrag_{q_type.value}",
                evidences=evidences[:self.max_evidence_count],
                sources=sources,
                entities_involved=[e.name for e in search_result.extracted_entities],
                graph_context_used=(
                    search_result.graph_context.has_context 
                    if search_result.graph_context else False
                ),
                confidence_score=self._calculate_confidence(search_result, evidences),
                generation_time_sec=elapsed,
                follow_up_suggestions=followups,
            )
            
            logger.info(
                f"✅ 问答完成! 置信度={answer.confidence_score:.2%}, "
                f"证据={len(evidences)}, 耗时={elapsed:.3f}s"
            )
            
            return answer
        
        except Exception as e:
            logger.error(f"❌ 问答失败: {e}", exc_info=True)
            
            # 返回错误答案
            return Answer(
                text=f"抱歉，无法为您解答这个问题。错误信息：{str(e)}",
                question_type=QuestionType.UNKNOWN,
                strategy_used="error_fallback",
                confidence_score=0.0,
                generation_time_sec=time.time() - start_time,
            )
    
    async def create_session(
        self,
        kb_id: Optional[int] = None,
        metadata: Optional[Dict] = None,
    ) -> ConversationSession:
        """创建新的对话会话。
        
        Args:
            kb_id: 知识库ID
            metadata: 会话元数据
            
        Returns:
            ConversationSession: 新创建的会话对象
        """
        session_id = str(uuid.uuid4())
        
        session = ConversationSession(
            session_id=session_id,
            metadata={
                **(metadata or {}),
                "kb_id": kb_id or self.default_kb_id,
            },
        )
        
        # 清理过期会话
        self._cleanup_expired_sessions()
        
        # 存储新会话
        self._sessions[session_id] = session
        
        logger.info(f"💬 创建新会话: {session_id[:8]}...")
        
        return session
    
    async def follow_up(
        self,
        session_id: str,
        question: str,
        collection_name: Optional[str] = None,
        kb_id: Optional[int] = None,
    ) -> Answer:
        """在已有会话中进行追问。
        
        Args:
            session_id: 会话ID
            question: 追问内容
            collection_name: Milvus集合名
            kb_id: 知识库ID
            
        Returns:
            Answer: 答案对象
            
        Raises:
            ValueError: 会话不存在或已过期
        """
        # 检查会话是否存在
        if session_id not in self._sessions:
            raise ValueError(f"会话不存在: {session_id}")
        
        session = self._sessions[session_id]
        
        # 检查是否过期
        if datetime.now() > session.last_active + timedelta(minutes=self.SESSION_TIMEOUT_MINUTES):
            del self._sessions[session_id]
            raise ValueError(f"会话已过期: {session_id}")
        
        # 更新最后活跃时间
        session.last_active = datetime.now()
        
        # 构建对话历史
        history = []
        for turn in session.turns[-5:]:  # 最近5轮作为上下文
            if turn.question:
                history.append({"role": "user", "content": turn.question})
            if turn.answer:
                history.append({"role": "assistant", "content": turn.answer.text})
        
        # 执行问答
        answer = await self.ask(
            question=question,
            collection_name=collection_name,
            kb_id=kb_id or session.metadata.get("kb_id"),
            conversation_history=history,
        )
        
        # 记录本轮对话
        turn = ConversationTurn(
            turn_id=str(uuid.uuid4())[:8],
            question=question,
            answer=answer,
        )
        session.turns.append(turn)
        
        logger.info(
            f"💬 会话 {session_id[:8]} 第 {len(session.turns)} 轮追问完成"
        )
        
        return answer
    
    async def get_session_history(
        self,
        session_id: str,
    ) -> ConversationSession:
        """获取会话历史记录。"""
        if session_id not in self._sessions:
            raise ValueError(f"会话不存在: {session_id}")
        
        return self._sessions[session_id]
    
    async def delete_session(self, session_id: str) -> bool:
        """删除会话。"""
        if session_id in self._sessions:
            del self._sessions[session_id]
            logger.info(f"🗑️ 会话已删除: {session_id[:8]}")
            return True
        return False
    
    # ============================================================
    # 内部方法
    # ============================================================
    
    def _build_evidence_list(
        self,
        fused_results: List[FusedSearchResult],
    ) -> List[Evidence]:
        """从融合结果中构建证据列表。"""
        evidences = []
        
        for i, result in enumerate(fused_results[:self.max_evidence_count]):
            evidence = Evidence(
                content=result.content,
                source_type=result.source,
                relevance=result.final_score,
                entity_mentioned=", ".join(result.entities_mentioned) if result.entities_mentioned else None,
                confidence=result.final_score,  # 使用融合分数作为可信度
            )
            
            # 尝试从metadata提取source_id
            if result.metadata and "chunk_id" in result.metadata:
                evidence.source_id = result.metadata["chunk_id"]
            elif result.metadata and "doc_id" in result.metadata:
                evidence.source_id = result.metadata["doc_id"]
            
            evidences.append(evidence)
        
        return evidences
    
    def _build_full_prompt(
        self,
        question: str,
        search_result: HybridSearchResult,
        strategy_prompt: str,
        conversation_history: Optional[List[Dict]],
    ) -> str:
        """构建用于LLM的完整Prompt。"""
        sections = [strategy_prompt]
        
        # 添加图谱上下文
        if search_result.graph_context and search_result.graph_context.has_context:
            sections.append("\n【知识图谱上下文】\n" + search_result.graph_context.subgraph_text)
        
        # 添加检索结果
        sections.append("\n【相关文档片段】\n")
        for i, r in enumerate(search_result.fused_results[:8], 1):
            sections.append(f"\n--- 片段{i} (相关性: {r.final_score:.1%}) ---\n")
            sections.append(r.content[:400])
            if r.evidence:
                sections.append(f"\n[图谱证据: {'; '.join(r.evidence)}]")
        
        # 添加对话历史
        if conversation_history:
            sections.append("\n\n【对话历史】\n")
            for msg in conversation_history[-6:]:
                role_label = "用户" if msg["role"] == "user" else "助手"
                sections.append(f"{role_label}: {msg['content']}\n")
        
        # 添加用户问题
        sections.append(f"\n\n---\n\n用户问题：{question}\n\n请根据以上信息回答：")
        
        return "\n".join(sections)
    
    def _generate_answer_from_template(
        self,
        q_type: QuestionType,
        search_result: HybridSearchResult,
    ) -> str:
        """生成模板化答案（回退方案，无LLM时使用）。"""
        parts = []
        
        # 根据问题类型选择不同的组织方式
        if q_type == QuestionType.FACTOID or q_type == QuestionType.DEFINITION:
            parts.append(f"**关于「{search_result.query}」的回答：**\n\n")
            
            # 主要内容
            if search_result.fused_results:
                best = search_result.fused_results[0]
                parts.append(best.content[:500])
                
                if len(search_result.fused_results) > 1:
                    parts.append("\n\n**补充信息：**\n")
                    for r in search_result.fused_results[1:3]:
                        parts.append(f"- {r.content[:200]}")
        
        elif q_type == QuestionType.RELATION:
            parts.append(f"**「{search_result.query}」的关系分析：**\n\n")
            
            # 显示路径信息（如果有）
            if search_result.graph_context and search_result.graph_context.paths_to_other_entities:
                parts.append("**实体间的关系路径：**\n")
                for path in search_result.graph_context.paths_to_other_entities[:3]:
                    parts.append(f"- 《{path['source']}》→《{path['target']}》 (距离: {path['path_length']})")
                    
                    if path.get("nodes"):
                        nodes_str = " → ".join([n.get("name", "?") for n in path["nodes"]])
                        parts.append(f"  具体路径: {nodes_str}")
                
                parts.append("")
            
            # 文档证据
            if search_result.fused_results:
                parts.append("**文档证据：**\n")
                for r in search_result.fused_results[:3]:
                    parts.append(f"- {r.content[:250]}")
        
        elif q_type == QuestionType.COMPARISON:
            parts.append(f"**「{search_result.query}」的对比分析：**\n\n")
            
            # 简单的表格形式
            if len(search_result.extracted_entities) >= 2:
                e1, e2 = search_result.extracted_entities[0].name, search_result.extracted_entities[1].name
                parts.append(f"| 维度 | {e1} | {e2} |\n|------|-----|-----|")
                
                # 为每个结果添加一行
                for r in search_result.fused_results[:5]:
                    preview = r.content[:80].replace("|", "\\|")
                    parts.append(f"| 信息 | {preview} | {preview} |")
        
        else:
            # 通用格式（适用于REASONING/LISTING/PROCEDURAL等）
            type_labels = {
                QuestionType.REASONING: "推理分析",
                QuestionType.LISTING: "相关信息列举",
                QuestionType.PROCEDURAL: "操作指南",
                QuestionType.UNKNOWN: "回答",
            }
            label = type_labels.get(q_type, "回答")
            
            parts.append(f"**「{search_result.query}」{label}：**\n\n")
            
            if search_result.fused_results:
                for i, r in enumerate(search_result.fused_results[:5], 1):
                    parts.append(f"{i}. {r.content[:300]}\n")
        
        # 添加实体信息（如果有的话）
        if search_result.extracted_entities:
            parts.append("\n---\n**相关知识实体：** ")
            entity_names = [f"**{e.name}**" for e in search_result.extracted_entities[:5]]
            parts.append(", ".join(entity_names))
        
        # 图谱增强提示
        if search_result.graph_context and search_result.graph_context.has_context:
            neighbor_count = len(search_result.graph_context.neighbors)
            parts.append(f"\n*本次回答使用了知识图谱增强（关联了 {neighbor_count} 个知识节点）*")
        
        # 来源标注
        parts.append("\n\n*由KQuery Pro GraphRAG智能问答引擎生成*")
        
        return "\n".join(parts)
    
    def _calculate_confidence(
        self,
        search_result: HybridSearchResult,
        evidences: List[Evidence],
    ) -> float:
        """计算整体答案置信度。"""
        if not search_result.fused_results and not evidences:
            return 0.1
        
        factors = []
        
        # 因素1: 最佳结果的融合分数
        if search_result.fused_results:
            best_score = search_result.fused_results[0].final_score
            factors.append(best_score)
        
        # 因素2: 结果数量充足性（越多越好，但有上限）
        result_count = len(search_result.fused_results)
        count_factor = min(result_count / 5.0, 1.0)  # 5个结果即满分
        factors.append(count_factor)
        
        # 因素3: 图谱上下文可用性
        if search_result.graph_context and search_result.graph_context.has_context:
            factors.append(0.9)  # 有图谱加分
        else:
            factors.append(0.7)  # 无图谱也还行
        
        # 因素4: 实体识别成功率
        if search_result.extracted_entities:
            matched_ratio = sum(
                1 for e in search_result.extracted_entities 
                if e.matched_db_id
            ) / len(search_result.extracted_entities)
            factors.append(0.6 + 0.4 * matched_ratio)
        
        # 综合评分（加权平均）
        weights = [0.35, 0.15, 0.25, 0.25]
        confidence = sum(f * w for f, w in zip(factors, weights))
        
        return round(min(max(confidence, 0.0), 1.0), 4)
    
    def _cleanup_expired_sessions(self) -> int:
        """清理过期会话，返回清理数量。"""
        now = datetime.now()
        expired = [
            sid for sid, session in self._sessions.items()
            if now > session.last_active + timedelta(minutes=self.SESSION_TIMEOUT_MINUTES)
        ]
        
        for sid in expired:
            del self._sessions[sid]
        
        if expired:
            logger.debug(f"清理了 {len(expired)} 个过期会话")
        
        return len(expired)
