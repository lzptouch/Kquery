#!/usr/bin/env python3
"""从代码里提取**事实**（用于同步文档）。

**为什么需要它**：文档最容易出的错是「写了代码里已经没有的东西」
（模块挪了、端点删了、配置改名了、指标名变了）。
本脚本把「代码现状」变成可复现的输出，文档更新时**对着它改**，
而不是对着记忆改。

用法：
    python scripts/doc_facts.py            # 全量（人读）
    python scripts/doc_facts.py --json     # 机器读（便于 diff / CI 校验）
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _py_files(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob("*.py")
        if not any(part in {"__pycache__", ".venv", "node_modules"} for part in p.parts)
    )


def endpoints() -> list[dict]:
    """从 OpenAPI 抽取全部端点（路径 / 方法 / 所在文件 / 鉴权依赖）。"""
    os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
    os.environ.setdefault("MODEL_PROVIDER", "mock")
    try:
        from app.main import create_app

        app = create_app()
        spec = app.openapi()
    except Exception as exc:  # noqa: BLE001 - 文档工具不该因导入失败而崩
        return [{"error": f"无法加载应用：{exc}"}]
    out: list[dict] = []
    for path, ops in sorted(spec.get("paths", {}).items()):
        for method, op in ops.items():
            if method.upper() not in {"GET", "POST", "PUT", "DELETE", "PATCH"}:
                continue
            tags = op.get("tags") or []
            out.append({
                "path": path,
                "method": method.upper(),
                "tag": (tags[0] if tags else "-"),
                "summary": (op.get("summary") or "").strip(),
            })
    return out


def modules() -> dict:
    """后端模块清单（按层分组 + 行数 + 主要顶层定义）。"""
    out: dict[str, dict] = {}
    for layer in ("core", "domain", "services", "api"):
        base = BACKEND / "app" / layer
        if not base.exists():
            continue
        files = []
        for p in _py_files(base):
            rel = p.relative_to(BACKEND / "app")
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            names = [
                n.name for n in tree.body
                if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            files.append({
                "path": str(rel),
                "lines": len(p.read_text(encoding="utf-8").splitlines()),
                "defs": [n for n in names if not n.startswith("_")][:12],
            })
        out[layer] = {"file_count": len(files), "files": files}
    return out


def settings() -> list[dict]:
    """配置项清单（名字 / 类型 / 默认值 / 说明）。"""
    try:
        from app.core.config import Settings

        rows = []
        for name, field in Settings.model_fields.items():
            default = field.default
            rows.append({
                "name": name,
                "type": str(field.annotation),
                "default": None if default is None else str(default),
                "doc": (field.description or "").strip(),
            })
        return rows
    except Exception as exc:  # noqa: BLE001
        return [{"error": str(exc)}]


def metrics() -> list[dict]:
    """Prometheus 指标清单（名字 / 类型 / label / 说明）。"""
    try:
        from app.core.metrics import PromMetrics

        prom = PromMetrics()
        out = []
        for attr in dir(prom):
            if attr.startswith("_"):
                continue
            obj = getattr(prom, attr)
            base = getattr(obj, "_name", None)
            if not base:
                continue
            kind = getattr(obj, "_type", "")
            labels = list(getattr(obj, "_labelnames", []) or [])
            if kind == "counter":
                out.append({"name": f"{base}_total", "type": kind, "labels": labels,
                            "doc": getattr(obj, "_documentation", "")})
            elif kind == "histogram":
                out.append({"name": base, "type": kind, "labels": labels,
                            "doc": getattr(obj, "_documentation", "")})
                out.append({"name": f"{base}_bucket/_count/_sum", "type": kind,
                            "labels": labels, "doc": "(派生)"})
            else:
                out.append({"name": base, "type": kind, "labels": labels,
                            "doc": getattr(obj, "_documentation", "")})
        return sorted(out, key=lambda r: r["name"])
    except Exception as exc:  # noqa: BLE001
        return [{"error": str(exc)}]


def scripts_and_deploy() -> dict:
    """脚本、部署清单、CI job。"""
    scripts = sorted(
        str(p.relative_to(REPO_ROOT))
        for p in (REPO_ROOT / "scripts").rglob("*.py")
        if "__pycache__" not in p.parts
    )
    deploy = sorted(
        str(p.relative_to(REPO_ROOT))
        for p in (REPO_ROOT / "deploy").rglob("*")
        if p.is_file() and p.suffix in {".yaml", ".yml", ".md", ".conf"}
    )
    ci_jobs: list[str] = []
    ci_file = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    if ci_file.exists():
        try:
            import yaml

            ci_jobs = list(yaml.safe_load(ci_file.read_text(encoding="utf-8"))["jobs"])
        except Exception:  # noqa: BLE001
            ci_jobs = []
    return {"scripts": scripts, "deploy": deploy, "ci_jobs": ci_jobs}


def tests() -> dict:
    """测试规模（按目录 + 总量）。"""
    root = REPO_ROOT / "tests"
    out: dict[str, int] = {}
    total = 0
    for p in sorted(root.rglob("test_*.py")):
        rel = p.relative_to(root)
        group = rel.parts[0] if len(rel.parts) > 1 else "root"
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        count = sum(
            1 for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")
        )
        out[group] = out.get(group, 0) + count
        total += count
    return {"by_group": out, "total_functions": total}


def eval_assets() -> dict:
    """评测资产（金标集 / 语料 / 图文素材）。"""
    eval_dir = REPO_ROOT / "tests" / "data" / "eval"
    out: dict = {}
    if eval_dir.exists():
        for p in sorted(eval_dir.rglob("*")):
            if p.is_file() and p.suffix in {".jsonl", ".tsv", ".json", ".md"}:
                out[str(p.relative_to(eval_dir))] = p.stat().st_size
    corpus = eval_dir / "corpus_img"
    if corpus.exists():
        out["_corpus_img_images"] = len(list((corpus / "images").glob("*")))
        out["_corpus_img_captions"] = len(list((corpus / "captions").glob("*.txt")))
        out["_corpus_img_annotations"] = len(list((corpus / "annotations").glob("*.md")))
    return out


def frontend() -> dict:
    """前端页面与组件清单。"""
    src = REPO_ROOT / "frontend" / "src"
    out: dict = {}
    if not src.exists():
        return out
    for group in ("features", "components"):
        base = src / group
        if not base.exists():
            continue
        pages = sorted(
            str(p.relative_to(src)) for p in base.rglob("*.tsx")
            if not p.name.endswith(".test.tsx") and "components" not in p.parts
        )
        comps = sorted(
            str(p.relative_to(src)) for p in (src / "features").rglob("components/*.tsx")
        )
        out["pages"] = pages
        out["components"] = comps
    return out


def collect() -> dict:
    return {
        "endpoints": endpoints(),
        "modules": modules(),
        "settings": settings(),
        "metrics": metrics(),
        "assets": scripts_and_deploy(),
        "tests": tests(),
        "eval": eval_assets(),
        "frontend": frontend(),
    }


def render_text(data: dict) -> str:
    lines: list[str] = []
    eps = data["endpoints"]
    # endpoints() 在应用导入失败时返回 [{"error": ...}]（无 path/method 键），
    # 这里必须先拦截，否则下面的排序会 KeyError: 'path'——文档工具不该因导入失败而崩。
    if eps and "error" in eps[0]:
        lines.append("## 端点（不可用）")
        lines.append(f"  错误：{eps[0]['error']}")
    else:
        lines.append(f"## 端点（{len(eps)} 个）")
        by_tag: dict[str, list[dict]] = {}
        for e in eps:
            by_tag.setdefault(e.get("tag", "-"), []).append(e)
        for tag in sorted(by_tag):
            lines.append(f"\n### {tag}（{len(by_tag[tag])}）")
            for e in sorted(by_tag[tag], key=lambda x: (x["path"], x["method"])):
                lines.append(f"  {e['method']:<6} {e['path']:<44} {e.get('summary', '')}")

    lines.append("\n## 模块")
    for layer, info in data["modules"].items():
        lines.append(f"\n### {layer}（{info['file_count']} 个文件）")
        for f in info["files"]:
            lines.append(
                f"  {f['path']:<52} {f['lines']:>5} 行  {', '.join(f['defs'][:6])}"
            )

    lines.append(f"\n## 配置项（{len(data['settings'])}）")
    for row in data["settings"]:
        if "error" in row:
            lines.append(f"  错误：{row['error']}")
            break
        lines.append(f"  {row['name']:<38} = {row['default']}")

    lines.append(f"\n## 指标（{len(data['metrics'])}）")
    for row in data["metrics"]:
        if "error" in row:
            lines.append(f"  错误：{row['error']}")
            break
        labels = ",".join(row["labels"]) or "-"
        lines.append(f"  {row['name']:<42} {row['type']:<10} labels=({labels})")

    assets = data["assets"]
    lines.append("\n## 脚本")
    for s in assets["scripts"]:
        lines.append(f"  {s}")
    lines.append("\n## 部署清单")
    for s in assets["deploy"]:
        lines.append(f"  {s}")
    lines.append(f"\n## CI jobs：{assets['ci_jobs']}")

    t = data["tests"]
    lines.append(f"\n## 测试（函数级 {t['total_functions']}）")
    for group, count in sorted(t["by_group"].items()):
        lines.append(f"  {group:<14} {count}")

    lines.append("\n## 评测资产")
    for name, size in sorted(data["eval"].items()):
        lines.append(f"  {name:<44} {size}")

    fe = data["frontend"]
    lines.append("\n## 前端页面")
    for p in fe.get("pages", []):
        lines.append(f"  {p}")
    lines.append("\n## 前端组件")
    for p in fe.get("components", []):
        lines.append(f"  {p}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="从代码提取文档事实")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--out", default="", help="写入文件（默认打印）")
    args = parser.parse_args()

    data = collect()
    text = json.dumps(data, ensure_ascii=False, indent=2) if args.json else render_text(data)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"[doc-facts] 已写入 {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
