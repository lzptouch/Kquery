# LangGraph 状态机与 Agent 编排 —— 智能 Agent 工程参考

> 本文档为原理性说明文档，回答三个通用问题：
> 1. LangGraph 是什么？与简单 Agent 循环的对比？
> 2. 智能 Agent 的核心状态与决策流？
> 3. 三层熔断与中断恢复如何设计？
>
> 文末以 `Kquery` 项目（阶段 6 Agentic RAG）为完整案例。

**文档结构**：
- 第一章：Agent 范式演进；
- 第二章：LangGraph 核心概念；
- 第三章：智能 Agent 决策流；
- 第四章：三层熔断与中断恢复；
- 第五章：Kquery 项目案例；
- 第六章：反模式清单与落地检查清单。

---

## 一、Agent 范式演进

### 1.1 ReAct 范式（Reasoning + Acting）

```python
while True:
    thought = llm.call(f"What should I do next? Context: {context}")
    action = parse_action(thought)
    observation = execute(action)
    context += observation
```

**优点**：实现简单
**缺点**：无状态、难中断、难恢复

### 1.2 PEO 范式（Plan / Execute / Observe）

```python
plan = llm.plan(question)  # 显式规划
for step in plan:
    result = execute(step)
    if needs_replan:
        plan = llm.replan(question, history)
```

**优点**：显式计划、可序列化
**缺点**：LLM 规划开销

### 1.3 LangGraph 状态机

LangGraph 用 `StateGraph` 表达 Agent 决策流：
- **状态显式**（TypedDict）
- **自动 Checkpointer**（中断恢复）
- **条件边**（decision points）

---

## 二、LangGraph 核心概念

### 2.1 StateGraph

```python
from langgraph.graph import StateGraph, START, END
from typing import TypedDict

class AgentState(TypedDict):
    question: str
    plan: list[dict]
    tool_results: list[dict]
    reflections: list[dict]
    final_answer: str

workflow = StateGraph(AgentState)
```

### 2.2 节点（Nodes）

```python
async def plan_node(state: AgentState) -> dict:
    plan = await planner.plan(state["question"])
    return {"plan": plan}

async def execute_node(state: AgentState) -> dict:
    results = []
    for step in state["plan"]:
        result = await tool_registry.call(step["tool"], step["args"])
        results.append({"step": step, "result": result})
    return {"tool_results": results}

workflow.add_node("plan", plan_node)
workflow.add_node("execute", execute_node)
```

### 2.3 边（Edges）

```python
# 普通边：plan → execute
workflow.add_edge("plan", "execute")

# 条件边：根据状态决定下一步
def should_continue(state: AgentState) -> str:
    if state.get("iterations", 0) >= 10:
        return "answer"  # 超过 10 步直接回答
    return "reflect"

workflow.add_conditional_edges(
    "execute",
    should_continue,
    {"reflect": "reflect", "answer": "answer"}
)
```

### 2.4 Checkpointer（中断恢复）

```python
from langgraph.checkpoint.memory import MemorySaver

memory = MemorySaver()
graph = workflow.compile(checkpointer=memory)

# 中断
config = {"configurable": {"thread_id": "user-123"}}
result = await graph.ainvoke({"question": "..."}, config=config)

# 恢复（从中断点继续）
async for event in graph.astream(None, config=config):
    print(event)
```

---

## 三、智能 Agent 决策流（Kquery 阶段 6）

### 3.1 五状态机

```python
def build_agent_graph(registry, planner, reflector, answer_llm, ...):
    workflow = StateGraph(AgentState)
    
    workflow.add_node("plan", planner.plan)
    workflow.add_node("execute", execute_tool_calls)
    workflow.add_node("reflect", reflector.reflect)
    workflow.add_node("answer", answer_llm.answer)
    
    workflow.add_edge(START, "plan")
    workflow.add_edge("plan", "execute")
    workflow.add_conditional_edges("execute", should_continue, {"reflect": "reflect", "answer": "answer"})
    workflow.add_conditional_edges("reflect", should_replan, {"replan": "plan", "answer": "answer"})
    workflow.add_edge("answer", END)
    
    return workflow.compile()
```

### 3.2 工具注册表

```python
class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Callable] = {}
    
    def register(self, tool: Tool):
        self._tools[tool.name] = tool
    
    async def call(self, name: str, args: dict) -> dict:
        if name not in self._tools:
            raise ToolNotFound(name)
        return await self._tools[name].call(args)

# 内置工具
tool_registry = ToolRegistry()
tool_registry.register(VectorSearchTool(hybrid_retriever, config))
tool_registry.register(CalculatorTool())
tool_registry.register(GraphSearchTool(graph_query))
tool_registry.register(ImageSearchTool(image_index))
```

### 3.3 反射器（Reflector）

```python
class Reflector:
    def __init__(self, llm):
        self.llm = llm
    
    async def reflect(self, question, tool_results) -> dict:
        prompt = f"""评估以下回答质量：
问题：{question}
工具结果：{tool_results}

判断：
- "supported"：证据充分，可生成答案
- "partial"：证据部分相关，需重检索
- "unsupported"：证据无关或缺失，需拒答或重新规划
"""
        response = await self.llm.call(prompt)
        return {"verdict": parse_verdict(response)}
```

---

## 四、三层熔断与中断恢复

### 4.1 三层熔断

```python
class AgentRunner:
    async def run(self, question, session_id, ...):
        # 第一层：整体超时
        with timeout(self.total_timeout):  # 默认 30s
            iteration = 0
            while iteration < self.max_iterations:  # 默认 10 步
                # 第二层：单步超时
                async with timeout(self.step_timeout):  # 默认 10s
                    state = await self._step(question, state, ...)
                iteration += 1
                
                # 第三层：防呆循环
                if self._detect_loop(state):
                    raise LoopDetected("no progress")
                
                if state["verdict"] == "supported":
                    return state["final_answer"]
```

