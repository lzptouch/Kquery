"""自动评测（阶段 9 §7）：轻量自建指标（CI 用，快；RAGAS 跑 Nightly 属扩展）。

样本四字段：question / gold_answer / gold_evidence / should_refuse。
指标：
- Faithfulness（轻量代理）：答案句子被证据文本支持的比例（词面重合）；
- Citation Accuracy：引用编号在证据范围内的有效比例；
- Refusal Accuracy：应拒样本中确实拒答的比例；
- Coverage：gold_evidence 被答案真正引用的比例——**这是唯一能把"检索没找到"与
  "生成写错了"分开的指标**。缺了它，检索退化会被误判成生成退化，调优方向直接跑偏。

运行记录绑定**三个**版本：评测集版本 + 模型档位 + 业务代码版本。只记模型版本
不够（指标变了到底是平台换版还是我们改了代码，无法解释）；只记代码版本同理。
"""
import hashlib
import inspect
import json
import logging
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_CITE_RE = re.compile(r"\[#(\d+)\]")
METRIC_KEYS = ("faithfulness", "citation_accuracy", "refusal_accuracy", "coverage")
CODE_VERSION_ENV = "KQUERY_CODE_VERSION"
UNKNOWN_VERSION = "unknown"
# 忠实度低于该值的样本计为"失败样本"（compare 端点据此找新增/修复的错误）
FAITHFULNESS_FAIL_TH = 0.5
# gold_evidence 与被引用证据的词面重合阈值
GOLD_HIT_TH = 0.5
_REPO_ROOT = Path(__file__).resolve().parents[3]


def detect_code_version() -> str:
    """业务代码版本：环境变量优先，其次 git 短 SHA，都取不到则 "unknown"。

    取版本失败绝不能让评测跑不起来：评测的价值在于记录"这次测的是什么"，
    记录不到也要留下一条可辨识的 unknown，而不是抛错中断整轮评测。
    """
    env_value = (os.environ.get(CODE_VERSION_ENV) or "").strip()
    if env_value:
        return env_value
    for cwd in (None, str(_REPO_ROOT)):
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=3.0, cwd=cwd, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("读取 git 版本失败（cwd=%s）: %s", cwd, exc)
            continue
        sha = (proc.stdout or "").strip()
        if proc.returncode == 0 and sha:
            return sha
    logger.info(
        "无法确定业务代码版本（未设置 %s 且不在 git 仓库内），记为 %s",
        CODE_VERSION_ENV, UNKNOWN_VERSION,
    )
    return UNKNOWN_VERSION


def _ask_arity(ask_fn) -> int:
    """探测 ask_fn 参数个数。

    既有注入点是单参 (question)；要带上租户过滤（按调用主体的租户检索）需要
    (question, tenant_id)。两种都支持，避免为了评测口径去改既有注入点。
    """
    try:
        return len(inspect.signature(ask_fn).parameters)
    except (TypeError, ValueError):
        return 1


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

    def compare(self, run_id_a: str, run_id_b: str) -> dict:
        """两次运行的对比（任一不存在即抛 FileNotFoundError，由 API 层转 404）。"""
        run_a, run_b = self.get_run(run_id_a), self.get_run(run_id_b)
        missing = [r for r, v in ((run_id_a, run_a), (run_id_b, run_b)) if v is None]
        if missing:
            raise FileNotFoundError(f"评测运行不存在: {', '.join(missing)}")
        return compare_runs(run_a, run_b)


def sample_failed(detail: dict | None) -> bool:
    """样本是否算"失败"：未评测 / 执行异常 / 该拒未拒 / 忠实度过低 / 期望证据一条没被引用。"""
    if not detail:
        return True
    if detail.get("error"):
        return True
    if detail.get("should_refuse") and not detail.get("refuse_ok"):
        return True
    if float(detail.get("faithfulness") or 0.0) < FAITHFULNESS_FAIL_TH:
        return True
    coverage = detail.get("coverage")
    return coverage is not None and float(coverage) == 0.0


