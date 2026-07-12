"""
LLM Graph Agent - 大模型驱动的统一知识图谱抽取 Agent

将命名实体识别(NER)和关系抽取(RE)合并为单个 LLM Agent 调用，
通过精心设计的提示词(Prompt Engineering)让大模型一次性完成：
1. 实体识别与分类
2. 关系抽取与验证
3. 实体消歧与归一化

相比传统的 pipeline 方式（先NER再RE），Agent 方式的优势：
- 全局上下文理解：模型同时看到所有实体和潜在关系，减少错误传播
- 一致性更好：实体类型和关系类型在单次推理中保持一致
- 效率更高：减少一次 LLM API 调用
- 复杂推理能力：能识别隐含关系和跨句关系

架构：
    文本输入
        ↓
    ┌─────────────────┐
    │  Prompt Builder  │ ← 构建结构化提示词
    └────────┬────────┘
             ↓
    ┌─────────────────┐
    │  LLM Agent Call  │ ← 单次调用获取结构化输出
    └────────┬────────┘
             ↓
    ┌─────────────────┐
    │  Output Parser   │ ← 解析 JSON，验证，去重
    └────────┬────────┘
             ↓
    实体列表 + 关系列表

作者：KQuery Pro Team
版本：2.0.0
更新：2026-05-07
"""

from __future__ import annotations

import json
import re
import logging
import asyncio
from typing import Any, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from enum import Enum

try:
    from app.repositories.graph.schema import (
        Entity, Relation, EntityType, RelationType
    )
    from app.core.logger import get_logger
except ImportError:
    class Entity:
        pass
    class Relation:
        pass
    class EntityType:
        pass
    class RelationType:
        pass

    def get_logger(name):
        return logging.getLogger(name)

logger = get_logger(__name__)


# ============================================================
# 数据模型
# ============================================================


@dataclass
class AgentExtractedEntity:
    """Agent 抽取的实体"""
    name: str
    type: str
    confidence: float = 0.8
    description: Optional[str] = None
    aliases: List[str] = field(default_factory=list)
    properties: Dict[str, Any] = field(default_factory=dict)
    evidence: Optional[str] = None  # 支撑文本片段

    def to_schema_entity(self, doc_id: Optional[str] = None) -> Entity:
        """转换为标准 Entity 模型"""
        try:
            entity_type = EntityType(self.type)
        except ValueError:
            entity_type = EntityType.KEYWORD
        return Entity(
            name=self.name,
            type=entity_type,
            confidence=self.confidence,
            description=self.description or "",
            aliases=self.aliases,
            properties=self.properties,
            source_document_id=doc_id,
        )


@dataclass
class AgentExtractedRelation:
    """Agent 抽取的关系"""
    source: str  # 头实体名称
    target: str  # 尾实体名称
    relation: str  # 关系类型
    confidence: float = 0.8
    evidence: Optional[str] = None  # 支撑文本片段
    properties: Dict[str, Any] = field(default_factory=dict)
    bidirectional: bool = False

    def to_schema_relation(
        self,
        source_id: str,
        target_id: str,
        doc_id: Optional[str] = None
    ) -> Relation:
        """转换为标准 Relation 模型"""
        try:
            rel_type = RelationType(self.relation)
        except ValueError:
            rel_type = RelationType.RELATED_TO
        return Relation(
            source_entity_id=source_id,
            target_entity_id=target_id,
            type=rel_type,
            confidence=self.confidence,
            properties={"evidence": self.evidence or "", **self.properties},
            source_document_id=doc_id,
            bidirectional=self.bidirectional,
        )


@dataclass
class GraphExtractionResult:
    """Agent 抽取的完整结果"""
    entities: List[AgentExtractedEntity] = field(default_factory=list)
    relations: List[AgentExtractedRelation] = field(default_factory=list)
    processing_time_ms: float = 0.0
    raw_response: Optional[str] = None

    @property
    def entity_count(self) -> int:
        return len(self.entities)

    @property
    def relation_count(self) -> int:
        return len(self.relations)


# ============================================================
# Prompt 构建器
# ============================================================


