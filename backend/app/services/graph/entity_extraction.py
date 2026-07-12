"""
实体抽取服务 (Entity Extraction Service)

从非结构化文本中自动识别和抽取命名实体（Named Entity Recognition, NER）。
这是知识图谱构建的第一步，决定了图谱的质量和完整性。

核心功能：
1. LLM驱动的NER - 使用大语言模型进行智能实体识别
2. 规则引擎 - 基于正则的结构化实体提取（高精度、零延迟）
3. 实体消歧 - 解决同名实体的歧义问题
4. 实体链接 - 将新实体链接到已有知识库中的实体
5. 批量处理 - 支持长文档的高效分块处理

架构设计：
    文本输入
        ↓
    ┌─────────────┐
    │  LLM NER     │ ← 主力：通用性强，能理解上下文
    │  (深度模型)   │   准确率 ~85%
    └──────┬──────┘
           ↓ 合并
    ┌─────────────┐
    │  规则引擎     │ ← 辅助：特定类型精度极高
    │  (正则/词典)  │   时间/数值/邮箱等准确率 ~99%
    └──────┬──────┘
           ↓ 去重合并
    ┌─────────────┐
    │  实体消歧器   │ ← 后处理：解决"苹果"(水果 vs 公司)
    │  (消歧算法)   │
    └──────┬──────┘
           ↓
    实体列表输出

作者：KQuery Pro Team
版本：1.0.0
更新：2026-04-11

依赖：
- openai 或 dashscope（LLM调用）
- app.repositories.graph.schema（数据模型）
"""

from __future__ import annotations

import re
import json
import logging
import asyncio
from typing import (
    Any, Dict, List, Optional, Set,
    Tuple, Callable
)
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

# 导入项目模块
try:
    from app.repositories.graph.schema import (
        Entity, EntityType, graph_schema
    )
    from app.core.logger import get_logger
    from app.core.settings import get_settings
except ImportError:
    # 开发环境降级处理
    class Entity:
        pass
    class EntityType:
        pass
    
    def get_logger(name):
        return logging.getLogger(name)
    
    def get_settings():
        return None

logger = get_logger(__name__)


# ============================================================
# 第一部分：规则定义（Rule Definitions）
# ============================================================


@dataclass  
class ExtractionRule:
    """抽取规则"""
    name: str                           # 规则名称
    pattern: str                        # 正则模式
    entity_type: EntityType             # 匹配的实体类型
    confidence: float = 0.95            # 规则匹配的置信度（通常很高）
    priority: int = 1                   # 优先级（数字越小优先级越高）


