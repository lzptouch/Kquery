#!/usr/bin/env python3
"""Kquery 数据备份（`OP-01` 值班资产）。

## 备什么

| 内容 | 路径 | 丢了会怎样 |
|---|---|---|
| 文档元数据 | `data/metadata.json` | 文档清单丢失 → 知识库「看起来空了」 |
| 图谱（SQLite） | `data/graph/graph.db` | 图谱丢失 → 关系检索与社区摘要失效 |
| 索引别名状态 | `data/graph/resolver.json` | 索引切换记录丢失 → 回滚路径断掉 |
| 会话与检查点 | `data/sessions/`、`data/agent_checkpoints.db` | 历史会话与「续跑」失效（可接受丢） |

## 特性

- **快照 + 校验和**：每个备份目录写 `manifest.json`（含每个文件的 sha256），
  恢复前先校验——**损坏的备份比没有备份更危险**（值班会以为恢复了）。
- **保留策略**：默认保留最近 N 份（`--keep`，默认 7），超出按时间删旧。
- **恢复指引随备份走**：备份目录里写 `RESTORE.md`，值班同学不用回头翻文档。

## 用法

```bash
# 备份到 data/backups/（默认）
python scripts/backup.py

# 指定目录 / 保留份数
python scripts/backup.py --out /mnt/backup/kquery --keep 14
```

退出码：0 成功；1 备份不完整（有文件缺失或校验失败）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# (相对 data_dir 的路径, 是否必需)
TARGETS: list[tuple[str, bool]] = [
    ("metadata.json", True),
    ("graph/graph.db", False),
    ("graph/resolver.json", False),
    ("sessions", False),
    ("agent_checkpoints.db", False),
]

RESTORE_GUIDE = """# 恢复指引（随备份走）

1. **先停服务**（避免恢复过程中有写入）：
   ```bash
   # docker compose
   docker compose stop backend
   ```
2. **校验备份完整性**（务必先做）：
   ```bash
   python scripts/backup.py --verify <本目录>
   ```
3. **恢复文件**（按需恢复，不要全量覆盖）：
   ```bash
   cp -a <本目录>/files/metadata.json  data/metadata.json
   cp -a <本目录>/files/graph/graph.db  data/graph/graph.db
   ```
4. **起服务并验证**：
   ```bash
   docker compose up -d backend
   curl -s localhost:8000/healthz | jq
   ```
   - `healthz` 全绿且 `vector_store` / `graph_store` 为 `up` → 恢复成功；
   - 若出现 5209（真相源损坏），说明恢复的仍不完整 → 换更早的备份。
5. **记录时间线**（复盘要用）：备份时间、恢复时间、影响面。
"""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)


def backup(data_dir: Path, out_dir: Path, keep: int) -> int:
    """做一次备份，返回退出码。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = out_dir / stamp
    files_dir = target / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, dict] = {}
    missing_required: list[str] = []
    for rel, required in TARGETS:
        src = data_dir / rel
        if not src.exists():
            if required:
                missing_required.append(rel)
            continue
        dst = files_dir / rel
        try:
            _copy(src, dst)
        except OSError as exc:
            print(f"  ✗ 复制失败 {rel}: {exc}", file=sys.stderr)
            missing_required.append(rel)
            continue
        if src.is_file():
            manifest[rel] = {"sha256": _sha256(dst), "size": dst.stat().st_size}
        else:
            manifest[rel] = {"type": "dir"}

    (target / "manifest.json").write_text(
        json.dumps(
            {
                "created_at": stamp,
                "data_dir": str(data_dir),
                "files": manifest,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (target / "RESTORE.md").write_text(RESTORE_GUIDE, encoding="utf-8")

    if missing_required:
        print(f"  ✗ 备份不完整，缺少必需项：{missing_required}", file=sys.stderr)
        return 1

    print(f"  ✓ 备份完成：{target}（{len(manifest)} 项）")
    _prune(out_dir, keep)
    return 0


def verify(target: Path) -> int:
    """校验一个备份目录的完整性。"""
    manifest_path = target / "manifest.json"
    if not manifest_path.exists():
        print(f"  ✗ 缺少 manifest.json：{target}", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files_dir = target / "files"
    bad: list[str] = []
    for rel, meta in manifest.get("files", {}).items():
        p = files_dir / rel
        if not p.exists():
            bad.append(f"{rel}（缺失）")
            continue
        if "sha256" in meta:
            if _sha256(p) != meta["sha256"]:
                bad.append(f"{rel}（校验和不符）")
    if bad:
        print(f"  ✗ 备份校验失败：{bad}", file=sys.stderr)
        return 1
    print(f"  ✓ 备份校验通过（{len(manifest.get('files', {}))} 项）")
    return 0


def _prune(out_dir: Path, keep: int) -> None:
    """保留最近 keep 份备份。"""
    if keep <= 0:
        return
    snaps = sorted(
        (p for p in out_dir.iterdir() if p.is_dir() and (p / "manifest.json").exists()),
        key=lambda p: p.name,
        reverse=True,
    )
    for old in snaps[keep:]:
        shutil.rmtree(old, ignore_errors=True)
        print(f"  · 清理旧备份 {old.name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kquery 数据备份")
    parser.add_argument("--data-dir", default=None, help="数据目录（默认 data/）")
    parser.add_argument("--out", default=None, help="备份输出目录（默认 data/backups/）")
    parser.add_argument("--keep", type=int, default=7, help="保留最近 N 份（默认 7）")
    parser.add_argument("--verify", default=None, help="校验某个备份目录而不是做备份")
    args = parser.parse_args(argv)

    if args.verify:
        return verify(Path(args.verify))

    data_dir = Path(args.data_dir) if args.data_dir else REPO_ROOT / "data"
    out_dir = Path(args.out) if args.out else data_dir / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"备份 {data_dir} → {out_dir}")
    return backup(data_dir, out_dir, args.keep)


if __name__ == "__main__":
    raise SystemExit(main())