class GraphExtractionPromptBuilder:
    """构建用于知识图谱抽取的结构化提示词"""

    SYSTEM_PROMPT = """你是一个专业的知识图谱构建专家 Agent。你的任务是从给定的文本中抽取出结构化的知识图谱数据（实体和关系）。

## 核心能力
1. **命名实体识别(NER)**: 识别文本中的重要实体并分类
2. **关系抽取(RE)**: 识别实体之间的语义关系
3. **实体消歧**: 当同一实体有不同表述时，统一为标准名称
4. **隐含关系推理**: 识别文本中隐含但未直接陈述的关系

## 实体类型定义

请从以下类型中选择最合适的类型：

**人物与组织**:
- Person: 人名（如：张三、马云、爱因斯坦）
- Organization: 组织机构（如：腾讯公司、清华大学、国务院）
- Position: 职位头衔（如：CEO、总经理、教授、工程师）

**地点**:
- Country: 国家（如：中国、美国、日本）
- City: 城市（如：北京、上海、纽约）
- Location: 其他地点

**产品与技术**:
- Product: 产品（如：iPhone、微信、Windows）
- Technology: 技术术语（如：人工智能、区块链、机器学习）
- Project: 项目（如：阿波罗计划、智慧城市）
- Concept: 抽象概念（如：数字化转型、可持续发展）

**时间与数值**:
- Date: 日期（如：2025年3月、去年）
- Money: 金额（如：100亿元、500万美元）
- Percent: 百分比（如：增长30%）
- Number: 其他数值

**其他**:
- Event: 事件（如：奥运会、金融危机）
- Document: 文档（如：报告、论文、合同）
- Keyword: 关键词/标签（其他重要术语）

## 关系类型定义

**层级关系**:
- part_of: A 是 B 的一部分（如：引擎 part_of 汽车）
- subclass_of: A 是 B 的子类（如：猫 subclass_of 动物）
- contains: A 包含 B（如：公司 contains 部门）

**社会关系**:
- employed_by: A 受雇于 B（如：张三 employed_by 腾讯）
- works_for: A 为 B 工作（同 employed_by）
- founded: A 创立了 B（如：马云 founded 阿里巴巴）
- manages: A 管理 B（如：经理 manages 团队）
- collaborates_with: A 与 B 合作

**属性关系**:
- has_attribute: A 具有属性 B
- located_in: A 位于 B（如：公司 located_in 北京）
- created_at: A 创建于 B（时间）

**依赖与因果**:
- depends_on: A 依赖 B
- uses: A 使用 B（如：系统 uses MySQL）
- requires: A 需要 B
- causes: A 导致 B

**通用关系**:
- related_to: A 与 B 相关（当没有更具体的关系时使用）
- similar_to: A 与 B 相似
- before: A 在 B 之前发生
- after: A 在 B 之后发生

## 抽取规则

1. **完整性**: 不要遗漏重要实体，宁可多抽也不要漏抽
2. **精确性**: 实体名称尽量使用文本中的原始表述（除非有明显的别名需要统一）
3. **关系质量**: 只抽取文本中有明确证据支持的关系，不要猜测
4. **方向性**: 关系是有方向的！source → target 的方向很重要
5. **置信度**: 基于文本明确程度给出 0.0-1.0 的置信度
6. **证据**: 每个关系必须提供支撑它的原文片段（evidence）
7. **隐含关系**: 如果文本暗示了某种关系但没有直接说明，可以抽取，但置信度应低于 0.7
8. **自反检查**: 不要创建实体指向自己的关系

## 输出格式

你必须严格按以下 JSON 格式返回，不要添加任何其他文字：

```json
{
  "entities": [
    {
      "name": "实体名称",
      "type": "实体类型英文",
      "confidence": 0.95,
      "description": "实体的简要描述（可选）",
      "aliases": ["别名1", "别名2"],
      "evidence": "支撑该实体识别的原文片段"
    }
  ],
  "relations": [
    {
      "source": "头实体名称",
      "target": "尾实体名称",
      "relation": "关系类型英文",
      "confidence": 0.92,
      "evidence": "支撑该关系的原文片段",
      "bidirectional": false
    }
  ]
}
```

## 注意事项
- 只返回 JSON，不要包含 markdown 代码块标记之外的任何解释文字
- 确保 JSON 格式完全正确（无尾随逗号、引号匹配等）
- 实体名称应该简洁（不超过 50 字符）
- 关系中的 source 和 target 必须在 entities 列表中存在
- confidence 值必须是 0.0 到 1.0 之间的数字"""

    FEW_SHOT_EXAMPLE = '''
## 示例

输入文本：
"2024年5月，阿里巴巴集团的CEO吴泳铭宣布，公司将投入500亿元人民币用于人工智能基础设施的建设。该项目的核心技术负责人是张建锋博士，他此前曾担任阿里云智能总裁。阿里巴巴总部设在杭州。"

期望输出：
```json
{
  "entities": [
    {"name": "2024年5月", "type": "Date", "confidence": 0.99, "evidence": "2024年5月"},
    {"name": "阿里巴巴集团", "type": "Organization", "confidence": 0.98, "aliases": ["阿里"], "evidence": "阿里巴巴集团"},
    {"name": "吴泳铭", "type": "Person", "confidence": 0.97, "evidence": "CEO吴泳铭"},
    {"name": "500亿元人民币", "type": "Money", "confidence": 0.96, "evidence": "500亿元人民币"},
    {"name": "人工智能", "type": "Technology", "confidence": 0.95, "aliases": ["AI"], "evidence": "人工智能"},
    {"name": "张建锋", "type": "Person", "confidence": 0.96, "aliases": ["张建锋博士"], "evidence": "张建锋博士"},
    {"name": "阿里云智能", "type": "Organization", "confidence": 0.93, "evidence": "阿里云智能"},
    {"name": "杭州", "type": "City", "confidence": 0.95, "evidence": "杭州"}
  ],
  "relations": [
    {"source": "吴泳铭", "target": "阿里巴巴集团", "relation": "employed_by", "confidence": 0.95, "evidence": "阿里巴巴集团的CEO吴泳铭", "bidirectional": false},
    {"source": "阿里巴巴集团", "target": "人工智能", "relation": "uses", "confidence": 0.90, "evidence": "投入500亿元用于人工智能基础设施的建设", "bidirectional": false},
    {"source": "张建锋", "target": "人工智能", "relation": "manages", "confidence": 0.88, "evidence": "该项目的核心技术负责人是张建锋博士", "bidirectional": false},
    {"source": "张建锋", "target": "阿里云智能", "relation": "employed_by", "confidence": 0.85, "evidence": "曾担任阿里云智能总裁", "bidirectional": false},
    {"source": "阿里巴巴集团", "target": "杭州", "relation": "located_in", "confidence": 0.94, "evidence": "阿里巴巴总部设在杭州", "bidirectional": false}
  ]
}
```'''

    @classmethod
    def build_messages(
        cls,
        text: str,
        context: Optional[str] = None,
        entity_types: Optional[List[str]] = None,
        relation_types: Optional[List[str]] = None,
    ) -> List[Dict[str, str]]:
        """构建发送给 LLM 的消息列表"""
        messages = [{"role": "system", "content": cls.SYSTEM_PROMPT}]

        # 添加 few-shot 示例
        messages.append({"role": "user", "content": cls.FEW_SHOT_EXAMPLE})
        messages.append({
            "role": "assistant",
            "content": "我已理解任务要求。请提供待分析的文本。"
        })

        # 构建用户输入
        user_parts = []
        if context:
            user_parts.append(f"【上下文信息】\n{context}\n")

        user_parts.append(f"【待分析文本】\n{text}\n")

        # 可选：限定类型范围
        if entity_types:
            user_parts.append(
                f"【限定实体类型】只抽取以下类型的实体: {', '.join(entity_types)}\n"
            )
        if relation_types:
            user_parts.append(
                f"【限定关系类型】只抽取以下类型的关系: {', '.join(relation_types)}\n"
            )

        user_parts.append(
            "请从上述文本中抽取所有实体和关系，严格按照 JSON 格式返回。"
        )

        messages.append({"role": "user", "content": "\n".join(user_parts)})
        return messages


