"""从 `Settings` 生成 `backend/.env.example` —— 配置项的唯一可执行清单。

## 为什么要有这个脚本

`.env.example` 过去是**手写**的：加一个配置项，忘了同步模板，部署时就会
「文档里有、示例里没有」，或者更糟——示例里有、代码里早已删掉。
手写模板必然漂移，因为它和 `Settings` 是两份东西。

这里让模板变成**生成物**：`Settings` 加字段 → 重新生成 → 模板与代码逐行一致。
CI 用 `--check` 守住这条不变量（不一致即红）。

## 规则

| 规则 | 说明 |
| --- | --- |
| 字段名 → 环境变量名 | 大写形式（`Settings` **无 `env_prefix`**，不要写 `KQUERY_` 前缀）；有 `validation_alias` 的字段用别名（如 `KQUERY_MILVUS_URI`） |
| 默认值 → 注释 | 作为 `# 默认：…` 注释给出，便于理解取值含义 |
| 敏感字段 | key / token / secret / password / dsn / redis_url 形态 → **值留空** + 提示 |
| 路径型默认值 | `Path` 默认值是**机器相关的绝对路径**，不能写进模板 → 留空 + 说明 |

## 用法

```bash
python scripts/gen_env_example.py           # 生成（就地覆盖 backend/.env.example）
python scripts/gen_env_example.py --check   # 校验：与仓库内文件不一致 → 退出码 1
```
"""
from __future__ import annotations

import argparse
import ast
import io
import sys
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
CONFIG_PY = BACKEND_DIR / "app" / "core" / "config.py"
ENV_EXAMPLE = BACKEND_DIR / ".env.example"

# 敏感字段判定：命中即留空。`dsn` / `redis_url` 单独列出——
# 连接串与 Redis URL 都可能内嵌口令，但字面上不含 key/token 这类词。
SECRET_PATTERNS = ("key", "token", "secret", "password", "passwd", "credential", "dsn")
SECRET_FIELDS = {"redis_url"}

# 分节标题：`# ---- 名称 ----`
SECTION_RE = r"^-{2,}\s*(.+?)\s*-{2,}$"

HEADER = """\
# 本文件由 `scripts/gen_env_example.py` **自动生成** —— 请勿手工编辑。
#
# 重新生成：python scripts/gen_env_example.py
# 校验（CI 会跑）：python scripts/gen_env_example.py --check
#
# 字段来源：backend/app/core/config.py 的 `Settings`（每个字段一条，注释取自源码）。
# 命名规则：字段名大写即环境变量名（本项目**无 env_prefix**，不要加 KQUERY_ 前缀）；
# 例外是 `milvus_uri`，它显式声明了别名 `KQUERY_MILVUS_URI`（避免与 pymilvus 内部变量冲突）。
#
# 注：注释里出现的 `doc/analysis/*.md` 是开发期的内部工程文档，
# 不随开源仓库发布；对外说明见 `wiki/06-配置参考.md`。
#
# ---- 安全提醒 ----
# 认证与授权已下线：请求身份恒为 default/local，**应用层没有任何访问控制**。
# ⇒ 服务必须部署在内网或反向代理之后，由网关承担鉴权；直接暴露公网 = 任何人可读全部知识库。
# 标了「敏感」的字段一律留空：真实值只写本地 `.env`，**不要提交**。
"""


def _is_sensitive(name: str) -> bool:
    low = name.lower()
    return name in SECRET_FIELDS or any(p in low for p in SECRET_PATTERNS)


def _load_settings_class():
    """导入 `Settings`（用于取默认值与别名）。"""
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from app.core.config import Settings  # noqa: PLC0415 - 运行时导入，避免脚本被当模块导入时报错

    return Settings


