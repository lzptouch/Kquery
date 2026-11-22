#!/usr/bin/env python3
"""upload_eval 端到端评估执行器（计划 §5）。

三阶段：reset（可选）→ upload（逐文件同步上传）→ ask（按 facts.jsonl 提问）→ score（离线评分）。
只用 stdlib，零第三方依赖。

用法::

    # L0（mock 后端）
    python scripts/eval/upload_e2e_eval.py --mode l0 --reset

    # L1（live 后端）
    python scripts/eval/upload_e2e_eval.py --mode l1 --reset --concurrency 3

    # 离线复算
    python scripts/eval/upload_e2e_eval.py --score-only

    # 基线门禁
    python scripts/eval/upload_e2e_eval.py --score-only --baseline tests/data/upload_eval/report.base.json --max-drop 0.05
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EVAL_DIR = REPO / "tests" / "data" / "upload_eval"
CORPUS = EVAL_DIR / "corpus"
RUNS = EVAL_DIR / "runs"

BOUNDARY = "----upeval7f2c1a9d"


def _normalize(text: str) -> str:
    """归一化：全半角统一 + 去全部空白。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


class Http:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")

    def _request(self, method: str, path: str, body: bytes | None, headers: dict, timeout: float) -> tuple[int, dict]:
        req = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                payload = exc.read().decode("utf-8", errors="replace")
                try:
                    return exc.code, json.loads(payload)
                except json.JSONDecodeError:
                    return exc.code, {"raw": payload}
            except urllib.error.URLError as exc:
                if attempt < 3:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError(f"{method} {path} 连接失败: {exc}") from exc
        raise RuntimeError(f"{method} {path} 重试耗尽")

    def get(self, path: str, timeout: float = 30) -> tuple[int, dict]:
        return self._request("GET", path, None, {}, timeout)

    def delete(self, path: str, timeout: float = 30) -> tuple[int, dict]:
        return self._request("DELETE", path, None, {}, timeout)

    def post_json(self, path: str, payload: dict, timeout: float = 120) -> tuple[int, dict]:
        return self._request(
            "POST", path, json.dumps(payload).encode("utf-8"),
            {"Content-Type": "application/json"}, timeout,
        )

    def post_file(self, path: str, file: Path, timeout: float = 120) -> tuple[int, dict]:
        payload = file.read_bytes()
        body = (
            f"--{BOUNDARY}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{file.name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8") + payload + f"\r\n--{BOUNDARY}--\r\n".encode("utf-8")
        return self._request(
            "POST", path, body,
            {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}, timeout,
        )


def phase_reset(http: Http) -> None:
    status, env = http.get("/knowledge/files")
    data = env.get("data")
    files = data if isinstance(data, list) else (data or {}).get("files") or []
    print(f"[reset] 现有文档 {len(files)} 个，逐一删除")
    for f in files:
        doc_id = f.get("doc_id") or f.get("file_id")
        if doc_id:
            http.delete(f"/knowledge/files/{doc_id}")


def phase_upload(http: Http) -> list[dict]:
    records: list[dict] = []
    files = sorted(p for p in CORPUS.rglob("*") if p.is_file())
    for path in files:
        rel = path.relative_to(CORPUS).as_posix()
        t0 = time.perf_counter()
        status, env = http.post_file("/knowledge/upload", path)
        elapsed = time.perf_counter() - t0
        data = env.get("data") or {}
        error = env.get("error") or {}
        rec = {
            "file": rel,
            "http_status": status,
            "status": data.get("status"),
            "chunks": data.get("chunks"),
            "doc_id": data.get("doc_id"),
            "error_code": error.get("code"),
            "error_message": (error.get("message") or "")[:120],
            "elapsed_s": round(elapsed, 3),
        }
        records.append(rec)
        mark = "OK " if (status == 200 and rec["status"] == "indexed") else "FAIL"
        print(f"[upload] {mark} {rel:<26} http={status} status={rec['status']} chunks={rec['chunks']} err={rec['error_code']}")
    return records


def phase_ask(http: Http, facts: list[dict], mode: str, concurrency: int) -> list[dict]:
    jobs: list[dict] = []
    for rec in facts:
        for fact in rec["facts"]:
            if mode == "l0" and not fact.get("l0", True):
                jobs.append({"kind": "skip", "file": rec["file"], "fact_id": fact["fact_id"], "reason": "l0_mode=link_only"})
                continue
            jobs.append({"kind": "fact", "file": rec["file"], **fact})
        for nq in rec["negative_questions"]:
            if mode == "l0" and not nq.get("l0", True):
                jobs.append({"kind": "skip", "file": rec["file"], "fact_id": "neg", "reason": "L0 不断言拒答（mock 无相关度下限）"})
                continue
            jobs.append({"kind": "negative", "file": rec["file"], **nq})

    def run(job: dict) -> dict:
        if job["kind"] == "skip":
            return job
        t0 = time.perf_counter()
        status, env = http.post_json("/ask", {"question": job["question"], "top_k": 10})
        elapsed = time.perf_counter() - t0
        data = env.get("data") or {}
        return {
            **job,
            "http_status": status,
            "answer": (data.get("answer") or "")[:400],
            "verdict": data.get("verdict"),
            "answer_source": data.get("answer_source"),
            "refusal_reason": data.get("refusal_reason"),
            "evidence": [
                {"content": (e.get("content") or "")[:600], "filename": e.get("filename")}
                for e in (data.get("evidence") or [])
            ],
            "trace_id": data.get("trace_id"),
            "elapsed_s": round(elapsed, 3),
        }

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for out in pool.map(run, jobs):
            results.append(out)
            if out["kind"] != "skip":
                print(f"[ask] {out['file']:<26} verdict={out.get('verdict')} src={out.get('answer_source')} {out.get('elapsed_s')}s")
    return results