# ============================================================
# 输出解析器
# ============================================================


class GraphExtractionParser:
    """解析 LLM 返回的 JSON，转换为结构化数据"""

    @staticmethod
    def parse(raw_text: str) -> GraphExtractionResult:
        """解析 LLM 原始输出"""
        result = GraphExtractionResult(raw_response=raw_text)

        # 提取 JSON
        json_str = GraphExtractionParser._extract_json(raw_text)
        if not json_str:
            logger.warning("未能从 LLM 响应中提取 JSON")
            return result

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON 解析失败: {e}，尝试修复")
            data = GraphExtractionParser._try_repair_json(json_str)

        if not data:
            return result

        # 解析实体
        entities_data = data.get("entities", [])
        seen_entities: Dict[str, AgentExtractedEntity] = {}

        for ent_data in entities_data:
            try:
                entity = GraphExtractionParser._parse_entity(ent_data)
                if entity and entity.name:
                    # 去重：同名实体保留置信度最高的
                    key = f"{entity.name.lower()}:{entity.type.lower()}"
                    if key in seen_entities:
                        if entity.confidence > seen_entities[key].confidence:
                            seen_entities[key] = entity
                    else:
                        seen_entities[key] = entity
            except Exception as e:
                logger.debug(f"解析实体失败: {e}, 数据: {ent_data}")

        result.entities = list(seen_entities.values())

        # 解析关系
        relations_data = data.get("relations", [])
        entity_names = {e.name.lower() for e in result.entities}
        seen_relations: Dict[str, AgentExtractedRelation] = {}

        for rel_data in relations_data:
            try:
                relation = GraphExtractionParser._parse_relation(rel_data)
                if not relation:
                    continue

                # 验证 source 和 target 是否在实体列表中
                src_lower = relation.source.lower()
                tgt_lower = relation.target.lower()

                # 尝试匹配实体（允许部分匹配）
                matched_src = GraphExtractionParser._match_entity_name(
                    relation.source, entity_names
                )
                matched_tgt = GraphExtractionParser._match_entity_name(
                    relation.target, entity_names
                )

                if matched_src:
                    relation.source = matched_src
                if matched_tgt:
                    relation.target = matched_tgt

                # 跳过自反关系
                if relation.source.lower() == relation.target.lower():
                    continue

                # 去重
                key = f"{relation.source.lower()}->{relation.relation.lower()}->{relation.target.lower()}"
                if key in seen_relations:
                    if relation.confidence > seen_relations[key].confidence:
                        seen_relations[key] = relation
                else:
                    seen_relations[key] = relation

            except Exception as e:
                logger.debug(f"解析关系失败: {e}, 数据: {rel_data}")

        result.relations = list(seen_relations.values())
        return result

    @staticmethod
    def _extract_json(text: str) -> Optional[str]:
        """从文本中提取 JSON 部分"""
        text = text.strip()

        # 尝试找 ```json ... ``` 代码块
        json_block_match = re.search(
            r'```(?:json)?\s*(\{.*?\})\s*```',
            text,
            re.DOTALL
        )
        if json_block_match:
            return json_block_match.group(1)

        # 尝试找最外层的大括号
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1 and end > start:
            return text[start:end + 1]

        return None

    @staticmethod
    def _try_repair_json(json_str: str) -> Optional[Dict]:
        """尝试修复常见 JSON 错误"""
        repairs = [
            # 移除尾随逗号
            lambda s: re.sub(r',\s*([}\]])', r'\1', s),
            # 修复单引号
            lambda s: s.replace("'", '"'),
            # 修复无引号的键（简单情况）
            lambda s: re.sub(r'(\w+):\s*"', r'"\1": "', s),
        ]

        for repair in repairs:
            try:
                fixed = repair(json_str)
                return json.loads(fixed)
            except:
                continue

        return None

    @staticmethod
    def _parse_entity(data: Dict[str, Any]) -> Optional[AgentExtractedEntity]:
        """解析单个实体"""
        name = data.get("name", "").strip()
        if not name or len(name) > 200:
            return None

        return AgentExtractedEntity(
            name=name,
            type=data.get("type", "Keyword").strip(),
            confidence=float(data.get("confidence", 0.8)),
            description=data.get("description"),
            aliases=data.get("aliases", []) or [],
            properties=data.get("properties", {}) or {},
            evidence=data.get("evidence"),
        )

    @staticmethod
    def _parse_relation(data: Dict[str, Any]) -> Optional[AgentExtractedRelation]:
        """解析单个关系"""
        source = data.get("source", "").strip()
        target = data.get("target", "").strip()
        relation = data.get("relation", "").strip()

        if not source or not target or not relation:
            return None

        return AgentExtractedRelation(
            source=source,
            target=target,
            relation=relation,
            confidence=float(data.get("confidence", 0.8)),
            evidence=data.get("evidence"),
            properties=data.get("properties", {}) or {},
            bidirectional=data.get("bidirectional", False),
        )

    @staticmethod
    def _match_entity_name(name: str, entity_names: Set[str]) -> Optional[str]:
        """在实体名称集合中匹配最接近的名称"""
        name_lower = name.lower()

        # 精确匹配
        for en in entity_names:
            if en == name_lower:
                # 返回原始大小写（从某个实体名称中）
                return name

        # 包含匹配
        for en in entity_names:
            if name_lower in en or en in name_lower:
                return name

        return None


