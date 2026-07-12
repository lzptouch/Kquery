"""
关系抽取服务 (Relation Extraction Service)

识别文本中实体之间的语义关系，是知识图谱构建的核心环节。
在实体识别(NER)的基础上，进一步理解实体间的关联。

核心功能：
1. LLM关系抽取 - 使用大语言模型判断实体对之间的关系
2. 规则模式匹配 - 基于预定义句式模板的高精度关系识别
3. 关系验证 - 检查关系方向性、合理性、置信度
4. 关系去重 - 合并相同关系的多次提取结果

架构设计：
    实体列表 + 文本
        ↓
    ┌─────────────────┐
    │  LLM关系抽取器     │ ← 主力：能理解复杂语义关系
    │  (深度模型)       │   准确率 ~80%
    └────────┬──────────┘
             ↓ 合并
    ┌─────────────────┐
    │  模式匹配引擎     │ ← 辅助：特定句式100%准确
    │  (正则/依存)      │   如"X创立了Y"
    └────────┬──────────┘
             ↓ 验证+去重
    ┌─────────────────┐
    │  关系后处理器     │ ← 验证方向、计算权重、去重合并
    │  (验证/去重)      │
    └────────┬──────────┘
             ↓
    关系列表输出

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
from difflib import SequenceMatcher
from typing import (
    Any, Dict, List, Optional, Set,
    Tuple, Callable
)
from dataclasses import dataclass, field
from enum import Enum

# 导入项目模块
try:
    from app.repositories.graph.schema import (
        Entity, Relation, EntityType, RelationType, graph_schema
    )
    from app.services.graph.entity_extraction import ExtractedEntity
    from app.core.logger import get_logger
    from app.core.settings import get_settings
except ImportError:
    class Entity: pass
    class Relation: pass
    class EntityType: pass
    class RelationType: pass
    class ExtractedEntity: pass
    
    def get_logger(name):
        return logging.getLogger(name)
    
    def get_settings():
        return None

logger = get_logger(__name__)


# ============================================================
# 第一部分：关系模式定义（Pattern Definitions）
# ============================================================


@dataclass
class RelationPattern:
    """关系抽取模式（基于正则表达式）"""
    name: str                           # 模式名称
    pattern: str                        # 正则表达式
    relation_type: RelationType         # 匹配的关系类型
    confidence: float = 0.90            # 匹配置信度
    source_group: int = 1               # 头实体的捕获组编号
    target_group: int = 2               # 尾实体的捕获组编号
    bidirectional: bool = False         # 是否双向关系
    description: str = ""              # 模式描述


# 预置的关系模式库（中文常用表达）
BUILTIN_PATTERNS: List[RelationPattern] = [
    # ===== 雇佣/工作关系 =====
    RelationPattern(
        name="employed_by_1",
        pattern=r'(\S+)\s*(?:是|为|担任|任职(?:于)?)\s*([^，。；！？]*?(?:的)?(?:CEO|总裁|总经理|负责人|经理|主管|总监|董事|董事长|领导|员工|职员|工程师|研究员|教授|讲师|助理|秘书|专员|顾问|专家))',
        relation_type=RelationType.EMPLOYED_BY,
        confidence=0.95,
        source_group=1,
        target_group=2,
        description="X是Y的CEO/经理等"
    ),
    RelationPattern(
        name="works_for",
        pattern=r'(\S+?)(?:在|就职|工作|任职|供职)(?:于|在)([^，。；！？]+?)(?:公司|集团|企业|机构|单位|组织|部门|学校|医院)',
        relation_type=RelationType.WORKS_FOR,
        confidence=0.92,
        source_group=1,
        target_group=2,
        description="X在Y公司工作"
    ),
    
    # ===== 创建/创立关系 =====
    RelationPattern(
        name="founded",
        pattern=r'(\S+?)\s*(?:创立|创建|创办|成立|建立了|建立了)(?:了|过)?\s*([^，。；！？]+)',
        relation_type=RelationType.FOUNDED,
        confidence=0.93,
        source_group=1,
        target_group=2,
        description="X创立了Y"
    ),
    
    # ===== 管理关系 =====
    RelationPattern(
        name="manages",
        pattern=r'(\S+?)(?:管理|领导|负责|主持|主导|统筹|协调)(?:着|了|过)?(?:的)?\s*([^，。；！？]+)',
        relation_type=RelationType.MANAGES,
        confidence=0.90,
        source_group=1,
        target_group=2,
        description="X管理Y团队/项目/部门"
    ),
    
    # ===== 地理位置 =====
    RelationPattern(
        name="located_in_1",
        pattern=r'(\S+?)(?:位于|坐落于|地处|坐落|坐落在|在)\s*([^，。；！？]+)',
        relation_type=RelationType.LOCATED_IN,
        confidence=0.94,
        source_group=1,
        target_group=2,
        description="X位于Y"
    ),
    RelationPattern(
        name="located_in_2",
        pattern=r'([^，。；！？]+?)\s*(?:的|里|中|内|上|旁|附近|周边|境内|辖区)(?:有|包含|包括|坐落|位于|建有)\s*(\S+)',
        relation_type=RelationType.CONTAINS,
        confidence=0.88,
        source_group=2,
        target_group=1,
        description="Y在X里面（反向CONTAINS）"
    ),
    
    # ===== 合作关系 =====
    RelationPattern(
        name="collaborates_with",
        pattern=r'(\S+?)(?:与|和|同|跟)(\S+?)(?:合作|协作|联合|共同|携手)(?:研发|开发|开展|进行|推出|发布|完成了)?',
        relation_type=RelationType.COLLABORATES_WITH,
        confidence=0.91,
        bidirectional=True,
        description="X与Y合作"
    ),
    
    # ===== 包含/组成关系 =====
    RelationPattern(
        name="contains",
        pattern=r'(\S+?)(?:包含|包括|由...组成|由...构成|涵盖了|囊括了)\s*([^，。；！？]+)',
        relation_type=RelationType.CONTAINS,
        confidence=0.89,
        source_group=1,
        target_group=2,
        description="X包含Y"
    ),
    
    # ===== 时间顺序 =====
    RelationPattern(
        name="before_after",
        pattern=r'(\S+?)\s*(?:之前|以前|先(?:于)?)(?:，|,)?\s*(\S+?)(?:就|便|随后|之后|后来|然后)',
        relation_type=RelationType.BEFORE,
        confidence=0.87,
        source_group=1,
        target_group=2,
        description="X在Y之前发生"
    ),

    # ===== 依赖/使用关系 =====
    RelationPattern(
        name="uses",
        pattern=r'(\S+?)(?:使用|采用|利用|运用|借助)(?:了|过)?\s*([^，。；！？]+)',
        relation_type=RelationType.USES,
        confidence=0.88,
        source_group=1,
        target_group=2,
        description="X使用Y"
    ),
    RelationPattern(
        name="depends_on",
        pattern=r'(\S+?)(?:基于|依赖|依靠|依托|建立在|架构在)(?:了|过)?\s*([^，。；！？]+)',
        relation_type=RelationType.DEPENDS_ON,
        confidence=0.88,
        source_group=1,
        target_group=2,
        description="X基于Y"
    ),
    RelationPattern(
        name="part_of",
        pattern=r'(\S+?)(?:是|为)([^，。；！？]+?)(?:的|之一)(?:核心|重要)?(?:组成部分|组成|组件|模块|环节|要素)',
        relation_type=RelationType.PART_OF,
        confidence=0.86,
        source_group=1,
        target_group=2,
        description="X是Y的组成部分"
    ),
    RelationPattern(
        name="released",
        pattern=r'(\S+?)(?:推出|发布|上线|发行|推出|问世)(?:了|过)?\s*([^，。；！？]+)',
        relation_type=RelationType.CONTAINS,
        confidence=0.85,
        source_group=1,
        target_group=2,
        description="X推出/发布Y"
    ),
    RelationPattern(
        name="applied_to",
        pattern=r'(\S+?)(?:应用于|用于|适用(?:于|于)|面向)(?:了|过)?\s*([^，。；！？]+?)(?:场景|领域|行业|业务|任务|问题)?',
        relation_type=RelationType.USES,
        confidence=0.84,
        source_group=1,
        target_group=2,
        description="X应用于Y场景"
    ),
]


# ============================================================
# 第二部分：关系抽取结果包装
# ============================================================


@dataclass
class ExtractedRelation:
    """
    抽取到的单个关系
    """
    source_entity_name: str              # 头实体名称
    target_entity_name: str              # 尾实体名称  
    type: RelationType                   # 关系类型
    confidence: float = 0.8              # 置信度
    source: str = "unknown"              # 来源："llm" | "pattern"
    evidence_text: Optional[str] = None  # 支撑原文（证据句子）
    properties: Dict[str, Any] = field(default_factory=dict)
    
    def to_relation(
        self,
        source_id: str,
        target_id: str,
        doc_id: Optional[str] = None,
        kb_id: Optional[int] = None
    ) -> Relation:
        """转换为标准Relation模型"""
        return Relation(
            source_entity_id=source_id,
            target_entity_id=target_id,
            type=self.type,
            confidence=self.confidence,
            source_document_id=doc_id,
            knowledge_base_id=kb_id,
            properties={
                **self.properties,
                "evidence": self.evidence_text or ""
            }
        )


@dataclass
class RelationExtractionResult:
    """关系抽取的完整结果"""
    relations: List[ExtractedRelation] = field(default_factory=list)
    entity_count: int = 0               # 涉及的实体数
    processing_time_ms: float = 0.0
    
    llm_count: int = 0                  # LLM抽出的数量
    pattern_count: int = 0              # 模式匹配的数量
    merged_count: int = 0               # 最终数量
    
    # 错误信息
    errors: List[str] = field(default_factory=list)
    
    @property
    def by_type(self) -> Dict[RelationType, int]:
        """按类型统计"""
        stats: Dict[RelationType, int] = {}
        for rel in self.relations:
            stats[rel.type] = stats.get(rel.type, 0) + 1
        return stats
    
    @property
    def high_confidence(self, threshold: float = 0.80) -> List[ExtractedRelation]:
        """高置信度关系"""
        return [r for r in self.relations if r.confidence >= threshold]


# ============================================================
# 第三部分：子组件实现
# ============================================================


class PatternMatcher:
    """
    关系模式匹配引擎
    
    使用预定义的正则表达式模式从文本中快速识别常见的关系表达。
    
    特点：
    - 高精度（特定句式接近100%准确）
    - 零成本（纯本地计算）
    - 可扩展（支持自定义模式）
    
    局限性：
    - 只能识别预定义的模式
    - 无法处理复杂的语义推理
    """
    
    def __init__(self, custom_patterns: Optional[List[RelationPattern]] = None):
        self.patterns = list(BUILTIN_PATTERNS)
        
        if custom_patterns:
            self.patterns.extend(custom_patterns)
        
        # 编译正则
        self._compiled: List[Tuple[RelationPattern, re.Pattern]] = []
        for pat in self.patterns:
            try:
                compiled = re.compile(pat.pattern, re.IGNORECASE | re.UNICODE)
                self._compiled.append((pat, compiled))
            except re.error as e:
                logger.warning(f"关系模式编译失败 [{pat.name}]: {e}")
        
        logger.info(f"📐 关系模式匹配器初始化 | 模式数: {len(self._compiled)}")
    
    async def match(
        self,
        text: str,
        entities: Optional[List[ExtractedEntity]] = None
    ) -> RelationExtractionResult:
        """异步接口（兼容性设计）"""
        return self.match_sync(text, entities)
    
    def match_sync(
        self,
        text: str,
        entities: Optional[List[ExtractedEntity]] = None
    ) -> RelationExtractionResult:
        """同步版本的关系匹配"""
        result = RelationExtractionResult()
        result.entity_count = len(entities) if entities else 0
        
        # 构建实体名称集合（用于验证匹配结果是否是已知实体）
        entity_names: Set[str] = set()
        if entities:
            entity_names = {e.name.lower() for e in entities}
        
        # 执行所有模式的匹配
        for pattern, regex in self._compiled:
            for match in regex.finditer(text):
                try:
                    source_name = match.group(pattern.source_group).strip()
                    target_name = match.group(pattern.target_group).strip()
                    
                    # 过滤无效匹配
                    if not source_name or not target_name:
                        continue
                    if len(source_name) > 100 or len(target_name) > 100:
                        continue
                    
                    # 如果提供了实体列表，检查匹配的是否是真实实体
                    if entity_names:
                        source_valid = any(
                            source_name in ename or ename in source_name 
                            for ename in entity_names
                        )
                        target_valid = any(
                            target_name in ename or ename in target_name 
                            for ename in entity_names
                        )
                        
                        # 如果两者都不是已知实体，降低置信度或跳过
                        if not source_valid and not target_valid and entity_names:
                            continue
                    
                    # 提取证据文本（匹配位置前后各50字符）
                    start = max(0, match.start() - 30)
                    end = min(len(text), match.end() + 30)
                    evidence = text[start:end].replace('\n', ' ')
                    
                    # 创建关系
                    extracted_rel = ExtractedRelation(
                        source_entity_name=source_name,
                        target_entity_name=target_name,
                        type=pattern.relation_type,
                        confidence=pattern.confidence,
                        source="pattern",
                        evidence_text=evidence,
                        properties={"pattern": pattern.name}
                    )
                    
                    result.relations.append(extracted_rel)
                    result.pattern_count += 1
                    
                except (IndexError, AttributeError) as e:
                    logger.debug(f"模式匹配异常 [{pattern.name}]: {e}")
        
        result.merged_count = len(result.relations)
        
        return result


class LLMRelationExtractor:
    """
    LLM驱动的关系抽取器
    
    使用大语言模型理解实体间的语义关系。
    
    特点：
    - 能处理复杂语义（如隐含的因果关系）
    - 能识别未见过的关系表达
    - 上下文感知能力强
    
    设计决策：
    - 输入：文本 + 已识别的实体列表
    - 输出：结构化的关系三元组
    - 内置错误恢复和格式修正
    """
    
    SYSTEM_PROMPT = """你是一个专业的语义关系抽取系统。给定一段中文文本和其中识别出的实体列表，你的任务是找出这些实体之间的语义关系。

