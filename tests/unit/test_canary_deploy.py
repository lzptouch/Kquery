"""部署层 canary（`OP-03`）。

## 四条验收

1. `deploy/k8s/canary/*.yaml` **能解析**（客户端干跑校验的最低要求）
2. canary 与 base 的**标签不同**（否则 base 的 Service 会选中 canary → 共享流量）
3. canary 与 base **同镜像同配置**（canary 测的是同一个东西）
4. `scripts/canary_promote.py --dry-run` 输出判定 / 原因 / 对比明细；**无指标时判定为「无数据」且不猜**
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CANARY_DIR = REPO_ROOT / "deploy" / "k8s" / "canary"
BASE_DIR = REPO_ROOT / "deploy" / "k8s" / "base"


def _load_yaml(path: Path):
    import yaml

    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "canary_promote_script", REPO_ROOT / "scripts" / "canary_promote.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["canary_promote_script"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_manifests_parse():
    """① 清单能解析（客户端干跑校验的最低要求）。"""
    files = sorted(CANARY_DIR.glob("*.yaml"))
    assert files, "deploy/k8s/canary/ 下没有清单"
    for f in files:
        docs = _load_yaml(f)
        assert docs, f"{f.name} 解析为空"


def test_kustomization_lists_all_resources():
    """kustomization 引用的资源都存在。"""
    kust = _load_yaml(CANARY_DIR / "kustomization.yaml")[0]
    for name in kust.get("resources", []):
        assert (CANARY_DIR / name).exists(), f"kustomization 引用的 {name} 不存在"


def test_canary_has_distinct_labels():
    """② canary 与 base 的标签不同（否则共享流量）。"""
    canary = next(
        d for d in _load_yaml(CANARY_DIR / "backend-canary-deployment.yaml")
        if d["kind"] == "Deployment"
    )
    base = next(
        d for d in _load_yaml(BASE_DIR / "backend-deployment.yaml")
        if d["kind"] == "Deployment"
    )
    assert canary["metadata"]["labels"].get("track") == "canary"
    assert "track" not in base["metadata"]["labels"]
    # selector 也必须区分（否则 Deployment 会抢 base 的 Pod）
    assert canary["spec"]["selector"]["matchLabels"].get("track") == "canary"
    assert canary["spec"]["template"]["metadata"]["labels"].get("track") == "canary"


def test_canary_uses_same_image_and_config():
    """③ canary 与 base 同镜像同配置（测的是同一个东西）。"""
    canary = next(
        d for d in _load_yaml(CANARY_DIR / "backend-canary-deployment.yaml")
        if d["kind"] == "Deployment"
    )
    base = next(
        d for d in _load_yaml(BASE_DIR / "backend-deployment.yaml")
        if d["kind"] == "Deployment"
    )
    c_env = {e["name"]: e.get("value") for e in canary["spec"]["template"]["spec"]["containers"][0]["env"]}
    b_env = {e["name"]: e.get("value") for e in base["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert canary["spec"]["template"]["spec"]["containers"][0]["image"] == \
        base["spec"]["template"]["spec"]["containers"][0]["image"]
    for key in ("DATA_DIR", "MODEL_PROVIDER", "VECTOR_STORE"):
        assert c_env.get(key) == b_env.get(key), f"{key} 配置不一致"


def test_canary_service_selects_only_canary():
    """canary Service 只选 canary Pod。"""
    svc = next(
        d for d in _load_yaml(CANARY_DIR / "backend-canary-deployment.yaml")
        if d["kind"] == "Service"
    )
    assert svc["spec"]["selector"].get("track") == "canary"


def test_promote_script_dry_run_without_metrics():
    """④ 无指标 → 判定「无数据」且**不改任何东西**。"""
    script = _load_script()
    rules = script.parse_rules(
        '[{"metric":"error_rate","direction":"gt","threshold":0.05,"min_samples":30}]'
    )
    assert len(rules) == 1
    controller, _ = script.build_controller("", rules, None)
    verdict = controller.check_scope("canary")
    assert verdict.action == "no_data"


def test_promote_script_rejects_bad_rules():
    """规则集非法 → 明确报错（不静默当成「无规则」）。"""
    script = _load_script()
    with pytest.raises(Exception):
        script.parse_rules("{不是数组}")
    with pytest.raises(Exception):
        script.parse_rules("[{坏 json}]")


def test_readme_documents_canary_and_removes_it_from_known_gaps():
    """README 有 canary 小节，且「已知未做」里不再有 canary 条目。"""
    readme = (REPO_ROOT / "deploy" / "k8s" / "README.md").read_text(encoding="utf-8")
    assert "部署层 canary" in readme
    known_gaps = readme.split("## 已知未做", 1)[1]
    assert "canary 部署" not in known_gaps
    # 代价要写出来（权重粒度粗 / 回滚快）
    assert "粒度" in readme
