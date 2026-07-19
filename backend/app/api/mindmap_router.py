"""思维导图 API 路由。

支持从知识图谱实体、文本内容或笔记生成思维导图数据。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/mindmap", tags=["思维导图"])


class MindMapNode(BaseModel):
    """思维导图节点。"""
    id: str
    name: str
    type: Optional[str] = None
    color: Optional[str] = None
    children: List["MindMapNode"] = Field(default_factory=list)


class MindMapResponse(BaseModel):
    """思维导图响应。"""
    root: MindMapNode
    total_nodes: int
    max_depth: int


class GenerateMindMapRequest(BaseModel):
    """从文本生成思维导图请求。"""
    text: str = Field(description="输入文本")
    max_depth: int = Field(default=3, ge=1, le=5, description="最大层级深度")


async def _build_tree_from_entity(
    entity_id: str, max_depth: int = 3, visited: Optional[set] = None
) -> Optional[MindMapNode]:
    """从知识图谱实体递归构建树形结构。

    Args:
        entity_id: 中心实体 ID
        max_depth: 最大深度
        visited: 已访问节点集合（防止成环）

    Returns:
        思维导图节点或 None
    """
    if visited is None:
        visited = set()

    if entity_id in visited or max_depth <= 0:
        return None

    visited.add(entity_id)

    try:
        from app.services.graph.graph_query import GraphQueryService

        graph_query = GraphQueryService()
        neighbors = await graph_query.get_neighbors(entity_id, depth=1)

        # 获取实体信息
        entities = neighbors.get("entities", [])
        center_entity = None
        children = []

        for entity in entities:
            if entity.get("id") == entity_id:
                center_entity = entity
            else:
                child_node = await _build_tree_from_entity(
                    entity.get("id", ""),
                    max_depth=max_depth - 1,
                    visited=visited,
                )
                if child_node is None:
                    child_node = MindMapNode(
                        id=entity.get("id", ""),
                        name=entity.get("name", entity.get("label", "未知")),
                        type=entity.get("type", ""),
                    )
                children.append(child_node)

        if center_entity is None:
            return MindMapNode(
                id=entity_id,
                name=entity_id,
                type="unknown",
                children=children,
            )

        return MindMapNode(
            id=center_entity.get("id", entity_id),
            name=center_entity.get("name", center_entity.get("label", entity_id)),
            type=center_entity.get("type", ""),
            children=children,
        )

    except Exception as e:
        logger.warning(f"构建树形结构失败 [{entity_id}]: {e}")
        return MindMapNode(
            id=entity_id,
            name=entity_id,
            type="unknown",
            children=[],
        )


def _count_nodes(node: MindMapNode) -> tuple[int, int]:
    """统计节点数和最大深度。"""
    if not node.children:
        return 1, 1
    total = 1
    max_d = 1
    for child in node.children:
        child_total, child_depth = _count_nodes(child)
        total += child_total
        max_d = max(max_d, child_depth + 1)
    return total, max_d


@router.get("/from-entity/{entity_id}", response_model=MindMapResponse)
async def mindmap_from_entity(
    entity_id: str,
    max_depth: int = Query(default=3, ge=1, le=5, description="最大层级深度"),
):
    """从知识图谱实体生成思维导图。

    Args:
        entity_id: 中心实体 ID
        max_depth: 最大层级深度
    """
    root = await _build_tree_from_entity(entity_id, max_depth=max_depth)
    if root is None:
        raise HTTPException(status_code=404, detail=f"实体 {entity_id} 不存在")

    total_nodes, depth = _count_nodes(root)
    return MindMapResponse(root=root, total_nodes=total_nodes, max_depth=depth)


@router.post("/from-text", response_model=MindMapResponse)
async def mindmap_from_text(request: GenerateMindMapRequest):
    """从文本生成思维导图（使用 LLM 提取层级结构）。

    Args:
        request: 生成请求
    """
    try:
        from app.core.llm_client import call_openai_chat
        from app.core.llm.router import resolve_provider_config

        provider_config = resolve_provider_config()

        prompt = f"""请将以下文本内容转换为层级分明的 Markdown 大纲格式。
