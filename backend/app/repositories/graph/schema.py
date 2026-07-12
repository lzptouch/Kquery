"""
知识图谱数据模型和Schema定义

定义完整的实体类型系统、关系类型系统和属性模型，
是整个知识图谱模块的数据基础。

核心内容：
1. EntityType - 实体类型枚举（22种预置类型）
2. RelationType - 关系类型枚举（25种预置类型）
3. Entity/Relation - Pydantic数据模型
4. GraphSchema - Schema管理工具类
5. Neo4j Cypher工具函数

设计原则：
- 类型安全：使用Python Enum确保类型一致性
- 可扩展：支持自定义类型
- 验证完整：Pydantic模型提供运行时验证
- 序列化友好：直接支持JSON导出

作者：KQuery Pro Team
版本：1.0.0
更新：2026-04-11

使用示例：
    from app.repositories.graph.schema import (
        Entity, Relation, EntityType, RelationType, graph_schema
    )
    
    # 创建实体
    entity = Entity(
        name="张三",
        type=EntityType.PERSON,
        properties={"age": 30, "company": "腾讯"},
        confidence=0.95
    )
    
    # 创建关系
    relation = Relation(
        source_entity_id="entity-1",
        target_entity_id="entity-2",
        type=RelationType.WORKS_FOR,
        weight=1.0
    )
"""

from __future__ import annotations

import uuid
from enum import Enum
from datetime import datetime
from typing import (
    Any, Dict, List, Optional, Set, 
    ClassVar, Union
)

try:
    from pydantic import BaseModel, Field, field_validator, model_validator
except ImportError:
    # 如果没有安装pydantic，使用dataclass作为后备方案
    from dataclasses import dataclass, field as datafield
    
    class Field:
        def __init__(self, default=None, **kwargs):
            self.default = default
    
    def model_validator(mode):
        def decorator(cls):
            return cls
        return decorator
    
    def field_validator(field_name, *args):
        def decorator(method):
            return method
        return decorator


# ============================================================
# 第一部分：实体类型定义（EntityType）
# ============================================================


