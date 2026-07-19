# KQuery Pro — 企业级智能销售助手平台 UML 图表文档

> **最后更新**: 2026-07-19  
> **项目版本**: v3.0.0  
> **架构体系**: 七层分层架构

本文档包含 KQuery Pro 企业级智能销售助手平台的完整 UML 图，使用 Mermaid 语法编写，全面反映七层架构设计。

---

## 1. 系统架构图

展示 KQuery Pro 的七层架构：多端交互层、API 网关层、会话管理层、业务服务层、核心决策层、连接集成层、数据持久层。

```mermaid
graph TB
    subgraph Layer1["① 多端交互层"]
        WeChat["微信小程序<br/>销售助手"]
        WebApp["Web 管理后台<br/>(Vue/React)"]
        MobileApp["移动 APP<br/>销售端"]
        VoiceSDK["语音转文字 SDK<br/>(ASR 集成)"]
    end

    subgraph Layer2["② API 网关层 (FastAPI)"]
        Auth_API["/api/v1/auth/*<br/>JWT 鉴权"]
        RateLimit_API["限流中间件<br/>Redis 滑动窗口"]
        Session_API["/api/v1/agent/session/*<br/>会话路由"]
        RAG_API["/api/v1/kquery/*<br/>知识检索"]
        Agent_API["/api/v1/agent/*<br/>Agent 调度"]
        Graph_API["/api/v1/graph/*<br/>知识图谱"]
        GraphRAG_API["/api/v1/graphrag/*<br/>GraphRAG 检索"]
        Ext_API["/api/v1/notes|mindmap|web-search|speech|memory|tasks|skills|knowledge-bases/*<br/>拓展能力"]
        Health["/health<br/>健康检查"]
    end

    subgraph Layer3["③ 会话管理层"]
        RedisCache["Redis 分布式缓存<br/>会话缓存 + 结果缓存"]
        StateMachine["对话状态机<br/>状态流转管理"]
        SessionStore["会话存储<br/>上下文持久化"]
        LockManager["分布式锁<br/>并发控制"]
    end

    subgraph Layer4["④ 业务服务层"]
        subgraph IntentServices["意图识别服务"]
            IntentModel["意图识别模型<br/>(NLU/分类)"]
            SlotFilling["槽位填充<br/>参数提取"]
            MultiTurn["多轮对话管理"]
        end

        subgraph RAGEngine["RAG 检索引擎"]
            Retriever["retriever.py<br/>(向量+HyDE+RRF+重排)"]
            Embedding["embedding.py<br/>文本向量化"]
            Chunking["chunking.py<br/>文档分块"]
            BM25["BM25 关键词检索"]
            Reranker["reranker.py<br/>重排序"]
        end

        subgraph ScriptLib["销售话术模板库"]
            ScriptTemplate["话术模板管理"]
            SceneMapping["场景-话术映射"]
            DynamicScript["动态话术生成"]
        end

        subgraph KnowledgeGraph["知识图谱服务"]
            GraphBuilder["graph_builder.py<br/>图谱构建"]
            EntityExtraction["entity_extraction.py<br/>实体抽取"]
            RelationExtraction["relation_extraction.py<br/>关系抽取"]
            GraphQuery["graph_query.py<br/>图谱查询"]
            CommunityEngine["community_engine.py<br/>社区发现(Leiden)"]
        end
    end

    subgraph Layer5["⑤ 核心决策层"]
        AgentScheduler["Agent 流程调度算法<br/>多 Agent 编排"]
        BizRuleEngine["业务规则引擎<br/>销售策略规则"]
        WorkflowEngine["工作流引擎<br/>销售流程编排"]
        DecisionEvaluator["决策评估器<br/>效果反馈优化"]
    end

    subgraph Layer6["⑥ 连接集成层"]
        CRM_Connector["CRM 系统 API<br/>客户数据同步"]
        OrderSDK["订单系统对接 SDK<br/>下单/查询"]
        ExternalAIService["外部 AI 服务<br/>通义千问/文心"]
        WebhookAdapter["Webhook 适配器<br/>事件通知"]
    end

    subgraph Layer7["⑦ 数据持久层"]
        SQLite["SQLite 业务数据库<br/>用户/订单/配置"]
        Milvus["Milvus 向量知识库<br/>文本向量"]
        Neo4j["Neo4j 图数据库<br/>知识图谱"]
        FileStorage["文件存储<br/>(文档/图片/音频)"]
    end

    Layer1 --> Layer2
    Layer2 --> Layer3
    Layer3 --> Layer4
    Layer4 --> Layer5
    Layer5 --> Layer6
    Layer6 --> Layer7
```

---

## 2. 用例图

展示用户与 KQuery Pro 企业级智能销售助手平台的核心交互场景。

