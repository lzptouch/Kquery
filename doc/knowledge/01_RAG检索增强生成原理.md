# RAG 检索增强生成原理 —— 智能问答系统参考

> 本文档为原理性说明文档，回答三个通用问题：
> 1. RAG 是什么？与 LLM 直答、Fine-tuning 的对比？
> 2. RAG 系统的核心链路如何设计？
> 3. 评估 RAG 效果的关键指标？
>
> 文末以 `Kquery` 项目（9 阶段演进 + 智能 RAG）为完整案例。

**文档结构**：
- 第一章：RAG 的本质与价值；
- 第二章：RAG 系统的核心链路；
- 第三章：评估与质量保障；
- 第四章：Kquery 项目案例（9 阶段演进）；
- 第五章：反模式清单与落地检查清单。

---

## 一、RAG 的本质与价值

### 1.1 LLM 的三大局限

| 局限 | 表现 | RAG 解法 |
|---|---|---|
| **知识截止** | 训练数据外的知识无法回答 | 检索外部知识 |
| **幻觉** | 编造不存在的内容 | 引用证据溯源 |
| **无法访问私有数据** | 不知道企业内部文档 | 检索企业内部知识库 |

### 1.2 RAG vs Fine-tuning vs Prompt Engineering

| 维度 | RAG | Fine-tuning | Prompt Engineering |
|---|---|---|---|
| **适用场景** | 知识频繁更新 | 模型行为调整 | 简单指令 |
| **成本** | 中（向量库 + 检索） | 高（GPU + 训练数据） | 低（仅 prompt） |
| **更新速度** | 实时（添加文档即可） | 慢（重训） | 立即 |
| **数据需求** | 文档库 | 大量 QA 对 | 无 |
| **可解释性** | 高（引用溯源） | 低 | 中 |

> **结论**：**知识更新场景首选 RAG**；行为调整选 Fine-tuning；简单指令用 Prompt。

### 1.3 RAG 范式演进（Naive → Advanced → Modular）

| 范式 | 特点 | Kquery 阶段 |
|---|---|---|
| **Naive RAG** | 向量检索 + Prompt 注入 | 阶段 1 |
| **Advanced RAG** | 混合检索 + 重排 + 自反思 | 阶段 2~5 |
| **Modular RAG** | Agentic 决策 + 多模块组合 | 阶段 6~8 |

---

## 二、RAG 系统的核心链路

### 2.1 端到端流程

```
用户问题
  ↓
1. Query 预处理（阶段 4）
  ├─ 意图识别（chitchat 短路）
  ├─ Fusion / HyDE / Step-Back
  └─ 多跳拆解
  ↓
2. 检索（阶段 2~3）
  ├─ 向量检索 Top-50
  ├─ BM25 检索 Top-20
  ├─ RRF 融合（k=60）
  └─ gte-rerank-v2 重排 Top-10
  ↓
3. Prompt 构造
  ├─ 系统提示词
  ├─ 检索 Top-5 片段
  └─ 用户问题
  ↓
4. LLM 生成（阶段 1 / 8）
  ↓
5. 自反思（阶段 5）
  ├─ CRAG 三分支（correct / ambiguous / incorrect）
  ├─ 幻觉自检
  └─ 拒答反馈工单
  ↓
6. 引用溯源 + 信封
```

### 2.2 关键设计决策

#### 决策 1：切片策略

| 策略 | 适用 | 优点 | 缺点 |
|---|---|---|---|
| **Recursive** | 通用 | 简单稳定 | 语义不连续 |
| **Semantic** | 高质量文档 | 语义聚合 | 慢（需 embedding） |
| **ParentChild** | 问答需上下文 | 检索子块、返回父块 | 切片数翻倍 |
| **RAPTOR** | 跨文档摘要 | 树状递归 | 慢（多轮聚类 + LLM 摘要） |

> Kquery 4 种策略全支持（`app/services/chunkers/`）。

#### 决策 2：检索混合

| 维度 | 向量 | BM25 |
|---|---|---|
| "什么是退货政策？" | ✅ | ✅ |
| "订单号 ABC123" | ❌ | ✅ |
| "iPhone 15 保修" | 🟡 | ✅ |
| "客户想取消订阅" | ✅ | 🟡 |

→ **混合 + RRF（k=60）** 优于单路。

#### 决策 3：重排序