def failure_reasons(detail: dict | None) -> list[str]:
    """失败原因清单：只给指标不给原因，运维就得自己重跑一遍去找。"""
    detail = detail or {}
    reasons: list[str] = []
    if detail.get("error"):
        reasons.append(f"执行错误：{detail['error']}")
    if detail.get("should_refuse") and not detail.get("refuse_ok"):
        reasons.append("应拒未拒")
    faith = float(detail.get("faithfulness") or 0.0)
    if faith < FAITHFULNESS_FAIL_TH:
        reasons.append(f"忠实度 {faith:.2f} < {FAITHFULNESS_FAIL_TH}")
    coverage = detail.get("coverage")
    if coverage is not None and float(coverage) == 0.0:
        reasons.append(f"期望证据 0/{detail.get('gold_total', 0)} 未被引用")
    return reasons


def _run_brief(run: dict) -> dict:
    return {
        k: run.get(k)
        for k in ("run_id", "dataset", "dataset_version", "model_version", "code_version",
                  "path", "sample_count", *METRIC_KEYS, "created_at")
    }


def compare_runs(run_a: dict, run_b: dict) -> dict:
    """两次运行的逐指标对比 + 失败样本差异。

    前端要展示 "v1.0 vs v1.2.0 逐指标 ↑+3.5%"，需要三样东西：两边取值、
    绝对差与相对变化率，以及"上次失败这次好了 / 上次好了这次败了"的样本清单——
    只看均值会掩盖"均值没变但错误类型换了"的情况。
    """
    metrics = []
    for key in METRIC_KEYS:
        value_a, value_b = run_a.get(key), run_b.get(key)
        delta = (
            None if value_a is None or value_b is None
            else round(float(value_b) - float(value_a), 4)
        )
        delta_pct = None if delta is None or not value_a else round(delta / float(value_a) * 100, 2)
        metrics.append({
            "metric": key, "value_a": value_a, "value_b": value_b,
            "delta": delta, "delta_pct": delta_pct,
        })

    map_a = {d.get("idx"): d for d in run_a.get("details") or []}
    map_b = {d.get("idx"): d for d in run_b.get("details") or []}
    regressions: list[dict] = []
    fixed: list[dict] = []
    for idx in sorted({*map_a, *map_b}, key=lambda v: (v is None, v)):
        detail_a, detail_b = map_a.get(idx), map_b.get(idx)
        failed_b, failed_a = sample_failed(detail_b), sample_failed(detail_a)
        if failed_b and not failed_a:
            regressions.append({
                "idx": idx, "question": (detail_b or {}).get("question", ""),
                "reasons": failure_reasons(detail_b),
            })
        elif failed_a and not failed_b:
            fixed.append({
                "idx": idx, "question": (detail_b or detail_a or {}).get("question", ""),
                "reasons": failure_reasons(detail_a),
            })

    same_dataset = (
        (run_a.get("dataset"), run_a.get("dataset_version"))
        == (run_b.get("dataset"), run_b.get("dataset_version"))
    )
    return {
        "a": _run_brief(run_a),
        "b": _run_brief(run_b),
        "same_dataset": same_dataset,
        "comparable": same_dataset,
        "note": "" if same_dataset else "两次运行使用了不同评测集或版本，逐样本失败差异不可比",
        "metrics": metrics,
        "regressions": regressions,
        "fixed": fixed,
        "regression_count": len(regressions),
        "fixed_count": len(fixed),
    }


def _gold_is_cited(gold, cited: list[int], evidence: list[dict]) -> bool:
    """gold_evidence 视为"被引用"的两种形态：
    1) 序号型（int 或 "2"）：答案里出现 [#2] 即可；
    2) 文本型：与被引用证据的词面重合 ≥ GOLD_HIT_TH（评测集的 gold_evidence 约定是文本）。
    """
    if isinstance(gold, bool):
        return False
    if isinstance(gold, int) or (isinstance(gold, str) and gold.isdigit()):
        return int(gold) in cited
    text = gold.get("content") or gold.get("text") or "" if isinstance(gold, dict) else str(gold or "")
    gold_tokens = _tokens(text)
    if not gold_tokens:
        return False
    for index in cited:
        if 1 <= index <= len(evidence):
            other = _tokens(str(evidence[index - 1].get("content", "")))
            if gold_tokens and len(gold_tokens & other) / len(gold_tokens) >= GOLD_HIT_TH:
                return True
    return False


