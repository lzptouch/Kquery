"""报告存储（阶段 8 §6）：报告 + 子任务 + 证据 + 每轮草稿 + 每轮评审。

- 草稿"每轮一份"而非只留最新（评估评审机制价值的唯一客观依据）；
- 评审记录逐轮追加不可覆盖；
- 九态状态机：planning/researching/validating/writing/critiquing/
  accepted/rejected/capped/unreviewed（未通过与未评审严格区分）；
- JSON 落盘（单实例方案），每轮结束即持久化——用户中途离开回来可见全过程。
"""
import json
import secrets
import threading
import time
from pathlib import Path

REPORT_STATUS = (
    "planning", "researching", "validating", "writing", "critiquing",
    "accepted", "rejected", "capped", "unreviewed", "failed",
)


class ReportStore:
    def __init__(self, reports_dir: Path):
        self._dir = reports_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._reports: dict[str, dict] = {}
        for path in self._dir.glob("*.json"):
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
                self._reports[report["report_id"]] = report
            except Exception:  # noqa: BLE001
                continue

    def create(self, goal: str, tenant_id: str, created_by: str) -> dict:
        report_id = f"rpt-{secrets.token_urlsafe(6)}"
        report = {
            "report_id": report_id,
            "title": goal[:30],
            "goal": goal,
            "tenant_id": tenant_id,
            "created_by": created_by,
            "status": "planning",
            "accepted": None,  # None=进行中/未评审
            "critic_score": None,
            "rounds": 0,
            "layers": 0,
            "role_calls": {},  # 各角色调用次数（成本归集到报告）
            "unresolved_issues": [],
            "drafts": [],  # 每轮一稿 [{round, content, word_count, created_at}]
            "blackboard": None,  # 黑板快照（plan/evidence/critiques）
            "created_at": time.time(),
            "updated_at": time.time(),
            "error": "",
        }
        with self._lock:
            self._reports[report_id] = report
            self._save(report)
        return report

    def update(self, report_id: str, **fields) -> dict:
        with self._lock:
            report = self._reports[report_id]
            report.update(fields)
            report["updated_at"] = time.time()
            self._save(report)
            return dict(report)

    def add_draft(self, report_id: str, round_no: int, content: str) -> None:
        with self._lock:
            report = self._reports[report_id]
            report["drafts"].append({
                "round": round_no, "content": content,
                "word_count": len(content), "created_at": time.time(),
            })
            report["updated_at"] = time.time()
            self._save(report)

    def get(self, report_id: str) -> dict | None:
        with self._lock:
            report = self._reports.get(report_id)
            return dict(report) if report else None

    def list(self, tenant_id: str = "default") -> list[dict]:
        with self._lock:
            reports = [r for r in self._reports.values() if r["tenant_id"] == tenant_id]
        reports.sort(key=lambda r: -r["created_at"])
        return [
            {
                "report_id": r["report_id"], "title": r["title"], "status": r["status"],
                "accepted": r["accepted"], "critic_score": r["critic_score"],
                "rounds": r["rounds"], "layers": r["layers"],
                "created_at": r["created_at"], "updated_at": r["updated_at"],
            }
            for r in reports
        ]

    def delete(self, report_id: str) -> bool:
        with self._lock:
            if report_id not in self._reports:
                return False
            del self._reports[report_id]
            (self._dir / f"{report_id}.json").unlink(missing_ok=True)
            return True

    def _save(self, report: dict) -> None:
        (self._dir / f"{report['report_id']}.json").write_text(
            json.dumps(report, ensure_ascii=False, default=str), encoding="utf-8"
        )