- **gte-rerank-v2** 对 Top-10 精排
- 比向量相似度更准（专门训练）
- 代价：每查询 ~200ms

#### 决策 4：自反思（CRAG）

```python
class CragRouter:
    def route(self, evidence_score):
        if evidence_score >= 0.7: return "correct"      # 直接生成
        elif evidence_score <= 0.3: return "incorrect"  # 拒答
        else: return "ambiguous"                         # 受限重生成
```

### 2.3 Kquery 实现（RagPipeline）

```python
# app/services/pipeline.py 的 RagPipeline.run
async def run(self, question, session_id=None, ...):
    # 1. Query 预处理（阶段 4）
    preprocessed = await self.preprocessor.preprocess(question)
    if preprocessed["intent"] == "chitchat":
        return await self.llm.call(...), {"intent": "chitchat"}
    
    # 2. 检索（阶段 2）
    chunks = await self.hybrid.retrieve(question, top_k=5)
    
    # 3. CRAG 路由（阶段 5）
    evidence_score = compute_evidence_score(chunks)
    verdict = self.crag_router.route(evidence_score)
    if verdict == "incorrect":
        return self._refuse(question, reason="evidence too weak")
    
    # 4. Prompt 构造
    prompt = build_prompt(chunks, question, history=...)
    
    # 5. LLM 生成
    answer = await self.llm.call(prompt)
    
    # 6. 幻觉自检（阶段 5）
    if self.self_checker:
        check = await self.self_checker.check(answer, chunks)
        if not check["is_grounded"]:
            return self._refuse(question, reason="hallucination detected")
    
    # 7. 引用溯源
    return {
        "answer": answer,
        "evidence": chunks,
        "verdict": verdict,
        "confidence": ...,
    }
```

---

## 三、评估与质量保障

### 3.1 评估指标

| 指标 | 含义 | 计算 |
|---|---|---|
| **pass_rate** | 通过率 | 通过用例 / 总用例 |
| **answer_quality** | 答案质量（人工 / LLM 评分） | LLM-as-a-judge |
| **citation_accuracy** | 引用准确率 | 引用相关 / 总引用 |
| **hallucination_rate** | 幻觉率 | 幻觉次数 / 总次数 |
| **cost_ratio** | 成本比 | 新成本 / 基准成本 |
| **latency_ratio** | 延迟比 | 新延迟 / 基准延迟 |

### 3.2 Kquery 上线门禁（阶段 9）

```python
# app/services/security/evalrunner.py
class EvalRunner:
    async def compare(self, baseline_id, current_id):
        base = self.store.get_eval_result(baseline_id)
        cur = self.store.get_eval_result(current_id)
        
        pass_rate_delta = cur.pass_rate - base.pass_rate
        cost_ratio = cur.cost / base.cost
        latency_ratio = cur.latency / base.latency
        
        verdict = "pass"
        if pass_rate_delta < -0.02: verdict = "fail"  # 回归 > 2%
        if cost_ratio > 1.2: verdict = "fail"          # 成本 > 1.2x
        if latency_ratio > 1.1: verdict = "fail"       # 延迟 > 1.1x
        
        return {"verdict": verdict, ...}
```

### 3.3 评测必须绑定版本

```python
class EvalResult(BaseModel):
    eval_id: str
    eval_set_version: str  # 评测集版本
    model_version: str     # 模型档位版本
    pass_rate: float
    cost_ratio: float
    latency_ratio: float
    verdict: str
```

> **关键**：千问平台模型会升级（应用侧无法拒绝），评测必须绑定版本才能追溯。

---

## 四、Kquery 项目案例（9 阶段演进）

### 4.1 阶段 1：Naive RAG

```python
# 最简实现
async def ask(question):
    chunks = vector_index.search(embed(question), top_k=5)
    prompt = build_prompt(chunks, question)
    return await llm.call(prompt)
```

### 4.2 阶段 2：混合检索 + 工程化

- BM25 + 向量 + RRF + gte-rerank-v2
- 语义缓存 + 令牌桶限流 + Prometheus 指标

### 4.3 阶段 3：智能分块 + 多模态

- 4 种分块策略 + PDF / Docx / OCR / ASR
- 入库任务队列 + 解析预览

### 4.4 阶段 4：Query 预处理

- 意图识别（qwen-turbo）
- Fusion / HyDE / Step-Back / 多跳拆解