```mermaid
graph LR
    actor SalesPerson["销售员"]
    actor Manager["销售经理"]
    actor Admin["系统管理员"]

    subgraph Auth["认证与授权"]
        UC1["登录/注册"]
        UC2["JWT 身份验证"]
        UC3["角色权限校验"]
    end

    subgraph Session["会话管理"]
        UC4["发起对话"]
        UC5["多轮连续对话"]
        UC6["查看对话历史"]
        UC7["切换客户场景"]
    end

    subgraph Intent["意图识别"]
        UC8["客户意图分析"]
        UC9["销售阶段判断"]
        UC10["参数自动提取"]
    end

    subgraph RAG["知识检索"]
        UC11["销售话术查询"]
        UC12["产品知识检索"]
        UC13["竞品对比查询"]
        UC14["行业资讯检索"]
    end

    subgraph Agent["智能决策"]
        UC15["销售策略推荐"]
        UC16["下一步行动建议"]
        UC17["异议处理方案"]
        UC18["跟单提醒"]
    end

    subgraph CRM["客户管理"]
        UC19["客户信息查询"]
        UC20["订单状态查询"]
        UC21["客户画像分析"]
        UC22["销售记录同步"]
    end

    subgraph AdminPanel["系统管理"]
        UC23["话术模板管理"]
        UC24["知识库维护"]
        UC25["规则配置"]
        UC26["数据统计报表"]
    end

    SalesPerson --> UC1 & UC2 & UC3
    SalesPerson --> UC4 & UC5 & UC6 & UC7
    SalesPerson --> UC8 & UC9 & UC10
    SalesPerson --> UC11 & UC12 & UC13 & UC14
    SalesPerson --> UC15 & UC16 & UC17 & UC18
    SalesPerson --> UC19 & UC20 & UC21 & UC22

    Manager --> UC15 & UC16 & UC17
    Manager --> UC23 & UC24 & UC25 & UC26

    Admin --> UC23 & UC24 & UC25 & UC26
    Admin --> UC1 & UC2 & UC3
```

---

## 3. 流程图

展示销售助手从接收用户语音到生成销售建议的完整处理流程。

```mermaid
flowchart TD
    Start([销售员发起对话]) --> InputType{输入方式}

    InputType -->|语音输入| ASR["语音转文字<br/>ASR SDK"]
    InputType -->|文字输入| TextInput["直接文字输入"]
    InputType -->|图片/文件| MultiModal["多模态输入解析"]

    ASR --> NLU
    TextInput --> NLU
    MultiModal --> NLU

    NLU["意图识别 & 实体提取<br/>NLU 模型"] --> ClassifyIntent{识别意图类型}

    ClassifyIntent -->|客户咨询| RAGSearch["RAG 知识检索<br/>向量 + 图谱"]
    ClassifyIntent -->|销售跟进| AgentDecision["Agent 决策调度<br/>销售策略推荐"]
    ClassifyIntent -->|下订单| OrderProcess["对接订单系统<br/>下单流程"]
    ClassifyIntent -->|查询客户| CRMQuery["CRM 客户查询<br/>客户画像"]

    RAGSearch --> VectorSearch["向量检索<br/>Milvus"]
    RAGSearch --> GraphSearch["图谱检索<br/>Neo4j"]
    RAGSearch --> Bm25Search["BM25 检索"]

    VectorSearch & GraphSearch & Bm25Search --> RRF["RRF 融合排序"]
    RRF --> RerankStep["重排序<br/>Reranker"]
    RerankStep --> ScriptMatch{"匹配话术模板?"}

    ScriptMatch -->|是| ApplyScript["应用销售话术模板"]
    ScriptMatch -->|否| DynamicGen["动态生成回答"]

    AgentDecision --> WorkflowDispatch["工作流引擎调度"]
    WorkflowDispatch --> BizRules["业务规则评估"]
    BizRules --> StrategyOutput["生成销售策略"]

    OrderProcess --> OrderSDKCall["调用订单 SDK"]
    OrderSDKCall --> OrderResult["返回订单结果"]

    CRMQuery --> CRMAPICall["调用 CRM API"]
    CRMAPICall --> CustomerProfile["返回客户画像"]

    ApplyScript & DynamicGen & StrategyOutput & OrderResult & CustomerProfile --> FormatResponse["格式化响应"]
    FormatResponse --> CacheResult["缓存结果<br/>Redis"]
    CacheResult --> ReturnResponse(["返回销售助手响应"])
```

---

## 4. 活动图

展示 Agent 智能决策与多引擎协作的完整活动流程。