class EntityType(str, Enum):
    """
    实体类型枚举
    
    定义知识图谱中所有支持的节点类型。
    分为6大类，共22种预置类型。
    
    分类：
    1. 人物类（Person系列）: 人名、职位、组织
    2. 地点类（Location系列）: 国家、城市、地址等
    3. 产品技术类（Product系列）: 产品、技术、项目、概念
    4. 时间数值类（Value系列）: 日期、时间、数值、金额、百分比
    5. 事件文档类（Event系列）: 事件、文档、关键词
    6. 自定义类型（Custom）: 用户可扩展的类型
    
    扩展方式：
        class CustomEntityTypes(EntityType):
            DISEASE = "Disease"  # 疾病
            DRUG = "Drug"       # 药物
    """
    
    # ===== 人物类 =====
    PERSON = "Person"                    # 人名：张三、马云
    ORGANIZATION = "Organization"         # 组织机构：腾讯公司、清华大学
    POSITION = "Position"                 # 职位：CEO、总经理、工程师
    
    # ===== 地点类 =====
    LOCATION = "Location"                 # 通用地点
    COUNTRY = "Country"                   # 国家：中国、美国
    CITY = "City"                         # 城市：北京、上海
    ADDRESS = "Address"                   # 详细地址
    
    # ===== 产品技术类 =====
    PRODUCT = "Product"                   # 产品：iPhone、微信
    TECHNOLOGY = "Technology"             # 技术：人工智能、区块链
    PROJECT = "Project"                   # 项目：登月计划、智慧城市
    CONCEPT = "Concept"                   # 概念：可持续发展、数字化转型
    
    # ===== 时间数值类 =====
    DATE = "Date"                         # 日期：2025-01-01
    TIME = "Time"                         # 时间点：14:30:00
    NUMBER = "Number"                     # 数值：100万、50%
    MONEY = "Money"                       # 金额：100万元、$500
    PERCENT = "Percent"                   # 百分比：85%
    
    # ===== 事件文档类 =====
    EVENT = "Event"                       # 事件：奥运会、金融危机
    DOCUMENT = "Document"                 # 文档：论文、报告
    KEYWORD = "Keyword"                   # 关键词/标签
    
    # ===== 文档元节点类（新增）=====
    PAGE = "Page"                         # 文档页码
    TABLE = "Table"                       # 文档内表格
    IMAGE = "Image"                       # 文档内嵌图片
    PARAGRAPH = "Paragraph"               # 段落/章节
    
    # ===== 其他常见类型 =====

    METHOD = "Method"                     # 方法/方法论
    INDICATOR = "Indicator"               # 指标/度量
    REGULATION = "Regulation"             # 法规/政策
    
    @classmethod
    def person_types(cls) -> Set['EntityType']:
        """获取所有人物相关类型"""
        return {cls.PERSON, cls.ORGANIZATION, cls.POSITION}
    
    @classmethod  
    def location_types(cls) -> Set['EntityType']:
        """获取所有地点相关类型"""
        return {cls.LOCATION, cls.COUNTRY, cls.CITY, cls.ADDRESS}
    
    @classmethod
    def tech_types(cls) -> Set['EntityType']:
        """获取产品技术相关类型"""
        return {
            cls.PRODUCT, cls.TECHNOLOGY, 
            cls.PROJECT, cls.CONCEPT, 
            cls.METHOD, cls.INDICATOR
        }
    
    @classmethod
    def value_types(cls) -> Set['EntityType']:
        """获取时间数值类型"""
        return {cls.DATE, cls.TIME, cls.NUMBER, cls.MONEY, cls.PERCENT}
    
    @classmethod
    def is_valid_type(cls, type_str: str) -> bool:
        """检查字符串是否为有效的实体类型"""
        try:
            cls(type_str)
            return True
        except ValueError:
            return False
    
    @classmethod
    def get_type_category(cls, entity_type: 'EntityType') -> str:
        """
        获取实体类型的分类
        
        Returns:
            'person' | 'location' | 'tech' | 'value' | 'event' | 'document_meta' | 'other'
        """
        if entity_type in cls.person_types():
            return 'person'
        elif entity_type in cls.location_types():
            return 'location'
        elif entity_type in cls.tech_types():
            return 'tech'
        elif entity_type in cls.value_types():
            return 'value'
        elif entity_type in {cls.EVENT, cls.DOCUMENT, cls.KEYWORD}:
            return 'event'
        elif entity_type in {cls.PAGE, cls.TABLE, cls.IMAGE, cls.PARAGRAPH}:
            return 'document_meta'
        else:
            return 'other'



# ============================================================
# 第二部分：关系类型定义（RelationType）
# ============================================================


