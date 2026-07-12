"""数据血缘追踪模型

定义文档、分块、实体、关系之间的血缘关系数据结构。

v2.1 新增
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional, Any


@dataclass
class DataLineageNode:
    """血缘图中的单个节点。"""
    id: str
    type: str  # document | chunk | entity | relationship
    name: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DataLineageEdge:
    """血缘图中的边（派生关系）。"""
    source_id: str
    target_id: str
    relation_type: str  # derived_from | contains | extracts | belongs_to
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DataLineage:
    """数据血缘追踪记录。
    
    记录从原始文档到最终知识图谱元素的完整派生链路。
    """
    document_id: str
    document_name: str
    created_at: datetime
    updated_at: datetime
    
    # 派生元素ID列表
    chunk_ids: List[str] = field(default_factory=list)
    entity_ids: List[str] = field(default_factory=list)
    relationship_ids: List[str] = field(default_factory=list)
    
    # 详细映射（chunk -> entities）
    chunk_to_entities: Dict[str, List[str]] = field(default_factory=dict)
    
    # 血缘图数据
    nodes: List[DataLineageNode] = field(default_factory=list)
    edges: List[DataLineageEdge] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为可序列化的字典。"""
        return {
            "document_id": self.document_id,
            "document_name": self.document_name,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "summary": {
                "chunk_count": len(self.chunk_ids),
                "entity_count": len(self.entity_ids),
                "relationship_count": len(self.relationship_ids),
            },
            "elements": {
                "chunks": self.chunk_ids,
                "entities": self.entity_ids,
                "relationships": self.relationship_ids,
            },
            "lineage_graph": {
                "nodes": [
                    {"id": n.id, "type": n.type, "name": n.name, **n.metadata}
                    for n in self.nodes
                ],
                "edges": [
                    {"source": e.source_id, "target": e.target_id,
                     "relation": e.relation_type, **e.metadata}
                    for e in self.edges
                ],
            }
        }