```mermaid
flowchart TD
    Start([销售员发起请求]) --> SessionCheck["会话状态检查"]

    SessionCheck --> LoadContext["加载对话上下文<br/>Redis 会话缓存"]
    LoadContext --> IntentAnalyze["意图分析<br/>NLU 意图识别"]

    IntentAnalyze --> IntentResult{意图类型判断}

    IntentResult -->|知识问答| KnowledgeFlow["知识检索流程"]
    IntentResult -->|销售建议| AgentFlow["Agent 决策流程"]
    IntentResult -->|系统操作| SystemFlow["系统对接流程"]

    subgraph KnowledgeFlow["知识检索流程"]
        K1["向量检索<br/>Milvus"]
        K2["图谱检索<br/>Neo4j"]
        K3["BM25 检索"]
        K1 & K2 & K3 --> K4["RRF 融合"]
        K4 --> K5["Reranker 重排"]
        K5 --> K6{"话术匹配?"}
        K6 -->|匹配| K7["应用话术模板"]
        K6 -->|不匹配| K8["动态生成"]
    end

    subgraph AgentFlow["Agent 决策流程"]
        A1["Agent 调度器"]
        A1 --> A2{"决策类型"}
        A2 -->|销售策略| A3["规则引擎评估"]
        A2 -->|跟单提醒| A4["工作流引擎调度"]
        A2 -->|异议处理| A5["知识图谱推理"]
        A3 & A4 & A5 --> A6["策略组合"]
        A6 --> A7["决策效果预估"]
    end

    subgraph SystemFlow["系统对接流程"]
        S1{"对接系统"}
        S1 -->|CRM| S2["CRM API 调用<br/>客户信息同步"]
        S1 -->|订单| S3["订单 SDK 调用<br/>订单操作"]
        S1 -->|外部 AI| S4["外部大模型调用<br/>增强推理"]
    end

    KnowledgeFlow --> MergeOutput
    AgentFlow --> MergeOutput
    SystemFlow --> MergeOutput

    MergeOutput["结果整合"] --> Evaluate{"是否需要<br/>人工确认?"}

    Evaluate -->|需要| Notify["通知销售经理<br/>待审批"]
    Evaluate -->|不需要| DirectOutput

    Notify --> WaitApproval["等待审批"]
    WaitApproval -->|批准| DirectOutput
    WaitApproval -->|驳回| Revise["重新生成"]

    DirectOutput["输出最终结果"] --> UpdateSession["更新会话状态"]
    Revise --> AgentFlow

    UpdateSession --> CacheUpdate["缓存更新<br/>Redis"]
    CacheUpdate --> End([结束])
```

---

## 5. 时序图

展示销售员从发起语音查询到获取销售建议的完整端到端交互流程。

```mermaid
sequenceDiagram
    actor Sales as 销售员
    participant Client as 多端交互层<br/>(微信/APP/Web)
    participant Gateway as API 网关层<br/>(FastAPI)
    participant Session as 会话管理层<br/>(Redis)
    participant Intent as 意图识别服务
    participant RAG as RAG 检索引擎
    participant KG as 知识图谱<br/>(Neo4j)
    participant Vector as 向量知识库<br/>(Milvus)
    participant Agent as 核心决策层<br/>(Agent 调度)
    participant BizRule as 业务规则引擎
    participant CRM as 连接集成层<br/>(CRM/订单)
    participant LLM as 外部 AI 服务
    participant DB as 数据持久层<br/>(SQLite)

    Sales->>Client: 发起语音/文字查询
    Client->>Client: 语音转文字(若语音输入)
    Client->>Gateway: POST /api/v1/agent/chat

    Gateway->>Gateway: JWT 鉴权验证
    Gateway->>Gateway: 接口限流检查

    Gateway->>Session: 获取/创建会话
    Session->>Session: 会话状态机流转
    Session-->>Gateway: 返回会话上下文

    Gateway->>Intent: 意图识别请求

    Intent->>Intent: NLU 模型推理
    Intent->>Intent: 槽位填充提取
    Intent-->>Gateway: 返回意图分类 & 实体

    alt 知识问答场景
        Gateway->>RAG: 发起知识检索

        par 并行检索
            RAG->>Vector: 向量相似度检索
            Vector-->>RAG: Top-K 文档块
        and
            RAG->>KG: 图谱子图查询
            KG-->>RAG: 实体与关系
        and
            RAG->>RAG: BM25 关键词检索
        end

        RAG->>RAG: RRF 融合排序
        RAG->>RAG: Reranker 重排序

        RAG->>LLM: 检索结果 + 查询
        LLM-->>RAG: 生成回答

        alt 话术模板匹配
            RAG->>RAG: 匹配销售话术模板
        end

        RAG-->>Gateway: 返回检索结果

    else 销售策略场景
        Gateway->>Agent: 触发 Agent 决策

        Agent->>Session: 获取完整对话历史
        Agent->>BizRule: 业务规则评估

        BizRule->>DB: 查询销售策略配置
        DB-->>BizRule: 返回策略规则

        BizRule-->>Agent: 规则评估结果

        Agent->>LLM: 策略推理请求
        LLM-->>Agent: 策略推荐

        Agent-->>Gateway: 返回销售策略

    else CRM/订单操作
        Gateway->>CRM: 调用 CRM/订单接口
        CRM->>DB: 数据查询/写入
        DB-->>CRM: 操作结果
        CRM-->>Gateway: 返回结果
    end

    Gateway->>Session: 更新会话状态
    Session->>Session: 持久化对话上下文

    Gateway-->>Client: 返回结果 JSON

    Client->>Client: 渲染销售建议/回答
    Client-->>Sales: 显示结果
```

