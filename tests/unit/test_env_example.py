"""`.env.example` 与 `Settings` 的一致性护栏（`A-19`）。

`backend/.env.example` 是**生成物**：字段来源是 `backend/app/core/config.py` 的
`Settings`。手写模板必然漂移——加了配置项忘了同步模板，部署时就会
「文档里有、示例里没有」。

这里守住三条不变量：

1. **不漂移**：仓库内文件与生成器输出**逐行一致**（不一致即红）；
2. **不遗漏**：`Settings` 的每个字段都在模板里出现（漏一个 = 部署时不知道能配什么）；
3. **不泄密**：敏感字段的值必须留空，且模板里不得出现机器相关的绝对路径。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / "backend" / ".env.example"


def _load_gen_env_example():
    spec = importlib.util.spec_from_file_location(
        "_t_gen_env_example", REPO_ROOT / "scripts" / "gen_env_example.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gen():
    return _load_gen_env_example()


@pytest.fixture(scope="module")
def rendered(gen) -> str:
    return gen.render()


def _parsed(text: str) -> dict[str, str]:
    """把模板解析成 {环境变量名: 值}（跳过注释与空行）。"""
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition("=")
        out[key.strip()] = value.strip()
    return out


def test_env_example_matches_generator(gen, rendered):
    """模板必须与生成器输出逐行一致（CI 的 --check 是同一判据）。"""
    current = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert current == rendered, (
        "backend/.env.example 与 Settings 不一致："
        "请执行 `python scripts/gen_env_example.py` 重新生成"
    )


def test_check_mode_passes(gen):
    """脚本自身的 --check 判据必须返回 0（CI 直接调用它）。"""
    assert gen.main(["--check"]) == 0


def test_every_settings_field_present(gen, rendered):
    """`Settings` 的每个字段都必须在模板里出现。"""
    settings_cls = gen._load_settings_class()
    parsed = _parsed(rendered)
    missing = []
    for name, info in settings_cls.model_fields.items():
        env_name = gen._env_name(name, info)
        if env_name not in parsed:
            missing.append(env_name)
    assert not missing, f"以下配置项在 .env.example 中缺失：{missing}"


def test_sensitive_fields_are_blank(gen, rendered):
    """敏感字段必须留空（真实值只放本地 .env）。"""
    settings_cls = gen._load_settings_class()
    parsed = _parsed(rendered)
    for name, info in settings_cls.model_fields.items():
        if not gen._is_sensitive(name):
            continue
        env_name = gen._env_name(name, info)
        assert parsed.get(env_name, "") == "", f"{env_name} 是敏感字段，模板里必须留空"


def test_no_machine_specific_paths(rendered):
    """模板里不得出现机器相关的绝对路径（每人生成的结果必须一致）。"""
    assert str(REPO_ROOT) not in rendered
    assert "/Users/" not in rendered
    assert "/Volumes/" not in rendered


def test_generated_output_is_deterministic(gen, rendered):
    """同一份代码两次生成必须完全一致（否则 --check 会随机变红）。"""
    assert gen.render() == rendered
