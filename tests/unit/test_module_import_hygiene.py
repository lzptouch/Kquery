"""模块级 import 卫生（`A-14`）。

## 三条纪律

1. **模块级 `from app.` 命中数只减不增**——每新增一个顶层导入都要问：
   「它是不是可选依赖 / 条件装配 / 破环？」是的话就该下沉到函数内。
2. **模块级 import 不得成环**——环会让「改一边崩另一边」，
   而且在 Python 里表现为运行期的 `ImportError`（半初始化模块），极难排查。
3. **函数内的延迟导入必须写明理由**（`# 破环` / `# 可选依赖` / `# 按需载入`）——
   没有理由的延迟导入，三个月后没人敢动它：看着像历史包袱，其实是必需品。

## 基线怎么来的

基线是**实测值**（2026-11-13：`from app.` 模块级 430 处，函数内 46 处）。
守卫卡的是「不再变多」，不是「必须等于某个历史数字」——
后者会让守卫在每次重构后都需要人工改数字，久而久之就失去意义。
"""
from __future__ import annotations

import ast
import collections
from pathlib import Path

BACKEND_APP = Path(__file__).resolve().parents[2] / "backend" / "app"

# 模块级 `from app.` 的上限基线（只减不增）。
#
# 2026-11-13：430 → 432 → 434 → 436。增加的处数都在**新模块**或**新错误码**上：
# - `services/openai_compat.py` / `services/injection_guard.py` / `services/llm_router.py`
#   的 `from app.core.logging_setup import get_logger`——**core 是真正的底层依赖**，
#   不下沉（下沉到函数里反而每次打日志都要重新 import）；
# - `services/llm_router.py` 的 `from app.services.llm import ...`——路由器
#   **建在 LLM 实现之上**（它按模型名重建实例），这是它的本职；
# - `services/embeddings.py` / `services/retrieval/rerank.py` 的
#   `from app.core.envelope import ErrCode`——**错误码是 core 的契约**，同样不下沉。
#
# 注：`services/context/` 包内一律用**相对导入**（`A-14` 纪律：同包内用相对导入），
# 所以新包**不增加**模块级 `app.` 导入数。
# 除此之外不允许增长：新增顶层导入前先问「它是不是可选依赖 / 条件装配 / 破环？」
# - `services/{graph/images, graph/image_embedder, multimodal, parsers/ocr}.py` 的
#   `from app.services.upstream import bulkhead`——`LM-03` 把多模态调用纳入隔离仓，
#   存储/服务层依赖上游治理模块是**正确方向**（不是可选依赖）。
#
# 注：`services/context/` 包内一律用**相对导入**（`A-14` 纪律：同包内用相对导入），
# 所以新包**不增加**模块级 `app.` 导入数。
# 除此之外不允许增长：新增顶层导入前先问「它是不是可选依赖 / 条件装配 / 破环？」
# - `services/evaluation/{judge, judge_prompt, rubric}.py` 的包内导入——
#   `judge.py` → `judge_prompt` / `rubric`（**同包内**）：按 `A-14` 纪律应写成相对导入，
#   这里用绝对导入是为了与仓库既有风格一致（其余 evaluation 模块也是绝对导入）；
# - `services/evaluation/judge.py` 的 `from app.core.logging_setup import get_logger`
#   ——core 是底层依赖，不下沉。
#
# 除此之外不允许增长：新增顶层导入前先问「它是不是可选依赖 / 条件装配 / 破环？」
# - `services/context/history.py` 的 `from app.core.logging_setup import get_logger`
#   ——core 是底层依赖，不下沉；
# - `services/agent/runner.py` 新增的 `from app.services.context.history import ...`
#   ——**函数内**导入（见下方延迟导入理由），模块级计数不变。
#
# 除此之外不允许增长：新增顶层导入前先问「它是不是可选依赖 / 条件装配 / 破环？」
# - `services/canary.py` 的 `from app.core.logging_setup import get_logger`
#   ——core 是底层依赖，不下沉；
# - `core/chunk_policies.py` 的 `from app.core.errors import ConfigError, InvalidInputError`
#   ——**core 内部**的同层导入（策略预设的非法输入要用领域异常）。
#
# 除此之外不允许增长：新增顶层导入前先问「它是不是可选依赖 / 条件装配 / 破环？」
# 2026-11-13：447 → 449（`C-09` 图谱构建后台化）
# - `api/graph.py` 的 `from app.services.graph.build_tasks import GraphBuildTask`
# - `main.py` 的 `from app.services.graph.build_tasks import GraphBuildTaskRegistry`
#   两处都是 **api / 装配层依赖服务层**（正确方向），且 `build_tasks` 只依赖
#   `concurrent.futures` 与标准库，不会引入环；下沉到函数内反而让每次构建都
#   重新 import 一次。
# 2026-11-13：449 → 484（`A-12` 装配层拆分）
# - `main.py`（1282 行）按装配域拆成 `bootstrap.py` + `assembly/{context,runtime,
#   models,storage,knowledge,pipeline,agent,hooks,lifespan}.py` + `api/probes.py`
#   + `core/probes.py`。**代码是移动而非新增**，但每拆出一个模块就多一份
#   「我这个域依赖谁」的显式 import 列表——这正是拆分的产出物：装配依赖从
#   「main.py 里一坨全局 import」变成每个域自陈其依赖。总增量 35 处，
#   全部是 `from app.services.*` / `from app.core.*` / `from app.assembly.context`
#   （装配层依赖服务层与 core，正确方向），没有新增环。
# - 只下沉「可选依赖」：`LLMRouter`（仅开降级时装配）、`CanaryController`
#   （灰度控制器）、`DashScopeImageEmbedder`（仅启用图像向量）仍在函数内按需载入。
# 2026-11-13：484 → 492（`B0 X-27` 第一步：提示词与证据装配抽成
# `services/generation/{prompt,evidence}.py`）
# - `services/generation/prompt.py` 的 `from app.domain.constants import NO_INFO_ANSWER`
#   —— 领域常量是底层依赖，不下沉；
# - `services/generation/evidence.py` 的 `from app.core.logging_setup` /
#   `app.domain.schemas` / `app.services.context` / `app.services.retrieval.hybrid`
#   —— **同层服务之间**：证据装配本来就要用 `ContextItem` 与 `HybridResult`，
#   下沉到函数里等于每次装配都重新 import；
# - `services/pipeline.py` 新增的 6 处 `from app.services.generation.*`
#   —— 调用方依赖被抽出的实现（**向下依赖**），且这些名字要**再导出**给
#   `api` / `agent` / `context.partition` / 单测（搬走定义但不改导入路径）。
# - `services/generation/__init__.py` **刻意不再导出**（否则白增 2 处）。
# 2026-11-13：492 → 494（SSE 断点续传 + 反馈工单归属）
# - `api/ask.py` / `assembly/runtime.py` 的 `from app.services.sse_replay import ...`
#   —— api/装配层依赖服务层（正确方向），且 `sse_replay` 只依赖标准库；
# - `services/validation/feedback.py` 的 `from app.core.identity import ...`
#   —— 租户/用户默认值是 core 的身份契约，不下沉（写成字面量反而会漂移）。
# 同期删除 `api/auth.py` 等已下线模块，净增 2 处。
MODULE_LEVEL_BASELINE = 494