class RelationType(str, Enum):
    """
    关系类型枚举
    
    定义知识图谱中所有支持的关系类型（边）。
    分为7大类，共25种预置类型。
    
    分类：
    1. 层级关系（Hierarchy）: part_of, subclass_of, contains
    2. 属性关系（Attribute）: has_attribute, has_value, located_in
    3. 社会关系（Social）: employed_by, works_for, founded, manages
    4. 关联关系（Association）: related_to, similar_to, opposed_to
    5. 时序关系（Temporal）: before, after, during
    6. 因果关系（Causal）: causes, enables, prevents
    7. 依赖关系（Dependency）: depends_on, requires, uses
    
    方向性说明：
    大多数关系都是有向的，方向很重要！
    例如：A WORKS_FOR B ≠ B WORKS_FOR A
    
    无向关系（双向语义）：
    - RELATED_TO, SIMILAR_TO, OPPOSED_TO
    """
    
    # ===== 层级关系 =====
    PART_OF = "part_of"                  # 组成部分：手是身体的一部分
    SUBCLASS_OF = "subclass_of"          # 子类继承：猫是动物的子类
    CONTAINS = "contains"                # 包含：公司包含部门
    
    # ===== 属性关系 =====
    HAS_ATTRIBUTE = "has_attribute"      # 有属性：人有年龄
    HAS_VALUE = "has_value"              # 有值：价格有数值
    LOCATED_IN = "located_in"            # 位于：腾讯位于深圳
    CREATED_AT = "created_at"            # 创建于：公司创立于某年
    
    # ===== 社会关系 =====
    EMPLOYED_BY = "employed_by"          # 受雇于：员工受雇于公司
    WORKS_FOR = "works_for"              # 工作于（同employed_by但更通用）
    FOUNDED = "founded"                  # 创立：马云创立阿里巴巴
    MANAGES = "manages"                  # 管理：经理管理团队
    COLLABORATES_WITH = "collaborates_with"  # 合作：A与B合作
    SUPPLIED_BY = "supplied_by"          # 由...供应：产品由供应商供应

    
    # ===== 关联关系 =====
    RELATED_TO = "related_to"            # 相关（通用关联）
    SIMILAR_TO = "similar_to"            # 相似：AI与机器学习相似
    OPPOSED_TO = "opposed_to"            # 对立：善与恶对立
    
    # ===== 时序关系 =====
    BEFORE = "before"                    # 在...之前：A发生在B之前
    AFTER = "after"                      # 在...之后：B在A之后发生
    DURING = "during"                    # 在...期间：C在D期间发生
    PRECEDES = "precedes"               # 先于（更正式的时序关系）
    
    # ===== 因果关系 =====
    CAUSES = "causes"                    # 导致：疫情导致经济下滑
    ENABLES = "enables"                  # 使能：云计算使能大数据分析
    PREVENTS = "prevents"                # 阻止：防火墙阻止攻击
    
    # ===== 依赖关系 =====
    DEPENDS_ON = "depends_on"            # 依赖：前端依赖后端API
    REQUIRES = "requires"                # 需要：AI需要大量数据
    USES = "uses"                        # 使用：系统使用MySQL
    
    # ===== 知识图谱特有关系 =====
    MENTIONED_IN = "mentioned_in"        # 提及于（实体出现在文档中）
    BELONGS_TO_KB = "belongs_to_kb"     # 属于知识库（实体-KB归属）
    
    # ===== 文档元关系（新增）=====
    HAS_ROW = "has_row"                  # 表格行对应实体
    DESCRIBES = "describes"              # 图片描述实体

    
    @classmethod

    def directed_relations(cls) -> Set['RelationType']:
        """获取所有有向关系"""
        undirected = {cls.RELATED_TO, cls.SIMILAR_TO, cls.OPPOSED_TO}
        return set(cls) - undirected
    
    @classmethod
    def undirected_relations(cls) -> Set['RelationType']:
        """获取无向关系（双向语义）"""
        return {cls.RELATED_TO, cls.SIMILAR_TO, cls.OPPOSED_TO}
    
    @classmethod
    def get_relation_group(cls, relation_type: 'RelationType') -> str:
        """获取关系的分组"""
        groups = [
            # (关系集合, 分组名称)
            ({cls.PART_OF, cls.SUBCLASS_OF, cls.CONTAINS}, "hierarchy"),
            ({cls.HAS_ATTRIBUTE, cls.HAS_VALUE, cls.LOCATED_IN, cls.CREATED_AT}, "attribute"),
            ({cls.EMPLOYED_BY, cls.WORKS_FOR, cls.FOUNDED, cls.MANAGES, cls.COLLABORATES_WITH, cls.SUPPLIED_BY}, "social"),
            ({cls.RELATED_TO, cls.SIMILAR_TO, cls.OPPOSED_TO}, "association"),
            ({cls.BEFORE, cls.AFTER, cls.DURING, cls.PRECEDES}, "temporal"),
            ({cls.CAUSES, cls.ENABLES, cls.PREVENTS}, "causal"),
            ({cls.DEPENDS_ON, cls.REQUIRES, cls.USES}, "dependency"),
            ({cls.MENTIONED_IN, cls.BELONGS_TO_KB, cls.CONTAINS, cls.HAS_ROW, cls.DESCRIBES}, "graph_special"),

        ]

        for rel_set, group_name in groups:
            if relation_type in rel_set:
                return group_name
        
        return "other"
    
    @classmethod
    def is_reverse_allowed(
        cls, 
        relation_type: 'RelationType',
        source_type: EntityType,
        target_type: EntityType
    ) -> bool:
        """
        检查关系方向是否合理
        
        例如：
        - Person WORKS_FOR Organization ✓ 合理
        - Organization WORKS_FOR Person ✗ 不合理
        """
        # 定义合理的类型配对规则
        valid_directions = {
            cls.EMPLOYED_BY: [(EntityType.PERSON, EntityType.ORGANIZATION)],
            cls.WORKS_FOR: [(EntityType.PERSON, EntityType.ORGANIZATION)],
            cls.FOUNDED: [
                (EntityType.PERSON, EntityType.ORGANIZATION),
                (EntityType.PERSON, EntityType.PRODUCT)
            ],
            cls.LOCATED_IN: [
                (EntityType.ORGANIZATION, EntityType.LOCATION),
                (EntityType.ORGANIZATION, EntityType.CITY),
                (EntityType.PERSON, EntityType.LOCATION)
            ]
        }
        
        if relation_type not in valid_directions:
            return True  # 未定义规则，允许任意方向
        
        allowed_pairs = valid_directions[relation_type]
        return (source_type, target_type) in allowed_pairs