### 4.2 中断恢复

```python
async def method_agent_task_resume(self, task_id, workspace_root, project_id):
    task = self.store.get_task(task_id)
    
    # 检查是否有活跃执行
    active = self._agent_tasks.get(task_id)
    
    if task["state"] == "running" and (active is None or active.done()):
        # 进程重启后 → 从未完成步骤继续
        self._agent_tasks[task_id] = asyncio.create_task(
            self._resume_agent_task(task_id, ...)
        )
        return {"resumed": task_id, "recovered": True}
    
    # 正常暂停后恢复
    self._agent_executor.resume(task_id)
    return {"resumed": task_id}
```

### 4.3 中断类型

| 中断类型 | 触发 | 恢复 |
|---|---|---|
| **用户暂停** | `agent.task.pause` | `agent.task.resume` |
| **进程崩溃** | SIGTERM / OOM | `agent.task.resume` + `recovered=true` |
| **LLM 超时** | step_timeout | 自动重试或中断 |
| **达到迭代上限** | max_iterations | 强制退出 + 部分结果 |

---

## 五、Kquery 项目案例

### 5.1 数据模型（多轮会话存档）

```python
class SessionStore:
    def __init__(self, sessions_dir, ttl_s):
        self._sessions: dict[str, dict] = {}
    
    def save(self, session_id, state: AgentState):
        path = self.sessions_dir / f"{session_id}.json"
        path.write_text(json.dumps(state), encoding="utf-8")
    
    def load(self, session_id) -> dict:
        path = self.sessions_dir / f"{session_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())
```

### 5.2 Agent 主循环

```python
class AgentRunner:
    async def run(self, question, session_id=None, tools=None):
        # 1. 加载或创建会话
        state = self.session_store.load(session_id) or self._init_state(question, session_id)
        
        # 2. 执行状态机
        async for event, payload in self.graph.run(state):
            if event == "_final":
                final = payload
                break
        
        # 3. 保存会话
        self.session_store.save(session_id, final)
        
        return final
```

### 5.3 工具调用埋点

```python
# metrics
AGENT_ITERATIONS = Histogram("kquery_agent_iterations", "Agent iterations per task",
                              buckets=[1, 2, 3, 5, 7, 10])
AGENT_TOOL_CALLS = Counter("kquery_agent_tool_calls_total", "Tool calls", ["tool_name", "result"])

async def execute_tool_calls(state: AgentState) -> dict:
    results = []
    for step in state["plan"]:
        try:
            result = await tool_registry.call(step["tool"], step["args"])
            AGENT_TOOL_CALLS.labels(tool_name=step["tool"], result="success").inc()
            results.append({"step": step, "result": result})
        except Exception as e:
            AGENT_TOOL_CALLS.labels(tool_name=step["tool"], result="error").inc()
            results.append({"step": step, "error": str(e)})
    return {"tool_results": results}
```

---

## 六、反模式清单

### ❌ 反模式 1：无状态机的简单循环

```python
# 错误：闭包内状态、难中断
async def agent_loop(question):
    context = {"question": question}
    while True:
        thought = await llm.call(...)
        # 中断恢复复杂
```

**正确**：StateGraph。
```python
workflow = StateGraph(AgentState)
workflow.add_node("plan", ...)
# 自动 Checkpointer 支持中断恢复
```

### ❌ 反模式 2：无熔断

```python
# 错误：LLM 永远停不下来
while True:
    result = await llm.call(...)  # 可能永远循环
```

**正确**：三层熔断。
```python
with timeout(total_timeout):
    while iteration < max_iterations:
        async with timeout(step_timeout):
            ...
```

### ❌ 反模式 3：工具无白名单

```python
# 错误：LLM 可调任意工具（包括危险工具）
await tool_registry.call(llm_chosen_tool, ...)
```

**正确**：工具白名单 + 风险分级。
```python
ALLOWED_TOOLS = {"vector_search", "calculator", "graph_search"}
if step["tool"] not in ALLOWED_TOOLS:
    raise ToolNotAllowed(step["tool"])
```

### ❌ 反模式 4：反射器无阈值

```python
# 错误：所有反射都通过
verdict = "supported"  # 永远通过
```

**正确**：CRAG 三分支 + 阈值。
```python
def route(self, evidence_score):
    if evidence_score >= 0.7: return "correct"
    elif evidence_score <= 0.3: return "incorrect"
    else: return "ambiguous"
```

---

## 七、落地检查清单

- [ ] **状态机**：StateGraph 显式表达决策流
- [ ] **Checkpointer**：MemorySaver / SqliteSaver 支持中断恢复
- [ ] **工具注册表**：ToolRegistry + 风险分级
- [ ] **三层熔断**：整体超时 / 迭代上限 / 单步超时
- [ ] **中断恢复**：`resume_task` 从未完成步骤继续
- [ ] **多轮会话存档**：SessionStore 持久化状态
- [ ] **CRAG 自检**：correct / ambiguous / incorrect 三分支
- [ ] **埋点**：迭代次数 / 工具调用 / 反射次数

---

## 八、附录：核心代码位置速查（Kquery）

- `build_agent_graph`（`app/services/agent/graph.py`）：LangGraph 状态机构建
- `AgentRunner.run`（`app/services/agent/runner.py`）：Agent 主循环
- `ToolRegistry`（`app/services/agent/tools.py`）：工具注册表
- `VectorSearchTool`（`app/services/agent/tools.py`）：向量检索工具
- `Reflector`（`app/services/agent/reflector.py`）：反射器
- `SessionStore`（`app/services/agent/sessions.py`）：多轮会话存档