### 4.5 阶段 5：自反思 + 自校正

- CRAG 三分支路由器
- 幻觉自检
- 拒答反馈工单

### 4.6 阶段 6：Agentic RAG

```python
# LangGraph 状态机
workflow = StateGraph(AgentState)
workflow.add_node("plan", planner.plan)
workflow.add_node("execute", execute_tool_calls)
workflow.add_node("reflect", reflector.reflect)
workflow.add_node("answer", answer_llm.answer)
# 条件边：should_continue / should_replan
```

### 4.7 阶段 7：GraphRAG + 多模态

- 实体关系抽取 + 消歧 + 图谱
- 多模态统一向量空间（放弃双塔对比模型）

### 4.8 阶段 8：多智能体报告

- Planner / Researcher / Validator / Writer / Critic
- Writer ↔ Critic 循环（默认 2 轮）

### 4.9 阶段 9：企业级护栏

- 认证 + ACL + 配额 + 敏感词 + 审计
- 评测门禁（pass_rate / cost / latency）

---

## 五、反模式清单

### ❌ 反模式 1：单文档一次性切片

```python
# 错误：一篇 100KB 文档只切 1 片
chunks = [document_text]
```

**正确**：固定窗口（500/50 重叠）。
```python
chunks = []
for start in range(0, len(text), 500):
    chunks.append(text[start:start+500])
    start += 450  # 50 字重叠
```

### ❌ 反模式 2：纯向量不用 BM25

```python
# 错误：精确关键词召回差
chunks = vector_index.search(embed(query), top_k=5)
```

**正确**：混合检索。
```python
vec = vector_index.search(embed(query), top_k=50)
bm25 = bm25_index.search(query, top_k=20)
chunks = rrf_fusion(vec, bm25, k=60)[:5]
```

### ❌ 反模式 3：Prompt 中无约束

```python
# 错误：LLM 自由发挥
prompt = f"问题：{query}\n请回答。"
```

**正确**：明确约束。
```python
prompt = f"""仅基于以下知识库内容回答。如无相关内容，明确告知用户。
{knowledge_chunks}
问题：{query}
"""
```

### ❌ 反模式 4：答案无引用

```python
# 错误：无法验证答案真实性
return {"answer": "..."}
```

**正确**：每条答案带 `evidence`。
```python
return {
    "answer": "根据 [#1] 文档，...",
    "evidence": [{"chunk_id": "abc", "text": "...", "score": 0.92}],
}
```

### ❌ 反模式 5：不评测就上线

```python
# 错误：模型升级直接发布
new_model = "qwen-plus-v2"
llm.model = new_model  # 直接生效
```

**正确**：先评测，pass 才上线。
```python
result = await eval_runner.compare(baseline_id, current_id)
if result["verdict"] == "pass":
    llm.model = new_model
else:
    raise Exception(f"评测未通过：{result}")
```

---

## 六、落地检查清单

- [ ] **文档解析**：PDF / Markdown / DOCX / 图片 OCR / 音频 ASR
- [ ] **切片策略**：固定窗口 + 重叠，或语义分块
- [ ] **向量化**：统一模型（text-embedding-v3）
- [ ] **混合检索**：向量 + BM25 + RRF
- [ ] **重排序**：gte-rerank-v2 精排
- [ ] **CRAG 自检**：correct / ambiguous / incorrect 三分支
- [ ] **幻觉自检**：LLM 自检"我的回答是否基于证据"
- [ ] **拒答兜底**：低置信度 → 显式拒答 + 转人工
- [ ] **引用溯源**：每条答案带 `evidence` 数组 + `[#N]` 占位符
- [ ] **评测门禁**：pass_rate / cost / latency 三指标
- [ ] **版本绑定**：评测必须绑定评测集版本 + 模型版本

---

## 七、附录：核心代码位置速查（Kquery）

- `RagPipeline.run`（`app/services/pipeline.py`）：RAG 主链路
- `HybridRetriever.retrieve`（`app/services/retrieval/hybrid.py`）：混合检索
- `CragRouter.route`（`app/services/validation/crag.py`）：自反思
- `HallucinationChecker.check`（`app/services/validation/selfcheck.py`）：幻觉自检
- `EvalRunner.compare`（`app/services/security/evalrun.py`）：评测门禁
- `RetrievalConfigStore`（`app/services/config_store.py`）：运行时检索配置
