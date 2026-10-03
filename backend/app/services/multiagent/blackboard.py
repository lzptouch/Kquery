"""Blackboard 黑板（阶段 8 §3）。

多 Agent 间只传 ID/指令，证据放在结构化存储。六类操作（多一个不必要，少一个不够用）：
写计划 / 读计划 / 追加证据（原子，返回标识）/ 列证据 / 更新证据（回填评分与淘汰原因，
不删除）/ 追加评审意见。

硬约束：
- "更新而非删除"——证据保留淘汰原因，是排查检索质量的原始依据；
- 并发追加必须原子（同层多个 Researcher 并行写入）；
- 评审意见逐轮追加，不可覆盖（观察"评审是否收敛"的依据）。
"""
import threading
import time
from dataclasses import dataclass, field


@dataclass
class SubTask:
    task_id: str
    description: str  # 可直接作为检索查询的具体表述
    tool_name: str
    depends_on: list[str] = field(default_factory=list)
    layer: int = 0  # 拓扑层（由依赖推导，全落一层是规划器未识别依赖的信号）
    status: str = "pending"  # pending / running / done / failed
    failure_reason: str = ""  # 失败必填：区分"没有信息"与"工具不可用"


@dataclass
class EvidenceRecord:
    """黑板证据记录：七字段缺一不可（§3.4）。"""

    evidence_id: str
    task_id: str  # 产出它的子任务
    content: str
    tool_name: str  # 来源工具（前端标注证据类型）
    score: float | None = None  # 校验分（未评分为空）
    adopted: bool | None = None  # None=未校验
    rejected_reason: str = ""  # 淘汰原因（保留而非删除）
    created_at: float = field(default_factory=time.time)
    used_by_tasks: list[str] = field(default_factory=list)  # 跨任务复用记录
    extra: dict = field(default_factory=dict)  # chunk_id / uri / modality 等


@dataclass
class Critique:
    round: int
    score: float  # 0-10
    issues: list[str]  # 问题清单（必须具体可执行）
    resolved_issues: list[str] = field(default_factory=list)  # 本轮解决的历史问题
    raw_output: str = ""  # 评审原始输出（排查解析问题）
    created_at: float = field(default_factory=time.time)


class Blackboard:
    """单报告粒度的黑板（线程安全）。"""

    def __init__(self, report_id: str, goal: str):
        self.report_id = report_id
        self.goal = goal
        self._lock = threading.Lock()
        self._plan: list[SubTask] = []
        self._evidence: list[EvidenceRecord] = []
        self._critiques: list[Critique] = []
        self._seq = 0

    # ---- 计划 ----

    def put_plan(self, tasks: list[SubTask]) -> None:
        with self._lock:
            self._plan = tasks

    def get_plan(self, pending_only: bool = False) -> list[SubTask]:
        with self._lock:
            tasks = list(self._plan)
        if pending_only:
            tasks = [t for t in tasks if t.status == "pending"]
        return tasks

    def update_task(self, task_id: str, status: str, failure_reason: str = "") -> None:
        with self._lock:
            for t in self._plan:
                if t.task_id == task_id:
                    t.status = status
                    if failure_reason:
                        t.failure_reason = failure_reason
                    return

    # ---- 证据 ----

    def append_evidence(self, task_id: str, content: str, tool_name: str, extra: dict | None = None) -> str:
        """原子追加并返回标识（并发写不丢失）。"""
        with self._lock:
            self._seq += 1
            eid = f"e{self._seq}"
            # 同内容去重：跨任务复用记 used_by_tasks（复用是长任务降本关键）
            for rec in self._evidence:
                if rec.content == content:
                    if task_id not in rec.used_by_tasks and rec.task_id != task_id:
                        rec.used_by_tasks.append(task_id)
                    return rec.evidence_id
            self._evidence.append(EvidenceRecord(
                evidence_id=eid, task_id=task_id, content=content,
                tool_name=tool_name, extra=extra or {},
            ))
            return eid

    def list_evidence(self, adopted: bool | None = None, task_id: str | None = None) -> list[EvidenceRecord]:
        with self._lock:
            records = list(self._evidence)
        if adopted is not None:
            records = [r for r in records if r.adopted is adopted]
        if task_id is not None:
            records = [r for r in records if r.task_id == task_id or task_id in r.used_by_tasks]
        return records

    def update_evidence(
        self, evidence_id: str, score: float, adopted: bool, rejected_reason: str = ""
    ) -> None:
        """回填评分与淘汰原因（更新而非删除）。"""
        with self._lock:
            for rec in self._evidence:
                if rec.evidence_id == evidence_id:
                    rec.score = score
                    rec.adopted = adopted
                    rec.rejected_reason = rejected_reason
                    return

    # ---- 评审 ----

    def add_critique(self, critique: Critique) -> None:
        with self._lock:
            self._critiques.append(critique)

    def list_critiques(self) -> list[Critique]:
        with self._lock:
            return list(self._critiques)

    # ---- 视图 ----

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "plan": [
                    {
                        "task_id": t.task_id, "description": t.description,
                        "tool_name": t.tool_name, "depends_on": t.depends_on,
                        "layer": t.layer, "status": t.status,
                        "failure_reason": t.failure_reason,
                    }
                    for t in self._plan
                ],
                "evidence": [
                    {
                        **r.extra,  # 检索附带的 chunk_id/uri/modality 等（先铺底）
                        "evidence_id": r.evidence_id, "task_id": r.task_id,
                        "content": r.content, "tool_name": r.tool_name,
                        "score": r.score, "adopted": r.adopted,
                        "rejected_reason": r.rejected_reason,
                        "used_by_tasks": r.used_by_tasks,
                        "created_at": r.created_at,
                        # 规范字段在后：校验分/采纳标记不被检索侧 extra 覆盖
                    }
                    for r in self._evidence
                ],
                "critiques": [
                    {
                        "round": c.round, "score": c.score, "issues": c.issues,
                        "resolved_issues": c.resolved_issues, "raw_output": c.raw_output,
                        "created_at": c.created_at,
                    }
                    for c in self._critiques
                ],
            }