# 预置规则集
BUILTIN_RULES: List[ExtractionRule] = [
    # ===== 时间日期规则 =====
    ExtractionRule(
        name="date_full",
        pattern=r'\b(\d{4})[年\-/\.](\d{1,2})[月\-/\.](\d{1,2})[日号]?\b',
        entity_type=EntityType.DATE,
        confidence=0.99,
        priority=0
    ),
    ExtractionRule(
        name="date_year_month",
        pattern=r'\b(\d{4})[年\-/](\d{1,2})月?\b',
        entity_type=EntityType.DATE,
        confidence=0.98,
        priority=1
    ),
    ExtractionRule(
        name="date_relative",
        pattern=r'((?:去年|今年|明年|前年|后年)\s*(?:上半年|下半年|春季|夏季|秋季|冬季|一二三四季度))',
        entity_type=EntityType.DATE,
        confidence=0.90,
        priority=2
    ),
    
    # ===== 数值金额规则 =====
    ExtractionRule(
        name="money_rmb",
        pattern=r'(?:￥|人民币|约?\s*)([\d,]+(?:\.\d+)?)(?:万|亿)?(?:元|块|圆)',
        entity_type=EntityType.MONEY,
        confidence=0.97,
        priority=0
    ),
    ExtractionRule(
        name="money_usd",
        pattern=r'(?:\$|USD|美元|美金)\s*([\d,]+(?:\.\d+)?)',
        entity_type=EntityType.MONEY,
        confidence=0.97,
        priority=0
    ),
    ExtractionRule(
        name="percent",
        pattern=r'([\d,]+(?:\.\d+)?)\s*(?:%|百分之|%%)',
        entity_type=EntityType.PERCENT,
        confidence=0.99,
        priority=0
    ),
    ExtractionRule(
        name="number_large",
        pattern=r'\b(\d+(?:,\d{3})*(?:\.\d+)?)\b(?!\s*(?:元|%|年|月|日|日|时|分))',
        entity_type=EntityType.NUMBER,
        confidence=0.90,
        priority=3
    ),
    
    # ===== 联系方式规则 =====
    ExtractionRule(
        name="email",
        pattern=r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}',
        entity_type=EntityType.KEYWORD,
        confidence=0.99,
        priority=0
    ),
    ExtractionRule(
        name="phone_cn",
        pattern=r'(?:(?:\+?86)?1[3-9]\d{9})(?!\d)',
        entity_type=EntityType.KEYWORD,
        confidence=0.99,
        priority=0
    ),
    ExtractionRule(
        name="url",
        pattern=r'https?://[^\s<>"{}|\\^`\[\]]+',
        entity_type=EntityType.KEYWORD,
        confidence=0.99,
        priority=0
    ),
    
    # ===== 组织机构标识规则 =====
    ExtractionRule(
        name="org_suffix",
        pattern=r'([^\s,，。；；！!？?()（）]{2,20}(?:公司|集团|企业|银行|医院|学校|大学|研究院|研究所|政府|部门|委员会|协会|基金会|中心|局|部|处|科))',
        entity_type=EntityType.ORGANIZATION,
        confidence=0.92,
        priority=1
    ),
    
    # ===== 地点识别规则（简化版）=====
    ExtractionRule(
        name="location_province",
        pattern=r'(?:在|位于|坐落于|地处)?\s*(北京|上海|天津|重庆|河北|山西|辽宁|吉林|黑龙江|江苏|浙江|安徽|福建|江西|山东|河南|湖北|湖南|广东|海南|四川|贵州|云南|陕西|甘肃|青海|台湾|内蒙古|广西|西藏|宁夏|新疆|香港|澳门)[省市自治区特别行政区]?',
        entity_type=EntityType.LOCATION,
        confidence=0.93,
        priority=1
    ),
    
    # ===== 领域术语规则（AI / 技术文档常见概念）=====
    ExtractionRule(
        name="ai_concepts",
        pattern=r'\b(LLM|GPT|RAG|GraphRAG)\b|(人工智能|机器学习|深度学习|知识图谱|大语言模型|神经网络|自然语言处理|强化学习|计算机视觉|数据挖掘|数据分析)',
        entity_type=EntityType.CONCEPT,
        confidence=0.85,
        priority=1
    ),
    ExtractionRule(
        name="tech_frameworks",
        pattern=r'\b(Neo4j|PyTorch|TensorFlow|FastAPI|Flask|Django|PostgreSQL|MySQL|MongoDB|Redis|Elasticsearch|Docker|Kubernetes|HuggingFace|LangChain|LlamaIndex|OpenAI|DeepSeek|Qwen|ChatGPT)\b',
        entity_type=EntityType.TECHNOLOGY,
        confidence=0.86,
        priority=1
    ),
]


# ============================================================
# 第二部分：实体抽取结果包装
# ============================================================


@dataclass
class ExtractedEntity:
    """
    抽取结果中的单个实体
    
    包含位置信息和置信度，便于后续的验证和过滤。
    """
    name: str                          # 实体文本
    type: EntityType                  # 实体类型
    start_pos: int = 0                # 在原文中的起始位置
    end_pos: int = 0                  # 在原文中的结束位置
    confidence: float = 0.8           # 置信度
    source: str = "unknown"           # 来源："llm" | "rule" | "merged"
    properties: Dict[str, Any] = field(default_factory=dict)
    
    def to_entity(self, doc_id: Optional[str] = None, kb_id: Optional[int] = None) -> Entity:
        """转换为标准的Entity模型"""
        return Entity(
            name=self.name,
            type=self.type,
            confidence=self.confidence,
            source_document_id=doc_id,
            knowledge_base_id=kb_id,
            properties=self.properties
        )


@dataclass
class ExtractionResult:
    """
    实体抽取的完整结果
    
    包含统计信息和所有抽取到的实体。
    """
    entities: List[ExtractedEntity] = field(default_factory=list)
    text_length: int = 0
    processing_time_ms: float = 0.0
    
    # 统计信息
    llm_count: int = 0                # LLM抽出的数量
    rule_count: int = 0               # 规则抽出的数量
    merged_count: int = 0             # 最终去重后的数量
    
    # 错误信息
    errors: List[str] = field(default_factory=list)
    
    @property
    def by_type(self) -> Dict[EntityType, int]:
        """按类型分组统计"""
        stats: Dict[EntityType, int] = {}
        for ent in self.entities:
            stats[ent.type] = stats.get(ent.type, 0) + 1
        return stats
    
    @property
    def high_confidence(self, threshold: float = 0.85) -> List[ExtractedEntity]:
        """获取高置信度实体"""
        return [e for e in self.entities if e.confidence >= threshold]
    
    def to_entities(self, doc_id: Optional[str] = None, kb_id: Optional[int] = None) -> List[Entity]:
        """转换为标准Entity列表（用于存储到Neo4j）"""
        return [e.to_entity(doc_id, kb_id) for e in self.entities]


