# Kquery 项目概览

Kquery 是一个本地优先的文档问答系统，后端用 FastAPI，前端用 React + Vite。
它面向的是"把一批 PDF / Markdown / PPTX / 表格丢进一个目录，然后能问、能溯源"的场景。

## 组成

- 后端：FastAPI 应用，包名 `app`，入口 `app.main:app`。
- 检索：BM25（关键词路）与向量（语义路）双路召回，RRF 融合，重排精排。
- 图谱：SQLite 存储的实体关系图，提供图谱检索工具。
- Agent：计划式（plan_execute）与 ReAct 两种模式，共用工具注册表与预算账本。
- 前端：管理台（监控、检索配置、知识库）与聊天页（SSE 流式）。

## 部署形态

默认是 Docker Compose：后端 8100、前端 8090、Milvus 19531、Redis 6381。
生产化改造的目标是上 Kubernetes，状态（会话、检查点、图谱、检索配置）需要外置。
