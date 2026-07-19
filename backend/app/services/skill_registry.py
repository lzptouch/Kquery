"""
P2-2 Skills 注册表服务：加载/管理/匹配 Skills
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from app.core.logger import get_logger
from app.models.skill import (
    SkillCreateRequest,
    SkillUpdateRequest,
    SkillResponse,
    SkillMatchResult,
)

logger = get_logger(__name__)

# 内置 Skill YAML 目录
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills" / "builtin"


class SkillRegistry:
    """Skills 注册表（单例）

    管理所有 Skill 的加载、启用/禁用、创建、匹配。
    内置 Skill 从 YAML 文件加载，自定义 Skill 存内存（生产需换 DB）。
    """

    _instance: Optional["SkillRegistry"] = None

    def __new__(cls) -> "SkillRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._skills: Dict[str, SkillResponse] = {}
            cls._instance._loaded = False
        return cls._instance

    def _load_builtin_skills(self):
        """从 builtin/ 目录加载 YAML 定义的 Skill"""
        if not BUILTIN_SKILLS_DIR.exists():
            logger.warning(f"内置 Skills 目录不存在: {BUILTIN_SKILLS_DIR}")
            return

        for yaml_file in BUILTIN_SKILLS_DIR.glob("*.yaml"):
            try:
                with open(yaml_file, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)

                if not data or "id" not in data:
                    continue

                skill = SkillResponse(
                    id=data["id"],
                    name=data.get("name", data["id"]),
                    description=data.get("description", ""),
                    system_prompt=data.get("system_prompt", ""),
                    tools=data.get("tools", []),
                    default_kb_id=data.get("default_kb_id"),
                    example_questions=data.get("example_questions", []),
                    model=data.get("model"),
                    temperature=data.get("temperature"),
                    is_builtin=True,
                    is_enabled=data.get("is_enabled", True),
                    author=data.get("author", "KQuery Pro"),
                    version=data.get("version", "1.0.0"),
                )
                self._skills[skill.id] = skill
                logger.info(f"内置 Skill 加载: {skill.name} ({skill.id})")
            except Exception as e:
                logger.error(f"加载 Skill 文件失败: {yaml_file}, 错误: {e}")

        self._loaded = True
        logger.info(f"共加载 {len(self._skills)} 个内置 Skill")

    def ensure_loaded(self):
        """确保 Skills 已加载"""
        if not self._loaded:
            self._load_builtin_skills()

    def list_skills(self, include_disabled: bool = False) -> List[SkillResponse]:
        """列出所有 Skill"""
        self.ensure_loaded()
        skills = list(self._skills.values())
        if not include_disabled:
            skills = [s for s in skills if s.is_enabled]
        return sorted(skills, key=lambda s: (s.is_builtin, s.name))

    def get_skill(self, skill_id: str) -> Optional[SkillResponse]:
        """获取单个 Skill"""
        self.ensure_loaded()
        return self._skills.get(skill_id)

    def create_skill(self, request: SkillCreateRequest) -> SkillResponse:
        """创建自定义 Skill"""
        self.ensure_loaded()
        skill_id = f"custom_{uuid.uuid4().hex[:8]}"

        skill = SkillResponse(
            id=skill_id,
            name=request.name,
            description=request.description,
            system_prompt=request.system_prompt,
            tools=request.tools,
            default_kb_id=request.default_kb_id,
            example_questions=request.example_questions,
            model=request.model,
            temperature=request.temperature,
            is_builtin=False,
            is_enabled=request.is_enabled,
            author=request.author,
            version=request.version,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        self._skills[skill_id] = skill
        logger.info(f"自定义 Skill 创建: {skill.name} ({skill_id})")
        return skill

    def update_skill(self, skill_id: str, request: SkillUpdateRequest) -> Optional[SkillResponse]:
        """更新 Skill（仅自定义 Skill 可更新完整信息）"""
        self.ensure_loaded()
        skill = self._skills.get(skill_id)
        if skill is None:
            return None

        if skill.is_builtin:
            # 内置 Skill 只能启用/禁用
            if request.is_enabled is not None:
                skill.is_enabled = request.is_enabled
                skill.updated_at = datetime.utcnow()
                logger.info(f"内置 Skill 状态变更: {skill.name} → enabled={request.is_enabled}")
            return skill

        # 自定义 Skill 全量更新
        update_data = request.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(skill, field, value)
        skill.updated_at = datetime.utcnow()
        return skill

    def delete_skill(self, skill_id: str) -> bool:
        """删除 Skill（仅自定义）"""
        self.ensure_loaded()
        skill = self._skills.get(skill_id)
        if skill is None or skill.is_builtin:
            return False
        del self._skills[skill_id]
        logger.info(f"Skill 删除: {skill.name} ({skill_id})")
        return True

    def enable_skill(self, skill_id: str) -> bool:
        """启用 Skill"""
        self.ensure_loaded()
        skill = self._skills.get(skill_id)
        if skill is None:
            return False
        skill.is_enabled = True
        skill.updated_at = datetime.utcnow()
        return True

    def disable_skill(self, skill_id: str) -> bool:
        """禁用 Skill"""
        self.ensure_loaded()
        skill = self._skills.get(skill_id)
        if skill is None:
            return False
        skill.is_enabled = False
        skill.updated_at = datetime.utcnow()
        return True

    async def match_skills(self, query: str, top_k: int = 3) -> List[SkillMatchResult]:
        """根据 query 匹配最合适的 Skill

        使用规则匹配（关键词）+ 回退到 LLM 分类。
        """
        self.ensure_loaded()
        enabled_skills = [s for s in self._skills.values() if s.is_enabled]
        if not enabled_skills:
            return []

        query_lower = query.lower()
        results: List[SkillMatchResult] = []

        for skill in enabled_skills:
            score = 0.0
            reasons = []

            # 关键词匹配（description + example_questions）
            desc_lower = skill.description.lower()
            name_lower = skill.name.lower()

            # 在示例问题中搜索
            for eq in skill.example_questions:
                if any(word in query_lower for word in eq.lower().split() if len(word) > 1):
                    score += 0.15
                    reasons.append(f"匹配示例问题: {eq}")

            # 描述关键词匹配
            if any(word in query_lower for word in desc_lower.split() if len(word) > 1):
                score += 0.2
                reasons.append("描述关键词匹配")

            # 名称包含匹配
            if name_lower in query_lower or any(
                word in query_lower for word in name_lower.split() if len(word) > 1
            ):
                score += 0.1
                reasons.append(f"Skill 名称匹配: {skill.name}")

            if score > 0:
                results.append(SkillMatchResult(
                    skill_id=skill.id,
                    skill_name=skill.name,
                    confidence=min(score, 0.95),  # 非 LLM 方式不会给 1.0
                    reason="; ".join(reasons),
                ))

        # 按置信度降序
        results.sort(key=lambda x: x.confidence, reverse=True)
        return results[:top_k]

    def get_active_tools(self) -> List[str]:
        """获取所有已启用 Skill 的工具集合（去重）"""
        self.ensure_loaded()
        all_tools: set = set()
        for skill in self._skills.values():
            if skill.is_enabled:
                all_tools.update(skill.tools)
        return list(all_tools)

    def build_skill_context_prompt(self, skill_id: Optional[str] = None) -> str:
        """构建 Skill 上下文提示词（用于注入 Agent）

        Args:
            skill_id: 可选指定 Skill ID；为 None 时返回通用提示词
        """
        self.ensure_loaded()

        if skill_id:
            skill = self._skills.get(skill_id)
            if skill and skill.is_enabled:
                return skill.system_prompt
            return ""

        # 返回所有启用 Skill 的简要说明，供 Agent 自动匹配
        enabled = [s for s in self._skills.values() if s.is_enabled]
        if not enabled:
            return ""

        lines = ["\n## 可用技能 (Skills)"]
        for s in enabled:
            lines.append(f"- **{s.name}** (id: `{s.id}`): {s.description}")
        return "\n".join(lines)


# 全局单例
skill_registry = SkillRegistry()