---

## 6. 类图

展示 KQuery Pro 核心服务类之间的七层架构关系。

```mermaid
classDiagram
    %% 多端交互层
    class VoiceHandler {
        -asr_sdk: ASRClient
        -language: str
        +speech_to_text(audio: bytes): str
        +text_to_speech(text: str): bytes
    }

    class WebClient {
        -session_id: str
        -user_id: str
        +send_message(msg: str): Response
        +upload_file(file: File): str
    }

    %% API 网关层
    class APIGateway {
        -auth_handler: JWTAuth
        -rate_limiter: RateLimiter
        -router: Router
        +authenticate(token: str): User
        +rate_limit(key: str): bool
        +route_request(req: Request): Response
    }

    class JWTAuth {
        -secret_key: str
        -algorithm: str
        +generate_token(user: User): str
        +verify_token(token: str): Claims
        +refresh_token(token: str): str
    }

    class RateLimiter {
        -strategy: TokenBucket
        -limits: dict
        +check_limit(key: str): bool
        +get_remaining(key: str): int
    }

    %% 会话管理层
    class SessionManager {
        -redis_client: RedisClient
        -state_machine: DialogStateMachine
        +create_session(user_id: str): Session
        +get_context(session_id: str): DialogContext
        +update_state(session_id: str, state: State): bool
    }

    class DialogStateMachine {
        -states: list~State~
        -transitions: dict
        +transition(current: State, event: Event): State
        +validate_transition(from_state: State, to_state: State): bool
    }

    class DistributedCache {
        -redis: RedisCluster
        -l1_cache: LRUCache
        +get(key: str): Any
        +set(key: str, value: Any, ttl: int)
        +invalidate(pattern: str)
    }

    %% 业务服务层
    class IntentService {
        -nlu_model: NLUModel
        -slot_filler: SlotFilling
        +classify_intent(text: str): IntentResult
        +extract_entities(text: str): List~Entity~
        +fill_slots(text: str, intent: Intent): dict
    }

    class RAGEngine {
        -retriever: HybridRetriever
        -embedding: EmbeddingService
        -reranker: Reranker
        +retrieve(query: str, top_k: int): List~Chunk~
        +hybrid_search(query: str): SearchResult
        +rerank(query: str, chunks: List~Chunk~): List~Chunk~
    }

    class HybridRetriever {
        -vector_retriever: VectorRetriever
        -graph_retriever: GraphRetriever
        -bm25_retriever: BM25Retriever
        +semantic_search(query: str): List~Chunk~
        +graph_search(query: str): List~Entity~
        +keyword_search(query: str): List~Chunk~
        +fusion_search(results: List): RRFResult
    }

    class ScriptTemplateManager {
        -templates: dict
        -scene_mapping: dict
        +match_template(intent: Intent, scene: Scene): Script
        +generate_dynamic(context: DialogContext): Script
        +evaluate_effectiveness(script_id: str): float
    }

    class KnowledgeGraphService {
        -graph_builder: GraphBuilder
        -entity_extractor: EntityExtraction
        -relation_extractor: RelationExtraction
        +build_graph(docs: List~Document~): Graph
        +query_subgraph(entity: str, depth: int): SubGraph
        +find_communities(): List~Community~
    }

    class EntityExtraction {
        -llm_client: LLMClient
        -rules: List~Rule~
        +extract_entities(text: str): List~Entity~
        +batch_extract(texts: List~str~): List~Entity~
    }

    %% 核心决策层
    class AgentScheduler {
        -workflow_engine: WorkflowEngine
        -rule_engine: BizRuleEngine
        -decision_evaluator: DecisionEvaluator
        +schedule(intent: Intent, context: Context): Action
        +orchestrate_agents(agents: List~Agent~): Result
        +evaluate_decision(action: Action): DecisionScore
    }

    class BizRuleEngine {
        -rules: List~BizRule~
        -rule_compiler: RuleCompiler
        +evaluate(context: dict): RuleResult
        +execute_rule(rule_id: str, params: dict): Any
    }

    class WorkflowEngine {
        -workflows: dict
        -executor: WorkflowExecutor
        +start_workflow(workflow_id: str, params: dict): Execution
        +get_status(execution_id: str): Status
        +cancel_workflow(execution_id: str): bool
    }

    class DecisionEvaluator {
        -metrics: dict
        +evaluate_outcome(action: Action, result: Result): Score
        +optimize_strategy(strategy: Strategy): Strategy
    }

    %% 连接集成层
    class CRMConnector {
        -api_client: HTTPClient
        -sync_strategy: SyncStrategy
        +query_customer(customer_id: str): Customer
        +sync_sales_record(record: SalesRecord): bool
        +update_customer_stage(customer_id: str, stage: str): bool
    }

    class OrderSDK {
        -api_endpoint: str
        -auth_credentials: dict
        +create_order(order: Order): OrderResult
        +query_order(order_id: str): Order
        +cancel_order(order_id: str): bool
    }

    class ExternalAIService {
        -providers: dict
        -load_balancer: LoadBalancer
        +chat_completion(messages: List~Message~): Response
        +embeddings(text: str): Vector
        +function_call(name: str, args: dict): Any
    }

    %% 数据持久层
    class SQLiteRepository {
        -session: Session
        +query(model: Model, filters: dict): List~Record~
        +save(entity: Entity): Entity
        +delete(model: Model, id: str): bool
    }

    class MilvusConnector {
        -collection: Collection
        +insert(vectors: List~Vector~, metadata: List~dict~)
        +search(query: Vector, top_k: int): List~SearchResult~
        +delete(ids: List~str~)
    }

    class Neo4jConnector {
        -driver: Driver
        +create_node(label: str, properties: dict): Node
        +create_relationship(type: str, from_node: Node, to_node: Node): Relationship
        +query(cypher: str): List~Record~
        +find_neighbors(node_id: str, depth: int): List~Node~
    }

    %% 数据模型
    class Session {
        +id: str
        +user_id: str
        +state: str
        +context: dict
        +created_at: datetime
        +expires_at: datetime
    }

    class DialogContext {
        +session_id: str
        +history: List~Message~
        +current_intent: Intent
        +extracted_entities: dict
        +scene: str
    }

    class IntentResult {
        +intent_type: str
        +confidence: float
        +entities: List~Entity~
        +slots: dict
    }

    class SalesScript {
        +id: str
        +scene: str
        +intent_type: str
        +template: str
        +variables: List~str~
        +effectiveness: float
    }

    %% 关系定义
    APIGateway --> JWTAuth
    APIGateway --> RateLimiter
    APIGateway --> SessionManager
    APIGateway --> IntentService
    APIGateway --> RAGEngine
    APIGateway --> AgentScheduler
    APIGateway --> CRMConnector

    SessionManager --> DialogStateMachine
    SessionManager --> DistributedCache

    IntentService --> DistributedCache

    RAGEngine --> HybridRetriever
    RAGEngine --> ScriptTemplateManager

    HybridRetriever --> MilvusConnector
    HybridRetriever --> Neo4jConnector
    KnowledgeGraphService --> Neo4jConnector
    KnowledgeGraphService --> EntityExtraction

    AgentScheduler --> WorkflowEngine
    AgentScheduler --> BizRuleEngine
    AgentScheduler --> DecisionEvaluator
    AgentScheduler --> RAGEngine
    AgentScheduler --> ExternalAIService

    CRMConnector --> SQLiteRepository
    OrderSDK --> SQLiteRepository

    VoiceHandler --> APIGateway
    WebClient --> APIGateway

    DialogStateMachine --> Session
    ScriptTemplateManager --> SalesScript
```