# ============================================================
# 第三部分：数据模型（Pydantic模型）
# ============================================================


class Entity(BaseModel):
    """
    实体数据模型
    
    表示知识图谱中的一个节点（Node）。
    包含实体的基本属性、元数据和统计信息。
    
    Attributes:
        id: 全局唯一标识符（UUID格式）
        name: 实体显示名称（如"张三"、"人工智能"）
        type: 实体类型（来自EntityType枚举）
        properties: 自定义属性字典（灵活的键值对存储）
        aliases: 别名列表（用于消歧和同义词匹配）
        description: 实体的详细描述（可选）
        source_document_id: 来源文档ID（追踪数据来源）
        knowledge_base_id: 所属知识库ID（用于多租户隔离）
        confidence: 抽取置信度（0.0~1.0，越高越可信）
        embedding_id: 对应的向量ID（用于混合检索）
        created_at: 创建时间
        updated_at: 最后更新时间
    
    Example:
        entity = Entity(
            name="张三",
            type=EntityType.PERSON,
            properties={
                "age": 35,
                "gender": "男",
                "company": "腾讯"
            },
            aliases=["小张", "Zhang San"],
            confidence=0.98
        )
    """
    
    id: str = Field(default_factory=lambda: f"entity-{uuid.uuid4().hex[:12]}")
    name: str = Field(..., min_length=1, max_length=200, 
                       description="实体名称")
    type: EntityType = Field(..., description="实体类型")
    
    properties: Dict[str, Any] = Field(
        default_factory=dict,
        description="自定义属性"
    )
    aliases: List[str] = Field(
        default_factory=list,
        description="别名/同义词列表"
    )
    description: Optional[str] = Field(
        None, max_length=2000,
        description="实体描述"
    )
    
    # 数据溯源字段
    source_document_id: Optional[str] = Field(
        None,
        description="来源文档ID（单一文档场景，保留向后兼容）"
    )
    source_doc_ids: List[str] = Field(
        default_factory=list,
        description="来源文档ID列表（支持多文档溯源）"
    )
    knowledge_base_id: Optional[int] = Field(
        None,
        description="所属知识库ID"
    )
    
    # 质量指标
    confidence: float = Field(
        default=1.0, ge=0.0, le=1.0,
        description="置信度（0-1）"
    )
    extraction_method: Optional[str] = Field(
        None,
        description="抽取方法：rule_mapping / llm_agent / pattern_match / multi_modal"
    )
    embedding_id: Optional[str] = Field(
        None,
        description="向量数据库中的对应ID"
    )
    
    # 时间戳
    created_at: datetime = Field(
        default_factory=datetime.now,
        description="创建时间"
    )
    updated_at: datetime = Field(
        default_factory=datetime.now,
        description="更新时间"
    )
    
    # 统计字段（自动维护）
    mention_count: int = Field(
        default=0, ge=0,
        description="被提及次数"
    )
    relation_count: int = Field(
        default=0, ge=0,
        description="关联的关系数"
    )
    
    @field_validator('name')
    @classmethod
    def validate_name(cls, v: str) -> str:
        """清理实体名称"""
        v = v.strip()
        if not v:
            raise ValueError("实体名称不能为空")
        return v
    
    @field_validator('aliases')
    @classmethod
    def validate_aliases(cls, v: List[str]) -> List[str]:
        """去重并清理别名"""
        seen = set()
        cleaned = []
        for alias in v:
            alias = alias.strip()
            if alias and alias not in seen:
                cleaned.append(alias)
                seen.add(alias)
        return cleaned
    
    @field_validator('properties', mode='before')
    @classmethod
    def validate_properties(cls, v: Any) -> Dict[str, Any]:
        """
        兼容 Neo4j 中 properties 存储为 JSON 字符串的情况
        
        在 to_neo4j_dict 中 properties 被序列化为 JSON 字符串，
        从数据库读取时可能是字符串，需要反序列化。
        """
        if isinstance(v, str):
            import json as _json
            try:
                parsed = _json.loads(v)
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        if isinstance(v, dict):
            return v
        return {}
    
    @model_validator(mode='after')

    def validate_entity(self) -> 'Entity':
        """模型级别验证"""
        # 确保name不在aliases中重复
        if self.name in self.aliases:
            self.aliases = [a for a in self.aliases if a != self.name]
        
        # 同步单一来源与多来源列表，保持向后兼容
        if self.source_document_id and self.source_document_id not in self.source_doc_ids:
            self.source_doc_ids.append(self.source_document_id)
        elif self.source_doc_ids and not self.source_document_id:
            self.source_document_id = self.source_doc_ids[0]
        
        return self
    
    def to_neo4j_dict(self) -> Dict[str, Any]:
        """
        转换为Neo4j节点属性字典
        
        用于Cypher语句中的参数传递。
        自动过滤掉内部字段（如created_at在Neo4j中自动设置）。
        """
        import json as _json
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type.value,
            "properties": _json.dumps(self.properties, ensure_ascii=False) if self.properties else "{}",
            "aliases": self.aliases or [],
            "description": self.description,
            "confidence": self.confidence,
            "extraction_method": self.extraction_method,
            "mention_count": self.mention_count,
            "relation_count": self.relation_count,
            "kb_id": self.knowledge_base_id,
            "embedding_id": self.embedding_id,
            "source_doc": self.source_document_id,
            "source_doc_ids": self.source_doc_ids or [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }
    
    @classmethod
    def from_neo4j_record(cls, record: Dict[str, Any]) -> 'Entity':
        """从Neo4j记录重建实体

        支持两种传入格式：
        1. {"n": neo4j.Node}  - graph_query 中 search_entities 的封装
        2. neo4j.Node 本身   - get_entity_by_id 直接传入的节点
        """
        import json as _json

        # 统一获取底层节点数据
        if isinstance(record, dict):
            node_data = record.get('n', record)
        else:
            node_data = record

        # 将 node_data 转换为普通 dict（兼容 Neo4j Node 和 dict）
        node_dict: Dict[str, Any] = {}
        if node_data is not None:
            try:
                if isinstance(node_data, dict):
                    node_dict = node_data
                elif hasattr(node_data, 'items'):
                    # Neo4j Node 实现了 Mapping 接口
                    node_dict = dict(node_data.items())
                else:
                    node_dict = {}
            except Exception:
                node_dict = {}

        # properties 在数据库中可能是 JSON 字符串，强制反序列化
        properties = node_dict.get('properties', {})
        if isinstance(properties, str):
            try:
                properties = _json.loads(properties)
            except Exception:
                properties = {}

        # 兜底：确保 properties 一定是 dict
        if not isinstance(properties, dict):
            properties = {}

        return cls(
            id=node_dict.get('id', ''),
            name=node_dict.get('name', ''),
            type=EntityType(node_dict.get('type', 'Keyword')),
            properties=properties,
            aliases=node_dict.get('aliases', []),
            description=node_dict.get('description'),
            confidence=node_dict.get('confidence', 1.0),
            knowledge_base_id=node_dict.get('kb_id'),
            source_document_id=node_dict.get('source_doc'),
            source_doc_ids=node_dict.get('source_doc_ids', []),
            extraction_method=node_dict.get('extraction_method'),
            mention_count=node_dict.get('mention_count', 0),
            relation_count=node_dict.get('relation_count', 0)
        )


