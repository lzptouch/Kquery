"""文档里引用的**文件路径**是否真实存在。

文档写 `backend/app/services/pipeline.py:123` 时，如果那个文件已经没了 /
改名了，读者按图索骥只会扑空 —— 而且没有任何工具会报错。
这个脚本把这类"指向不存在文件"的引用抓出来。
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 只校验**公开文档**（开源仓库里真实存在的那些）。
#: doc/analysis 是内部工程文档，已被 .gitignore 排除、不随仓库发布，
#: 它的断链在本地报错没有意义，所以不纳入扫描。
DOC_GLOBS = (
    "README.md",
    "CONTRIBUTING.md",
    "wiki/**/*.md",
    "deploy/**/*.md",
    "backend/README.md",
    "tests/data/eval/README.md",
)

#: 匹配 `路径:行号` 或 `路径:行号-行号` 形式的引用
REF = re.compile(
    r"`((?:backend|frontend|tests|scripts|deploy|doc|wiki|\.github)/[A-Za-z0-9_./\-]+?"
    r"(?:\.(?:py|ts|tsx|yml|yaml|md|json|toml|sh|mjs|Dockerfile)|/))"
    r"(?::\d+(?:-\d+)?)?`"
)
#: Dockerfile 统一在 deploy/docker/
DOCKERFILE = re.compile(
    r"`((?:backend|frontend)/Dockerfile|deploy/docker/[A-Za-z0-9_.\-]+\.Dockerfile)`"
)

#: **设计稿里声明为"待建 / 未实现"的路径**（不是断链）。
#: 这些路径出现在 gap 台账与设计文档的"未实现项"表格里，
#: 描述的是**还没做的事**（例如"要建 tests/security/"）——
#: 它们本来就不该存在，否则就变成"已实现"了。
#: 判定依据：文档里同时出现了这些路径与"未实现 / 无 / 要建 / 建议 / 验收"等措辞。
#: 扫描范围已限定为**公开文档**（见 DOC_GLOBS），这些文档只应引用仓库里真实存在的文件。
#: 原先的「设计稿待建路径」白名单是为内部文档（doc/analysis）服务的，
#: 那些文档已不随仓库发布，因此白名单清空——公开文档里的任何断链都会被报出来。
PROPOSED_PATHS: set[str] = set()


def collect() -> dict[str, list[str]]:
    missing: dict[str, list[str]] = defaultdict(list)
    docs: list[Path] = []
    for pattern in DOC_GLOBS:
        docs.extend(sorted(REPO_ROOT.glob(pattern)))
    seen: set[Path] = set()
    for doc in docs:
        if doc in seen or not doc.is_file():
            continue
        seen.add(doc)
        text = doc.read_text(encoding="utf-8")
        for m in REF.finditer(text):
            path = m.group(1).rstrip("/")
            if (REPO_ROOT / path).exists():
                continue
            # 提案路径按"前缀"匹配（白名单里有的带尾斜杠、有的不带）
            if any(path.startswith(p.rstrip("/")) for p in PROPOSED_PATHS):
                continue
            missing[str(doc.relative_to(REPO_ROOT))].append(path)
        for m in DOCKERFILE.finditer(text):
            path = m.group(1)
            if not (REPO_ROOT / path).exists():
                missing[str(doc.relative_to(REPO_ROOT))].append(path)
    return missing


def main() -> int:
    missing = collect()
    total = sum(len(v) for v in missing.values())
    if not missing:
        print("文档引用的文件路径全部存在 ✓")
        return 0
    for doc in sorted(missing):
        uniq = sorted(set(missing[doc]))
        print(f"\n{doc}（{len(uniq)} 处）")
        for path in uniq:
            print(f"  不存在：{path}")
    print(f"\n合计 {total} 处指向不存在文件的引用")
    return 1


if __name__ == "__main__":
    sys.exit(main())