# ============================================================
# LLM Graph Agent 主类
# ============================================================


class LLMGraphAgent:
    """大模型驱动的统一知识图谱抽取 Agent

    使用单次 LLM 调用完成实体识别和关系抽取，
    通过精心设计的提示词实现高质量的图谱构建。

    使用示例:
        agent = LLMGraphAgent(model_name="qwen-plus")
        result = await agent.extract(
            text="阿里巴巴的CEO吴泳铭宣布...",
            doc_id="doc-001"
        )
        print(f"抽取了 {result.entity_count} 个实体, {result.relation_count} 条关系")
    """

    def __init__(
        self,
        model_name: str = "qwen-plus",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.1,
        max_text_length: int = 4000,
    ):
        """
        Args:
            model_name: LLM 模型名称
            api_key: API 密钥（默认从环境变量读取）
            base_url: API 基础 URL
            max_tokens: 最大生成 token 数
            temperature: 温度参数（低温度确保稳定输出）
            max_text_length: 单次处理的最大文本长度
        """
        self.model_name = model_name
        self.api_key = api_key
        self.base_url = base_url
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_text_length = max_text_length

        self.client = None
        self.client_type = None
        self._initialize_client()

        self.prompt_builder = GraphExtractionPromptBuilder()
        self.parser = GraphExtractionParser()

        logger.info(f"🤖 LLM Graph Agent 初始化 | 模型: {model_name}")

    def _initialize_client(self):
        """初始化 LLM 客户端"""
        import os

        api_key = self.api_key or os.getenv("DASHSCOPE_API_KEY") or os.getenv("OPENAI_API_KEY")
        base_url = self.base_url or os.getenv("OPENAI_BASE_URL")

        if not api_key:
            logger.warning("⚠️ 未配置 API 密钥，LLM Graph Agent 将不可用")
            return

        if base_url and "dashscope" in base_url.lower():
            try:
                import dashscope
                dashscope.api_key = api_key
                self.client_type = "dashscope"
            except ImportError:
                logger.warning("dashscope 未安装")
        else:
            try:
                from openai import AsyncOpenAI
                self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
                self.client_type = "openai"
            except ImportError:
                logger.warning("openai 未安装")

    async def extract(
        self,
        text: str,
        doc_id: Optional[str] = None,
        context: Optional[str] = None,
    ) -> GraphExtractionResult:
        """从文本中抽取实体和关系（单次 Agent 调用）

        Args:
            text: 待分析的文本
            doc_id: 来源文档 ID
            context: 额外上下文（如文档标题、摘要）

        Returns:
            GraphExtractionResult: 包含实体列表和关系列表
        """
        import time
        start_time = time.time()

        if not self.client and self.client_type != "dashscope":
            logger.error("❌ LLM 客户端未初始化，无法执行抽取")
            return GraphExtractionResult()

        # 截断过长的文本
        if len(text) > self.max_text_length:
            text = text[:self.max_text_length] + "..."

        # 构建提示词
        messages = self.prompt_builder.build_messages(text, context)

        try:
            # 调用 LLM
            raw_response = await self._call_llm(messages)

            # 解析结果
            result = self.parser.parse(raw_response)
            result.processing_time_ms = (time.time() - start_time) * 1000

            # 补充 doc_id 到实体和关系
            for entity in result.entities:
                if doc_id and hasattr(entity, 'to_schema_entity'):
                    pass  # doc_id 在 to_schema_entity 时传入

            logger.info(
                f"🤖 Agent 抽取完成 | 实体: {result.entity_count}, "
                f"关系: {result.relation_count}, "
                f"耗时: {result.processing_time_ms:.0f}ms"
            )

            return result

        except Exception as e:
            logger.error(f"❌ Agent 抽取失败: {e}", exc_info=True)
            return GraphExtractionResult(
                processing_time_ms=(time.time() - start_time) * 1000
            )

    async def extract_batch(
        self,
        chunks: List[str],
        doc_id: Optional[str] = None,
        context: Optional[str] = None,
        concurrency: int = 3,
    ) -> GraphExtractionResult:
        """批量抽取（长文档分块处理）

        对长文档先分块，再并发抽取，最后合并去重。

        Args:
            chunks: 文本块列表
            doc_id: 文档 ID
            context: 上下文
            concurrency: 并发数

        Returns:
            合并后的抽取结果
        """
        logger.info(f"📦 开始批量 Agent 抽取 | 块数: {len(chunks)}, 并发: {concurrency}")

        semaphore = asyncio.Semaphore(concurrency)

        async def process_chunk(chunk: str, idx: int) -> GraphExtractionResult:
            async with semaphore:
                chunk_context = f"{context or ''}\n[这是文档的第 {idx + 1}/{len(chunks)} 段]"
                return await self.extract(chunk, doc_id, chunk_context)

        tasks = [process_chunk(chunk, i) for i, chunk in enumerate(chunks)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 合并所有结果
        merged = GraphExtractionResult()
        all_entities: Dict[str, AgentExtractedEntity] = {}
        all_relations: Dict[str, AgentExtractedRelation] = {}

        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"分块处理失败: {result}")
                continue

            # 合并实体
            for entity in result.entities:
                key = f"{entity.name.lower()}:{entity.type.lower()}"
                if key in all_entities:
                    if entity.confidence > all_entities[key].confidence:
                        all_entities[key] = entity
                else:
                    all_entities[key] = entity

            # 合并关系
            for relation in result.relations:
                key = (
                    f"{relation.source.lower()}->"
                    f"{relation.relation.lower()}->"
                    f"{relation.target.lower()}"
                )
                if key in all_relations:
                    if relation.confidence > all_relations[key].confidence:
                        all_relations[key] = relation
                else:
                    all_relations[key] = relation

        merged.entities = list(all_entities.values())
        merged.relations = list(all_relations.values())

        logger.info(
            f"📦 批量抽取完成 | 总实体: {merged.entity_count}, "
            f"总关系: {merged.relation_count}"
        )

        return merged

    async def _call_llm(self, messages: List[Dict[str, str]]) -> str:
        """调用 LLM API（带重试和 Mock 降级）。"""
        from app.core.llm_client import call_openai_chat, call_dashscope, is_llm_configured

        if self.client_type == "dashscope":
            # 检查 DashScope 是否配置
            if not is_llm_configured("dashscope"):
                logger.warning("DashScope API 未配置，使用 Mock 数据")
                from app.core.mock_data import get_mock_data
                mock_result = get_mock_data("graph_extraction", text=str(messages))
                return mock_result.get("raw_response", '{"entities": [], "relations": []}')

            return await call_dashscope(
                model=self.model_name,
                messages=messages,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                mock_module="graph_extraction",
            )
        else:
            # 检查 OpenAI 兼容接口是否配置
            if not is_llm_configured("agent"):
                logger.warning("Agent API 未配置，使用 Mock 数据")
                from app.core.mock_data import get_mock_data
                mock_result = get_mock_data("graph_extraction", text=str(messages))
                return mock_result.get("raw_response", '{"entities": [], "relations": []}')

            return await call_openai_chat(
                model=self.model_name,
                messages=messages,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                api_key=self.api_key,
                base_url=self.base_url,
                mock_module="graph_extraction",
            )


