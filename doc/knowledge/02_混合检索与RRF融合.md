# 混合检索与 RRF 融合 —— 智能 RAG 检索原理参考

> 本文档为原理性说明文档，回答三个通用问题：
> 1. 向量检索 vs BM25 各自的优劣？
> 2. RRF（Reciprocal Rank Fusion）融合的工程实现？
> 3. 重排序（Reranker）在混合检索中的作用？
>
> 文末以 `Kquery` 项目（阶段 2 混合检索 + gte-rerank-v2）为完整案例。

**文档结构**：
- 第一章：向量检索 vs BM25；
- 第二章：RRF 融合原理与公式；
- 第三章：重排序（gte-rerank-v2）；
- 第四章：运行时配置（RetrievalConfigStore）；
- 第五章：Kquery 项目案例；
- 第六章：反模式清单与落地检查清单。

---

## 一、向量检索 vs BM25

### 1.1 向量检索（语义匹配）

```python
# 文本 → 1024 维向量
query_vec = embed("退货政策是什么？")  # 1024 维

# cosine 相似度
scores = cosine(query_vec, knowledge_vectors)
top_k = top_k_by_score(scores, k=5)
```

**优势**：
- 语义匹配（"退货"召回"退款"）
- 容忍同义改写
- 跨语言（同一向量空间）

**劣势**：
- 精确关键词召回差（订单号 / 型号 / 法律条款）
- 需要 embedding 服务
- 不透明（为何匹配不易解释）

### 1.2 BM25（精确关键词）

```python
# 经典 TF-IDF + 文档长度归一化
def bm25_score(query, doc, k1=1.5, b=0.75):
    score = 0
    for term in query.terms:
        tf = doc.term_freq(term)
        df = collection.doc_freq(term)
        idf = log((N - df + 0.5) / (df + 0.5))
        score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * doc.length / avgdl))
    return score
```

**优势**：
- 精确关键词召回（型号 / 订单号 / 法规）
- 无需 embedding
- 解释性强（哪些词命中）

**劣势**：
- 不懂语义（同义词无法匹配）
- 中文分词质量影响
- 长文档噪声大

### 1.3 互补两路

| 场景 | 向量 | BM25 |
|---|---|---|
| "什么是退货政策？" | ✅ | ✅ |
| "订单号 ABC123" | ❌ 难匹配 | ✅ 精确 |
| "iPhone 15 保修" | 🟡 可能命中"保修" | ✅ 完整匹配 |
| "客户想取消订阅" | ✅ "取消订阅"语义 | 🟡 部分命中 |

> **结论**：向量 + BM25 互补，混合检索优于单路。

---

## 二、RRF 融合原理与公式

### 2.1 公式

```
score(doc) = Σ 1 / (k + rank_i(doc))
```

其中：
- `k = 60`（常用默认值）
- `rank_i(doc)` = 文档在第 i 路检索中的排名（从 1 开始）

### 2.2 简化示例

假设有两路检索结果：
- 向量 Top-3: [A, B, C]
- BM25 Top-3: [B, D, A]

计算每个文档的 RRF 分数：

| 文档 | 向量 rank | BM25 rank | RRF 分数 |
|---|---|---|---|
| A | 1 | 3 | 1/(60+1) + 1/(60+3) = 0.0164 + 0.0159 = 0.0323 |
| B | 2 | 1 | 1/(60+2) + 1/(60+1) = 0.0161 + 0.0164 = 0.0325 |
| C | 3 | — | 1/(60+3) = 0.0159 |
| D | — | 2 | 1/(60+2) = 0.0161 |

排序：B > A > D > C（向量第 3 但被 BM25 救回第 3）。

### 2.3 实现

```python
from collections import defaultdict

def reciprocal_rank_fusion(route_results: dict, k: int = 60) -> list[dict]:
    """
    route_results: {"keyword": [...], "vector": [...], ...}
    每个 list 是按相关性排序的 chunks。
    """
    scores = defaultdict(float)
    doc_lookup = {}
    
    for route_name, results in route_results.items():
        for rank, doc in enumerate(results, start=1):
            doc_id = doc["chunk_id"]
            scores[doc_id] += 1 / (k + rank)
            doc_lookup[doc_id] = doc
    
    # 按 RRF 分数排序
    sorted_docs = sorted(scores.items(), key=lambda x: -x[1])
    return [{"doc": doc_lookup[doc_id], "rrf_score": score} for doc_id, score in sorted_docs]
```