def _field_comments() -> dict[str, list[str]]:
    """字段名 → 其正上方的注释行（源码里的中文说明就是模板的解释）。"""
    src = CONFIG_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)

    comments: dict[int, str] = {}
    src_lines = src.splitlines()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        # 只收**整行注释**（该行注释之前只有空白）：行尾注释属于上一行的字段说明，
        # 把它当成下一个字段的说明会张冠李戴。
        if tok.type != tokenize.COMMENT:
            continue
        line = src_lines[tok.start[0] - 1]
        if line[: tok.start[1]].strip():
            continue
        comments[tok.start[0]] = tok.string.lstrip("#").strip()

    settings_node = next(
        n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "Settings"
    )

    out: dict[str, list[str]] = {}
    for node in settings_node.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        lines: list[str] = []
        ln = node.lineno - 1
        # 紧邻字段的连续注释行（中间不能有空行，否则视为不相关的说明）
        while ln in comments:
            lines.append(comments[ln])
            ln -= 1
        out[node.target.id] = list(reversed(lines))
    return out


def _env_name(name: str, info) -> str:
    alias = getattr(info, "validation_alias", None)
    if isinstance(alias, str) and alias:
        return alias.upper()
    return name.upper()


def _format_default(name: str, default) -> str:
    """模板里的取值文本。空串表示「不写值」。"""
    if _is_sensitive(name):
        return ""
    if isinstance(default, Path):
        # 绝对路径与机器相关，写进模板会让每个人的 .env.example 都不一样
        return ""
    if isinstance(default, bool):
        return "true" if default else "false"
    if isinstance(default, (int, float)):
        return str(default)
    return str(default)


def _default_note(name: str, default) -> str:
    """默认值注释。"""
    if _is_sensitive(name):
        return "敏感：留空；真实值只写本地 .env，不要提交"
    if isinstance(default, Path):
        return "默认：仓库根 data/（留空即用默认；相对路径按进程工作目录解析）"
    if isinstance(default, bool):
        return f"默认：{'true' if default else 'false'}"
    if isinstance(default, (int, float)):
        return f"默认：{default}"
    if default == "":
        return "默认：空（不配置即不启用该能力）"
    return f"默认：{default}"


def render() -> str:
    """生成 `backend/.env.example` 的完整文本。"""
    import re  # noqa: PLC0415 - 只在渲染时用

    settings_cls = _load_settings_class()
    comments = _field_comments()
    section_re = re.compile(SECTION_RE)

    lines: list[str] = [HEADER.rstrip("\n")]
    current_section: str | None = None

    for name, info in settings_cls.model_fields.items():
        raw_comments = comments.get(name, [])
        section: str | None = None
        body: list[str] = []
        for c in raw_comments:
            m = section_re.match(c)
            if m:
                section = m.group(1)
            else:
                body.append(c)

        if section and section != current_section:
            lines.append("")
            lines.append(f"# ---- {section} ----")
            current_section = section

        for c in body:
            lines.append(f"# {c}")

        default = info.default
        value = _format_default(name, default)
        lines.append(f"# {_default_note(name, default)}")
        lines.append(f"{_env_name(name, info)}={value}")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 / 校验 backend/.env.example")
    parser.add_argument("--check", action="store_true", help="只校验，不写文件；不一致则退出码 1")
    args = parser.parse_args(argv)

    content = render()

    if args.check:
        current = ENV_EXAMPLE.read_text(encoding="utf-8") if ENV_EXAMPLE.exists() else ""
        if current != content:
            sys.stderr.write(
                "backend/.env.example 与 Settings 不一致：\n"
                "  重新生成：python scripts/gen_env_example.py\n"
            )
            # 打印首个差异行，便于定位
            cur_lines = current.splitlines()
            new_lines = content.splitlines()
            for idx in range(max(len(cur_lines), len(new_lines))):
                a = cur_lines[idx] if idx < len(cur_lines) else "<缺失>"
                b = new_lines[idx] if idx < len(new_lines) else "<缺失>"
                if a != b:
                    sys.stderr.write(f"  第 {idx + 1} 行：\n    现有：{a}\n    应为：{b}\n")
                    break
            return 1
        print("backend/.env.example 与 Settings 一致 ✓")
        return 0

    ENV_EXAMPLE.write_text(content, encoding="utf-8")
    print(f"已生成 {ENV_EXAMPLE.relative_to(REPO_ROOT)}（{len(content.splitlines())} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