# ============================================================
# 与原有系统的适配器
# ============================================================


class AgentEntityExtractionAdapter:
    """适配器：将 LLMGraphAgent 的结果转换为原有实体抽取接口"""

    def __init__(self, agent: Optional[LLMGraphAgent] = None):
        self.agent = agent or LLMGraphAgent()

    async def extract_entities(
        self,
        text: str,
        doc_id: Optional[str] = None,
        context: Optional[str] = None,
        **kwargs
    ) -> "ExtractionResult":
        """兼容原有 EntityExtractionService 接口"""
        from app.services.graph.entity_extraction import (
            ExtractionResult, ExtractedEntity
        )

        agent_result = await self.agent.extract(text, doc_id, context)

        result = ExtractionResult(text_length=len(text))
        result.llm_count = len(agent_result.entities)
        result.merged_count = len(agent_result.entities)

        for agent_entity in agent_result.entities:
            extracted = ExtractedEntity(
                name=agent_entity.name,
                type=EntityType(agent_entity.type)
                if EntityType.is_valid_type(agent_entity.type)
                else EntityType.KEYWORD,
                confidence=agent_entity.confidence,
                source="llm_agent",
                properties={
                    "description": agent_entity.description or "",
                    "aliases": agent_entity.aliases,
                    "evidence": agent_entity.evidence or "",
                    **agent_entity.properties,
                },
            )
            result.entities.append(extracted)

        result.processing_time_ms = agent_result.processing_time_ms
        return result