### 2.4 k 参数选择

| k 值 | 含义 |
|---|---|
| k=0 | 完全由最佳命中决定（偏向最佳） |
| k=60 | 平衡（常用默认值） |
| k=∞ | 完全由最差命中决定（偏向平均） |

> **业内参考**：k=60 是 Cohere / Vespa 的默认值，平衡两路权重。

---

## 三、重排序（gte-rerank-v2）

### 3.1 为什么需要重排序

混合检索的 Top-K 可能仍然不完美：
- RRF 只看排名，不看分数
- 某些文档排名靠前但实际不相关

**重排序**：用专门的模型对 Top-K 精排。

### 3.2 gte-rerank-v2

千问平台的 `gte-rerank-v2` 模型：
- 输入：query + 候选 chunks
- 输出：每个 chunk 的相关性分数
- 用途：对 Top-10 精排到 Top-5

### 3.3 Kquery 实现

```python
# app/services/retrieval/rerank.py
class GteReranker:
    def __init__(self, api_key):
        self.api_key = api_key
        self.url = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
    
    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        # 1. 调用千问 rerank API
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": "gte-rerank-v2",
                    "input": {"query": query, "documents": texts},
                    "parameters": {"top_n": len(texts)},
                },
            )
            data = resp.json()
        # 2. 提取分数
        return [item["relevance_score"] for item in data["output"]["results"]]
```

### 3.4 HybridRetriever 编排

```python
# app/services/retrieval/hybrid.py
class HybridRetriever:
    async def retrieve(self, query: str, top_k: int = 5) -> list[dict]:
        # 1. 多路召回
        route_results = {}
        for name, retriever in self.retrievers.items():
            route_results[name] = await retriever.retrieve(query, top_k=self.per_route_top_n)
        
        # 2. RRF 融合（k=60）
        fused = reciprocal_rank_fusion(route_results, k=self.rrf_k)
        
        # 3. Reranker 精排 Top-10
        top = fused[:self.fused_top_n]
        scores = await self.reranker.rerank(query, [c["text"] for c in top])
        for chunk, score in zip(top, scores):
            chunk["rerank_score"] = score
        
        # 4. 按 rerank_score 排序，取 Top-K
        return sorted(top, key=lambda c: c["rerank_score"], reverse=True)[:top_k]
```

---

## 四、运行时配置（RetrievalConfigStore）

### 4.1 为什么需要运行时配置

- **实验 A/B**：不同 query 用不同混合策略
- **灰度发布**：先小流量启用新策略
- **延迟生效**：避免频繁切换导致检索结果抖动

### 4.2 实现

```python
# app/services/config_store.py
class RetrievalConfigStore:
    def __init__(self, path, apply_delay_s=30):
        self._active = self._load_default()
        self._pending = None
        self._pending_at = 0
        self.apply_delay_s = apply_delay_s
    
    def set_active(self, new_config: RetrievalConfig):
        """设置新配置（延迟生效）"""
        self._pending = new_config
        self._pending_at = time.time()
    
    def get_active(self) -> RetrievalConfig:
        """获取当前生效配置"""
        if self._pending and time.time() - self._pending_at > self.apply_delay_s:
            self._active = self._pending
            self._pending = None
        return self._active
```

**关键设计**：
- 延迟 30s 生效（避免频繁切换）
- `apply_delay_s` 可配置

### 4.3 REST API

```http
GET /api/knowledge/config
# 返回当前生效配置

PUT /api/knowledge/config
{ "bm25_weight": 0.6, "vector_weight": 0.4, "rrf_k": 60 }
# 30s 后生效
```

---

## 五、Kquery 项目案例

### 5.1 完整检索链路