class Relation(BaseModel):
    """
    关系数据模型
    
    表示知识图谱中的一条边（Relationship/Edge）。
    连接两个实体，描述它们之间的语义关系。
    
    Attributes:
        id: 全局唯一标识符
        source_entity_id: 头实体ID（起始节点）
        target_entity_id: 尾实体ID（目标节点）
        type: 关系类型（来自RelationType枚举）
        properties: 关系的自定义属性
        weight: 关系权重（用于图算法和重要性计算）
        source_document_id: 来源文档
        confidence: 抽取置信度
        bidirectional: 是否为双向关系
    
    Example:
        relation = Relation(
            source_entity_id="person-001",
            target_entity_id="org-tencent",
            type=RelationType.WORKS_FOR,
            properties={"since": "2020", "position": "工程师"},
            weight=1.0,
            confidence=0.92
        )
    """
    
    id: str = Field(default_factory=lambda:f"rel-{uuid.uuid4().hex[:12]}")
    source_entity_id: str = Field(..., description="头实体ID")
    target_entity_id: str = Field(..., description="尾实体ID")
    type: RelationType = Field(..., description="关系类型")
    
    properties: Dict[str, Any] = Field(
        default_factory=dict,
        description="关系属性"
    )
    weight: float = Field(
        default=1.0, ge=0.0, le=10.0,
        description="关系权重"
    )
    
    # 数据溯源
    source_document_id: Optional[str] = Field(None, description="来源文档")
    knowledge_base_id: Optional[int] = Field(None, description="知识库ID")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    
    # 元数据
    bidirectional: bool = Field(default=False, description="是否双向")
    extraction_method: Optional[str] = Field(
        None,
        description="抽取方法：rule_mapping / llm_agent / pattern_match / multi_modal"
    )
    created_at: datetime = Field(default_factory=datetime.now)

    
    @model_validator(mode='after')
    def validate_relation(self) -> 'Relation':
        """自引用关系检查"""
        if self.source_entity_id == self.target_entity_id:
            raise ValueError("不允许自引用关系（source和target不能相同）")
        return self
    
    def to_neo4j_dict(self) -> Dict[str, Any]:
        """转换为Neo4j边属性"""
        import json as _json
        return {
            "id": self.id,
            "type": self.type.value,
            "properties": _json.dumps(self.properties, ensure_ascii=False) if self.properties else "{}",
            "weight": self.weight,
            "confidence": self.confidence,
            "bidirectional": self.bidirectional,
            "extraction_method": self.extraction_method,
            "source_doc": self.source_document_id,
            "kb_id": self.knowledge_base_id,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }
    
    def to_cypher_create_statement(self) -> str:

        """
        生成CREATE关系的Cypher语句片段
        
        用于批量构建时拼接完整的Cypher查询。
        """
        props_dict = self.to_neo4j_dict()
        props_str = ", ".join(
            f"{k}: ${k}" for k in props_dict.keys()
        )
        
        direction = "<-" if self.bidirectional else "->"
        
        return f"""(a)-[r:{self.type.value} {{ {props_str} }}]{direction}(b)"""


