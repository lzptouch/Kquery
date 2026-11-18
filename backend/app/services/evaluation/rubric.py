"""生成质量 rubric（`EV-02`）：版本化的评分维度 / 权重 / 通过阈值。

## 为什么要有

生成质量此前**没有任何判定标准**：门禁阈值散落在 4 个脚本里（硬编码），
改阈值要改代码；「版本」这个概念更不存在——两次评测结果无从比较。

## 三条纪律

1. **加载即校验**：权重和必须为 1（容差 1e-6）；关键维度必须是维度的子集；
   缺字段 → **立刻抛错**。绝不允许「缺维度 = 0 分」这种静默降级——
   静默降级会让一次「维度写漏了」的评测看起来像「质量下降了」。
2. **版本号必填**：没有版本号的评分无法比较。
3. **评分区间统一 0~1**：不同量纲的分数没法加权。

## 文件格式

JSON（`rubric.v1.json`）——用标准库就能解析，**不给项目新增依赖**。
`.yaml` 也支持，但需要 `pyyaml`（可选依赖，缺了会明确报错而不是静默失败）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

#: 权重和的容差（浮点误差）
WEIGHT_SUM_TOLERANCE = 1e-6

#: 默认 rubric 文件（仓库内，随代码走）
#: 路径：`backend/app/services/evaluation/rubric.py` → 上溯 4 层到仓库根。
#: 也可用环境变量 `KQUERY_RUBRIC_PATH` 覆盖（夜间任务 / CI 可以指向自己的版本）。
DEFAULT_RUBRIC_PATH = (
    Path(__file__).resolve().parents[4] / "tests" / "data" / "eval" / "rubric.v1.json"
)


class RubricError(ValueError):
    """rubric 文件非法（缺字段 / 权重和不为 1 / 关键维度不是子集）。"""


@dataclass(frozen=True)
class RubricDim:
    """一个评分维度。"""

    id: str
    name: str
    weight: float
    scale: tuple[float, float]
    criteria: str
    critical: bool = False

    def score(self, raw: float) -> float:
        """把原始分规整到 `[scale[0], scale[1]]`（越界即报错，不静默截断）。"""
        lo, hi = self.scale
        if raw < lo - WEIGHT_SUM_TOLERANCE or raw > hi + WEIGHT_SUM_TOLERANCE:
            raise RubricError(f"维度 {self.id} 的分数 {raw} 超出区间 [{lo}, {hi}]")
        return float(raw)


@dataclass(frozen=True)
class Rubric:
    """一份版本化 rubric。"""

    version: str
    dims: tuple[RubricDim, ...]
    weighted_score_min: float
    critical_dim_min: float
    description: str = ""
    _by_id: dict[str, RubricDim] = field(default_factory=dict, repr=False, compare=False)

    def dim(self, dim_id: str) -> RubricDim:
        if dim_id not in self._by_id:
            raise RubricError(f"rubric v{self.version} 没有维度 {dim_id}")
        return self._by_id[dim_id]

    def weighted_score(self, scores: dict[str, float]) -> float:
        """加权分 = Σ 权重 × 分数（缺维度 → **抛错**，不当 0 分）。"""
        missing = [d.id for d in self.dims if d.id not in scores]
        if missing:
            raise RubricError(f"缺少维度分数：{missing}（不允许静默当 0 分）")
        total = 0.0
        for d in self.dims:
            total += d.weight * d.score(scores[d.id])
        return round(total, 6)

    def critical_dims(self) -> tuple[RubricDim, ...]:
        return tuple(d for d in self.dims if d.critical)

    def passes(self, scores: dict[str, float]) -> tuple[bool, str]:
        """是否通过 + 未通过的原因。

        两条判据**都要满足**：加权分 ≥ 通过线 **且** 每个关键维度 ≥ 关键下限。
        """
        weighted = self.weighted_score(scores)
        if weighted < self.weighted_score_min:
            return False, f"加权分 {weighted} < 通过线 {self.weighted_score_min}"
        for d in self.critical_dims():
            value = d.score(scores[d.id])
            if value < self.critical_dim_min:
                return False, f"关键维度 {d.id} = {value} < 下限 {self.critical_dim_min}"
        return True, ""


def _parse_payload(payload: dict, source: str) -> Rubric:
    """解析 + 校验 rubric 载荷（加载即校验）。"""
    if not isinstance(payload, dict):
        raise RubricError(f"{source}: rubric 必须是 JSON 对象")

    version = payload.get("version")
    if not version or not isinstance(version, str):
        raise RubricError(f"{source}: 缺少 version（没有版本号的评分无法比较）")

    raw_dims = payload.get("dims")
    if not raw_dims or not isinstance(raw_dims, list):
        raise RubricError(f"{source}: 缺少 dims")

    dims: list[RubricDim] = []
    seen: set[str] = set()
    for raw in raw_dims:
        if not isinstance(raw, dict):
            raise RubricError(f"{source}: dims 元素必须是对象")
        dim_id = raw.get("id")
        if not dim_id or not isinstance(dim_id, str):
            raise RubricError(f"{source}: 维度缺少 id")
        if dim_id in seen:
            raise RubricError(f"{source}: 维度 id 重复：{dim_id}")
        seen.add(dim_id)
        weight = raw.get("weight")
        if not isinstance(weight, (int, float)):
            raise RubricError(f"{source}: 维度 {dim_id} 缺少 weight")
        scale = raw.get("scale")
        if (
            not isinstance(scale, (list, tuple))
            or len(scale) != 2
            or not all(isinstance(v, (int, float)) for v in scale)
        ):
            raise RubricError(f"{source}: 维度 {dim_id} 的 scale 必须是 [min, max]")
        if scale[0] >= scale[1]:
            raise RubricError(f"{source}: 维度 {dim_id} 的 scale 区间非法：{scale}")
        criteria = raw.get("criteria")
        if not criteria or not isinstance(criteria, str):
            raise RubricError(f"{source}: 维度 {dim_id} 缺少 criteria（评分标准必须可操作）")
        dims.append(
            RubricDim(
                id=dim_id,
                name=raw.get("name") or dim_id,
                weight=float(weight),
                scale=(float(scale[0]), float(scale[1])),
                criteria=criteria,
                critical=bool(raw.get("critical", False)),
            )
        )

    weight_sum = sum(d.weight for d in dims)
    if abs(weight_sum - 1.0) > WEIGHT_SUM_TOLERANCE:
        raise RubricError(f"{source}: 权重和必须为 1（实际 {weight_sum:.6f}）")

    pass_cfg = payload.get("pass")
    if not isinstance(pass_cfg, dict):
        raise RubricError(f"{source}: 缺少 pass 配置")
    weighted_min = pass_cfg.get("weighted_score_min")
    if not isinstance(weighted_min, (int, float)):
        raise RubricError(f"{source}: pass.weighted_score_min 缺失")
    critical_min = pass_cfg.get("critical_dim_min")
    if not isinstance(critical_min, (int, float)):
        raise RubricError(f"{source}: pass.critical_dim_min 缺失")

    critical_ids = pass_cfg.get("critical_dims") or []
    if not isinstance(critical_ids, list):
        raise RubricError(f"{source}: pass.critical_dims 必须是数组")
    unknown = [cid for cid in critical_ids if cid not in seen]
    if unknown:
        raise RubricError(f"{source}: 关键维度不是 dims 的子集：{unknown}")

    # 关键维度标记：dims[].critical 与 pass.critical_dims 任一即可
    final_dims = tuple(
        RubricDim(
            id=d.id, name=d.name, weight=d.weight, scale=d.scale,
            criteria=d.criteria, critical=d.critical or d.id in critical_ids,
        )
        for d in dims
    )
    rubric = Rubric(
        version=version,
        dims=final_dims,
        weighted_score_min=float(weighted_min),
        critical_dim_min=float(critical_min),
        description=payload.get("description") or "",
    )
    object.__setattr__(rubric, "_by_id", {d.id: d for d in final_dims})
    return rubric


def load_rubric(path: str | Path | None = None) -> Rubric:
    """加载 rubric 文件（加载即校验；非法立刻抛错）。

    优先级：显式参数 > 环境变量 `KQUERY_RUBRIC_PATH` > 仓库内默认版本。
    """
    if path is None:
        env_path = os.environ.get("KQUERY_RUBRIC_PATH", "").strip()
        path = Path(env_path) if env_path else DEFAULT_RUBRIC_PATH
    p = Path(path)
    if not p.exists():
        raise RubricError(
            f"找不到 rubric 文件：{p}；可用版本：{available_rubrics() or '（仓库内没有 rubric.*.json）'}"
        )
    if p.suffix in (".yaml", ".yml"):
        try:
            import yaml  # 可选依赖
        except ImportError as exc:  # pragma: no cover - 环境缺 pyyaml 时
            raise RubricError(
                f"{p} 是 YAML 格式，需要 pyyaml；建议改用 JSON（标准库即可解析）"
            ) from exc
        payload = yaml.safe_load(p.read_text(encoding="utf-8"))
    else:
        payload = json.loads(p.read_text(encoding="utf-8"))
    return _parse_payload(payload, str(p))


def available_rubrics() -> list[str]:
    """仓库里可用的 rubric 版本（给报错信息用）。"""
    return sorted(q.name for q in DEFAULT_RUBRIC_PATH.parent.glob("rubric.*.json"))