---

## 7. 组件图

展示系统主要组件及其跨层接口关系。

```mermaid
graph TB
    subgraph Layer1["① 多端交互层"]
        WeChatClient["微信小程序<br/>销售助手客户端"]
        WebAdmin["Web 管理后台<br/>Vue/React"]
        MobileAPP["移动 APP<br/>Flutter/RN"]
        ASRModule["语音转文字模块<br/>ASR SDK"]
    end

    subgraph Layer2["② API 网关层"]
        FastAPI["FastAPI 网关<br/>路由 + 中间件"]
        JWTAuthModule["JWT 鉴权模块"]
        RateLimitModule["限流模块<br/>令牌桶"]
        RequestRouter["请求路由分发"]
    end

    subgraph Layer3["③ 会话管理层"]
        SessionModule["会话管理模块"]
        StateMachineModule["对话状态机"]
        DistributedCacheModule["分布式缓存<br/>Redis"]
    end

    subgraph Layer4["④ 业务服务层"]
        subgraph IntentModule["意图识别模块"]
            NLUModel["NLU 模型服务"]
            SlotFillingModule["槽位填充"]
        end

        subgraph RAGModule["RAG 检索模块"]
            VectorRetriever["向量检索"]
            GraphRetriever["图谱检索"]
            KeywordRetriever["BM25 检索"]
            RerankerModule["重排序"]
        end

        subgraph ScriptModule["话术模板模块"]
            TemplateEngine["模板引擎"]
            SceneMatcher["场景匹配器"]
        end

        subgraph KnowledgeGraphModule["知识图谱模块"]
            GraphBuilder["图谱构建器"]
            EntityExtractor["实体抽取"]
            RelationExtractor["关系抽取"]
            CommunityDetector["社区发现"]
        end
    end

    subgraph Layer5["⑤ 核心决策层"]
        AgentSchedulerModule["Agent 调度器"]
        BizRuleEngineModule["业务规则引擎"]
        WorkflowEngineModule["工作流引擎"]
        DecisionEvalModule["决策评估器"]
    end

    subgraph Layer6["⑥ 连接集成层"]
        CRMAdapter["CRM 适配器"]
        OrderAdapter["订单系统适配器"]
        ExternalAIAdapter["外部 AI 适配器"]
        WebhookModule["Webhook 事件通知"]
    end

    subgraph Layer7["⑦ 数据持久层"]
        SQLiteDB["SQLite<br/>业务数据库"]
        MilvusDB["Milvus<br/>向量知识库"]
        Neo4jDB["Neo4j<br/>图数据库"]
        FileStore["文件存储"]
    end

    %% 跨层连接
    Layer1 --> Layer2
    Layer2 --> Layer3
    Layer3 --> Layer4
    Layer4 --> Layer5
    Layer5 --> Layer6
    Layer6 --> Layer7

    %% 层内连接
    FastAPI --> JWTAuthModule
    FastAPI --> RateLimitModule
    FastAPI --> RequestRouter

    SessionModule --> StateMachineModule
    SessionModule --> DistributedCacheModule

    RequestRouter --> SessionModule
    RequestRouter --> IntentModule
    RequestRouter --> RAGModule
    RequestRouter --> AgentSchedulerModule

    IntentModule --> NLUModel
    IntentModule --> SlotFillingModule

    RAGModule --> VectorRetriever
    RAGModule --> GraphRetriever
    RAGModule --> KeywordRetriever
    RAGModule --> RerankerModule

    ScriptModule --> TemplateEngine
    ScriptModule --> SceneMatcher

    RAGModule --> ScriptModule

    AgentSchedulerModule --> BizRuleEngineModule
    AgentSchedulerModule --> WorkflowEngineModule
    AgentSchedulerModule --> DecisionEvalModule
    AgentSchedulerModule --> RAGModule
    AgentSchedulerModule --> IntentModule
    AgentSchedulerModule --> ScriptModule

    CRMAdapter --> SQLiteDB
    OrderAdapter --> SQLiteDB

    VectorRetriever --> MilvusDB
    KnowledgeGraphModule --> Neo4jDB
    GraphRetriever --> Neo4jDB

    ExternalAIAdapter --> ExternalAIService["外部大模型 API<br/>通义千问/文心"]
```

