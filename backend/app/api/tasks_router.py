"""任务式 Agent API 路由。"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.logger import get_logger
from app.services.task_service import TaskService, TaskStatus

logger = get_logger(__name__)

router = APIRouter(prefix="/tasks", tags=["任务式 Agent"])


class CreateTaskRequest(BaseModel):
    """创建任务请求。"""
    goal: str = Field(description="任务目标")
    user_id: str = Field(default="default", description="用户 ID")


class TaskStepResponse(BaseModel):
    """任务步骤响应。"""
    step_id: str
    name: str
    tool_name: str
    description: str
    status: str
    depends_on: list[str] = []
    start_time: str = ""
    end_time: str = ""
    error: str = ""


class TaskResponse(BaseModel):
    """任务响应。"""
    task_id: str
    goal: str
    status: str
    steps: list[TaskStepResponse] = []
    result: str = ""
    created_at: str = ""
    updated_at: str = ""


@router.post("", response_model=TaskResponse)
async def create_task(request: CreateTaskRequest):
    """创建任务并自动规划步骤。

    Args:
        request: 创建任务请求
    """
    task = await TaskService.plan_task(request.goal, request.user_id)

    return TaskResponse(
        task_id=task.task_id,
        goal=task.goal,
        status=task.status.value,
        steps=[
            TaskStepResponse(
                step_id=s.step_id,
                name=s.name,
                tool_name=s.tool_name,
                description=s.description,
                status=s.status.value,
                depends_on=s.depends_on,
            )
            for s in task.steps
        ],
        result=task.result,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):
    """查询任务状态与步骤详情。

    Args:
        task_id: 任务 ID
    """
    task = TaskService.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    return TaskResponse(
        task_id=task.task_id,
        goal=task.goal,
        status=task.status.value,
        steps=[
            TaskStepResponse(
                step_id=s.step_id,
                name=s.name,
                tool_name=s.tool_name,
                description=s.description,
                status=s.status.value,
                depends_on=s.depends_on,
                start_time=s.start_time,
                end_time=s.end_time,
                error=s.error,
            )
            for s in task.steps
        ],
        result=task.result,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


@router.get("")
async def list_tasks(
    user_id: str = Query(default="default", description="用户 ID"),
):
    """列出用户的全部任务。"""
    tasks = TaskService.list_tasks(user_id)
    return {
        "tasks": [
            TaskResponse(
                task_id=t.task_id,
                goal=t.goal,
                status=t.status.value,
                steps=[
                    TaskStepResponse(
                        step_id=s.step_id,
                        name=s.name,
                        tool_name=s.tool_name,
                        description=s.description,
                        status=s.status.value,
                    )
                    for s in t.steps
                ],
                result=t.result,
                created_at=t.created_at,
                updated_at=t.updated_at,
            )
            for t in tasks
        ],
        "total": len(tasks),
    }


@router.post("/{task_id}/execute")
async def execute_task(task_id: str):
    """执行任务的所有步骤。

    Args:
        task_id: 任务 ID
    """
    task = TaskService.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 按依赖顺序执行步骤
    for step in task.steps:
        await TaskService.execute_step(task_id, step.step_id)

    task = TaskService.get_task(task_id)
    return TaskResponse(
        task_id=task.task_id,
        goal=task.goal,
        status=task.status.value,
        steps=[
            TaskStepResponse(
                step_id=s.step_id,
                name=s.name,
                tool_name=s.tool_name,
                description=s.description,
                status=s.status.value,
                start_time=s.start_time,
                end_time=s.end_time,
                error=s.error,
            )
            for s in task.steps
        ],
        result=task.result,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str):
    """取消任务。"""
    success = TaskService.cancel_task(task_id)
    if not success:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"success": True, "message": "任务已取消"}


@router.get("/{task_id}/stream")
async def stream_task_progress(task_id: str):
    """SSE 流式推送任务进度。"""
    task = TaskService.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    async def event_stream():
        yield f"data: {json.dumps({'event': 'start', 'task_id': task_id, 'total_steps': len(task.steps)})}\n\n"

        for step in task.steps:
            yield f"data: {json.dumps({'event': 'step', 'step_id': step.step_id, 'name': step.name, 'status': step.status.value})}\n\n"
            await TaskService.execute_step(task_id, step.step_id)

            updated_task = TaskService.get_task(task_id)
            updated_step = next((s for s in updated_task.steps if s.step_id == step.step_id), None)
            if updated_step:
                yield f"data: {json.dumps({'event': 'step_result', 'step_id': step.step_id, 'status': updated_step.status.value, 'error': updated_step.error})}\n\n"

        final_task = TaskService.get_task(task_id)
        yield f"data: {json.dumps({'event': 'complete', 'status': final_task.status.value})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
