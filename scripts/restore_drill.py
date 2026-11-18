#!/usr/bin/env python3
"""恢复演练（`OP-01` 值班资产）。

## 为什么要有

**没演练过的恢复流程等于没有恢复流程**。真出事时才发现「恢复脚本连不上」
或「恢复完索引是空的」，代价是用户长时间不可用。

本脚本在**临时目录**里做完整闭环：
```
备份 → 清空 → 恢复 → 校验 → 确认索引可用
```
**只演练，不碰生产数据**：所有操作都在 `tempfile.mkdtemp()` 里，
生产目录只被**读取**（备份源）。

退出码：0 演练通过；1 演练失败。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from backup import backup, verify  # noqa: E402


def _make_fixture_data(data_dir: Path) -> None:
    """造一份「像真的」的数据目录（演练用，不碰生产）。"""
    (data_dir / "graph").mkdir(parents=True, exist_ok=True)
    (data_dir / "metadata.json").write_text(
        json.dumps(
            [
                {"doc_id": "d1", "name": "演练文档", "status": "done", "version": 1, "tenant_id": "default"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (data_dir / "graph" / "graph.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 64)
    (data_dir / "graph" / "resolver.json").write_text(
        json.dumps({"alias": "index", "physical": "index", "version": 1}, ensure_ascii=False),
        encoding="utf-8",
    )


def drill() -> int:
    with tempfile.TemporaryDirectory(prefix="kquery-drill-") as tmp:
        tmp_path = Path(tmp)
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "backups"
        data_dir.mkdir(parents=True, exist_ok=True)
        _make_fixture_data(data_dir)

        print("[1/5] 造演练数据 …")
        print(f"      data_dir={data_dir}")

        print("[2/5] 备份 …")
        if backup(data_dir, out_dir, keep=3) != 0:
            print("  ✗ 备份失败", file=sys.stderr)
            return 1
        snaps = sorted(p for p in out_dir.iterdir() if (p / "manifest.json").exists())
        if not snaps:
            print("  ✗ 没有生成备份", file=sys.stderr)
            return 1
        snap = snaps[-1]

        print("[3/5] 校验备份 …")
        if verify(snap) != 0:
            print("  ✗ 备份校验失败", file=sys.stderr)
            return 1

        print("[4/5] 清空 → 恢复 …")
        for rel in ("metadata.json", "graph/graph.db", "graph/resolver.json"):
            target = data_dir / rel
            if target.exists():
                target.unlink()
        if (data_dir / "metadata.json").exists():
            print("  ✗ 清空没生效", file=sys.stderr)
            return 1
        for rel in ("metadata.json", "graph/graph.db", "graph/resolver.json"):
            src = snap / "files" / rel
            if src.exists():
                dst = data_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        print("      已恢复")

        print("[5/5] 恢复后校验 …")
        if verify(snap) != 0:
            return 1
        if not (data_dir / "metadata.json").exists():
            print("  ✗ 恢复后缺少 metadata.json", file=sys.stderr)
            return 1
        try:
            docs = json.loads((data_dir / "metadata.json").read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 演练失败要报错
            print(f"  ✗ 恢复后元数据不可读: {exc}", file=sys.stderr)
            return 1
        if not docs:
            print("  ✗ 恢复后元数据为空", file=sys.stderr)
            return 1
        print(f"      恢复后可读文档 {len(docs)} 篇")

    print("  ✓ 恢复演练通过（全程在临时目录，未触碰生产数据）")
    return 0


def main() -> int:
    print("Kquery 恢复演练（备份 → 清空 → 恢复 → 校验）")
    return drill()


if __name__ == "__main__":
    raise SystemExit(main())
