# Kquery 后端（阶段 1：最小可用 RAG）

依据 `doc/series/stage-1-naive-rag/README.md` 实现：加载文档 → 切块 → Embedding → 向量索引 → 检索 → LLM 生成 → 答案 + 引用。

## 技术栈

- FastAPI + uvicorn，uv 管理依赖
- FAISS `IndexFlatIP`（向量归一化后内积即余弦相似度），索引落盘复用
- 模型统一走千问平台（阿里云百炼）：`text-embedding-v3` / `qwen-plus`，全部接口调用，无本地模型
- 依赖注入：Embedding / LLM 为协议接口，内置 Mock 实现供无凭据环境测试

## 快速开始

```bash
cd backend
cp .env.example .env        # 填入 DASHSCOPE_API_KEY；留空则自动使用 Mock
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 端点

| 端点 | 说明 |
| --- | --- |
| `GET /health` | 健康检查 |
| `POST /ask` | 同步问答，返回 `answer + evidence[] + trace_id`（统一信封） |
| `POST /ask/stream` | SSE 流式问答，事件：`token` / `evidence` / `done` / `error` |
| `POST /knowledge/upload` | 上传文档（.pdf / .md），同步完成解析与索引 |
| `GET /knowledge/files` | 知识库列表（含块数与状态） |
| `POST /knowledge/rebuild/{doc_id}` | 重建索引 |
| `DELETE /knowledge/files/{doc_id}` | 删除文档 |

## 测试

```bash
uv run pytest          # 单元 + 端到端功能测试（Mock 模型，无需 API Key）
```
