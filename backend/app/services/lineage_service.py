"""数据血缘追踪服务

提供文档处理过程中的血缘记录与追溯能力。

v2.1 新增
"""

from __future__ import annotations

import uuid
import json
from datetime import datetime
from typing import Dict, List, Optional, Any
from pathlib import Path

from app.models.lineage import DataLineage, DataLineageNode, DataLineageEdge
from app.core.logger import get_logger


logger = get_logger(__name__)


class LineageService:
    """数据血缘追踪服务。
    
    管理文档→分块→实体→关系的完整血缘链路。
    当前使用内存存储，生产环境可扩展为数据库存储。
    """
    
    def __init__(self):
        """初始化血缘服务。"""
        # 内存存储：document_id -> DataLineage
        self._lineage_store: Dict[str, DataLineage] = {}
        
        # 持久化文件路径（可选）
        self._persist_dir = Path("./data/lineage")
        self._persist_dir.mkdir(parents=True, exist_ok=True)
    
    async def record_document_processing(
        self,
        document_id: str,
        document_name: str,
        chunk_ids: List[str],
        entity_ids: List[str],
        relationship_ids: List[str],
        chunk_to_entities: Optional[Dict[str, List[str]]] = None,
    ) -> DataLineage:
        """记录文档处理后的血缘信息。
        
        在文档切分完成和图谱构建完成后调用。
        
        Args:
            document_id: 文档唯一标识
            document_name: 文档名称
            chunk_ids: 生成的分块ID列表
            entity_ids: 抽取的实体ID列表
            relationship_ids: 抽取的关系ID列表
            chunk_to_entities: 分块到实体的映射
            
        Returns:
            创建的血缘记录
        """
        now = datetime.now()
        
        lineage = DataLineage(
            document_id=document_id,
            document_name=document_name,
            created_at=now,
            updated_at=now,
            chunk_ids=chunk_ids,
            entity_ids=entity_ids,
            relationship_ids=relationship_ids,
            chunk_to_entities=chunk_to_entities or {},
        )
        
        # 构建血缘图
        lineage.nodes.append(DataLineageNode(
            id=document_id, type="document", name=document_name
        ))
        
        for chunk_id in chunk_ids:
            lineage.nodes.append(DataLineageNode(
                id=chunk_id, type="chunk", name=f"Chunk-{chunk_id[:8]}"
            ))
            lineage.edges.append(DataLineageEdge(
                source_id=chunk_id, target_id=document_id,
                relation_type="derived_from"
            ))
        
        for entity_id in entity_ids:
            lineage.nodes.append(DataLineageNode(
                id=entity_id, type="entity", name=f"Entity-{entity_id[:8]}"
            ))
        
        # chunk -> entity 关系
        if chunk_to_entities:
            for chunk_id, entities in chunk_to_entities.items():
                for entity_id in entities:
                    lineage.edges.append(DataLineageEdge(
                        source_id=entity_id, target_id=chunk_id,
                        relation_type="extracted_from"
                    ))
        
        for rel_id in relationship_ids:
            lineage.nodes.append(DataLineageNode(
                id=rel_id, type="relationship", name=f"Rel-{rel_id[:8]}"
            ))
        
        self._lineage_store[document_id] = lineage
        
        # 异步持久化
        await self._persist_lineage(lineage)
        
        logger.info(
            f"血缘记录已创建: doc={document_id}, "
            f"chunks={len(chunk_ids)}, entities={len(entity_ids)}, "
            f"relations={len(relationship_ids)}"
        )
        
        return lineage
    
    async def get_lineage(self, document_id: str) -> Optional[DataLineage]:
        """获取指定文档的血缘信息。
        
        Args:
            document_id: 文档ID
            
        Returns:
            血缘记录，不存在则返回 None
        """
        # 先查内存
        if document_id in self._lineage_store:
            return self._lineage_store[document_id]
        
        # 尝试从磁盘加载
        lineage = await self._load_lineage(document_id)
        if lineage:
            self._lineage_store[document_id] = lineage
        
        return lineage
    
    async def trace_upstream(
        self, target_id: str, target_type: str = "entity"
    ) -> List[Dict[str, Any]]:
        """追溯上游血缘。
        
        查找指定元素的来源文档和分块。
        
        Args:
            target_id: 目标元素ID
            target_type: 目标元素类型
            
        Returns:
            上游链路列表
        """
        upstream = []
        
        for lineage in self._lineage_store.values():
            # 检查目标是否在当前文档血缘中
            if target_id in lineage.entity_ids or target_id in lineage.chunk_ids:
                upstream.append({
                    "document_id": lineage.document_id,
                    "document_name": lineage.document_name,
                    "chunks": lineage.chunk_ids,
                    "created_at": lineage.created_at.isoformat(),
                })
        
        return upstream
    
    async def _persist_lineage(self, lineage: DataLineage) -> None:
        """将血缘记录持久化到磁盘。"""
        try:
            file_path = self._persist_dir / f"{lineage.document_id}.json"
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(lineage.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"血缘记录持久化失败: {e}")
    
    async def _load_lineage(self, document_id: str) -> Optional[DataLineage]:
        """从磁盘加载血缘记录。"""
        try:
            file_path = self._persist_dir / f"{document_id}.json"
            if not file_path.exists():
                return None
            
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            return DataLineage(
                document_id=data["document_id"],
                document_name=data["document_name"],
                created_at=datetime.fromisoformat(data["created_at"]),
                updated_at=datetime.fromisoformat(data["updated_at"]),
                chunk_ids=data.get("elements", {}).get("chunks", []),
                entity_ids=data.get("elements", {}).get("entities", []),
                relationship_ids=data.get("elements", {}).get("relationships", []),
            )
        except Exception as e:
            logger.warning(f"血缘记录加载失败: {e}")
            return None


# 全局实例
lineage_service = LineageService()