# ============================================================
# 第四部分：Schema管理工具类
# ============================================================


class GraphSchema:
    """
    知识图谱Schema管理工具
    
    提供类型系统的元信息和辅助功能：
    - 所有实体类型及其分类
    - 所有关系类型及其分组
    - 类型兼容性检查
    - 统计信息
    - 导出功能（用于前端展示）
    
    单例模式，全局共享一个实例。
    """
    
    _instance: Optional['GraphSchema'] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if not self._initialized:
            self._build_type_index()
            self._initialized = True
    
    def _build_type_index(self):
        """构建快速查找索引"""
        # 实体类型索引
        self.entity_type_map = {
            etype.value: {
                "enum": etype,
                "category": EntityType.get_type_category(etype),
                "label_zh": self._get_chinese_label(etype.value, "entity"),
                "description": self._get_type_description(etype.value, "entity")
            } 
            for etype in EntityType
        }
        
        # 关系类型索引
        self.relation_type_map = {
            rtype.value: {
                "enum": rtype,
                "group": RelationType.get_relation_group(rtype),
                "label_zh": self._get_chinese_label(rtype.value, "relation"),
                "is_directed": rtype in RelationType.directed_relations(),
                "description": self._get_type_description(rtype.value, "relation")
            }
            for rtype in RelationType
        }
    
    @staticmethod
    def _get_chinese_label(type_value: str, kind: str) -> str:
        """获取中文标签"""
        labels = {
            # 实体类型
            "Person": "人物", "Organization": "组织", "Position": "职位",
            "Location": "地点", "Country": "国家", "City": "城市",
            "Address": "地址", "Product": "产品", "Technology": "技术",
            "Project": "项目", "Concept": "概念", "Date": "日期",
            "Time": "时间", "Number": "数值", "Money": "金额",
            "Percent": "百分比", "Event": "事件", "Document": "文档",
            "Keyword": "关键词", "Method": "方法", "Indicator": "指标",
            "Regulation": "法规", "Page": "页码", "Table": "表格",
            "Image": "图片", "Paragraph": "段落",
            
            # 关系类型
            "part_of": "组成", "subclass_of": "子类", "contains": "包含",
            "has_attribute": "具有属性", "has_value": "值为",
            "located_in": "位于", "created_at": "创建于",
            "employed_by": "受雇于", "works_for": "工作于",
            "founded": "创立", "manages": "管理", "collaborates_with": "合作", "supplied_by": "供应",
            "related_to": "相关", "similar_to": "相似", "opposed_to": "对立",
            "before": "之前", "after": "之后", "during": "期间",
            "precedes": "先于", "causes": "导致", "enables": "使能",
            "prevents": "阻止", "depends_on": "依赖", "requires": "需要",
            "uses": "使用", "mentioned_in": "提及", "belongs_to_kb": "属于",
            "has_row": "表格行", "describes": "描述"

        }
        return labels.get(type_value, type_value)
    
    @staticmethod
    def _get_type_description(type_value: str, kind: str) -> str:
        """获取类型描述"""
        descriptions = {
            "Person": "人名或角色名",
            "Organization": "公司、机构、学校等组织",
            "Product": "产品、软件、服务等",
            "Technology": "技术、框架、协议等",
            "works_for": "某人受雇于某组织",
            "founded": "某人创建了某组织/项目",
            "located_in": "位于某个地理位置"
        }
        return descriptions.get(type_value, "")
    
    def get_all_entity_types(self) -> List[Dict[str, Any]]:
        """获取所有实体类型信息（供API使用）"""
        result = []
        for value, info in sorted(self.entity_type_map.items()):
            result.append({
                "value": value,
                "label": info["label_zh"],
                "category": info["category"]
            })
        return result
    
    def get_all_relation_types(self) -> List[Dict[str, Any]]:
        """获取所有关系类型信息（供API使用）"""
        result = []
        for value, info in sorted(self.relation_type_map.items()):
            result.append({
                "value": value,
                "label": info["label_zh"],
                "group": info["group"],
                "is_directed": info["is_directed"]
            })
        return result
    
    def validate_entity_type(self, type_str: str) -> bool:
        """验证实体类型字符串"""
        return type_str in self.entity_type_map
    
    def validate_relation_type(self, type_str: str) -> bool:
        """验证关系类型字符串"""
        return type_str in self.relation_type_map
    
    def suggest_relations_for_entities(
        self,
        source_type: EntityType,
        target_type: EntityType
    ) -> List[Dict[str, str]]:
        """
        根据实体类型建议可能的关系
        
        基于预定义的启发式规则推荐最合适的关系类型。
        """
        suggestions = []
        
        # 人物 -> 组织
        if source_type == EntityType.PERSON and target_type == EntityType.ORGANIZATION:
            suggestions.extend([
                {"type": RelationType.WORKS_FOR.value, "desc": "工作于"},
                {"type": RelationType.EMPLOYED_BY.value, "desc": "受雇于"},
                {"type": RelationType.FOUNDED.value, "desc": "创立"}
            ])
        
        # 组织 -> 地点
        elif source_type == EntityType.ORGANIZATION and target_type in EntityType.location_types():
            suggestions.extend([
                {"type": RelationType.LOCATED_IN.value, "desc": "位于"}
            ])
        
        # 人物 -> 人物
        elif source_type == EntityType.PERSON and target_type == EntityType.PERSON:
            suggestions.extend([
                {"type": RelationType.COLLABORATES_WITH.value, "desc": "合作"},
                {"type": RelationType.MANAGES.value, "desc": "管理"}
            ])
        
        # 默认建议
        if not suggestions:
            suggestions.append({"type": RelationType.RELATED_TO.value, "desc": "相关"})
        
        return suggestions
    
    def get_statistics(self) -> Dict[str, int]:
        """获取Schema统计信息"""
        return {
            "total_entity_types": len(EntityType),
            "total_relation_types": len(RelationType),
            "entity_categories": 6,  # person/location/tech/value/event/other
            "relation_groups": 7      # hierarchy/attribute/social/association/temporal/causal/dependency
        }


# 全局单例实例
graph_schema = GraphSchema()

# 便捷导出
__all__ = [
    # 枚举
    'EntityType',
    'RelationType',
    # 数据模型
    'Entity',
    'Relation',
    # 工具类
    'GraphSchema',
    'graph_schema'
]
