"""表头映射引擎 (Table Mapper)

将结构化表格（Excel/CSV/数据库导出表）的表头映射为知识图谱实体类型和关系类型，
并基于行数据直接生成三元组。全程零 LLM 调用、零幻觉、毫秒级完成。

核心能力：
1. 表头 -> 实体类型映射（如"产品名称"->Product）
2. 表头 -> 关系类型映射（如"供应商"->SUPPLIED_BY）
3. 数值/日期列 -> 实体属性（如"价格"->properties.price）
4. 多表外键关联检测（如"供应商ID"列跨表关联）
5. 用户可覆盖的自定义映射规则

作者：KQuery Pro Team
版本：1.0.0
更新：2026-07-05
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from app.services.document_models import NormalizedTable
from app.services.graph.entity_extraction import ExtractedEntity
from app.services.graph.relation_extraction import ExtractedRelation
from app.repositories.graph.schema import EntityType, RelationType
from app.core.logger import get_logger

logger = get_logger(__name__)


# ============================================================
# 默认映射规则
# ============================================================

DEFAULT_ENTITY_HEADER_PATTERNS: List[Tuple[str, EntityType]] = [
    # 人物
    (r".*(?:姓名|名字|负责人|联系人|经办人|操作人|创建人|修改人|员工|经理|主管|总监|CEO|CTO|CFO).*", EntityType.PERSON),
    (r".*(?:人员|个人|用户|账号|工号).*", EntityType.PERSON),

    # 组织
    (r".*(?:公司|企业|集团|组织|机构|单位|供应商|客户|合作方|厂商|银行|学校|医院|部门|团队).*", EntityType.ORGANIZATION),

    # 产品
    (r".*(?:产品|商品|物料|设备|型号|SKU|货号|名称).*", EntityType.PRODUCT),

    # 技术
    (r".*(?:技术|框架|平台|系统|算法|协议|语言|引擎|数据库|中间件).*", EntityType.TECHNOLOGY),

    # 项目
    (r".*(?:项目|课题|计划|方案|工程|任务).*", EntityType.PROJECT),

    # 地点
    (r".*(?:地点|地址|城市|省份|国家|地区|区域|位置).*", EntityType.LOCATION),

    # 事件
    (r".*(?:事件|活动|会议|发布会|事故|故障|案例).*", EntityType.EVENT),

    # 文档
    (r".*(?:文档|合同|订单|报告|单据|证书|编号|单号|发票).*", EntityType.DOCUMENT),
]

DEFAULT_RELATION_HEADER_PATTERNS: List[Tuple[str, RelationType, bool]] = [
    # 供货/供应关系
    (r".*(?:供应商|供货方|供应方|厂商|制造商|生产商|来源).*", RelationType.SUPPLIED_BY, False),
    # 负责人/管理人
    (r".*(?:负责人|管理人|主管|经理|所有者|所有者|创建人|修改人).*", RelationType.MANAGES, False),
    # 所属/包含关系
    (r".*(?:所属|属于|归属|项目|部门|类别|分类|分组).*", RelationType.PART_OF, False),
    # 位置
    (r".*(?:地点|地址|城市|省份|国家|地区|位置).*", RelationType.LOCATED_IN, False),
    # 合作
    (r".*(?:合作方|合作伙伴|合作单位|乙方|甲方).*", RelationType.COLLABORATES_WITH, True),
    # 使用/依赖
    (r".*(?:使用|采用|依赖|基于|运行环境|技术栈).*", RelationType.USES, False),
]

DEFAULT_PROPERTY_HEADER_PATTERNS: List[Tuple[str, str, EntityType]] = [
    # 数值属性映射到实体属性字段名
    (r".*(?:价格|金额|成本|费用|单价|总价|预算|收入|支出).*", "price", EntityType.MONEY),
    (r".*(?:数量|库存|销量|人数|次数|时长|规模).*", "quantity", EntityType.NUMBER),
    (r".*(?:日期|时间|创建日期|更新日期|截止日期|上市日期|签约日期).*", "date", EntityType.DATE),
    (r".*(?:比例|百分比|完成率|增长率|占比|折扣).*", "percent", EntityType.PERCENT),
    (r".*(?:状态|阶段|类型|等级|优先级|级别).*", "status", EntityType.KEYWORD),
    (r".*(?:描述|备注|说明|摘要|详情|介绍).*", "description", EntityType.KEYWORD),
]


# 常见外键列名模式（用于跨表关联检测）
FOREIGN_KEY_PATTERNS: List[str] = [
    r".*(?:ID|编号|编码|No|序号|代码)$",
    r".*(?:_id|_code|_no)$",
]


@dataclass
class TableMappingConfig:
    """表头映射配置
    
    用户可自定义或扩展默认映射规则。
    """
    entity_patterns: List[Tuple[str, EntityType]] = field(
        default_factory=lambda: list(DEFAULT_ENTITY_HEADER_PATTERNS)
    )
    relation_patterns: List[Tuple[str, RelationType, bool]] = field(
        default_factory=lambda: list(DEFAULT_RELATION_HEADER_PATTERNS)
    )
    property_patterns: List[Tuple[str, str, EntityType]] = field(
        default_factory=lambda: list(DEFAULT_PROPERTY_HEADER_PATTERNS)
    )
    foreign_key_patterns: List[str] = field(
        default_factory=lambda: list(FOREIGN_KEY_PATTERNS)
    )
    default_entity_type: EntityType = EntityType.KEYWORD
    skip_empty_value: bool = True  # 是否跳过空值单元格
    min_entity_name_length: int = 1


@dataclass
class MappedEntityColumn:
    """映射后的实体列信息"""
    column_name: str
    entity_type: EntityType
    is_relation_source: bool = True  # 该列是否作为关系头实体


@dataclass
class MappedRelationColumn:
    """映射后的关系列信息"""
    column_name: str
    relation_type: RelationType
    bidirectional: bool = False
    target_column: Optional[str] = None  # 关系的尾实体所在列（默认取主实体列）


@dataclass
class MappedPropertyColumn:
    """映射后的属性列信息"""
    column_name: str
    property_name: str
    value_type: EntityType


class TableMapper:
    """表头映射引擎
    
    将 NormalizedTable 映射为实体和关系抽取结果。
    """

    def __init__(self, config: Optional[TableMappingConfig] = None):
        self.config = config or TableMappingConfig()
        # 预编译正则提高效率
        self._compiled_entity_patterns = [
            (re.compile(p, re.IGNORECASE | re.UNICODE), t) for p, t in self.config.entity_patterns
        ]
        self._compiled_relation_patterns = [
            (re.compile(p, re.IGNORECASE | re.UNICODE), t, b) for p, t, b in self.config.relation_patterns
        ]
        self._compiled_property_patterns = [
            (re.compile(p, re.IGNORECASE | re.UNICODE), name, t) for p, name, t in self.config.property_patterns
        ]
        self._compiled_fk_patterns = [
            re.compile(p, re.IGNORECASE | re.UNICODE) for p in self.config.foreign_key_patterns
        ]

    def map_table(self, table: NormalizedTable, doc_id: Optional[str] = None) -> Tuple[List[ExtractedEntity], List[ExtractedRelation]]:
        """将单个表格映射为实体和关系列表
        
        Args:
            table: 规范化表格
            doc_id: 来源文档ID
        
        Returns:
            (entities, relations)
        """
        if not table.headers or not table.rows:
            return [], []

        # 1. 表头映射分类
        entity_cols: List[MappedEntityColumn] = []
        relation_cols: List[MappedRelationColumn] = []
        property_cols: List[MappedPropertyColumn] = []
        used_headers: Set[str] = set()

        for header in table.headers:
            matched = False

            # 优先匹配关系列
            for pattern, rel_type, bidirectional in self._compiled_relation_patterns:
                if pattern.match(header):
                    relation_cols.append(MappedRelationColumn(
                        column_name=header,
                        relation_type=rel_type,
                        bidirectional=bidirectional
                    ))
                    used_headers.add(header)
                    matched = True
                    break

            if matched:
                continue

            # 其次匹配实体列
            for pattern, entity_type in self._compiled_entity_patterns:
                if pattern.match(header):
                    entity_cols.append(MappedEntityColumn(
                        column_name=header,
                        entity_type=entity_type
                    ))
                    used_headers.add(header)
                    matched = True
                    break

            if matched:
                continue

            # 最后匹配属性列
            for pattern, prop_name, value_type in self._compiled_property_patterns:
                if pattern.match(header):
                    property_cols.append(MappedPropertyColumn(
                        column_name=header,
                        property_name=prop_name,
                        value_type=value_type
                    ))
                    used_headers.add(header)
                    matched = True
                    break

        # 如果没有识别到任何实体列，默认第一列作为主实体列（通常是名称/ID）
        if not entity_cols and table.headers:
            first_header = table.headers[0]
            entity_cols.append(MappedEntityColumn(
                column_name=first_header,
                entity_type=self.config.default_entity_type
            ))
            used_headers.add(first_header)

        # 2. 选择主实体列（用于作为关系源头的实体）
        # 优先选择 Product/Organization/Person/Project/Technology 作为主实体
        priority_types = [
            EntityType.PRODUCT, EntityType.ORGANIZATION, EntityType.PERSON,
            EntityType.TECHNOLOGY, EntityType.PROJECT, EntityType.DOCUMENT, EntityType.EVENT
        ]
        primary_entity_col = None
        for priority_type in priority_types:
            for col in entity_cols:
                if col.entity_type == priority_type:
                    primary_entity_col = col
                    break
            if primary_entity_col:
                break
        if primary_entity_col is None and entity_cols:
            primary_entity_col = entity_cols[0]

        # 3. 逐行生成实体和关系
        entities: List[ExtractedEntity] = []
        relations: List[ExtractedRelation] = []
        entity_name_to_obj: Dict[Tuple[str, str], ExtractedEntity] = {}

        for row_idx, row in enumerate(table.rows):
            # 生成主实体
            primary_name = self._get_cell_value(row, primary_entity_col.column_name) if primary_entity_col else None
            if not primary_name or (self.config.skip_empty_value and not str(primary_name).strip()):
                continue

            primary_name = str(primary_name).strip()

            # 收集属性
            properties: Dict[str, Any] = {}
            for prop_col in property_cols:
                value = self._get_cell_value(row, prop_col.column_name)
                if value is not None and str(value).strip():
                    properties[prop_col.property_name] = {
                        "value": str(value),
                        "type": prop_col.value_type.value
                    }

            # 为每个实体列生成实体（避免重复生成主实体）
            row_entities: Dict[str, ExtractedEntity] = {}
            for col in entity_cols:
                value = self._get_cell_value(row, col.column_name)
                if value is None or (self.config.skip_empty_value and not str(value).strip()):
                    continue
                entity_name = str(value).strip()
                if len(entity_name) < self.config.min_entity_name_length:
                    continue

                key = (entity_name, col.entity_type.value)
                if key in entity_name_to_obj:
                    entity = entity_name_to_obj[key]
                else:
                    entity = ExtractedEntity(
                        name=entity_name,
                        type=col.entity_type,
                        confidence=1.0,
                        source="rule_mapping",
                        properties=properties.copy() if col.column_name == primary_entity_col.column_name else {}
                    )
                    entity_name_to_obj[key] = entity
                    entities.append(entity)
                row_entities[col.column_name] = entity

            # 4. 生成关系
            if primary_entity_col and primary_entity_col.column_name in row_entities:
                source_entity = row_entities[primary_entity_col.column_name]

                for rel_col in relation_cols:
                    target_value = self._get_cell_value(row, rel_col.column_name)
                    if target_value is None or (self.config.skip_empty_value and not str(target_value).strip()):
                        continue
                    target_name = str(target_value).strip()

                    # 判断目标实体类型：尝试在关系列关键词里推断，否则默认 Organization
                    target_type = EntityType.ORGANIZATION
                    if rel_col.relation_type in (RelationType.MANAGES, RelationType.EMPLOYED_BY, RelationType.WORKS_FOR):
                        target_type = EntityType.PERSON
                    elif rel_col.relation_type == RelationType.LOCATED_IN:
                        target_type = EntityType.LOCATION
                    elif rel_col.relation_type == RelationType.PART_OF:
                        target_type = EntityType.PROJECT
                    elif rel_col.relation_type == RelationType.USES:
                        target_type = EntityType.TECHNOLOGY

                    # 创建或复用目标实体
                    target_key = (target_name, target_type.value)
                    if target_key in entity_name_to_obj:
                        target_entity = entity_name_to_obj[target_key]
                    else:
                        target_entity = ExtractedEntity(
                            name=target_name,
                            type=target_type,
                            confidence=1.0,
                            source="rule_mapping"
                        )
                        entity_name_to_obj[target_key] = target_entity
                        entities.append(target_entity)

                    relation = ExtractedRelation(
                        source_entity_name=source_entity.name,
                        target_entity_name=target_entity.name,
                        type=rel_col.relation_type,
                        confidence=1.0,
                        source="rule_mapping",
                        evidence_text=f"{table.sheet_name} 第 {row_idx + 1} 行",
                        properties={
                            "source_column": primary_entity_col.column_name,
                            "target_column": rel_col.column_name,
                            "sheet_name": table.sheet_name
                        }
                    )
                    relations.append(relation)

        return entities, relations

    def map_tables(self, tables: List[NormalizedTable], doc_id: Optional[str] = None) -> Tuple[List[ExtractedEntity], List[ExtractedRelation]]:
        """将多个表格批量映射，并尝试跨表外键关联"""
        all_entities: List[ExtractedEntity] = []
        all_relations: List[ExtractedRelation] = []
        table_primary_entities: Dict[str, Dict[str, ExtractedEntity]] = {}

        for table in tables:
            entities, relations = self.map_table(table, doc_id)
            all_entities.extend(entities)
            all_relations.extend(relations)

            # 记录每张表的主实体（按主实体名称索引），用于跨表外键关联
            if entities:
                table_primary_entities[table.table_id] = {
                    e.name: e for e in entities
                }

        # 跨表外键关联：检测表格头中符合外键模式且名称与其他表主实体匹配的列
        cross_table_relations = self._build_cross_table_relations(
            tables, table_primary_entities
        )
        all_relations.extend(cross_table_relations)

        return all_entities, all_relations

    def _build_cross_table_relations(
        self,
        tables: List[NormalizedTable],
        table_primary_entities: Dict[str, Dict[str, ExtractedEntity]]
    ) -> List[ExtractedRelation]:
        """基于外键列名跨表建立关系"""
        relations: List[ExtractedRelation] = []

        for table in tables:
            current_primary = table_primary_entities.get(table.table_id, {})
            for header in table.headers:
                # 判断是否是外键列（如 供应商ID、产品编码）
                if not any(p.match(header) for p in self._compiled_fk_patterns):
                    continue

                # 查找哪张表的主实体与此列名语义匹配
                target_table_id = None
                target_type_hint = EntityType.KEYWORD
                for other_table in tables:
                    if other_table.table_id == table.table_id:
                        continue
                    other_name = other_table.sheet_name or other_table.table_id
                    # 简单启发：如果外键列名包含其他表名或其关键词
                    if any(keyword in header.lower() for keyword in [other_name.lower(), "id", "编号"]):
                        target_table_id = other_table.table_id
                        # 推断目标类型：基于表名关键词
                        if any(k in other_name for k in ["产品", "商品", "物料"]):
                            target_type_hint = EntityType.PRODUCT
                        elif any(k in other_name for k in ["供应商", "厂商", "公司", "企业"]):
                            target_type_hint = EntityType.ORGANIZATION
                        elif any(k in other_name for k in ["人员", "员工", "用户"]):
                            target_type_hint = EntityType.PERSON
                        break

                if not target_table_id:
                    continue

                target_entities = table_primary_entities.get(target_table_id, {})

                for row_idx, row in enumerate(table.rows):
                    fk_value = self._get_cell_value(row, header)
                    if fk_value is None or not str(fk_value).strip():
                        continue
                    fk_value_str = str(fk_value).strip()

                    # 在当前表找到主实体作为关系源头
                    primary_entity = None
                    for entity in current_primary.values():
                        # 简单匹配：假设主实体 value 与外键相同（行索引一致）
                        # 这里无法精确匹配行，所以采用同名匹配
                        if fk_value_str == entity.name:
                            primary_entity = entity
                            break

                    if not primary_entity:
                        continue

                    target_entity = target_entities.get(fk_value_str)
                    if not target_entity:
                        # 目标实体可能不存在，则创建占位实体
                        target_entity = ExtractedEntity(
                            name=fk_value_str,
                            type=target_type_hint,
                            confidence=1.0,
                            source="rule_mapping_foreign_key"
                        )
                        target_entities[fk_value_str] = target_entity

                    relation = ExtractedRelation(
                        source_entity_name=primary_entity.name,
                        target_entity_name=target_entity.name,
                        type=RelationType.RELATED_TO,
                        confidence=1.0,
                        source="rule_mapping_foreign_key",
                        evidence_text=f"外键列 {header} 匹配",
                        properties={
                            "foreign_key_column": header,
                            "source_table": table.sheet_name,
                            "target_table": target_table_id
                        }
                    )
                    relations.append(relation)

        return relations

    @staticmethod
    def _get_cell_value(row: Dict[str, Any], column: str) -> Any:
        """获取单元格值，兼容列名大小写"""
        if column in row:
            return row[column]
        # 大小写不敏感回退
        for k, v in row.items():
            if k.lower() == column.lower():
                return v
        return None