# ============================================================
# 第三部分：核心服务类
# ============================================================


class RuleEngine:
    """
    规则引擎
    
    使用预定义的正则表达式从文本中抽取结构化实体。
    
    特点：
    - 零成本（无需调用外部API）
    - 高精度（针对特定类型可达99%+）
    - 可扩展（支持自定义规则）
    - 快速（毫秒级响应）
    
    适用场景：
    时间、日期、金额、百分比、邮箱、电话、URL、组织名称后缀
    """
    
    def __init__(self, custom_rules: Optional[List[ExtractionRule]] = None):
        """
        初始化规则引擎
        
        Args:
            custom_rules: 自定义规则列表（会与内置规则合并）
        """
        self.rules = list(BUILTIN_RULES)  # 复制内置规则
        
        if custom_rules:
            self.rules.extend(custom_rules)
        
        # 编译所有正则表达式（性能优化）
        self._compiled_rules: List[Tuple[ExtractionRule, re.Pattern]] = []
        for rule in self.rules:
            try:
                compiled = re.compile(rule.pattern, re.IGNORECASE | re.UNICODE)
                self._compiled_rules.append((rule, compiled))
            except re.error as e:
                logger.warning(f"规则编译失败 [{rule.name}]: {e}")
        
        logger.info(f"📏 规则引擎初始化完成 | 规则数: {len(self._compiled_rules)}")
    
    async def extract(
        self,
        text: str,
        doc_id: Optional[str] = None
    ) -> ExtractionResult:
        """
        从文本中抽取实体（异步接口，兼容性设计）
        
        实际执行是同步的，但提供async接口以便与LLM抽取统一调用方式。
        """
        return self.extract_sync(text, doc_id)
    
    def extract_sync(
        self,
        text: str,
        doc_id: Optional[str] = None
    ) -> ExtractionResult:
        """
        同步版本的实体抽取
        """
        result = ExtractionResult(text_length=len(text))
        seen_spans: Set[Tuple[int, int]] = set()  # 用于避免重复匹配
        
        # 按优先级排序并执行
        sorted_rules = sorted(self._compiled_rules, key=lambda x: x[0].priority)
        
        for rule, pattern in sorted_rules:
            for match in pattern.finditer(text):
                span = (match.start(), match.end())
                
                # 检查是否已被更高优先级的规则匹配
                if any(
                    s <= span[0] and e >= span[1]
                    for s, e in seen_spans
                ):
                    continue
                
                # 提取实体文本
                matched_text = match.group().strip()
                
                if len(matched_text) < 1 or len(matched_text) > 200:
                    continue  # 过滤太短或太长的匹配
                
                # 创建抽取结果
                extracted = ExtractedEntity(
                    name=matched_text,
                    type=rule.entity_type,
                    start_pos=span[0],
                    end_pos=span[1],
                    confidence=rule.confidence,
                    source="rule",
                    properties={
                        "rule_name": rule.name,
                        "matched_pattern": rule.pattern[:50]
                    }
                )
                
                result.entities.append(extracted)
                seen_spans.add(span)
                result.rule_count += 1
        
        result.merged_count = len(result.entities)
        
        logger.debug(f"✓ 规则抽取完成 | 实体数: {result.merged_count} | "
                     f"类型分布: {result.by_type}")
        
        return result


