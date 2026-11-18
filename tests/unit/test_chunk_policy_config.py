"""切块策略配置化（`MP-05` 的准备）。

## 三条验收

1. `chunk_policy` 为空 → **旧行为**（`chunk_size` / `chunk_overlap`）
2. `chunk_policy` 填预设 → **预设覆盖**块大小与重叠（改默认值 = 改一行配置）
3. 非法预设 → **启动期校验报错**（不带着坏配置启动）

## 出厂默认已改为 800/100（2026-11-13）

本地金标上 `recursive-800-100` 已被证明「每个维度不劣且成本低 31%」
（`EV-04`：+0.048，CI [0.012, 0.090]，p=0.006，n=110；`EV-06` 帕累托前沿只含它一套），
此前因「改默认属于显式决策」而停在 300/50。现在决策已下：**出厂默认 800/100**。

保留的两条边界：

- 显式配置 `CHUNK_SIZE` / `CHUNK_OVERLAP` 的部署**不受影响**（环境变量优先）；
- 本地评测用的是 **mock 向量**，绝对值仍不代表线上——真实 embedding 的复核
  是 `MP-04`（live 复测）的事，与本默认值相互独立。

证据与口径见 `doc/analysis/检索策略推荐表.md`。
"""
from __future__ import annotations

from app.core.config import Settings
from app.core.chunk_policies import (
    DEFAULT_PRESET,
    PRESETS,
    available_presets,
    resolve_policy,
)


def _settings(**overrides) -> Settings:
    s = Settings(_env_file=None)
    for key, value in overrides.items():
        setattr(s, key, value)
    return s


def test_empty_policy_keeps_old_behavior():
    """① `chunk_policy` 为空 → 旧行为（不改变任何现有语义）。"""
    s = _settings(chunk_size=300, chunk_overlap=50)
    resolved = s.resolved_chunk_policy
    assert resolved["chunk_size"] == 300
    assert resolved["overlap"] == 50
    assert resolved["policy"]["strategy"] == "recursive"


def test_preset_overrides_chunk_size():
    """② 预设覆盖块大小与重叠（改默认值 = 改一行配置）。"""
    s = _settings(chunk_size=300, chunk_overlap=50, chunk_policy="recursive-800-100")
    resolved = s.resolved_chunk_policy
    assert resolved["chunk_size"] == 800
    assert resolved["overlap"] == 100
    assert resolved["name"] == "recursive-800-100"


def test_invalid_preset_fails_startup_validation():
    """③ 非法预设 → 启动期校验报错。"""
    s = _settings(chunk_policy="recursive-999-999")
    issues = s.validate_startup()
    policy_issues = [i for i in issues if i["field"] == "chunk_policy"]
    assert policy_issues, "非法预设必须在启动期暴露，不能带着坏配置启动"


def test_presets_are_shared_between_production_and_evaluation():
    """生产与评测**同一份预设**（两份定义必然漂移）。"""
    from app.services.evaluation import policies as eval_policies

    assert eval_policies.PRESETS is PRESETS
    assert eval_policies.DEFAULT_PRESET == DEFAULT_PRESET


def test_available_presets_include_the_evaluated_candidate():
    """被评测过的候选策略必须在可用列表里。"""
    names = available_presets()
    assert "recursive-300-50" in names
    assert "recursive-800-100" in names


def test_resolve_policy_json_form_still_works():
    """JSON 写法仍然支持（离线脚本在用）。"""
    resolved = resolve_policy('{"strategy":"recursive","params":{},"chunk_size":512,"overlap":64}')
    assert resolved["chunk_size"] == 512
    assert resolved["overlap"] == 64


def test_factory_default_is_800_100(monkeypatch):
    """出厂默认值本身就是被评测过的那一套（`MP-05` 验收）。

    没有这个断言，「默认值」就只是散落在三个模块里的三个数字字面量
    （`core/config.py` / `services/chunking.py` / `services/chunkers/factory.py`），
    改了其中一处、漏了另外两处，线上行为取决于谁先被调用。

    必须显式清环境变量：`pymilvus` import 时的 `load_dotenv()` 会把
    `backend/.env`（其中有 `CHUNK_SIZE=300`）注入 `os.environ`，不清掉就测的是
    本地配置而不是出厂默认。
    """
    from app.services.chunkers.factory import apply_chunk_policy
    import inspect

    for key in ("CHUNK_SIZE", "CHUNK_OVERLAP", "CHUNK_POLICY"):
        monkeypatch.delenv(key, raising=False)

    default = Settings(_env_file=None)
    assert default.resolved_chunk_policy["chunk_size"] == 800
    assert default.resolved_chunk_policy["overlap"] == 100

    sig = inspect.signature(apply_chunk_policy)
    assert sig.parameters["chunk_size"].default == 800
    assert sig.parameters["chunk_overlap"].default == 100

    from app.services.chunking import build_chunks, split_text

    assert inspect.signature(split_text).parameters["chunk_size"].default == 800
    assert inspect.signature(build_chunks).parameters["chunk_size"].default == 800
    assert inspect.signature(build_chunks).parameters["overlap"].default == 100
