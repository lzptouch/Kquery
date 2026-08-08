# Kquery 压力测试说明

## 场景

- `chat_load.py`：聊天/问答，测试 `/api/v1/agent/chat` 的延迟与吞吐
- `search_load.py`：混合检索，测试 `/api/v1/kquery/search` 与图谱查询
- `upload_load.py`：文档上传，测试上传解析链路
- `auth_burst.py`：登录突发，测试限流与 429 响应

## 运行

```bash
# 确保后端已启动
cd test/stress
locust -f locustfile.py --host http://localhost:8000 --headless \
  -u 50 -r 5 -t 5m --csv results/stress
```

## 基线（真实 DashScope qwen-turbo 标定，5 并发 20s）

| 场景 | 并发 | P95 | 错误率 | 备注 |
|---|---|---|---|---|
| chat | 5 | ~5.3s | 0% | 真实 LLM 流式响应，延迟主要来自网络+模型 |
| search | 5 | ~370ms | 0% | 未命中集合时返回空，服务层响应极快 |
| upload | 5 | ~400ms | 0% | `/kquery/documents/process` 200 success |
| auth-login | 5 | ~950ms | 0% | 含注册/登录完整链路 |

## 生产建议基线

| 场景 | 并发 | P95 目标 | 错误率目标 |
|---|---|---|---|
| chat | 50 | < 8s | < 1% |
| search | 80 | < 1.5s | < 1% |
| upload | 20 | < 5s | < 2% |
| auth | 100 突发 | < 1.5s | < 0.5%（429 不算错误） |

说明：后端未配置 LLM Key 时会自动走 `core/mock_data/` 内置 Mock 数据，无需真实大模型。