要求：
1. 使用 # 表示一级主题，## 表示二级，### 表示三级
2. 最多 {request.max_depth} 层级
3. 每层提取 2-5 个关键点
4. 只输出 Markdown 大纲，不要其他内容

文本内容：
{request.text[:3000]}"""

        markdown_outline = await call_openai_chat(
            model=provider_config["model"],
            messages=[
                {"role": "system", "content": "你是一个专业的思维导图大纲生成助手。"},
                {"role": "user", "content": prompt},
            ],
            api_key=provider_config["api_key"],
            base_url=provider_config["base_url"],
            temperature=0.3,
            max_tokens=2048,
        )

        # 解析 Markdown 为树形结构
        root = _parse_markdown_to_tree(markdown_outline, max_depth=request.max_depth)
        total_nodes, depth = _count_nodes(root)

        return MindMapResponse(root=root, total_nodes=total_nodes, max_depth=depth)

    except Exception as e:
        logger.error(f"从文本生成思维导图失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"生成失败: {str(e)}")


def _parse_markdown_to_tree(markdown: str, max_depth: int = 3) -> MindMapNode:
    """解析 Markdown 标题为树形结构。"""
    import uuid

    lines = markdown.strip().split("\n")
    stack: List[tuple[int, MindMapNode]] = []

    root = MindMapNode(id=str(uuid.uuid4()), name="思维导图", type="root")
    stack.append((0, root))

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # 计算标题级别
        level = 0
        for char in line:
            if char == "#":
                level += 1
            else:
                break

        if level == 0 or level > max_depth:
            continue

        title = line[level:].strip()
        if not title:
            continue

        node = MindMapNode(id=str(uuid.uuid4()), name=title)

        # 找到合适的父节点
        while stack and stack[-1][0] >= level:
            stack.pop()

        if stack:
            parent_level, parent_node = stack[-1]
            parent_node.children.append(node)

        stack.append((level, node))

    # 如果 root 只有一个顶层子节点，提升为 root
    if len(root.children) == 1:
        return root.children[0]

    return root


@router.post("/from-note/{note_id}", response_model=MindMapResponse)
async def mindmap_from_note(note_id: str, max_depth: int = Query(default=3, ge=1, le=5)):
    """从笔记内容生成思维导图。

    Args:
        note_id: 笔记 ID
        max_depth: 最大层级深度
    """
    import json
    from pathlib import Path

    from app.core.settings import get_settings

    settings = get_settings()
    notes_dir = Path(settings.storage.upload_dir).parent / "notes" / note_id

    if not notes_dir.exists():
        raise HTTPException(status_code=404, detail=f"笔记 {note_id} 不存在")

    content_path = notes_dir / "content.md"
    if not content_path.exists():
        raise HTTPException(status_code=400, detail="笔记内容为空")

    with open(content_path, "r", encoding="utf-8") as f:
        content = f.read()

    return await mindmap_from_text(
        GenerateMindMapRequest(text=content, max_depth=max_depth)
    )


@router.get("/export/{entity_id}")
async def export_mindmap_markdown(
    entity_id: str,
    max_depth: int = Query(default=3, ge=1, le=5),
):
    """导出思维导图为 Markdown 大纲。

    Args:
        entity_id: 中心实体 ID
        max_depth: 最大层级深度
    """
    root = await _build_tree_from_entity(entity_id, max_depth=max_depth)
    if root is None:
        raise HTTPException(status_code=404, detail=f"实体 {entity_id} 不存在")

    def to_markdown(node: MindMapNode, level: int = 1) -> str:
        prefix = "#" * min(level, 6)
        lines = [f"{prefix} {node.name}"]
        for child in node.children:
            lines.append(to_markdown(child, level + 1))
        return "\n".join(lines)

    markdown = to_markdown(root)
    return {"entity_id": entity_id, "markdown": markdown}