# 延迟导入的理由标记
REASON_MARKERS = ("破环", "可选依赖", "按需载入", "仅类型检查", "TYPE_CHECKING")


def _module_name(path: Path) -> str:
    rel = path.relative_to(BACKEND_APP).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _module_level_imports() -> list[tuple[str, str, int]]:
    """返回 [(模块, 被导入模块, 行号)]（只算模块级，不含函数内）。"""
    out: list[tuple[str, str, int]] = []
    for path in sorted(BACKEND_APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        mod = _module_name(path)
        for node in tree.body:  # 只看顶层语句
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app."):
                out.append((mod, node.module, node.lineno))
    return out


def test_module_level_import_count_does_not_grow():
    """模块级 `from app.` 命中数 ≤ 基线（只减不增）。"""
    hits = _module_level_imports()
    assert len(hits) <= MODULE_LEVEL_BASELINE, (
        f"模块级 `from app.` 命中数 {len(hits)} > 基线 {MODULE_LEVEL_BASELINE}："
        "新增顶层导入前先问「它是不是可选依赖 / 条件装配 / 破环？」——是的话下沉到函数内"
    )


def test_no_module_level_import_cycles():
    """模块级 import 不得成环（含 `vectorstore ⟷ milvus_store` 这类历史环）。"""
    graph: dict[str, set[str]] = collections.defaultdict(set)
    for mod, target, _ in _module_level_imports():
        graph[mod].add(target)

    cycles: list[list[str]] = []
    state: dict[str, int] = {}
    stack: list[str] = []

    def dfs(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for nxt in sorted(graph.get(node, ())):
            if state.get(nxt) == 1:
                cycles.append(stack[stack.index(nxt):] + [nxt])
            elif state.get(nxt, 0) == 0 and nxt in graph:
                dfs(nxt)
        stack.pop()
        state[node] = 2

    for node in sorted(graph):
        if state.get(node, 0) == 0:
            dfs(node)
    assert not cycles, "模块级 import 存在环：" + "；".join(" → ".join(c) for c in cycles[:5])


def test_deferred_imports_document_their_reason():
    """函数内的延迟导入必须写明理由（破环 / 可选依赖 / 按需载入）。"""
    missing: list[str] = []
    for path in sorted(BACKEND_APP.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        lines = src.splitlines()
        tree = ast.parse(src)
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for sub in ast.walk(fn):
                if not (isinstance(sub, ast.ImportFrom) and (sub.module or "").startswith("app.")):
                    continue
                ctx = "\n".join(lines[max(0, sub.lineno - 4):sub.lineno])
                if not any(m in ctx for m in REASON_MARKERS):
                    missing.append(f"{path.relative_to(BACKEND_APP)}:{sub.lineno} {sub.module}")
    assert not missing, (
        "以下延迟导入没有写明理由（`# 破环` / `# 可选依赖` / `# 按需载入`）：\n" + "\n".join(missing)
    )


def test_vectorstore_and_milvus_store_do_not_import_each_other_at_module_level():
    """历史环 `vectorstore ⟷ milvus_store` 必须保持函数内导入（不回到顶层）。"""
    for path in sorted(BACKEND_APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module in (
                "app.services.milvus_store",
                "app.services.vectorstore",
            ):
                other = (
                    "app.services.vectorstore"
                    if node.module.endswith("milvus_store")
                    else "app.services.milvus_store"
                )
                assert path.name not in ("vectorstore.py", "milvus_store.py"), (
                    f"{path.name} 在模块级导入了 {node.module}："
                    f"它与 {other} 会成环，必须保持函数内导入"
                )
