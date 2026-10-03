"""拓扑分层（阶段 8 §4）：同层并行、跨层串行。

- Kahn 算法按依赖分层；环检测（规划器幻觉防御）；
- 层数 > 4 或单层 > 8 是规划质量信号，记录 warning；
- 全部落在一层 → 记录"规划器可能未识别依赖"的提示。
"""
import logging

from app.services.multiagent.blackboard import SubTask

logger = logging.getLogger(__name__)


def topo_layers(tasks: list[SubTask]) -> list[list[SubTask]]:
    """按依赖关系分层。返回 [(第0层任务...), (第1层...), ...]。环 → 剩余任务并入最后一层并告警。"""
    by_id = {t.task_id: t for t in tasks}
    remaining = dict(by_id)
    done: set[str] = set()
    layers: list[list[SubTask]] = []
    while remaining:
        ready = [t for tid, t in remaining.items() if all(d in done or d not in by_id for d in t.depends_on)]
        if not ready:
            # 环依赖（规划器幻觉）：把剩余任务并入新层继续，不中断流程
            logger.warning("子任务依赖成环，剩余 %d 个任务强制并入新层: %s", len(remaining), list(remaining))
            layer = sorted(remaining.values(), key=lambda t: t.task_id)
            layers.append(layer)
            break
        layer = sorted(ready, key=lambda t: t.task_id)
        for t in layer:
            t.layer = len(layers)
            done.add(t.task_id)
            del remaining[t.task_id]
        layers.append(layer)
    if len(layers) > 4:
        logger.warning("拓扑层数 %d 超过 4，串行等待将累积", len(layers))
    if any(len(layer) > 8 for layer in layers):
        logger.warning("单层任务数超过 8，并行度过高")
    if len(layers) == 1 and len(tasks) > 1:
        logger.info("全部子任务落在同一层：规划器可能未识别依赖（警惕未就绪数据隐患）")
    return layers
