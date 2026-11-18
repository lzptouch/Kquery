"""切块策略预设（**core 层**：配置数据，生产与评测共用同一份，`MP-05` 的准备）。

## 为什么在 core 而不是 services

预设表是**配置数据**（一张策略名 → 参数的表），不是业务逻辑：
- 它原先住在 `services/evaluation/policies.py` → **生产代码要用它就得反向依赖评测模块**
  （评测是「离线脚本 + CI」的领地，生产运行时不该依赖它）；
- 它现在住在 `core/` → **core 不依赖 services**（`A-13` 的边界守卫），
  `services/` 与 `core/config.py` 都向下依赖它，依赖方向全部正确。

评测模块反过来 import core —— 方向从「生产 → 评测」纠正为「评测 → 生产」。

## 命名

`<strategy>[-<关键参数>]`，例如 `recursive-800-100`、`parent_child-1200-200-40`。
"""
from __future__ import annotations

import json

from app.core.errors import ConfigError, InvalidInputError

#: 策略预设：名字 → {policy, chunk_size, overlap}
#: chunk_size / overlap 只对 recursive 生效（其他策略用自己的 params）。
PRESETS: dict[str, dict] = {
    "recursive-300-50": {
        "policy": {"strategy": "recursive", "params": {}},
        "chunk_size": 300, "overlap": 50,
    },
    "recursive-500-100": {
        "policy": {"strategy": "recursive", "params": {}},
        "chunk_size": 500, "overlap": 100,
    },
    "recursive-800-100": {
        "policy": {"strategy": "recursive", "params": {}},
        "chunk_size": 800, "overlap": 100,
    },
    "semantic-0.4": {
        "policy": {"strategy": "semantic", "params": {"threshold": 0.4}},
        "chunk_size": 300, "overlap": 50,
    },
    "parent_child-1200-200-40": {
        "policy": {
            "strategy": "parent_child",
            "params": {"parent_size": 1200, "child_size": 200, "child_overlap": 40},
        },
        "chunk_size": 300, "overlap": 50,
    },
    "raptor-4-3": {
        "policy": {"strategy": "raptor", "params": {"max_clusters": 4, "max_top_nodes": 3}},
        "chunk_size": 300, "overlap": 50,
    },
}

#: 当前**出厂默认**（`MP-05`：改这个值 = 改全局切块策略）
#:
#: **仍是 300/50**：本地金标上 `recursive-800-100` 已被证明「每个维度不劣且便宜 31%」
#: （`EV-04` 配对 bootstrap：+0.048，CI [0.012, 0.090]，p=0.006，n=110），
#: 但**改默认值属于需要显式决策的事项**——本地评测用的是 mock 向量，
#: 真实 embedding 的绝对值需 `MP-04`（live 复测）确认。
#: 证据与口径见 `doc/analysis/检索策略推荐表.md`。
DEFAULT_PRESET = "recursive-300-50"


def resolve_policy(spec: str | None) -> dict:
    """把策略规格解析成 ``{policy, chunk_size, overlap}``。

    支持三种写法：
    - 预设名（``recursive-500-100``）；
    - JSON（``{"strategy":"recursive","params":{}}``，可带 ``chunk_size``/``overlap``）；
    - 空 → 默认预设。
    """
    text = (spec or "").strip()
    if not text:
        preset = dict(PRESETS[DEFAULT_PRESET])
        preset["name"] = DEFAULT_PRESET
        return preset
    if text in PRESETS:
        preset = dict(PRESETS[text])
        preset["name"] = text
        return preset
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ConfigError(f"策略 JSON 无法解析: {exc}") from exc
        strategy = str(data.get("strategy") or "recursive")
        return {
            "name": f"custom:{strategy}",
            "policy": {"strategy": strategy, "params": dict(data.get("params") or {})},
            "chunk_size": int(data.get("chunk_size") or 300),
            "overlap": int(data.get("overlap") or 50),
        }
    raise InvalidInputError(
        f"未知策略规格 {text!r}；可用预设：{', '.join(sorted(PRESETS))}"
    )


def available_presets() -> list[str]:
    """可用的预设名（给报错信息与配置校验用）。"""
    return sorted(PRESETS)