class LLMEntityExtractor:
    """
    LLM驱动的实体抽取器
    
    使用大语言模型（如通义千问、GPT-4等）进行智能实体识别。
    
    特点：
    - 语义理解能力强（能识别隐含的实体关系）
    - 泛化性好（未见过的实体类型也能识别）
    - 上下文感知（能根据前后文判断实体类型）
    
    缺点：
    - 有API成本
    - 延迟较高（~1-5秒）
    - 结果不稳定（可能需要多次重试）
    
    设计决策：
    - 使用结构化Prompt确保输出格式一致
    - 提供Few-shot示例提升准确性
    - 内置JSON解析和错误恢复机制
    """
    
    SYSTEM_PROMPT = """你是一个专业的命名实体识别(NER)系统。你的任务是从给定的中文文本中识别出所有重要的命名实体。

## 识别要求

请仔细阅读文本，识别以下类型的实体：

### 人物类 (Person/Organization/Position)
- **Person**: 人名、人物代号（如"张三"、"马云"、"小王"）
- **Organization**: 组织机构名称（如"腾讯公司"、"清华大学"、"发改委"）
- **Position**: 职位头衔（如"CEO"、"总经理"、"教授"）

### 地点类 (Location/Country/City/Address)
- **Country**: 国家名称
- **City**: 城市名称
- **Location**: 其他地点（如"深圳"、"西湖"）

### 产品技术类 (Product/Technology/Project/Concept)
- **Product**: 产品、软件名（如"iPhone"、"微信"、"Windows"）
- **Technology**: 技术术语（如"人工智能"、"区块链"、"机器学习"）
- **Project**: 项目名称（如"登月计划"、"智慧城市"）
- **Concept**: 抽象概念（如"可持续发展"、"数字化转型"）

### 其他重要实体
- **Event**: 重大事件（如"奥运会"、"金融危机"）
- **Date**: 重要时间点
- **Money**: 金额数字

## 输出格式

请严格按照JSON数组格式返回，不要添加任何其他文字：

```json
[
  {
    "name": "实体名称",
    "type": "实体类型英文",
    "confidence": 0.0-1.0之间的置信度,
    "start_pos": 在原文中的起始字符位置(可选),
    "end_pos": 结束位置(可选)
  }
]
```

## 注意事项
1. 不要遗漏重要实体，宁可多抽也不要漏抽
2. 置信度应该基于文本中实体明确程度给出
3. 同一个实体如果多次出现，只需返回一次
4. 如果某个词可能是多种类型，选择最具体的一个
5. 只返回JSON数组，不要包含解释文字"""

    FEW_SHOT_EXAMPLES = [
        {
            "text": "2025年3月，阿里巴巴集团的CEO张勇宣布，公司将投入100亿元用于人工智能技术的研发，该项目的负责人是李明博士。",
            "expected_entities": [
                {"name": "阿里巴巴集团", "type": "Organization", "confidence": 0.98},
                {"name": "张勇", "type": "Person", "confidence": 0.97},
                {"name": "100亿元", "type": "Money", "confidence": 0.96},
                {"name": "人工智能", "type": "Technology", "confidence": 0.95},
                {"name": "李明博士", "type": "Person", "confidence": 0.92}
            ]
        }
    ]
    
    def __init__(
        self,
        model_name: str = "qwen-plus",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        max_tokens: int = 2048,
        temperature: float = 0.1  # 低温度以确保稳定输出
    ):
        """
        初始化LLM抽取器
        
        Args:
            model_name: 模型名称
            api_key: API密钥（如果不传则从环境变量读取）
            base_url: API基础URL
            max_tokens: 最大生成token数
            temperature: 温度参数（低温度=更确定性的输出）
        """
        self.model_name = model_name
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.api_key = api_key
        self.base_url = base_url
        
        # 尝试初始化客户端
        self.client = None
        try:
            self._initialize_client()
        except Exception as e:
            logger.warning(f"⚠️ LLM客户端初始化失败（将回退到纯规则模式）: {e}")
        
        logger.info(f"🤖 LLM实体抽取器初始化 | 模型: {model_name}")
    
    def _initialize_client(self):
        """初始化LLM客户端"""
        import os
        
        api_key = self.api_key or os.getenv("DASHSCOPE_API_KEY") or os.getenv("OPENAI_API_KEY")
        base_url = self.base_url or os.getenv("OPENAI_BASE_URL")
        
        if not api_key:
            raise ValueError("未配置API密钥，请设置 DASHSCOPE_API_KEY 或 OPENAI_API_KEY")
        
        # 根据base_url判断使用的SDK
        if base_url and 'dashscope' in base_url.lower():
            import dashscope
            dashscope.api_key = api_key
            self.client_type = "dashscope"
        else:
            # 默认使用OpenAI兼容接口
            from openai import AsyncOpenAI
            self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
            self.client_type = "openai"
    
    async def extract(
        self,
        text: str,
        doc_id: Optional[str] = None,
        context: Optional[str] = None
    ) -> ExtractionResult:
        """
        使用LLM从文本中抽取实体
        
        Args:
            text: 待抽取的文本（建议不超过5000字）
            doc_id: 来源文档ID（用于溯源）
            context: 额外的上下文信息（如文档标题、摘要等）
        
        Returns:
            ExtractionResult对象
        """
        import time
        start_time = time.time()
        
        result = ExtractionResult(text_length=len(text))
        
        if not self.client and not hasattr(self, 'client_type'):
            logger.warning("⚠️ LLM客户端不可用，跳过LLM抽取")
            return result
        
        # 截断过长的文本
        max_chars = 4000  # 控制token消耗
        truncated = text[:max_chars] + ("..." if len(text) > max_chars else "")
        
        try:
            # 构建消息
            messages = self._build_messages(truncated, context)
            
            # 调用LLM
            response_text = await self._call_llm(messages)
            
            # 解析结果
            entities = self._parse_llm_response(response_text)
            
            # 转换为ExtractedEntity
            for ent_data in entities:
                try:
                    entity = ExtractedEntity(
                        name=ent_data["name"],
                        type=EntityType(ent_data["type"]),
                        confidence=float(ent_data.get("confidence", 0.8)),
                        start_pos=int(ent_data.get("start_pos", 0)),
                        end_pos=int(ent_data.get("end_pos", 0)),
                        source="llm",
                        properties={"llm_model": self.model_name}
                    )
                    result.entities.append(entity)
                    result.llm_count += 1
                    
                except (ValueError, KeyError) as e:
                    logger.debug(f"解析单条实体失败: {e}, 数据: {ent_data}")
            
        except Exception as e:
            logger.error(f"❌ LLM实体抽取失败: {e}", exc_info=True)
            # 不抛异常，允许继续使用规则引擎的结果
        
        result.processing_time_ms = (time.time() - start_time) * 1000
        
        logger.info(f"🤖 LLM抽取完成 | 实体数: {result.llm_count} | "
                     f"耗时: {result.processing_time_ms:.0f}ms")
        
        return result
    
    def _build_messages(
        self,
        text: str,
        context: Optional[str] = None
    ) -> List[Dict[str, str]]:
        """构建发送给LLM的消息"""
        messages = [{"role": "system", "content": self.SYSTEM_PROMPT}]
        
        # 添加few-shot示例（如果有）
        if self.FEW_SHOT_EXAMPLES:
            example = self.FEW_SHOT_EXAMPLES[0]
            example_msg = (
                f"\n\n## 示例\n\n文本: \"{example['text']}\"\n\n"
                f"请识别上述文本中的实体。\n\n期望输出格式参考:\n"
                f"{json.dumps(example['expected_entities'], ensure_ascii=False, indent=2)}"
            )
            messages.append({"role": "user", "content": example_msg})
            
            # 示例的助手回复（模拟）
            messages.append({
                "role": "assistant",
                "content": json.dumps(example['expected_entities'], ensure_ascii=False)
            })
        
        # 实际待处理的文本
        user_content = f"\n\n请识别以下文本中的实体:\n\n{text}"
        if context:
            user_content = f"[上下文: {context}]\n\n" + user_content
        
        messages.append({"role": "user", "content": user_content})
        
        return messages
    
    async def _call_llm(self, messages: List[Dict]) -> str:
        """调用LLM API并返回原始文本（带重试和 Mock 降级）。"""
        from app.core.llm_client import call_openai_chat, call_dashscope, is_llm_configured

        if self.client_type == "dashscope":
            if not is_llm_configured("dashscope"):
                logger.warning("DashScope API 未配置，使用 Mock 数据")
                from app.core.mock_data import get_mock_data
                mock_result = get_mock_data("entity_extraction", text=str(messages))
                return json.dumps(mock_result.get("entities", []))

            return await call_dashscope(
                model=self.model_name,
                messages=messages,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                mock_module="entity_extraction",
            )
        else:  # OpenAI兼容接口
            if not is_llm_configured("agent"):
                logger.warning("Agent API 未配置，使用 Mock 数据")
                from app.core.mock_data import get_mock_data
                mock_result = get_mock_data("entity_extraction", text=str(messages))
                return json.dumps(mock_result.get("entities", []))

            return await call_openai_chat(
                model=self.model_name,
                messages=messages,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                api_key=self.api_key,
                base_url=self.base_url,
                mock_module="entity_extraction",
            )
    
    @staticmethod
    def _parse_llm_response(response_text: str) -> List[Dict[str, Any]]:
        """
        解析LLM返回的JSON
        
        处理各种可能的格式问题：
        - JSON被包裹在```json代码块中
        - JSON前后有多余的文字
        - JSON格式有轻微错误
        """
        import json as _json
        
        text = response_text.strip()
        
        # 尝试提取JSON部分
        json_start = text.find('[')
        json_end = text.rfind(']')
        
        if json_start == -1 or json_end == -1:
            logger.warning("LLM返回内容中没有找到JSON数组")
            return []
        
        json_str = text[json_start:json_end + 1]
        
        try:
            entities = _json.loads(json_str)
            if isinstance(entities, list):
                return entities
            else:
                logger.warning("解析结果不是数组")
                return []
                
        except _json.JSONDecodeError as e:
            logger.warning(f"JSON解析失败，尝试修复: {e}")
            
            # 尝试简单的修复策略
            try:
                # 移除尾随逗号
                fixed = re.sub(r',\s*([}\]])', r'\1', json_str)
                return _json.loads(fixed)
            except:
                return []


