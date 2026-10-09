"""多智能体编排器（阶段 8 §8.1 时序）。

Planner → 拓扑分层 → Researcher 同层并行（跨层串行、失败不阻塞、层内超时）→
Validator 评分淘汰 → Writer↔Critic 循环（≤3 轮，反馈落黑板，禁止伪装通过）→ 终稿。

硬约束落实：
- 子任务全部失败 → 不产出空报告（8201，明确失败远好于伪装完成）；
- 达轮次上限 → 返回最后一稿 + accepted=false + 遗留问题清单（8301 语义，正常返回）；
- 评审环节本身异常 → status=unreviewed（与"未通过"严格区分，8401 语义）；
- 每一步状态落盘（用户中途离开回来可见全过程）；
- 各角色调用次数归集到报告（成本可归因）。
"""
import logging
import queue
import threading
from typing import Callable
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict

from app.core.envelope import ApiError, ErrCode
from app.services.agent.tools import TOOL_TIMEOUT_S, ToolRegistry
from app.services.multiagent.blackboard import Blackboard, Critique
from app.services.multiagent.roles import (
    LLMCritic,
    LLMWriter,
    Planner,
    Validator,
)
from app.services.multiagent.store import ReportStore
from app.services.multiagent.topology import topo_layers

logger = logging.getLogger(__name__)

MAX_CRITIC_ROUNDS = 3  # 第 3 轮以后问题通常在数据层而非写作层
LAYER_MAX_WORKERS = 8  # 层内并发上限（防打满外部依赖）


