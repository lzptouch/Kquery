"""问答流水线装配：查询预处理 + CRAG + 自检 + 生成编排（`A-12`）。

## 装配层只做「接线」，不做「策略」

哪些开关打开（`crag_enabled` / `selfcheck_enabled` / `preprocess_enabled`）、
扩写器怎么组合，全部来自配置；装配层不写默认值、不做二次判断——
否则「配置说关、实际开着」这类问题会同时存在于配置与装配两处。

## 三个可选件传 `None` 而不是桩对象

CRAG 路由与自检器关闭时传 `None`：流水线里对它们的判断是
`if self._crag is not None`。传一个「永远返回通过」的桩对象会让
「关闭」与「开启但从不触发」在指标上无法区分。
"""
from __future__ import annotations

from app.assembly.context import Assembly
from app.services.pipeline import RagPipeline
from app.services.query.analysis_store import AnalysisStore
from app.services.query.decomposer import Decomposer, SourceRouter
from app.services.query.expanders import FusionExpander, HydeExpander, StepBackExpander
from app.services.query.intent import IntentClassifier
from app.services.query.orchestrator import QueryPreprocessor
from app.services.validation.crag import CragRouter
from app.services.validation.selfcheck import HallucinationChecker


def build_pipeline(asm: Assembly) -> None:
    """装好查询预处理器与 `RagPipeline`（问答链路的编排核心）。"""
    settings = asm.settings
    app = asm.app

    # Query 预处理（意图识别压到最低档，HyDE 与 Fusion 互斥）
    fast_llm = asm.fast_llm
    llm = asm.llm
    expander_names = {n.strip() for n in settings.query_expanders.split(",") if n.strip()}
    expanders: list = []
    if "fusion" in expander_names:
        expanders.append(FusionExpander(fast_llm, n=settings.fusion_n))
    elif "hyde" in expander_names:  # HyDE 与 Fusion 不同时启用（互相稀释）
        expanders.append(HydeExpander(llm))
    if "stepback" in expander_names:
        expanders.append(StepBackExpander(llm))
    preprocessor = QueryPreprocessor(
        intent_classifier=IntentClassifier(fast_llm),
        expanders=expanders,
        decomposer=Decomposer(llm),
        router=SourceRouter(),
        query_budget=settings.query_budget,
        enabled=settings.preprocess_enabled,
    )
    analysis_store = AnalysisStore(settings.data_dir / "query_analysis.json")

    pipeline = RagPipeline(
        settings,
        llm,
        asm.hybrid,
        asm.cache,
        asm.config_store,
        asm.prom,
        asm.metrics_store,
        doc_name_resolver=asm.knowledge.filename_of,
        docstore=asm.docstore,
        index_version_provider=lambda: asm.knowledge.data_version,
        preprocessor=preprocessor,
        analysis_store=analysis_store,
        crag_router=CragRouter(
            correct_th=settings.crag_correct_th,
            incorrect_th=settings.crag_incorrect_th,
            max_iterations=settings.crag_max_iterations,
        )
        if settings.crag_enabled
        else None,
        self_checker=HallucinationChecker(
            llm, model_version=f"{settings.llm_model}"
        ) if settings.selfcheck_enabled else None,
    )

    asm.preprocessor = preprocessor
    asm.analysis_store = analysis_store
    asm.pipeline = pipeline
    app.state.analysis_store = analysis_store
    app.state.pipeline = pipeline
