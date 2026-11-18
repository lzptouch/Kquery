"""跨域回调的接线：入库后置钩子 / 删除编排 / 校验历史回填（`A-12`）。

## 为什么这些接线单独成文件

它们是**域与域之间的耦合点**：入库（知识域）完成时要建图（Agent 域）、
建图像索引（多模态），删除文档时要同时收敛图谱/图像/资产/任务原件。
把这些耦合点集中到一处，比散落在各装配域里更容易回答
「两个域到底通过什么互相知道对方存在」。

## 钩子失败不能反噬入库

钩子返回的是统计信息（`{"graph": {...}, "images": {...}}`），调用方据此记录
`post_index` 结果；单个文件/图片的失败在钩子内部计为 `skipped`，
不让一个坏图片把整份文档的入库判失败。
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.assembly.context import Assembly


def _make_post_index(asm: Assembly) -> Callable[[object, object], dict]:
    """构造入库后置钩子：建图 + 建图像索引。"""
    settings = asm.settings
    store = asm.store
    graph_builder = asm.graph_builder
    image_index = asm.image_index

    # 图谱构建与图像索引改为**入库联动**（）：此前两者只能靠
    # 手动打 /graph/build 与 /media/index，文档入库后图谱长期是空的，
    # "图谱检索"这个能力等于形同虚设。
    def _post_index(record, parsed) -> dict:
        stats: dict = {"graph": {}, "images": {}}
        if not settings.graph_enabled:
            return stats
        chunks = [c for c in store.all_chunks() if c.doc_id == record.doc_id]
        if chunks:
            stats["graph"] = graph_builder.build_from_chunks(
                chunks, rebuild_doc_id=record.doc_id
            )
        assets = getattr(parsed, "assets", []) or []
        indexed, skipped = 0, 0
        for asset in assets:
            image_path = getattr(asset, "path", "") or ""
            if not image_path or not Path(image_path).exists():
                skipped += 1
                continue
            if image_index.index_image(
                Path(image_path), asset.asset_id,
                doc_id=record.doc_id, page=getattr(asset, "page", 0),
                tenant_id=record.tenant_id,
            ) is not None:
                indexed += 1
            else:
                skipped += 1
        stats["images"] = {"indexed": indexed, "skipped": skipped, "total": len(assets)}
        return stats

    return _post_index


def install_hooks(asm: Assembly) -> None:
    """把入库钩子、删除编排依赖、校验历史回填一次接好。"""
    post_index = _make_post_index(asm)
    asm.post_index = post_index
    # 任务队列侧是晚绑定（`lambda` 调用），这里填进容器即生效
    asm.post_index_box["hook"] = post_index
    # 同步上传路径同样要触发（否则只有异步任务能建出图谱）
    asm.knowledge.set_post_index(post_index)
    # 删除编排的协作者回填（KQ-08：删除文档必须收敛图谱/图像/资产/任务原件）
    asm.knowledge.set_deletion_deps(
        graph_store=asm.graph_store,
        image_index=asm.image_index,
        task_manager=asm.task_manager,
    )
    # 校验历史回填到**线性路径**：此前只挂 Agent 路径，默认问答不产生任何痕迹
    asm.pipeline.attach_history(history_store=asm.validation_history)