class EntityDisambiguator:
    """
    实体消歧器
    
    解决同名实体和多义词问题。
    
    例如：
    - "苹果" 可能是：水果 / 苹果公司 / 电影《苹果》
    - "Java" 可能是：编程语言 / 印尼爪哇岛 / 咖啡
    
    策略：
    1. 基于上下文的消歧（查看周围的词汇）
    2. 类型一致性检查（如果周围都是IT词汇，更可能是苹果公司）
    3. 实体链接（尝试匹配已有知识库中的实体）
    4. 别名归一化（将"阿里"、"阿里巴巴"、"Alibaba"统一）
    """
    
    def __init__(self):
        self.alias_map: Dict[str, Set[str]] = {}
        logger.info("🔍 实体消歧器初始化完成")
    
    def add_alias_mapping(self, canonical: str, *aliases: str):
        """添加别名映射"""
        if canonical not in self.alias_map:
            self.alias_map[canonical] = set()
        self.alias_map[canonical].update(aliases)
    
    def disambiguate(
        self,
        entities: List[ExtractedEntity],
        context: Optional[str] = None
    ) -> List[ExtractedEntity]:
        """
        对实体列表进行消歧处理
        
        Args:
            entities: 原始实体列表
            context: 原始文本上下文
        
        Returns:
            消歧后的实体列表（已去除重复和归一化别名）
        """
        # Step 1: 别名归一化
        normalized = self._normalize_aliases(entities)
        
        # Step 2: 去重（基于名称和类型的精确匹配）
        deduped = self._deduplicate(normalized)
        
        # Step 3: 上下文消歧（基于周围词语调整类型）
        if context:
            deduped = self._context_disambiguation(deduped, context)
        
        logger.debug(f"🔍 消歧完成 | 输入: {len(entities)}, "
                      f"输出: {len(deduped)}")
        
        return deduped
    
    def _normalize_aliases(
        self, 
        entities: List[ExtractedEntity]
    ) -> List[ExtractedEntity]:
        """别名归一化"""
        result = []
        
        for entity in entities:
            # 检查是否是某个规范名的别名
            canonical_name = None
            for norm_name, aliases in self.alias_map.items():
                if entity.name in aliases or entity.name.lower() == norm_name.lower():
                    canonical_name = norm_name
                    break
            
            if canonical_name:
                # 替换为规范名
                entity.name = canonical_name
                entity.properties["original_name"] = entity.name
            
            result.append(entity)
        
        return result
    
    def _deduplicate(
        self,
        entities: List[ExtractedEntity]
    ) -> List[ExtractedEntity]:
        """
        去重逻辑
        
        规则：
        1. 名称完全相同 + 类型相同 → 保留置信度高的
        2. 名称相似度>90% → 可能是同一实体
        3. 相同名称不同类型 → 都保留（多义词）
        """
        seen: Dict[Tuple[str, EntityType], ExtractedEntity] = {}
        
        for entity in entities:
            key = (entity.name, entity.type)
            
            if key in seen:
                existing = seen[key]
                # 保留置信度更高的
                if entity.confidence > existing.confidence:
                    seen[key] = entity
            else:
                seen[key] = entity
        
        return list(seen.values())
    
    def _context_disambiguation(
        self,
        entities: List[ExtractedEntity],
        context: str
    ) -> List[ExtractedEntity]:
        """
        基于上下文的简单消歧
        
        这里使用简化的启发式规则，
        生产环境可以使用Embedding相似度等方法。
        """
        context_lower = context.lower()
        
        for entity in entities:
            # 如果实体是"苹果"，检查上下文是否有IT相关词汇
            if entity.name == "苹果":
                it_keywords = ["手机", "电脑", "科技", "iphone", "ios"]
                food_keywords = ["好吃", "水果", "吃", "红富士"]
                
                it_score = sum(1 for kw in it_keywords if kw in context_lower)
                food_score = sum(1 for kw in food_keywords if kw in context_lower)
                
                if it_score > food_score:
                    entity.type = EntityType.PRODUCT
                    entity.properties["disambiguation"] = "context_it"
                elif food_score > 0:
                    entity.type = EntityType.PRODUCT  # 水果也是产品
                    entity.properties["disambiguation"] = "context_food"
        
        return entities