class AgentRelationExtractionAdapter:
    """适配器：将 LLMGraphAgent 的结果转换为原有关系抽取接口"""

    def __init__(self, agent: Optional[LLMGraphAgent] = None):
        self.agent = agent or LLMGraphAgent()

    async def extract_relations(
        self,
        text: str,
        entities: List[Any],
        doc_id: Optional[str] = None,
        **kwargs
    ) -> "RelationExtractionResult":
        """兼容原有 RelationExtractionService 接口"""
        from app.services.graph.relation_extraction import (
            RelationExtractionResult, ExtractedRelation
        )

        # Agent 已经同时抽取了实体和关系
        # 这里我们需要重新运行 Agent 以获取关系
        # 或者复用之前的结果（如果已缓存）
        agent_result = await self.agent.extract(text, doc_id)

        result = RelationExtractionResult(entity_count=len(entities))
        result.llm_count = len(agent_result.relations)
        result.merged_count = len(agent_result.relations)

        for agent_rel in agent_result.relations:
            extracted = ExtractedRelation(
                source_entity_name=agent_rel.source,
                target_entity_name=agent_rel.target,
                type=RelationType(agent_rel.relation)
                if any(r.value == agent_rel.relation for r in RelationType)
                else RelationType.RELATED_TO,
                confidence=agent_rel.confidence,
                source="llm_agent",
                evidence_text=agent_rel.evidence,
                properties=agent_rel.properties,
            )
            result.relations.append(extracted)

        result.processing_time_ms = agent_result.processing_time_ms
        return result


__all__ = [
    "AgentExtractedEntity",
    "AgentExtractedRelation",
    "GraphExtractionResult",
    "GraphExtractionPromptBuilder",
    "GraphExtractionParser",
    "LLMGraphAgent",
    "AgentEntityExtractionAdapter",
    "AgentRelationExtractionAdapter",
]
