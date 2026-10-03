"""自动评测（阶段 9 §7）：轻量自建指标（CI 用，快；RAGAS 跑 Nightly 属扩展）。

样本四字段：question / gold_answer / gold_evidence / should_refuse。
指标：
- Faithfulness（轻量代理）：答案句子被证据文本支持的比例（词面重合）；
- Citation Coverage：引用编号在证据范围内的有效比例；
- Refusal Accuracy：应拒样本中确实拒答的比例。

运行记录绑定评测集版本 + 模型版本（缺一结论不可信——平台升级应用侧无法拒绝）。
"""
import json
import logging
import re
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_CITE_RE = re.compile(r"\[#(\d+)\]")


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]{2,}", text))


class EvalStore:
    def __init__(self, eval_dir: Path):
        self._dir = eval_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    # ---- 评测集 ----

    def save_dataset(self, name: str, version: str, samples: list[dict]) -> dict:
        for s in samples:
            missing = {"question", "gold_answer", "gold_evidence", "should_refuse"} - set(s)
            if missing:
                raise ValueError(f"样本缺字段 {missing}（四字段缺一不可，§7.2）")
        path = self._dir / f"dataset-{name}@{version}.json"
        path.write_text(json.dumps({"name": name, "version": version, "samples": samples}, ensure_ascii=False), encoding="utf-8")
        return {"name": name, "version": version, "count": len(samples)}

    def list_datasets(self) -> list[dict]:
        datasets = []
        for path in self._dir.glob("dataset-*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                datasets.append({"name": data["name"], "version": data["version"], "count": len(data["samples"])})
            except Exception:  # noqa: BLE001
                continue
        return sorted(datasets, key=lambda d: (d["name"], d["version"]))

    def load_dataset(self, name: str, version: str) -> list[dict]:
        path = self._dir / f"dataset-{name}@{version}.json"
        if not path.exists():
            raise FileNotFoundError(f"评测集不存在: {name}@{version}")
        return json.loads(path.read_text(encoding="utf-8"))["samples"]

    # ---- 运行记录 ----

    def save_run(self, run: dict) -> None:
        (self._dir / f"run-{run['run_id']}.json").write_text(
            json.dumps(run, ensure_ascii=False, default=str), encoding="utf-8"
        )

    def list_runs(self) -> list[dict]:
        runs = []
        for path in self._dir.glob("run-*.json"):
            try:
                runs.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                continue
        return sorted(runs, key=lambda r: -r["created_at"])

    def get_run(self, run_id: str) -> dict | None:
        path = self._dir / f"run-{run_id}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


class EvalRunner:
    """逐样本跑问答流水线并计算三指标。"""

    def __init__(self, store: EvalStore, ask_fn, model_version: str):
        # ask_fn: (question) -> dict（含 answer / evidence / verdict）
        self._store = store
        self._ask = ask_fn
        self._model_version = model_version

    def run(self, dataset_name: str, dataset_version: str) -> dict:
        samples = self._store.load_dataset(dataset_name, dataset_version)
        run_id = uuid.uuid4().hex[:12]
        details = []
        faith_sum = cite_sum = 0.0
        refuse_total = refuse_correct = 0
        for i, sample in enumerate(samples):
            result = self._ask(sample["question"])
            answer = result.get("answer", "")
            evidence = result.get("evidence", [])
            verdict = result.get("verdict", "answered")

            # Faithfulness 代理：答案句被证据支持的比例
            faith = self._faithfulness(answer, evidence)
            # Citation：引用编号有效比例
            cite = self._citation_accuracy(answer, len(evidence))
            # Refusal
            refuse_ok = None
            if sample["should_refuse"]:
                refuse_total += 1
                refuse_ok = verdict == "refused"
                refuse_correct += int(refuse_ok)

            faith_sum += faith
            cite_sum += cite
            details.append({
                "idx": i, "question": sample["question"][:80],
                "verdict": verdict, "faithfulness": round(faith, 4),
                "citation": round(cite, 4), "should_refuse": sample["should_refuse"],
                "refuse_ok": refuse_ok,
                "answer_hash": __import__("hashlib").sha256(answer.encode()).hexdigest()[:16],
            })

        n = max(len(samples), 1)
        run = {
            "run_id": run_id,
            "dataset": dataset_name,
            "dataset_version": dataset_version,
            "model_version": self._model_version,
            "sample_count": len(samples),
            "faithfulness": round(faith_sum / n, 4),
            "citation_accuracy": round(cite_sum / n, 4),
            "refusal_accuracy": round(refuse_correct / refuse_total, 4) if refuse_total else None,
            "created_at": time.time(),
            "details": details,
        }
        self._store.save_run(run)
        return run

    @staticmethod
    def _faithfulness(answer: str, evidence: list[dict]) -> float:
        sentences = [s.strip() for s in re.split(r"[。！？!?\n]", answer) if len(s.strip()) > 8]
        if not sentences:
            return 0.0
        evidence_text = " ".join(e.get("content", "") for e in evidence)
        evidence_tokens = _tokens(evidence_text)
        supported = 0
        for s in sentences:
            s_clean = _CITE_RE.sub("", s)
            s_tokens = _tokens(s_clean)
            if not s_tokens:
                continue
            overlap = len(s_tokens & evidence_tokens) / len(s_tokens)
            if overlap >= 0.5:
                supported += 1
        return supported / len(sentences)

    @staticmethod
    def _citation_accuracy(answer: str, evidence_count: int) -> float:
        cited = [int(n) for n in _CITE_RE.findall(answer)]
        if not cited:
            return 1.0 if evidence_count == 0 else 0.0
        valid = sum(1 for n in cited if 1 <= n <= evidence_count)
        return valid / len(cited)
