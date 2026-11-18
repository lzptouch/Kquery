"""生成侧纯函数集合（`B0 X-27` 第一步从 `services/pipeline.py` 抽出）。

为什么单独成包：`pipeline.py` 同时承担缓存、预处理、检索编排、CRAG、上下文
组装、生成、自检、拒答、推荐追问、single-flight，改一处要读全文。这里收的都是
**无状态、可单测**的部分——提示词渲染与证据装配，输入输出全是显式参数，
不持有 `RagPipeline` 的任何可变状态。

- `prompt`：提示词模板与证据块渲染（含注入加固的成对边界标记）；
- `evidence`：候选 → `ContextItem` → `Evidence` 的装配，以及未采纳证据的留痕。

编排、缓存、检索与生成阶段仍在 `services/pipeline.py`（第二步未做）。
"""
# 注意：这里**不做再导出**。包内两个模块各自被调用方直接导入
# （`from app.services.generation.prompt import build_prompt`），
# `__init__` 若再 import 一遍会白增两处模块级导入（`A-14` 只减不增）。
__all__ = ["evidence", "prompt"]
