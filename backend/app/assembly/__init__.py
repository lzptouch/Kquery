"""按装配域拆分的应用装配层（`A-12`）。

每个模块回答一个「怎么装」的问题，顺序由 `lifespan.py` 显式编排：

| 模块 | 回答的问题 |
|---|---|
| `context.py` | 装配期在哪些对象之间传手 |
| `runtime.py` | 指标 / 共享状态 / 限流 / 缓存 / 执行器怎么装 |
| `models.py` | LLM / Embedding / Rerank / OCR 怎么装 |
| `storage.py` | 向量库 / BM25 / 文档存储 / 混合检索怎么装 |
| `knowledge.py` | 知识服务 / 任务队列 / 快问怎么装 |
| `pipeline.py` | 查询预处理与问答流水线怎么装 |
| `agent.py` | 工具目录 / 图谱 / 检查点 / Agent 运行器怎么装 |
| `hooks.py` | 入库后置钩子等跨域回调怎么接 |
| `lifespan.py` | 上面这些按什么顺序跑、退出时怎么收尾 |
"""