---

## 8. 部署架构图

展示 KQuery Pro 企业级智能销售助手平台的容器化部署架构。

```mermaid
graph TB
    subgraph Internet["外部网络"]
        WeChatUser["微信用户"]
        WebUser["Web 用户"]
        AppUser["APP 用户"]
    end

    subgraph CDN["CDN 加速层"]
        StaticCDN["静态资源 CDN"]
    end

    subgraph Docker["Docker 容器编排 (Kubernetes)"]
        subgraph Frontend["多端交互服务"]
            WeChatService["kquery-wechat<br/>微信服务 :3001"]
            WebService["kquery-web<br/>Web 服务 :3000"]
            APPService["kquery-app<br/>APP 接口 :3002"]
            VoiceService["kquery-voice<br/>语音服务 :3003"]
        end

        subgraph Gateway["API 网关服务"]
            APIGateway["kquery-gateway<br/>FastAPI :8000"]
            NginxReverse["Nginx 反向代理<br/>:80/:443"]
        end

        subgraph SessionService["会话管理服务"]
            SessionCluster["kquery-session<br/>会话服务 :8100"]
            RedisCluster["Redis 集群<br/>:6379"]
        end

        subgraph BusinessService["业务服务集群"]
            IntentService["kquery-intent<br/>意图识别 :8200"]
            RAGService["kquery-rag<br/>RAG 检索 :8300"]
            ScriptService["kquery-script<br/>话术模板 :8400"]
            KGService["kquery-kg<br/>知识图谱 :8500"]
        end

        subgraph DecisionService["核心决策服务"]
            AgentService["kquery-agent<br/>Agent 调度 :8600"]
            RuleEngine["kquery-rules<br/>规则引擎 :8700"]
            WorkflowSrv["kquery-workflow<br/>工作流引擎 :8800"]
        end

        subgraph IntegrationService["连接集成服务"]
            CRMService["kquery-crm<br/>CRM 对接 :8900"]
            OrderService["kquery-order<br/>订单对接 :9000"]
            AIProxy["kquery-ai-proxy<br/>AI 代理 :9100"]
        end

        subgraph DataServices["数据服务"]
            SQLiteDB["SQLite 业务数据库<br/>本地文件"]
            MilvusCluster["Milvus 集群<br/>:19530"]
            Neo4jCluster["Neo4j 集群<br/>:7474/7687"]
            MinIO["MinIO 对象存储<br/>:9000"]
            MilvusETCD["etcd<br/>:2379"]
        end

        subgraph Monitor["监控与运维"]
            Prometheus["Prometheus<br/>:9090"]
            Grafana["Grafana<br/>:3000"]
            ELK["ELK 日志<br/>Filebeat+ES+Kibana"]
            AlertManager["AlertManager<br/>告警通知"]
        end
    end

    subgraph External["外部集成"]
        WeChatMP["微信开放平台<br/>小程序 API"]
        AliyunDashScope["阿里云 DashScope<br/>通义千问"]
        BaiduWenXin["百度文心一言<br/>ERNIE"]
        CRM_SaaS["第三方 CRM 系统"]
        OrderSystem["企业内部订单系统"]
    end

    WeChatUser --> WeChatMP
    WeChatMP --> NginxReverse
    WebUser --> StaticCDN
    StaticCDN --> NginxReverse
    AppUser --> NginxReverse

    NginxReverse --> APIGateway
    APIGateway --> Frontend
    APIGateway --> SessionCluster
    APIGateway --> BusinessService
    APIGateway --> DecisionService
    APIGateway --> IntegrationService

    SessionCluster --> RedisCluster
    BusinessService --> SessionCluster
    DecisionService --> BusinessService
    DecisionService --> SessionCluster

    BusinessService --> DataServices
    DecisionService --> IntegrationService
    IntegrationService --> DataServices

    IntegrationService --> CRM_SaaS
    IntegrationService --> OrderSystem
    AIProxy --> AliyunDashScope
    AIProxy --> BaiduWenXin

    Prometheus --> APIGateway
    Prometheus --> BusinessService
    Prometheus --> DecisionService
    Prometheus --> DataServices
    Grafana --> Prometheus
    ELK --> BusinessService
    ELK --> APIGateway
    AlertManager --> Prometheus
```