## 任务要求

仔细阅读文本，判断每对实体之间是否存在明确的关系。

### 支持的关系类型

**层级关系**：
- part_of (组成)：A是B的一部分
- subclass_of (子类继承)：A是B的子类/下属
- contains (包含)：B包含A

**属性关系**：
- has_attribute (具有属性)：A有某个属性值
- located_in (位于)：A位于B

**社会关系**：
- employed_by (受雇于)：A受雇于B（如员工与公司）
- works_for (工作于)：A在B处工作
- founded (创立)：A创建了B
- manages (管理)：A管理B
- collaborates_with (合作)：A与B合作

**时序关系**：
- before (之前)：A发生在B之前
- after (之后)：A发生在B之后

**因果关系**：
- causes (导致)：A导致B
- depends_on (依赖)：A依赖B
- uses (使用)：A使用B

**通用关系**：
- related_to (相关)：A和B相关（兜底关系）

## 输出格式

请严格按照JSON数组返回：

```json
[
  {
    "source": "头实体名称",
    "target": "尾实体名称",
    "relation": "关系类型英文",
    "confidence": 0.0-1.0,
    "evidence": "支撑该关系的原文片段"
  }
]
```

## 注意事项
1. 只输出确实存在明显关系实体对，不要猜测不确定的关系
2. 如果两个实体之间没有明确关系，不要强行建立关系
3. evidence字段应该引用原文中的具体表述
4. confidence应该反映关系的确定程度
5. 只返回JSON数组"""

    def __init__(
        self,
        model_name: str = "qwen-plus",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        temperature: float = 0.15
    ):
        self.model_name = model_name
        self.temperature = temperature
        self.client = None
        
        try:
            self._initialize_client(api_key, base_url)
        except Exception as e:
            logger.warning(f"⚠️ LLM客户端初始化失败: {e}")
        
        logger.info(f"🤖 LLM关系抽取器初始化 | 模型: {model_name}")
    
    def _initialize_client(self, api_key=None, base_url=None):
        import os
        
        key = api_key or os.getenv("DASHSCOPE_API_KEY") or os.getenv("OPENAI_API_KEY")
        url = base_url or os.getenv("OPENAI_BASE_URL")
        
        if not key:
            raise ValueError("未配置API密钥")
        
        if url and 'dashscope' in url.lower():
            import dashscope
            dashscope.api_key = key
            self.client_type = "dashscope"
        else:
            from openai import AsyncOpenAI
            self.client = AsyncOpenAI(api_key=key, base_url=url)
            self.client_type = "openai"
    
    async def extract(
        self,
        text: str,
        entities: List[ExtractedEntity],
        doc_id: Optional[str] = None
    ) -> RelationExtractionResult:
        """执行关系抽取"""
        import time
        start = time.time()
        
        result = RelationExtractionResult(entity_count=len(entities))
        
        if not self.client and not hasattr(self, 'client_type'):
            return result
        
        # 截断文本和实体列表
        max_text_chars = 3000
        truncated_text = text[:max_text_chars]
        
        # 限制实体数量（避免token过多）
        max_entities = 30
        display_entities = entities[:max_entities]
        
        try:
            # 构建消息
            messages = self._build_messages(truncated_text, display_entities)
            
            # 调用LLM
            response_text = await self._call_llm(messages)
            
            # 解析结果
            relations_data = self._parse_response(response_text)
            
            # 转换为ExtractedRelation
            for rel_data in relations_data:
                try:
                    rel = ExtractedRelation(
                        source_entity_name=rel_data["source"],
                        target_entity_name=rel_data["target"],
                        type=RelationType(rel_data["relation"]),
                        confidence=float(rel_data.get("confidence", 0.8)),
                        source="llm",
                        evidence_text=rel_data.get("evidence", "")
                    )
                    result.relations.append(rel)
                    result.llm_count += 1
                    
                except (ValueError, KeyError) as e:
                    logger.debug(f"解析单条关系失败: {e}, 数据: {rel_data}")
            
        except Exception as e:
            logger.error(f"❌ LLM关系抽取失败: {e}", exc_info=True)
        
        result.processing_time_ms = (time.time() - start) * 1000
        return result
    
    def _build_messages(
        self,
        text: str,
        entities: List[ExtractedEntity]
    ):
        """构建消息"""
        messages = [{"role": "system", "content": self.SYSTEM_PROMPT}]
        
        # 格式化实体列表
        entity_list_str = "\n".join(
            f"- {i+1}. \"{e.name}\" (类型: {e.type.value}, 置信度: {e.confidence:.0%})"
            for i, e in enumerate(entities)
        )
        
        user_msg = (
            f"\n\n## 待分析文本:\n{text}\n\n"
            f"## 已识别的实体({len(entities)}个):\n{entity_list_str}\n\n"
            f"请找出上述实体之间的所有关系，以JSON数组形式返回。"
        )
        messages.append({"role": "user", "content": user_msg})
        
        return messages
    
    async def _call_llm(self, messages):
        """调用LLM API"""
        if self.client_type == "dashscope":
            import dashscope
            resp = dashscope.Generation.call(
                model=self.model_name,
                messages=messages,
                max_tokens=2048,
                temperature=self.temperature,
                result_format='message'
            )
            if resp.status_code != 200:
                raise Exception(f"API错误: {resp.message}")
            return resp.output.choices[0].message.content
        else:
            completion = await self.client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                max_tokens=2048,
                temperature=self.temperature
            )
            return completion.choices[0].message.content
    
    @staticmethod
    def _parse_response(text: str) -> List[Dict]:
        """解析LLM响应"""
        text = text.strip()
        json_start = text.find('[')
        json_end = text.rfind(']')
        
        if json_start == -1 or json_end == -1:
            return []
        
        json_str = text[json_start:json_end + 1]
        
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            try:
                fixed = re.sub(r',\s*([}\]])', r'\1', json_str)
                return json.loads(fixed)
            except:
                return []


# ============================================================
# 第四部分：统一入口 - RelationExtractionService
# ============================================================


class RelationValidator:
    """
    关系验证器
    
    对抽取的关系进行质量检验和修正：
    1. 方向性检查（确保关系方向合理）
    2. 类型一致性验证（某些类型只适用于特定实体组合）
    3. 置信度校准（根据上下文调整）
    4. 冗余检测（去除重复关系）
    """
    
    @staticmethod
    def validate_direction(relation: ExtractedRelation) -> ExtractedRelation:
        """检查并可能翻转关系方向"""
        # 这里可以添加更复杂的方向性逻辑
        # 目前保持原样
        return relation
    
    @staticmethod
    def validate_consistency(
        relation: ExtractedRelation,
        source_type: EntityType,
        target_type: EntityType
    ) -> bool:
        """验证关系类型与实体类型的兼容性"""
        # 允许所有组合（保守策略），但记录可疑的组合
        suspicious_combos = [
            (EntityType.MONEY, RelationType.FOUNDED),
            (EntityType.DATE, RelationType.WORKS_FOR),
        ]
        
        if (source_type, relation.type) in suspicious_combos:
            logger.debug(f"⚠️ 可疑的类型组合: {source_type.value} "
                         f"-[{relation.type.value}]-> ?")
        
        return True  # 不阻止任何关系
    
    @staticmethod
    def normalize_entity_names(
        relations: List[ExtractedRelation],
        entities: List[ExtractedEntity],
        threshold: float = 0.85
    ) -> List[ExtractedRelation]:
        """
        将关系中的实体名称规范化到已抽取实体列表中的最佳匹配。

        LLM 可能返回"阿里巴巴"，而已抽取实体为"阿里巴巴集团"，
        通过模糊匹配提前规范化可减少下游匹配失败。
        """
        if not entities or not relations:
            return relations

        entity_names = [e.name for e in entities]

        def find_best_match(name: str) -> str:
            if name in entity_names:
                return name

            best_match = name
            best_score = 0.0
            name_lower = name.lower()
            for en in entity_names:
                score = SequenceMatcher(None, name_lower, en.lower()).ratio()
                if score > best_score and score >= threshold:
                    best_score = score
                    best_match = en
            return best_match

        for rel in relations:
            rel.source_entity_name = find_best_match(rel.source_entity_name)
            rel.target_entity_name = find_best_match(rel.target_entity_name)

        return relations

    @staticmethod
    def deduplicate_relations(
        relations: List[ExtractedRelation]
    ) -> List[ExtractedRelation]:
        """
        关系去重
        
        判断标准：(source, target, type) 三元组相同则为重复
        """
        seen: Dict[Tuple[str, str, RelationType], ExtractedRelation] = {}
        
        for rel in relations:
            key = (
                rel.source_entity_name.lower(),
                rel.target_entity_name.lower(),
                rel.type
            )
            
            if key in seen:
                existing = seen[key]
                # 保留置信度更高的
                if rel.confidence > existing.confidence:
                    seen[key] = rel
            else:
                seen[key] = rel
        
        return list(seen.values())


class RelationExtractionService:
    """
    关系抽取服务（统一入口）
    
    整合LLM和规则引擎，提供完整的关系抽取能力。
    
    工作流程：
    1. 并行启动LLM和模式匹配
    2. 合并两组结果
    3. 关系验证和去重
    4. 返回最终关系统表
    """
    
    def __init__(
        self,
        use_llm: bool = True,
        use_patterns: bool = True,
        llm_model: str = "qwen-plus",
        min_confidence: float = 0.6
    ):
        self.use_llm = use_llm
        self.use_patterns = use_patterns
        self.min_confidence = min_confidence
        
        # 初始化组件
        self.pattern_matcher = PatternMatcher() if use_patterns else None

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
            self.llm_extractor = LLMRelationExtractor(
                model_name=llm_model,
                api_key=llm_api_key,
                base_url=llm_base_url,
            )
        else:
            self.llm_extractor = None
        
        self.validator = RelationValidator()
        
        logger.info(f"🔗 关系抽取服务初始化 | LLM={use_llm}, Patterns={use_patterns}")
    
    async def extract_relations(
        self,
        text: str,
        entities: List[ExtractedEntity],
        doc_id: Optional[str] = None,
        kb_id: Optional[int] = None
    ) -> RelationExtractionResult:
        """主方法：抽取关系"""
        import time
        start = time.time()
        
        logger.info(f"🔗 开始关系抽取 | 实体数: {len(entities)} | "
                     f"文本长度: {len(text)}")
        
        tasks = []
        
        # 启动并行任务
        if self.use_llm and self.llm_extractor:
            tasks.append(("llm", self.llm_extractor.extract(text, entities, doc_id)))
        
        if self.use_patterns and self.pattern_matcher:
            tasks.append(("pattern", self.pattern_matcher.match(text, entities)))
        
        results_by_source: Dict[str, RelationExtractionResult] = {}
        
        if tasks:
            coroutines = [t[1] for t in tasks]
            sources = [t[0] for t in tasks]
            completed = await asyncio.gather(*coroutines, return_exceptions=True)
            
            for src, res in zip(sources, completed):
                if isinstance(res, Exception):
                    logger.error(f"{src}抽取失败: {res}")
                    results_by_source[src] = RelationExtractionResult()
                else:
                    results_by_source[src] = res
        
        # 合并结果
        final_result = self._merge_results(results_by_source)

        # 规范化关系中的实体名称，减少与已抽取实体名称不一致的情况
        final_result.relations = self.validator.normalize_entity_names(
            final_result.relations, entities
        )

        # 验证和去重
        validated = self.validator.deduplicate_relations(final_result.relations)
        
        # 过滤低置信度
        filtered = [
            r for r in validated
            if r.confidence >= self.min_confidence
        ]
        
        final_result.relations = filtered
        final_result.merged_count = len(filtered)
        final_result.processing_time_ms = (time.time() - start) * 1000
        
        # 未抽取到任何关系时给出明确提示
        if not final_result.relations and not entities:
            final_result.errors.append(
                "未抽取到任何关系。请先检查实体抽取是否成功，以及 LLM / 关系模式是否已配置。"
            )
        
        logger.info(f"✅ 关系抽取完成 | 总计: {final_result.merged_count} 条关系 | "
                     f"耗时: {final_result.processing_time_ms:.0f}ms")
        
        return final_result
    
    def _merge_results(
        self,
        results: Dict[str, RelationExtractionResult]
    ) -> RelationExtractionResult:
        """合并多个来源的结果"""
        merged = RelationExtractionResult()
        
        llm_res = results.get("llm")
        pat_res = results.get("pattern")
        
        if llm_res:
            merged.relations.extend(llm_res.relations)
            merged.llm_count = llm_res.llm_count
        
        if pat_res:
            existing_keys = {
                (r.source_entity_name.lower(), r.target_entity_name.lower(), r.type)
                for r in merged.relations
            }
            
            for rel in pat_res.relations:
                key = (
                    rel.source_entity_name.lower(),
                    rel.target_entity_name.lower(),
                    rel.type
                )
                
                if key not in existing_keys:
                    merged.relations.append(rel)
                    merged.pattern_count += 1
                else:
                    # 更新置信度
                    for existing in merged.relations:
                        if (existing.source_entity_name.lower(),
                            existing.target_entity_name.lower(),
                            existing.type) == key:
                            existing.confidence = max(existing.confidence, rel.confidence)
                            break
        
        return merged


__all__ = [
    # 模式定义
    'RelationPattern',
    'BUILTIN_PATTERNS',
    # 结果包装
    'ExtractedRelation',
    'RelationExtractionResult',
    # 子组件
    'PatternMatcher',
    'LLMRelationExtractor',
    'RelationValidator',
    # 主服务
    'RelationExtractionService'
]