# ---------------------------------------------------------------------------
# 评分
# ---------------------------------------------------------------------------

def _citation_ok(answer: str, n_evidence: int) -> bool:
    refs = re.findall(r"\[#(\d+)\]", answer)
    return all(1 <= int(n) <= n_evidence for n in refs)


def score(upload: list[dict], answers: list[dict], negative_cases: list[dict], mode: str) -> dict:
    neg_by_file = {c["file"]: c for c in negative_cases}
    upload_rows = []
    for rec in upload:
        rel = rec["file"]
        if rel.startswith("negative/"):
            spec = neg_by_file.get(rel, {})
            if spec.get("expect_code"):
                # 同步上传的扩展名前置校验走旧码 2003（通用提示），路由层才给 3101（可读提示）
                passed = rec["http_status"] == 400 and rec["error_code"] in (2003, spec["expect_code"])
                if passed and rec["error_code"] == 3101 and spec.get("hint_contains"):
                    passed = spec["hint_contains"] in (rec["error_message"] or "")
            else:
                # corrupt.pdf / legacy.doc：不接受静默成功（无论错误码是什么）
                passed = not (rec["http_status"] == 200 and rec["status"] == "indexed")
            upload_rows.append({**rec, "kind": "negative", "passed": passed})
        else:
            passed = rec["http_status"] == 200 and rec["status"] == "indexed" and (rec["chunks"] or 0) > 0
            upload_rows.append({**rec, "kind": "positive", "passed": passed})

    answer_rows = []
    for a in answers:
        if a["kind"] == "skip":
            answer_rows.append({**a, "skipped": True})
            continue
        joined_evidence = _normalize(" ".join(e["content"] for e in a.get("evidence") or []))
        norm_answer = _normalize(a.get("answer") or "")
        if a["kind"] == "negative":
            passed = a.get("verdict") == "refused" or a.get("answer_source") == "parametric"
            answer_rows.append({**a, "passed": passed})
            continue
        subs = [_normalize(s) for s in a["expected_contains"]]
        evidence_hit = any(s in joined_evidence for s in subs)
        target_name = Path(a["file"]).name
        provenance_hit = evidence_hit and any(
            e["filename"] == target_name and any(s in _normalize(e["content"]) for s in subs)
            for e in a.get("evidence") or []
        )
        answer_hit = any(s in norm_answer for s in subs) if mode == "l1" else None
        citation_ok = _citation_ok(a.get("answer") or "", len(a.get("evidence") or [])) if mode == "l1" else None
        not_refused = a.get("verdict") != "refused"
        answer_rows.append({
            **a, "passed": evidence_hit and not_refused,
            "evidence_hit": evidence_hit, "provenance_hit": provenance_hit,
            "answer_hit": answer_hit, "citation_ok": citation_ok, "not_refused": not_refused,
        })

    def rate(rows, key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return round(sum(1 for v in vals if v) / len(vals), 4) if vals else None

    def by_dir(rows, key_fn):
        groups: dict[str, list] = {}
        for r in rows:
            groups.setdefault(r["file"].split("/")[0], []).append(r)
        return {d: key_fn(rs) for d, rs in sorted(groups.items())}

    pos_up = [r for r in upload_rows if r["kind"] == "positive"]
    neg_up = [r for r in upload_rows if r["kind"] == "negative"]
    fact_rows = [r for r in answer_rows if r.get("kind") == "fact"]
    neg_rows = [r for r in answer_rows if r.get("kind") == "negative"]
    skipped = [r for r in answer_rows if r.get("skipped")]

    totals = {
        "upload_success": rate(pos_up, "passed"),
        "negative_upload_correct": rate(neg_up, "passed"),
        "evidence_recall": rate(fact_rows, "evidence_hit"),
        "provenance_accuracy": rate(fact_rows, "provenance_hit"),
        "answer_recall": rate(fact_rows, "answer_hit"),
        "positive_refused_rate": rate(fact_rows, "not_refused") if not fact_rows else None,
        "negative_correct": rate(neg_rows, "passed"),
        "citation_consistency": rate(fact_rows, "citation_ok"),
        "counts": {
            "positive_files": len(pos_up), "negative_files": len(neg_up),
            "fact_questions": len(fact_rows), "negative_questions": len(neg_rows),
            "skipped_questions": len(skipped),
        },
    }
    # positive_refused_rate 语义反转：not_refused 的命中率即"未误拒答率"
    if totals["positive_refused_rate"] is not None:
        totals["positive_not_refused_rate"] = totals.pop("positive_refused_rate")
    report = {
        "mode": mode,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "corpus_sha256": hashlib.sha256((EVAL_DIR / "facts.jsonl").read_bytes()).hexdigest()[:12],
        "totals": totals,
        "by_dir": {
            "upload_success": by_dir(pos_up, lambda rs: rate(rs, "passed")),
            "evidence_recall": by_dir(fact_rows, lambda rs: rate(rs, "evidence_hit")),
            "answer_recall": by_dir(fact_rows, lambda rs: rate(rs, "answer_hit")),
        },
        "failures": {
            "upload": [r["file"] for r in pos_up if not r["passed"]],
            "negative_upload": [r["file"] for r in neg_up if not r["passed"]],
            "evidence": [f"{r['file']}::{r['fact_id']}" for r in fact_rows if not r.get("evidence_hit")],
            "provenance": [f"{r['file']}::{r['fact_id']}" for r in fact_rows if r.get("evidence_hit") and not r.get("provenance_hit")],
            "answer": [f"{r['file']}::{r['fact_id']}" for r in fact_rows if r.get("answer_hit") is False],
            "negative": [f"{r['file']}::{r['question'][:20]}" for r in neg_rows if not r.get("passed")],
        },
    }
    return report


def print_summary(report: dict) -> None:
    t = report["totals"]
    print("\n## upload_eval 汇总")
    print("| 指标 | 值 |")
    print("| --- | --- |")
    for key in ("upload_success", "negative_upload_correct", "evidence_recall", "provenance_accuracy", "answer_recall", "negative_correct", "citation_consistency", "positive_not_refused_rate"):
        if t.get(key) is not None:
            print(f"| {key} | {t[key]} |")
    print(f"\n计数：{json.dumps(t['counts'], ensure_ascii=False)}")
    print("\n### 分目录（上传成功率 / 证据召回 / 答案召回）")
    dirs = sorted(set(report["by_dir"]["upload_success"]) | set(report["by_dir"]["evidence_recall"]))
    print("| 目录 | 上传 | 证据 | 答案 |")
    print("| --- | --- | --- | --- |")
    for d in dirs:
        up = report["by_dir"]["upload_success"].get(d)
        ev = report["by_dir"]["evidence_recall"].get(d)
        an = report["by_dir"]["answer_recall"].get(d)
        print(f"| {d} | {up} | {ev} | {an} |")
    if any(report["failures"].values()):
        print("\n失败明细：")
        print(json.dumps(report["failures"], ensure_ascii=False, indent=2))


def check_baseline(report: dict, baseline_path: Path, max_drop: float) -> bool:
    base = json.loads(baseline_path.read_text(encoding="utf-8"))
    ok = True
    for key in ("evidence_recall", "answer_recall", "provenance_accuracy", "negative_correct"):
        now, before = report["totals"].get(key), base["totals"].get(key)
        if now is None or before is None:
            continue
        if before - now > max_drop:
            print(f"[gate] {key} 回退 {before} -> {now}（超阈值 {max_drop}）")
            ok = False
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://127.0.0.1:8100")
    ap.add_argument("--mode", choices=["l0", "l1"], default="l0")
    ap.add_argument("--reset", action="store_true", help="先删除知识库中全部文档")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--score-only", action="store_true", help="只基于 runs/ 落盘记录离线评分")
    ap.add_argument("--baseline", type=Path, default=None)
    ap.add_argument("--max-drop", type=float, default=0.05)
    args = ap.parse_args()

    facts = [json.loads(line) for line in (EVAL_DIR / "facts.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    negative_cases = json.loads((EVAL_DIR / "negative_cases.json").read_text(encoding="utf-8"))

    RUNS.mkdir(parents=True, exist_ok=True)
    if args.score_only:
        upload = [json.loads(l) for l in (RUNS / "upload.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        answers = [json.loads(l) for l in (RUNS / "answers.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        mode = json.loads((RUNS / "meta.json").read_text(encoding="utf-8"))["mode"]
    else:
        mode = args.mode
        http = Http(args.base_url)
        status, env = http.get("/healthz")
        if status != 200:
            raise SystemExit(f"后端不可用：{args.base_url}/healthz -> {status}")
        if args.reset:
            phase_reset(http)
        upload = phase_upload(http)
        answers = phase_ask(http, facts, mode, args.concurrency)
        (RUNS / "upload.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in upload), encoding="utf-8")
        (RUNS / "answers.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in answers), encoding="utf-8")
        (RUNS / "meta.json").write_text(json.dumps({"mode": mode, "base_url": args.base_url}, ensure_ascii=False), encoding="utf-8")

    report = score(upload, answers, negative_cases, mode)
    (EVAL_DIR / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print_summary(report)

    ok = True
    if args.baseline:
        ok = check_baseline(report, args.baseline, args.max_drop)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