class Orchestrator:
    def __init__(
        self,
        planner: Planner,
        validator: Validator,
        writer: LLMWriter,
        critic,  # LLMCritic | ScriptedCritic
        registry: ToolRegistry,
        reports: ReportStore,
        pass_score: float = 8.0,
        max_rounds: int = MAX_CRITIC_ROUNDS,
        adopt_threshold: float = 0.14,
    ):
        self._planner = planner
        self._validator = validator
        self._writer = writer
        self._critic = critic
        self._registry = registry
        self._reports = reports
        self._pass_score = pass_score
        self._max_rounds = max_rounds
        self._adopt_threshold = adopt_threshold
        # 进度事件回调：(事件名, 载荷) -> None。默认 None（同步跑批不产出事件）。
        # SSE 端点传入自己的推送函数后，8 类事件就实时到前端（stage-8 §4）。
        self._on_event: Callable[[str, dict], None] | None = None

    def set_event_sink(self, sink: Callable[[str, dict], None] | None) -> None:
        """注册进度事件回调（同一编排器实例同时只服务一个 SSE 连接）。"""
        self._on_event = sink

    def _emit(self, event: str, payload: dict) -> None:
        """发进度事件。推送失败绝不打断报告生成——事件是增强，不是主流程。"""
        if self._on_event is None:
            return
        try:
            self._on_event(event, payload)
        except Exception as exc:  # noqa: BLE001
            logger.debug("报告进度事件推送失败（已忽略）: %s", exc)

    # ---- 主流程 ----

    def run(self, report_id: str, tenant_id: str) -> dict:
        report = self._reports.get(report_id)
        goal = report["goal"]
        role_calls = {"planner": 0, "researcher": 0, "validator": 0, "writer": 0, "critic": 0}
        bb = Blackboard(report_id, goal)

        def bump(role: str) -> None:
            role_calls[role] = role_calls.get(role, 0) + 1

        try:
            # ---- 1. 规划 ----
            self._reports.update(report_id, status="planning", role_calls=role_calls)
            self._emit("plan_ready", {"report_id": report_id, "status": "planning"})
            bump("planner")
            title, tasks = self._planner.plan(goal)
            layers = topo_layers(tasks)
            bb.put_plan(tasks)
            self._reports.update(
                report_id, title=title, layers=len(layers),
                blackboard=bb.snapshot(), role_calls=role_calls,
            )
            self._emit("plan_ready", {
                "report_id": report_id, "title": title,
                "subtasks": [asdict(t) for t in tasks],
                "layers": len(layers),
            })

            # ---- 2. 检索（同层并行、跨层串行、失败不阻塞）----
            self._reports.update(report_id, status="researching")
            for layer in layers:
                self._run_layer(bb, layer, tenant_id, bump, report_id)
                self._reports.update(report_id, blackboard=bb.snapshot(), role_calls=role_calls)
                self._emit("evidence_added", {
                    "report_id": report_id,
                    "evidence": bb.snapshot().get("evidence", []),
                    "tasks_done": sum(1 for t in bb.get_plan() if t.status == "done"),
                    "tasks_total": len(bb.get_plan()),
                })

            # ---- 3. 校验（gte-rerank 口径，淘汰保留原因）----
            self._reports.update(report_id, status="validating")
            self._validate(bb, bump, report_id)
            accepted_evidence = [r.__dict__ | r.extra for r in bb.list_evidence(adopted=True)]
            self._reports.update(report_id, blackboard=bb.snapshot(), role_calls=role_calls)
            self._emit("validate_done", {
                "report_id": report_id,
                "total": len(bb.list_evidence(adopted=None)),
                "adopted": len(accepted_evidence),
                "adopt_threshold": self._adopt_threshold,
            })

            failed_tasks = [t for t in bb.get_plan() if t.status == "failed"]
            if not accepted_evidence:
                # 硬约束：不产出空报告
                reason = "全部子任务失败" if len(failed_tasks) == len(bb.get_plan()) else "证据全部被校验淘汰"
                raise ApiError(
                    ErrCode.REPORT_TASKS_ALL_FAILED,
                    f"{reason}，未获取到任何有效信息，不产出空报告", http_status=422,
                )

            # ---- 4. Writer↔Critic 循环 ----
            final = self._write_critique_loop(report_id, goal, bb, accepted_evidence, bump)
            final["role_calls"] = role_calls
            self._reports.update(report_id, blackboard=bb.snapshot(), **final)
            return self._reports.get(report_id)

        except ApiError as exc:
            self._reports.update(
                report_id, status="failed", error=f"[{exc.code}] {exc.message}",
                blackboard=bb.snapshot(), role_calls=role_calls,
            )
            self._emit("finished", {
                "report_id": report_id, "status": "failed",
                "accepted": False, "error": f"[{exc.code}] {exc.message}",
            })
            return self._reports.get(report_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("报告 %s 执行异常", report_id)
            self._reports.update(
                report_id, status="failed", error=str(exc),
                blackboard=bb.snapshot(), role_calls=role_calls,
            )
            self._emit("finished", {
                "report_id": report_id, "status": "failed",
                "accepted": False, "error": str(exc),
            })
            return self._reports.get(report_id)

    # ---- 分层检索 ----

    def _run_layer(self, bb: Blackboard, layer, tenant_id: str, bump, report_id: str = "") -> None:
        def work(task) -> None:
            bb.update_task(task.task_id, "running")
            bump("researcher")
            self._emit("task_start", {
                "report_id": report_id, "task_id": task.task_id,
                "description": task.description, "tool": task.tool_name,
                "status": "running",
            })
            result, _, actual_tool = self._registry.run_with_timeout(
                task.tool_name, task.description, tenant_id
            )
            if result.error:
                bb.update_task(task.task_id, "failed", f"工具异常: {result.error}")
                self._emit("task_end", {
                    "report_id": report_id, "task_id": task.task_id,
                    "status": "failed", "hits": 0, "error": result.error,
                })
                return
            if not result.items:
                bb.update_task(task.task_id, "failed", "无结果（0 条）")
                self._emit("task_end", {
                    "report_id": report_id, "task_id": task.task_id,
                    "status": "failed", "hits": 0, "error": "无结果（0 条）",
                })
                return
            for item in result.items:
                bb.append_evidence(
                    task.task_id, item["content"], actual_tool,
                    extra={k: v for k, v in item.items() if k in ("chunk_id", "uri", "modality", "score")},
                )
            bb.update_task(task.task_id, "done")
            self._emit("task_end", {
                "report_id": report_id, "task_id": task.task_id,
                "status": "done", "hits": len(result.items), "tool": actual_tool,
            })

        workers = min(len(layer), LAYER_MAX_WORKERS)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(work, t): t for t in layer}
            for future in as_completed(futures, timeout=TOOL_TIMEOUT_S * len(layer) + 10):
                task = futures[future]
                try:
                    future.result(timeout=TOOL_TIMEOUT_S)
                except Exception as exc:  # noqa: BLE001
                    bb.update_task(task.task_id, "failed", f"执行异常: {exc}")
                    self._emit("task_end", {
                        "report_id": report_id, "task_id": task.task_id,
                        "status": "failed", "hits": 0, "error": f"执行异常: {exc}",
                    })

    # ---- 校验 ----

    def _validate(self, bb: Blackboard, bump, report_id: str = "") -> None:
        task_by_id = {t.task_id: t for t in bb.get_plan()}
        for record in bb.list_evidence(adopted=None):
            bump("validator")
            task = task_by_id.get(record.task_id)
            query = task.description if task else bb.goal
            try:
                score = self._validator.score(query, record.content)
            except Exception as exc:  # noqa: BLE001
                logger.warning("校验评分失败 %s: %s", record.evidence_id, exc)
                bb.update_evidence(record.evidence_id, 0.0, False, f"评分异常: {exc}")
                continue
            adopted = score >= self._adopt_threshold
            bb.update_evidence(
                record.evidence_id, round(score, 4), adopted,
                "" if adopted else f"相关分 {score:.3f} 低于阈值 {self._adopt_threshold}",
            )

    # ---- Writer↔Critic 循环 ----

    def _write_critique_loop(
        self, report_id: str, goal: str, bb: Blackboard, evidence: list[dict], bump
    ) -> dict:
        critiques: list[Critique] = []
        for round_no in range(1, self._max_rounds + 1):
            # 撰写（反馈可见性：评审意见进 prompt）
            self._reports.update(report_id, status="writing")
            bump("writer")
            draft = self._writer.write(goal, evidence, critiques)
            self._reports.add_draft(report_id, round_no, draft)
            self._emit("draft_ready", {"report_id": report_id, "round": round_no, "draft": draft})

            # 评审
            self._reports.update(report_id, status="critiquing")
            bump("critic")
            try:
                critique = self._critic.critique(goal, draft, evidence, critiques)
            except Exception as exc:  # noqa: BLE001
                # 评审环节本身失败 ≠ 未通过：标记未评审（8401 语义）
                logger.warning("评审环节异常: %s", exc)
                self._emit("critique_done", {
                    "report_id": report_id, "round": round_no,
                    "score": None, "passed": None, "status": "unreviewed",
                    "error": str(exc), "issues": [],
                })
                self._emit("finished", {
                    "report_id": report_id, "status": "unreviewed",
                    "accepted": None, "rounds": round_no - 1,
                })
                return {
                    "status": "unreviewed", "accepted": None,
                    "critic_score": None, "rounds": round_no - 1,
                    "unresolved_issues": [f"评审环节异常: {exc}"],
                }
            bb.add_critique(critique)
            critiques.append(critique)
            self._reports.update(report_id, blackboard=bb.snapshot())
            passed = critique.score >= self._pass_score
            self._emit("critique_done", {
                "report_id": report_id, "round": round_no,
                "score": critique.score, "passed": passed,
                "status": "accepted" if passed else "rejected",
                "pass_score": self._pass_score, "issues": critique.issues,
            })

            if passed:
                self._emit("finished", {
                    "report_id": report_id, "status": "accepted",
                    "accepted": True, "critic_score": critique.score, "rounds": round_no,
                })
                return {
                    "status": "accepted", "accepted": True,
                    "critic_score": critique.score, "rounds": round_no,
                    "unresolved_issues": [],
                }
            # 本轮判否。轮次还有余量就继续改，没有余量由下面的 capped 分支收口。
            self._reports.update(
                report_id,
                rejected_round=round_no,
                rejected_score=round(critique.score, 4),
                rejected_issues=critique.issues,
            )

        # 达轮次上限：诚实返回最后一稿 + 未通过标记 + 遗留问题（禁止伪装通过）。
        #
        # status 取 rejected 还是 capped，取决于"还有没有改的机会"：
        # - max_rounds == 1 → 第一次就被否，没有任何机会再改 → rejected（本轮判否）
        # - max_rounds > 1  → 改满了还没过，是"没机会了" → capped（达上限）
        # 两者都是 accepted=false，语义上不混淆；此前一律记 capped，
        # 导致"第一稿就被否"与"改满仍不过"在报告详情里长得一模一样。
        last = critiques[-1]
        final_status = "rejected" if len(critiques) == 1 else "capped"
        self._emit("finished", {
            "report_id": report_id, "status": final_status, "accepted": False,
            "critic_score": last.score, "rounds": len(critiques),
            "max_rounds": self._max_rounds, "issues": last.issues,
        })
        return {
            "status": final_status, "accepted": False,
            "critic_score": last.score, "rounds": len(critiques),
            "unresolved_issues": last.issues,
        }

    # ---- 异步入口 ----

    def submit(self, goal: str, tenant_id: str, created_by: str) -> dict:
        report = self._reports.create(goal, tenant_id, created_by)
        thread = threading.Thread(
            target=self.run, args=(report["report_id"], tenant_id), daemon=True
        )
        thread.start()
        return report

    def run_stream(self, report_id: str, tenant_id: str):
        """同步产出 8 类进度事件（生成器），供 SSE 端点逐条转发。

        与 `submit`（后台线程）的区别：这里由调用方在自己的线程里消费生成器，
        事件边产生边发，客户端能看到实时进度；`submit` 之后只能轮询。
        """
        sink = self._on_event
        # 局部变量不能叫 queue：会遮蔽同名模块，queue.Queue() 直接抛
        # UnboundLocalError（import 了却用不了，最难查的一类错）
        events: queue.Queue = queue.Queue()
        done = object()

        def push(event: str, payload: dict) -> None:
            events.put((event, payload))

        self._on_event = push

        def work() -> None:
            """跑报告并保证一定会往队列里放终止标记。

            用 try/finally 而不是 `lambda: (self.run(...), events.put(done))`：
            后者在 run 抛异常时根本执行不到 put，消费者会永久阻塞在
            events.get() 上——线上表现是 SSE 连接一直转圈直到 nginx
            read_timeout（600s）才断，用户既看不到错误也看不到结束。
            """
            error: str | None = None
            try:
                self.run(report_id, tenant_id)
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                logger.exception("报告流式生成失败 report_id=%s", report_id)
            finally:
                if error is not None:
                    events.put(("error", {
                        "report_id": report_id,
                        "message": "报告生成失败，请稍后重试或联系管理员",
                        "detail": error,
                    }))
                events.put(done)

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        try:
            while True:
                item = events.get()
                if item is done:
                    return
                yield item
        finally:
            # 无论消费方是否断开，都要摘掉 sink，否则下一次 run 会往死信队列推
            self._on_event = sink
            worker.join(timeout=0.1)
