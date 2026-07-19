"""任务式 Agent 服务。

支持复杂任务拆解、步骤执行、进度追踪和结果溯源。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from app.core.logger import get_logger

logger = get_logger(__name__)


class TaskStatus(str, Enum):
    """任务状态枚举。"""
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(str, Enum):
    """步骤状态枚举。"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class TaskStep:
    """任务步骤。"""
    step_id: str
    name: str
    tool_name: str
    description: str = ""
    input: Dict[str, Any] = field(default_factory=dict)
    output: Dict[str, Any] = field(default_factory=dict)
    status: StepStatus = StepStatus.PENDING
    depends_on: List[str] = field(default_factory=list)
    start_time: str = ""
    end_time: str = ""
    error: str = ""


@dataclass
class Task:
    """任务数据类。"""
    task_id: str
    user_id: str
    goal: str
    status: TaskStatus = TaskStatus.PENDING
    steps: List[TaskStep] = field(default_factory=list)
    result: str = ""
    sources: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


class TaskService:
    """任务服务。

    负责任务的创建、规划、执行和历史查询。
    """

    _tasks: Dict[str, Task] = {}

    @classmethod
    async def plan_task(
        cls, goal: str, user_id: str = "default"
    ) -> Task:
        """根据目标生成任务计划。

        Args:
            goal: 任务目标
            user_id: 用户 ID

        Returns:
            包含计划步骤的任务
        """
        from app.core.llm_client import call_openai_chat
        from app.core.llm.router import resolve_provider_config

        task_id = str(uuid.uuid4())
        config = resolve_provider_config()

        # 可用工具列表
        available_tools = [
            {"name": "rag_search", "description": "在知识库中搜索相关文档"},
            {"name": "web_search", "description": "联网搜索最新信息"},
            {"name": "process_document", "description": "处理用户上传的文档"},
            {"name": "knowledge_graph_query", "description": "查询知识图谱中的实体关系"},
            {"name": "summarize", "description": "总结/摘要文本内容"},
            {"name": "compare", "description": "对比分析多份文本"},
            {"name": "note_create", "description": "创建笔记"},
        ]

        prompt = f"""你是一个任务规划器。请将用户目标拆解为具体的执行步骤。

可用工具：
{json.dumps(available_tools, ensure_ascii=False, indent=2)}

用户目标：{goal}

请输出 JSON 格式的执行计划，不要附带其他文字和 markdown 标记：
{{
  "steps": [
    {{
      "name": "步骤名称",
      "tool_name": "使用的工具名",
      "description": "步骤描述",
      "input": {{"query": "搜索查询"}},
      "depends_on": []
    }}
  ]
}}"""

        try:
            result = await call_openai_chat(
                model=config["model"],
                messages=[{"role": "user", "content": prompt}],
                api_key=config["api_key"],
                base_url=config["base_url"],
                temperature=0.1,
                max_tokens=2048,
            )

            result = result.strip()
            if result.startswith("```"):
                lines = result.split("\n")
                result = "\n".join(lines[1:]) if len(lines) > 1 else result
                if result.endswith("```"):
                    result = result[:-3]

            plan = json.loads(result)
            steps_data = plan.get("steps", [])

            steps = []
            for s in steps_data:
                steps.append(TaskStep(
                    step_id=str(uuid.uuid4()),
                    name=s.get("name", "未命名步骤"),
                    tool_name=s.get("tool_name", "rag_search"),
                    description=s.get("description", ""),
                    input=s.get("input", {}),
                    depends_on=s.get("depends_on", []),
                ))

            task = Task(
                task_id=task_id,
                user_id=user_id,
                goal=goal,
                status=TaskStatus.PLANNING,
                steps=steps,
                created_at=datetime.now().isoformat(),
                updated_at=datetime.now().isoformat(),
            )

            cls._tasks[task_id] = task
            logger.info(f"任务已创建: {task_id}, {len(steps)} 步骤")
            return task

        except Exception as e:
            logger.error(f"任务规划失败: {e}")
            # 创建失败任务
            task = Task(
                task_id=task_id,
                user_id=user_id,
                goal=goal,
                status=TaskStatus.FAILED,
                result=f"任务规划失败: {str(e)}",
                created_at=datetime.now().isoformat(),
                updated_at=datetime.now().isoformat(),
            )
            return task

    @classmethod
    async def execute_step(cls, task_id: str, step_id: str) -> bool:
        """执行任务中的一个步骤。

        Args:
            task_id: 任务 ID
            step_id: 步骤 ID
        """
        task = cls._tasks.get(task_id)
        if not task:
            return False

        step = next((s for s in task.steps if s.step_id == step_id), None)
        if not step:
            return False

        task.status = TaskStatus.RUNNING
        step.status = StepStatus.RUNNING
        step.start_time = datetime.now().isoformat()

        try:
            # 根据工具名执行对应操作
            if step.tool_name == "rag_search":
                from app.services.retriever import RAGEngine
                engine = RAGEngine()
                query = step.input.get("query", "")
                results = await engine.query(query, top_k=5)
                step.output = {"results": results, "count": len(results)}

            elif step.tool_name == "web_search":
                from app.services.web_search import get_web_search_service
                service = get_web_search_service()
                query = step.input.get("query", "")
                results = await service.search(query, top_n=5)
                step.output = {
                    "results": [
                        {"title": r.title, "url": r.url, "snippet": r.snippet}
                        for r in results
                    ],
                    "count": len(results),
                }

            elif step.tool_name == "summarize":
                from app.services.writing_service import WritingService
                service = WritingService()
                text = step.input.get("text", "")
                summary = await service.summarize(text)
                step.output = {"summary": summary}

            else:
                step.output = {"info": f"工具 {step.tool_name} 已调度"}

            step.status = StepStatus.COMPLETED
            logger.info(f"步骤执行成功: {step.name}")

        except Exception as e:
            step.status = StepStatus.FAILED
            step.error = str(e)
            logger.error(f"步骤执行失败 [{step.name}]: {e}")

        finally:
            step.end_time = datetime.now().isoformat()

        # 检查是否所有步骤完成
        all_done = all(
            s.status in (StepStatus.COMPLETED, StepStatus.SKIPPED, StepStatus.FAILED)
            for s in task.steps
        )
        if all_done:
            task.status = TaskStatus.COMPLETED
        task.updated_at = datetime.now().isoformat()

        return True

    @classmethod
    def get_task(cls, task_id: str) -> Optional[Task]:
        """获取任务。"""
        return cls._tasks.get(task_id)

    @classmethod
    def list_tasks(cls, user_id: str = "default") -> List[Task]:
        """列出用户的任务。"""
        return [
            t for t in cls._tasks.values()
            if t.user_id == user_id
        ]

    @classmethod
    def cancel_task(cls, task_id: str) -> bool:
        """取消任务。"""
        task = cls._tasks.get(task_id)
        if not task:
            return False
        task.status = TaskStatus.CANCELLED
        task.updated_at = datetime.now().isoformat()
        return True

    @classmethod
    def delete_task(cls, task_id: str) -> bool:
        """删除任务。"""
        if task_id in cls._tasks:
            del cls._tasks[task_id]
            return True
        return False