class EvalRunner:
    """逐样本跑问答流水线并计算四指标。"""

    def __init__(
        self,
        store: EvalStore,
        ask_fn,
        model_version: str,
        code_version: str | None = None,
        path: str = "agent",
        default_tenant_id: str = "default",
    ):
        # ask_fn: (question) 或 (question, tenant_id) -> dict（含 answer / evidence / verdict）
        self._store = store
        self._ask = ask_fn
        self._ask_arity = _ask_arity(ask_fn)
        self._model_version = model_version
        self._code_version = code_version or detect_code_version()
        # path 记录评测走的是哪条链路：阶段 5 的 faithfulness 自检与 CRAG 只在线性路径产出，
        # 走 Agent 路径只会拿到最终答案，两种口径混在一起做的对比没有意义。
        self._path = path
        self._default_tenant_id = default_tenant_id

    def run(self, dataset_name: str, dataset_version: str) -> dict:
        samples = self._store.load_dataset(dataset_name, dataset_version)
        run_id = uuid.uuid4().hex[:12]
        details = []
        faith_sum = cite_sum = 0.0
        cov_sum, cov_count = 0.0, 0
        refuse_total = refuse_correct = 0
        for i, sample in enumerate(samples):
            detail = self._eval_one(i, sample)
            details.append(detail)
            faith_sum += detail["faithfulness"]
            cite_sum += detail["citation"]
            if detail["coverage"] is not None:
                cov_sum += detail["coverage"]
                cov_count += 1
            if detail["should_refuse"]:
                refuse_total += 1
                refuse_correct += int(bool(detail["refuse_ok"]))

        n = max(len(samples), 1)
        run = {
            "run_id": run_id,
            "dataset": dataset_name,
            "dataset_version": dataset_version,
            "model_version": self._model_version,
            "code_version": self._code_version,
            "path": self._path,
            "sample_count": len(samples),
            "faithfulness": round(faith_sum / n, 4),
            "citation_accuracy": round(cite_sum / n, 4),
            "refusal_accuracy": round(refuse_correct / refuse_total, 4) if refuse_total else None,
            "coverage": round(cov_sum / cov_count, 4) if cov_count else None,
            "created_at": time.time(),
            "details": details,
        }
        self._store.save_run(run)
        return run

    def _eval_one(self, idx: int, sample: dict) -> dict:
        """单样本评测；执行异常也记录，**不中断整轮**——中断会让"这次比上次差"做不出来。"""
        tenant_id = sample.get("tenant_id") or self._default_tenant_id
        gold_evidence = sample.get("gold_evidence") or []
        try:
            if self._ask_arity >= 2:
                result = self._ask(sample["question"], tenant_id)
            else:
                result = self._ask(sample["question"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("评测样本 %d 执行失败: %s", idx, exc)
            return {
                "idx": idx, "question": sample["question"][:80], "verdict": "error",
                "faithfulness": 0.0, "citation": 0.0,
                "coverage": 0.0 if gold_evidence else None,
                "gold_hit": 0, "gold_total": len(gold_evidence),
                "should_refuse": sample["should_refuse"],
                "refuse_ok": False if sample["should_refuse"] else None,
                "answer_hash": "", "error": str(exc)[:200],
            }

        answer = result.get("answer", "")
        evidence = result.get("evidence", [])
        verdict = result.get("verdict", "answered")
        faith = self._faithfulness(answer, evidence)
        cite = self._citation_accuracy(answer, len(evidence))
        cited = sorted({int(n) for n in _CITE_RE.findall(answer)})
        # 未标注 gold_evidence 的样本返回 None 并从均值中剔除——
        # 把"没标期望证据"当成 0 分，指标会随标注习惯漂移，跨版本就失去可比性
        coverage = None
        gold_hit = 0
        if gold_evidence:
            gold_hit = sum(1 for gold in gold_evidence if _gold_is_cited(gold, cited, evidence))
            coverage = round(gold_hit / len(gold_evidence), 4)
        refuse_ok = None
        if sample["should_refuse"]:
            refuse_ok = verdict == "refused"
        return {
            "idx": idx, "question": sample["question"][:80], "verdict": verdict,
            "faithfulness": round(faith, 4), "citation": round(cite, 4),
            "coverage": coverage, "gold_hit": gold_hit, "gold_total": len(gold_evidence),
            "should_refuse": sample["should_refuse"], "refuse_ok": refuse_ok,
            "answer_hash": hashlib.sha256(answer.encode()).hexdigest()[:16],
        }

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