---

## 9. 缓存架构图

展示 Redis 分布式缓存 + 多级缓存架构。

```mermaid
flowchart LR
    subgraph Request["用户请求入口"]
        Query["销售助手请求"]
    end

    subgraph L1_Cache["L1 应用本地缓存<br/>(进程内 LRU)"]
        LocalCache["本地内存缓存<br/>TTL: 30s-5min"]
        HotData["热点数据缓存<br/>常用话术/高频实体"]
    end

    subgraph L2_Cache["L2 分布式缓存<br/>(Redis 集群)"]
        subgraph CacheTypes["缓存类型分区"]
            SessionCache["会话缓存<br/>TTL: 30min<br/>key: session:{id}"]
            ResultCache["结果缓存<br/>TTL: 1h<br/>key: result:{hash}"]
            ScriptCache["话术模板缓存<br/>TTL: 24h<br/>key: script:{scene}"]
            EntityCache["实体缓存<br/>TTL: 12h<br/>key: entity:{name}"]
            RateLimitCache["限流计数<br/>TTL: 1min<br/>key: ratelimit:{ip}"]
            LockCache["分布式锁<br/>TTL: 10s<br/>key: lock:{resource}"]
        end
    end

    subgraph CacheStrategy["缓存策略"]
        Strategy1["Cache-Aside<br/>旁路缓存"]
        Strategy2["Write-Through<br/>写入穿透"]
        Strategy3["Cache Invalidation<br/>事件驱动失效"]
    end

    subgraph Backend["后端数据源"]
        SQLiteDB["SQLite<br/>业务数据库"]
        MilvusDB["Milvus<br/>向量知识库"]
        Neo4jDB["Neo4j<br/>图数据库"]
        LLMAPI["外部 LLM API<br/>大模型调用"]
    end

    Query --> CheckL1["查询 L1 本地缓存"]

    CheckL1 -->|命中| ReturnL1["返回本地缓存<br/>（微秒级）"]
    CheckL1 -->|未命中| CheckL2["查询 L2 Redis 缓存"]

    CheckL2 -->|命中| PopulateL1["回填 L1 缓存"]
    PopulateL1 --> ReturnL2["返回 Redis 缓存<br/>（毫秒级）"]

    CheckL2 -->|未命中| SelectSource{选择数据源}

    SelectSource -->|会话数据| QuerySession["查询 Redis<br/>会话存储"]
    SelectSource -->|业务数据| QuerySQLite["查询 SQLite"]
    SelectSource -->|向量检索| QueryMilvus["查询 Milvus"]
    SelectSource -->|图谱查询| QueryNeo4j["查询 Neo4j"]
    SelectSource -->|AI 生成| QueryLLM["调用 LLM API"]

    QuerySession --> SetCache["写入 L2 缓存"]
    QuerySQLite --> SetCache
    QueryMilvus --> SetCache
    QueryNeo4j --> SetCache
    QueryLLM --> SetCache

    SetCache --> SetL1["写入 L1 缓存"]
    SetL1 --> ReturnBackend["返回结果"]

    ReturnL1 & ReturnL2 & ReturnBackend --> Response(["响应销售员"])

    subgraph Invalidation["缓存失效机制"]
        Event1["数据更新事件"]
        Event2["手动刷新"]
        Event3["TTL 过期"]
        Event1 --> Invalidate["失效对应缓存键"]
        Event2 --> Invalidate
        Event3 --> Invalidate
        Invalidate --> ClearL1["广播清除 L1 缓存"]
        Invalidate --> ClearL2["删除 L2 缓存键"]
    end
```