# ============================================================
# 第四部分：统一入口 - EntityExtractionService
# ============================================================


class EntityExtractionService:
    """
    实体抽取服务（统一入口）
    
    整合LLM和规则引擎，提供完整的实体抽取流程。
    这是外部调用的主要类。
    
    工作流程：
    1. 同时启动LLM和规则引擎抽取（并行）
    2. 合并两组结果（以LLM为主，规则为辅补充）
    3. 实体消歧（去重、归一化、上下文消歧）
    4. 返回最终结果
    
    配置选项：
    use_llm: 是否启用LLM抽取（默认True）
    use_rules: 是否启���规则引擎（默认True）
    llm_priority: LLM结果的优先级（当两者冲突时）
    confidence_threshold: 最终输出的最低置信度
    """
    
    def __init__(
        self,
        use_llm: bool = True,
        use_rules: bool = True,
        llm_model: str = "qwen-plus",
        confidence_threshold: float = 0.5,  # 降低默认阈值，提升实体召回率
        custom_rules: Optional[List[ExtractionRule]] = None
    ):
        """
        初始化实体抽取服务
        
        Args:
            use_llm: 是否使用LLM（需要配置API密钥）
            use_rules: 是否使用规则引擎
            llm_model: LLM模型名称
            confidence_threshold: 低于此值的实体将被过滤
            custom_rules: 自定义抽取规则
        """
        self.use_llm = use_llm
        self.use_rules = use_rules
        self.confidence_threshold = confidence_threshold
        
        # 初始化子组件
        self.rule_engine = RuleEngine(custom_rules) if use_rules else None
        
        if use_llm:
            # 优先读取 pydantic settings 中的 LLM 配置（与 .env 中 LLM_* 配置一致），
            # 兼容旧版 DASHSCOPE_API_KEY / OPENAI_API_KEY 环境变量
            import os
            llm_api_key = None
            llm_base_url = None
            try:
                settings = get_settings()
                if settings and hasattr(settings, "llm"):
                    llm_api_key = settings.llm.agent_api_key
                    llm_base_url = settings.llm.agent_api_base
            except Exception as e:
                logger.debug(f"读取 settings LLM 配置失败: {e}")

            llm_api_key = (
                llm_api_key
                or os.getenv("LLM_AGENT_API_KEY")
                or os.getenv("DASHSCOPE_API_KEY")
                or os.getenv("OPENAI_API_KEY")
            )
            llm_base_url = (
                llm_base_url
                or os.getenv("LLM_AGENT_API_BASE")
                or os.getenv("OPENAI_BASE_URL")
            )
            self.llm_extractor = LLMEntityExtractor(
                model_name=llm_model,
                api_key=llm_api_key,
                base_url=llm_base_url,
            )
        else:
            self.llm_extractor = None
        
        self.disambiguator = EntityDisambiguator()
        
        # 加载预置的别名映射
        self._load_builtin_aliases()
        
        logger.info(f"🧩 实体抽取服务初始化完成 | "
                     f"LLM={use_llm}, Rules={use_rules}")
    
    def _load_builtin_aliases(self):
        """加载常见的别名映射"""
        common_aliases = [
            # 科技公司
            ("阿里巴巴", "阿里", "阿里巴巴集团", "Alibaba"),
            ("腾讯", "腾讯公司", "Tencent", "騰訊"),
            ("字节跳动", "ByteDance", "字节"),
            ("百度", "Baidu"),
            ("华为", "Huawei", "华为技术"),
            ("微软", "Microsoft", "MS"),
            ("苹果公司", "Apple", "苹果"),
            ("谷歌", "Google"),
            # 人物
            ("马云", "Jack Ma"),
        ]
        
        for canonical, *aliases in common_aliases:
            self.disambiguator.add_alias_mapping(canonical, *aliases)
    
    async def extract_entities(
        self,
        text: str,
        doc_id: Optional[str] = None,
        context: Optional[str] = None,
        kb_id: Optional[int] = None
    ) -> ExtractionResult:
        """
        从文本中抽取实体（主方法）
        
        Args:
            text: 待分析的文本
            doc_id: 来源文档ID
            context: 上下文信息（如标题、摘要）
            kb_id: 所属知识库ID
        
        Returns:
            ExtractionResult（包含所有抽取到的实体）
        """
        import time
        start = time.time()
        
        logger.info(f"🚀 开始实体抽取 | 文本长度: {len(text)} 字符")
        
        tasks = []
        
        # 并行启动LLM和规则抽取
        if self.use_llm and self.llm_extractor:
            tasks.append(("llm", self.llm_extractor.extract(text, doc_id, context)))
        
        if self.use_rules and self.rule_engine:
            tasks.append(("rule", self.rule_engine.extract(text, doc_id)))
        
        # 执行并行任务
        results_by_source: Dict[str, ExtractionResult] = {}
        
        if tasks:
            coroutines = [task[1] for task in tasks]
            sources = [task[0] for task in tasks]
            
            completed_results = await asyncio.gather(*coroutines, return_exceptions=True)
            
            for source, result in zip(sources, completed_results):
                if isinstance(result, Exception):
                    logger.error(f"{source.upper()}抽取失败: {result}")
                    results_by_source[source] = ExtractionResult()
                else:
                    results_by_source[source] = result
        
        # 合并结果
        final_result = self._merge_extraction_results(results_by_source)
        final_result.text_length = len(text)

        # 检测 LLM 配置缺失，避免静默 0 实体
        if self.use_llm and self.llm_extractor:
            if not self.llm_extractor.client and not hasattr(self.llm_extractor, 'client_type'):
                final_result.errors.append(
                    "LLM 实体抽取器未初始化：请配置 DASHSCOPE_API_KEY 或 OPENAI_API_KEY"
                )

        # 实体消歧
        all_entities = final_result.entities
        disambiguated = self.disambiguator.disambiguate(all_entities, text)
        
        # 过滤低置信度实体
        filtered = [
            e for e in disambiguated 
            if e.confidence >= self.confidence_threshold
        ]
        
        final_result.entities = filtered
        final_result.merged_count = len(filtered)
        final_result.processing_time_ms = (time.time() - start) * 1000
        
        # 未抽取到任何实体时给出明确提示
        if not final_result.entities:
            final_result.errors.append(
                "未抽取到任何实体。可能原因：1) LLM 未配置；2) 文本过短；"
                "3) 规则引擎未覆盖该领域术语；4) 置信度阈值过滤后无结果。"
            )
        
        logger.info(f"✅ 实体抽取完成 | 总计: {final_result.merged_count} 个实体 | "
                     f"耗时: {final_result.processing_time_ms:.0f}ms")
        
        return final_result
    
    def _merge_extraction_results(
        self,
        results: Dict[str, ExtractionResult]
    ) -> ExtractionResult:
        """
        合并多个来源的抽取结果
        
        合并策略：
        1. LLM结果优先（覆盖规则结果）
        2. 规则结果作为补充（只添加LLM没有的类型）
        3. 合并置信度取最高值
        """
        merged = ExtractionResult()
        
        llm_result = results.get("llm")
        rule_result = results.get("rule")
        
        # 先添加LLM结果
        if llm_result:
            merged.entities.extend(llm_result.entities)
            merged.llm_count = llm_result.llm_count
        
        # 再合并规则结果（排除已存在的同名同类实体）
        if rule_result:
            existing_keys = {(e.name, e.type) for e in merged.entities}
            
            for entity in rule_result.entities:
                key = (entity.name, entity.type)
                
                if key not in existing_keys:
                    # 规则发现的新实体，直接添加
                    merged.entities.append(entity)
                    merged.rule_count += 1
                else:
                    # 已存在，更新置信度（取最大值）
                    for existing in merged.entities:
                        if (existing.name, existing.type) == key:
                            existing.confidence = max(existing.confidence, entity.confidence)
                            break
        
        return merged
    
    async def batch_extract(
        self,
        chunks: List[str],
        doc_id: Optional[str] = None,
        concurrency: int = 3
    ) -> ExtractionResult:
        """
        批量抽取（长文档分块处理）
        
        对于长文档（>5000字），建议先分块再批量抽取。
        
        Args:
            chunks: 文本块列表
            doc_id: 文档ID
            concurrency: 并发数（控制API调用速率）
        Returns:
            合并后的抽取结果
        """
        logger.info(f"📦 开始批量实体抽取 | 块数: {len(chunks)} | 并发: {concurrency}")
        
        semaphore = asyncio.Semaphore(concurrency)
        
        async def process_chunk(chunk: str, index: int) -> ExtractionResult:
            async with semaphore:
                return await self.extract_entities(
                    chunk,
                    doc_id=f"{doc_id}_chunk_{index}" if doc_id else None
                )
        
        # 并行处理所有块
        tasks = [process_chunk(chunk, i) for i, chunk in enumerate(chunks)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 合并所有结果
        final_result = ExtractionResult()
        total_entities = 0
        
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning(f"Chunk {i} 处理失败: {result}")
                continue
            
            final_result.entities.extend(result.entities)
            total_entities += len(result.entities)
        
        # 全局去重
        final_result.entities = self.disambiguator.disambiguate(final_result.entities)
        final_result.merged_count = len(final_result.entities)
        
        logger.info(f"📦 批量抽取完成 | 总实体数(去重前): {total_entities}, "
                     f"(去重后): {final_result.merged_count}")
        
        return final_result


# ==================== 导出 ====================

__all__ = [
    # 规则相关
    'ExtractionRule',
    'BUILTIN_RULES',
    # 结果包装
    'ExtractedEntity',
    'ExtractionResult',
    # 子组件
    'RuleEngine',
    'LLMEntityExtractor',
    'EntityDisambiguator',
    # 主服务
    'EntityExtractionService'
]