```
用户问题
  ↓
QueryPreprocessor (阶段 4)
  ├─ 意图识别 (qwen-turbo)
  ├─ Fusion / HyDE / Step-Back
  └─ 子问题拆解
  ↓
HybridRetriever (阶段 2)
  ├─ VectorRetriever → Top-20
  ├─ BM25Retriever → Top-20
  ├─ RRF 融合 (k=60)
  └─ GteReranker → Top-5
  ↓
Prompt 构造
  ↓
LLM 生成 (qwen-plus)
  ↓
HallucinationChecker (阶段 5)
  ↓
答案 + 引用溯源
```

### 5.2 性能特征

| 步骤 | 耗时 |
|---|---|
| Query 预处理（LLM 调用） | ~500ms |
| 向量检索 | ~50ms |
| BM25 检索 | ~10ms |
| RRF 融合 | < 1ms |
| gte-rerank-v2 | ~200ms |
| LLM 生成（流式首 token） | ~500ms |
| **总 P50** | **~1.3s** |

### 5.3 配置默认值

```python
# app/core/config.py
bm25_k1: float = 1.5
bm25_b: float = 0.75
rrf_k: int = 60
retrieval_per_route_top_n: int = 20
retrieval_fused_top_n: int = 10
```

---

## 六、反模式清单

### ❌ 反模式 1：纯向量不用 BM25

```python
# 错误
chunks = vector_index.search(embed(query), top_k=5)
```

**正确**：混合 + RRF。
```python
vec = vector_index.search(embed(query), top_k=20)
bm25 = bm25_index.search(query, top_k=20)
fused = rrf_fusion(vec, bm25, k=60)[:10]
chunks = await reranker.rerank(query, [c["text"] for c in fused])
```

### ❌ 反模式 2：RRF 后直接返回 Top-K（无重排序）

```python
# 错误：RRF 后直接取 Top-K
chunks = rrf_fusion(vec, bm25, k=60)[:5]
```

**正确**：Top-10 重排到 Top-5。
```python
fused = rrf_fusion(vec, bm25, k=60)[:10]
scores = await reranker.rerank(query, [c["text"] for c in fused])
chunks = sorted(fused, key=lambda c: scores[c["chunk_id"]], reverse=True)[:5]
```

### ❌ 反模式 3：硬编码混合权重

```python
# 错误：硬编码
vec_weight = 0.5
bm25_weight = 0.5
```

**正确**：运行时配置。
```python
config = config_store.get_active()
chunks = weighted_fusion(vec, bm25, vec_weight=config.vector_weight, bm25_weight=config.bm25_weight)
```

### ❌ 反模式 4：每次检索都重新加载配置

```python
# 错误：每次检索都读文件
config = load_config_from_file()  # 慢
```

**正确**：启动加载 + 热更新。
```python
# 启动期
config_store.load()
# 热更新
config_store.set_active(new_config)  # 内存直接改
```

---

## 七、落地检查清单

- [ ] **多路召回**：向量 + BM25（每路 Top-20）
- [ ] **RRF 融合**：k=60（默认值）
- [ ] **重排序**：gte-rerank-v2 对 Top-10 精排
- [ ] **运行时配置**：RetrievalConfigStore 热更新
- [ ] **延迟生效**：30s apply_delay（避免抖动）
- [ ] **性能监控**：每路召回耗时 / Reranker 耗时 / 总耗时
- [ ] **A/B 测试**：不同 query 用不同配置
- [ ] **降级**：无 Reranker 时跳过（不影响主链路）

---

## 八、附录：核心代码位置速查（Kquery）

- `HybridRetriever.retrieve`（`app/services/retrieval/hybrid.py`）：混合检索编排
- `VectorRetriever`（`app/services/retrieval/vector.py`）：向量检索
- `BM25Retriever`（`app/services/retrieval/bm25.py`）：BM25 检索
- `GteReranker`（`app/services/retrieval/rerank.py`）：重排序
- `reciprocal_rank_fusion`（`app/services/retrieval/hybrid.py`）：RRF 融合函数
- `RetrievalConfigStore`（`app/services/config_store.py`）：运行时配置