---

## 10. 拓展能力图（对标腾讯 ima）

展示 P0（多模型切换、AI 写作笔记、思维导图、联网搜索）、P1（语音、长期记忆、任务式 Agent）、P2（共享知识库、Skills 生态）在七层架构中的组件与时序关系，与《接口设计文档》第 12 章、《系统架构文档》第 18 章对应。

### 10.1 拓展能力组件图

```mermaid
graph TB
    subgraph Layer1["① 多端交互层"]
        WebUI["Web/小程序<br/>对话 + 笔记 + 导图"]
        VoiceClient["语音输入/播报"]
    end

    subgraph Layer2["② API 网关层"]
        ExtAPI["/api/v1/models · /notes · /mindmap<br/>/web-search · /voice · /memory<br/>/tasks · /knowledge-bases · /skills"]
        SSE["SSE 流式通道"]
    end

    subgraph Layer4["④ 业务服务层"]
        ModelSvc["多模型调度服务<br/>Provider Registry"]
        NoteSvc["AI 写作笔记服务"]
        MindmapSvc["思维导图服务"]
        WebSearchSvc["联网搜索服务<br/>三源 RRF"]
        VoiceSvc["语音服务<br/>ASR/TTS"]
        MemorySvc["长期记忆服务<br/>memory_records"]
        TaskSvc["任务式 Agent 服务<br/>DAG 调度"]
        KBShareSvc["共享知识库服务"]
        SkillSvc["Skills 匹配服务"]
    end

    subgraph Layer6["⑥ 连接集成层"]
        LLMProviders["LLM Providers<br/>通义/OpenAI/本地"]
        VoiceProviders["语音 Providers<br/>Whisper/讯飞"]
        SearchProviders["搜索 Providers<br/>Serper/Tavily"]
    end

    subgraph Layer7["⑦ 数据持久层"]
        Milvus["Milvus<br/>向量 + memory_records"]
        Neo4j["Neo4j<br/>知识图谱"]
        SQLite["SQLite<br/>笔记/任务/KB 元数据"]
    end

    Layer1 --> Layer2
    ExtAPI --> ModelSvc & NoteSvc & MindmapSvc & WebSearchSvc & VoiceSvc & MemorySvc & TaskSvc & KBShareSvc & SkillSvc
    SSE --> NoteSvc & MindmapSvc & WebSearchSvc & VoiceSvc & TaskSvc
    ModelSvc --> LLMProviders
    VoiceSvc --> VoiceProviders
    WebSearchSvc --> SearchProviders
    MemorySvc --> Milvus
    MindmapSvc --> Neo4j
    KBShareSvc --> Neo4j & Milvus
    NoteSvc --> SQLite
    TaskSvc --> SQLite
    SkillSvc --> SQLite
```

### 10.2 联网搜索增强时序图

```mermaid
sequenceDiagram
    actor User as 销售员
    participant Client as 多端交互层
    participant GW as API 网关层
    participant Web as 联网搜索服务
    participant Vec as Milvus(向量)
    participant KG as Neo4j(图谱)
    participant Search as 搜索 Provider
    participant LLM as LLM

    User->>Client: 提问（开启联网）
    Client->>GW: POST /api/v1/web-search/query
    GW->>Web: 检索请求
    par 三源并行
        Web->>Vec: 向量 + HyDE 检索
        Vec-->>Web: vec_results
    and
        Web->>KG: 子图检索
        KG-->>Web: graph_results
    and
        Web->>Search: 联网检索
        Search-->>Web: web_results
    end
    Web->>Web: RRF 融合 (k=60) + 重排
    Web->>LLM: 融合结果 + 查询 (带 citation)
    LLM-->>Web: 带引用回答
    Web-->>GW: SSE 流式返回
    GW-->>Client: 渲染回答 + 引用来源
```

### 10.3 任务式 Agent（DAG）时序图

```mermaid
sequenceDiagram
    actor User as 销售员
    participant GW as API 网关层
    participant Task as 任务式 Agent 服务
    participant Planner as Planner(LLM)
    participant Worker as Celery Worker
    participant Tools as 工具集(检索/CRM)

    User->>GW: POST /api/v1/tasks
    GW->>Task: 创建任务
    Task->>Planner: 拆解 DAG
    Planner-->>Task: 节点 + 依赖
    Task->>Worker: 提交 DAG 执行
    loop 按拓扑排序
        Worker->>Tools: 执行节点
        Tools-->>Worker: 节点结果
        Worker->>Task: SSE 进度
    end
    Task-->>GW: 任务完成 + 结果
    GW-->>User: 返回编排结果
```

---

*文档生成时间：基于 KQuery Pro v3.0 七层架构文档自动生成*
