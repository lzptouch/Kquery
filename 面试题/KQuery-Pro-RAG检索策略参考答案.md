# KQuery Pro深度面试题参考答案
## 第一部分：RAG模块 - 检索策略（题目28-55）

---

### 题目28：BM25算法的核心参数与调优 ⭐⭐

#### 一、核心原理

**理论阐述：**

BM25（Best Matching 25）是基于概率检索模型的经典算法，是TF-IDF的改进版本。其核心公式为：

```
score(D, Q) = Σ IDF(qi) × (f(qi, D) × (k1 + 1)) / (f(qi, D) + k1 × (1 - b + b × |D| / avgdl))
```

**三大核心参数：**

1. **k1参数（词频饱和参数）**：
   - 取值范围：1.2-2.0（典型值1.5）
   - 作用：控制词频对得分的影响速度
   - 效果：k1越大，词频增长对得分的影响越显著，但存在饱和效应
   - 调优策略：短文档取较小值（1.2），长文档取较大值（1.8-2.0）

2. **b参数（文档长度归一化参数）**：
   - 取值范围：0.75（典型值）
   - 作用：控制文档长度对得分的影响
   - 效果：b=0时忽略文档长度，b=1时完全归一化
   - 调优策略：文档长度差异大时取0.75-0.9，差异小时取0.5-0.75

3. **IDF（逆文档频率）**：
   - 公式：`IDF(qi) = log((N - n(qi) + 0.5) / (n(qi) + 0.5))`
   - 作用：衡量词的区分度
   - 效果：罕见词IDF高，常见词IDF低

**关键代码示例：**

从KQuery Pro的BM25检索器中提取参数配置：

```python
# 文件：backend/app/services/bm25_retriever.py（第158-186行）
class BM25Retriever:
    """BM25 检索器。

    基于 BM25 算法的关键词检索实现，支持增量索引更新和高效检索。

    Attributes:
        k1: BM25 参数，控制词频饱和度（默认 1.5）
        b: BM25 参数，控制文档长度归一化（默认 0.75）
        epsilon: BM25 参数，用于平滑 IDF（默认 0.25）
        algorithm: BM25 算法变体（okapi, l, plus）
    """

    def __init__(
        self,
        k1: float = 1.5,
        b: float = 0.75,
        epsilon: float = 0.25,
        algorithm: str = "okapi",
    ):
        """初始化 BM25 检索器。

        Args:
            k1: BM25 k1 参数
            b: BM25 b 参数
            epsilon: BM25 epsilon 参数（仅用于 okapi 算法）
            algorithm: BM25 算法变体（okapi, l, plus）
        """
        if not RANK_BM25_AVAILABLE:
            raise ImportError("rank_bm25 library is required. Install it with: pip install rank_bm25")

        self.k1 = k1
        self.b = b
        self.epsilon = epsilon
        self.algorithm = algorithm

        self._documents: dict[str, Document] = {}
        self._tokenized_docs: list[list[str]] = []
        self._doc_id_to_index: dict[str, int] = {}
        self._index_to_doc_id: dict[int, str] = {}
        self._bm25_model: Any = None
        self._index_hash: str = ""
```

#### 二、实现细节

**关键步骤：**

1. **文本预处理流程**
   - 中文分词：使用jieba进行分词
   - 去停用词：移除高频无意义词（"的"、"是"、"在"等）
   - 词干提取：英文需要词干提取

```python
# 文件：backend/app/services/bm25_retriever.py（第104-143行）
class TextTokenizer:
    """文本分词器。

    支持中文和英文分词，使用 jieba 进行中文分词。
    """

    @staticmethod
    def tokenize(text: str) -> list[str]:
        """对文本进行分词。

        Args:
            text: 待分词的文本

        Returns:
            分词结果列表
        """
        if not text:
            return []

        text = text.strip()
        if not text:
            return []

        if JIEBA_AVAILABLE:
            tokens = list(jieba.cut(text))
            return [token for token in tokens if token.strip()]
        else:
            return text.split()

    @staticmethod
    def tokenize_batch(texts: Iterable[str]) -> list[list[str]]:
        """批量分词。

        Args:
            texts: 文本列表

        Returns:
            分词结果列表的列表
        """
        return [TextTokenizer.tokenize(text) for text in texts]
```

2. **索引构建与检索**

```python
# 文件：backend/app/services/bm25_retriever.py（第294-344行）
def retrieve(
    self,
    query: str,
    top_k: int = 10,
    min_score: float = 0.0,
) -> list[RetrievalResult]:
    """检索相关文档。

    Args:
        query: 查询文本
        top_k: 返回的最相关文档数量
        min_score: 最小相关性分数阈值

    Returns:
        检索结果列表，按相关性分数降序排序
    """
    if not self.is_indexed:
        logger.warning("BM25 index not built, returning empty results")
        return []

    if not query:
        return []

    query_tokens = TextTokenizer.tokenize(query)

    if not query_tokens:
        return []

    scores = self._bm25_model.get_scores(query_tokens)

    results: list[tuple[int, float]] = []
    for idx, score in enumerate(scores):
        if score >= min_score:
            results.append((idx, score))

    results.sort(key=lambda x: x[1], reverse=True)

    retrieval_results = []
    for rank, (idx, score) in enumerate(results[:top_k]):
        doc_id = self._get_doc_id_by_index(idx)
        if doc_id:
            doc = self._documents[doc_id]
            result = RetrievalResult(
                doc_id=doc_id,
                score=float(score),
                rank=rank,
                metadata=doc.metadata.copy(),
            )
            retrieval_results.append(result)

    return retrieval_results
```

#### 三、场景应用

**基于KQuery Pro的实际案例：**

在销售助手场景中的BM25应用：

```python
# KQuery Pro销售助手BM25检索实现
class SalesKBRetriever:
    """销售知识库检索器"""

    def __init__(self):
        self.bm25_retriever = BM25Retriever(k1=1.5, b=0.75, algorithm="okapi")

    async def build_index(self, documents: List[dict]):
        """构建销售知识库索引"""
        # 转换为BM25文档格式
        bm25_docs = []
        for doc in documents:
            bm25_doc = Document(
                doc_id=doc["id"],
                content=doc["content"],
                metadata={
                    "title": doc["title"],
                    "category": doc["category"],
                    "created_at": doc["created_at"]
                }
            )
            bm25_docs.append(bm25_doc)

        self.bm25_retriever.build_index(bm25_docs)

    async def search(self, query: str, top_k: int = 5) -> List[dict]:
        """搜索销售知识库"""
        results = self.bm25_retriever.retrieve(query, top_k=top_k)

        return [
            {
                "id": result.doc_id,
                "content": result.metadata.get("content", ""),
                "score": result.score,
                "rank": result.rank
            }
            for result in results
        ]
```

**参数调优实验：**

```python
# 参数调优实验代码
class BM25ParameterTuner:
    """BM25参数调优器"""

    def tune_parameters(self, test_data: List[dict]) -> dict:
        """通过网格搜索调优参数"""
        k1_values = [1.2, 1.5, 1.8, 2.0]
        b_values = [0.5, 0.75, 0.9]

        best_score = 0
        best_params = {"k1": 1.5, "b": 0.75}

        for k1 in k1_values:
            for b in b_values:
                # 创建检索器并测试
                retriever = BM25Retriever(k1=k1, b=b)
                score = self.evaluate(retriever, test_data)

                if score > best_score:
                    best_score = score
                    best_params = {"k1": k1, "b": b}

        return best_params
```

#### 四、问题排查

**诊断流程：**

1. **常见词权重过高**
   - 现象：查询"腾讯云产品介绍"，"腾讯"权重过高
   - 诊断：检查IDF值分布，分析停用词列表
   - 解决方案：
     - 扩充停用词表（添加领域特定停用词）
     - 使用自定义IDF权重
     - 实施查询词权重调整

```python
# 停用词处理方案
class CustomStopWordsFilter:
    """自定义停用词过滤器"""

    def __init__(self):
        # 领域特定停用词
        self.domain_stopwords = {
            "产品", "介绍", "说明", "文档", "信息"
        }

    def filter_query(self, query: str) -> str:
        """过滤查询中的停用词"""
        tokens = TextTokenizer.tokenize(query)
        filtered_tokens = [t for t in tokens if t not in self.domain_stopwords]
        return "".join(filtered_tokens)
```

2. **长文档倾向性**
   - 现象：长文档得分普遍偏高
   - 诊断：统计文档长度分布，分析b参数效果
   - 解决方案：
     - 调大b参数（0.85-0.95）
     - 实施文档分段检索
     - 使用BM25L算法（更适合长文档）

3. **中文分词错误**
   - 现象："深度学习"被分词为["深度", "学习"]
   - 诊断：分析分词结果，检查领域词库
   - 解决方案：
     - 添加领域词典到jieba
     - 使用专业分词器（如THULAC、HanLP）

```python
# 添加领域词典
import jieba

# 添加腾讯云产品领域词典
domain_words = [
    "腾讯云", "云服务器", "对象存储", "云数据库",
    "深度学习", "机器学习", "知识图谱"
]

for word in domain_words:
    jieba.add_word(word, freq=10000)
```

#### 五、扩展思考

**进阶问题：**

1. **如何实现BM25与深度学习模型的融合？**
   - Dense Retrieval + Sparse Retrieval混合
   - 使用Cross-Encoder重排序
   - 实施Late Interaction机制（如ColBERT）

2. **如何处理多语言BM25检索？**
   - 语言检测 + 分语言索引
   - 多语言词向量映射
   - 跨语言检索对齐

3. **如何优化BM25的实时更新性能？**
   - 增量索引更新（避免全量重建）
   - 内存索引 + 定期持久化
   - 分布式索引分片

---

### 题目29：向量检索与关键词检索的融合策略 ⭐⭐

#### 一、核心原理

**理论阐述：**

向量检索与BM25检索的融合是现代RAG系统的核心能力，通过互补性结合实现最佳检索效果。

**两种检索方式的互补性：**

| 维度 | 向量检索 | BM25检索 |
|------|----------|----------|
| 语义理解 | ✅ 强（同义词、近义词） | ❌ 弱（精确匹配） |
| 精确匹配 | ❌ 弱（专有名词识别差） | ✅ 强（精确匹配） |
| 长尾查询 | ✅ 好（语义泛化） | ❌ 差（词汇不匹配） |
| 专业术语 | ❌ 弱（模型不熟悉） | ✅ 强（关键词匹配） |
| 计算成本 | ❌ 高（向量计算） | ✅ 低（倒排索引） |

**融合策略分类：**

1. **前融合（Pre-fusion）**：
   - 查询扩展：将关键词转化为向量
   - 查询重写：使用LLM生成多个查询变体
   - 混合表示：将关键词和语义信息融合

2. **中融合（Mid-fusion）**：
   - 并行检索：同时进行向量检索和BM25检索
   - 结果合并：使用RRF（Reciprocal Rank Fusion）算法
   - 实时融合：根据查询特征动态调整权重

3. **后融合（Post-fusion）**：
   - 重排序：使用Cross-Encoder对结果重新打分
   - 多样性控制：确保结果多样性
   - 业务规则：应用业务逻辑过滤

**关键代码示例：**

从KQuery Pro的RRF融合引擎中提取核心实现：

```python
# 文件：backend/app/services/retriever.py（第173-247行）
class RRFusionEngine:
    """RRF (Reciprocal Rank Fusion) 融合引擎。

    用于融合向量检索和BM25检索的结果。
    """

    def __init__(self, rrf_k: int = 60):
        """初始化RRF融合引擎。

        Args:
            rrf_k: RRF参数k，越大各结果权重越均衡
        """
        self.rrf_k = rrf_k
        logger.info(f"RRF融合引擎初始化完成 (k={rrf_k})")

    def fuse(
        self,
        vector_results: List[Dict[str, Any]],
        bm25_results: List[Dict[str, Any]],
        vector_weight: float = 0.6,
        bm25_weight: float = 0.4,
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        """融合向量检索和BM25检索结果。

        RRF公式：score = sum(1 / (k + rank_i))
        """
        if not vector_results and not bm25_results:
            return []

        id_to_result = {}

        # 处理向量检索结果
        for i, result in enumerate(vector_results):
            doc_id = result.get("id", f"vec_{i}")
            result = result.copy()
            result["_vector_rank"] = i + 1
            result["_bm25_rank"] = None
            id_to_result[doc_id] = result

        # 处理BM25检索结果
        for i, result in enumerate(bm25_results):
            doc_id = result.get("id", f"bm25_{i}")
            if doc_id in id_to_result:
                id_to_result[doc_id]["_bm25_rank"] = i + 1
            else:
                result = result.copy()
                result["_vector_rank"] = None
                result["_bm25_rank"] = i + 1
                id_to_result[doc_id] = result

        # 计算RRF分数
        for result in id_to_result.values():
            rrf_score = 0.0

            if result["_vector_rank"]:
                rrf_score += vector_weight * self.rrf_k / (self.rrf_k + result["_vector_rank"])

            if result["_bm25_rank"]:
                rrf_score += bm25_weight * self.rrf_k / (self.rrf_k + result["_bm25_rank"])

            result["rrf_score"] = rrf_score
            del result["_vector_rank"]
            del result["_bm25_rank"]

        # 按RRF分数排序
        fused_list = list(id_to_result.values())
        fused_list.sort(key=lambda x: x.get("rrf_score", 0), reverse=True)

        return fused_list[:top_k]
```

#### 二、实现细节

**关键步骤：**

1. **查询类型判断**

```python
# 查询类型判断器
class QueryTypeClassifier:
    """查询类型分类器"""

    def classify(self, query: str) -> str:
        """判断查询类型，决定检索策略

        Returns:
            "semantic": 偏重向量检索（语义型查询）
            "keyword": 偏重BM25检索（关键词型查询）
            "hybrid": 均衡融合
        """
        # 特征提取
        has_entities = self._has_named_entities(query)
        query_length = len(query.split())
        has_special_terms = self._has_special_terms(query)

        if has_entities or has_special_terms:
            return "keyword"  # 专有名词查询，偏向BM25
        elif query_length < 3:
            return "semantic"  # 短查询，偏向向量
        else:
            return "hybrid"  # 中等查询，均衡融合

    def _has_named_entities(self, query: str) -> bool:
        """检查是否包含命名实体"""
        # 使用NER模型或规则识别
        entity_patterns = ["腾讯", "阿里", "华为", "张三", "李四"]
        return any(pattern in query for pattern in entity_patterns)

    def _has_special_terms(self, query: str) -> bool:
        """检查是否包含特殊术语"""
        # 检查专业术语、代码等
        import re
        return bool(re.search(r'[A-Z]{2,}|[\d]{4,}', query))
```

2. **动态权重分配**

```python
# 动态权重分配器
class DynamicWeightAllocator:
    """动态权重分配器"""

    def get_weights(self, query: str, query_type: str) -> dict:
        """根据查询特征动态分配权重

        Returns:
            {"vector_weight": 0.7, "bm25_weight": 0.3}
        """
        if query_type == "semantic":
            return {"vector_weight": 0.8, "bm25_weight": 0.2}
        elif query_type == "keyword":
            return {"vector_weight": 0.3, "bm25_weight": 0.7}
        else:
            # 根据查询特征微调
            features = self._extract_features(query)
            vector_weight = 0.5 + 0.1 * features["semantic_score"]
            return {
                "vector_weight": vector_weight,
                "bm25_weight": 1 - vector_weight
            }

    def _extract_features(self, query: str) -> dict:
        """提取查询特征"""
        return {
            "length": len(query.split()),
            "semantic_score": self._calculate_semantic_score(query),
            "keyword_density": self._calculate_keyword_density(query)
        }
```

#### 三、场景应用

**基于KQuery Pro的实际案例：**

销售助手场景的混合检索实现：

```python
# KQuery Pro混合检索实现
class HybridSearchEngine:
    """混合检索引擎"""

    def __init__(self):
        self.rag_engine = RAGEngine(enable_rrf=True)
        self.query_classifier = QueryTypeClassifier()
        self.weight_allocator = DynamicWeightAllocator()

    async def search(
        self,
        query: str,
        collection_name: str,
        top_k: int = 5
    ) -> List[RetrievalResult]:
        """混合检索（向量 + BM25）"""
        # 1. 判断查询类型
        query_type = self.query_classifier.classify(query)

        # 2. 分配权重
        weights = self.weight_allocator.get_weights(query, query_type)

        # 3. 执行混合检索
        results = await self.rag_engine.hybrid_search(
            query=query,
            collection_name=collection_name,
            top_k=top_k,
            bm25_weight=weights["bm25_weight"]
        )

        logger.info(
            f"混合检索完成 | 查询类型: {query_type} | "
            f"权重: 向量{weights['vector_weight']:.2f}/BM25{weights['bm25_weight']:.2f} | "
            f"返回{len(results)}个结果"
        )

        return results
```

**效果对比实验：**

```python
# 检索效果对比实验
class RetrievalEvaluator:
    """检索效果评估器"""

    async def compare_strategies(
        self,
        test_queries: List[str],
        ground_truth: List[List[str]]
    ) -> dict:
        """对比不同检索策略的效果"""
        results = {
            "vector_only": [],
            "bm25_only": [],
            "hybrid": []
        }

        for query, truth in zip(test_queries, ground_truth):
            # 向量检索
            vector_results = await self.vector_search(query)
            vector_metrics = self._calculate_metrics(vector_results, truth)
            results["vector_only"].append(vector_metrics)

            # BM25检索
            bm25_results = await self.bm25_search(query)
            bm25_metrics = self._calculate_metrics(bm25_results, truth)
            results["bm25_only"].append(bm25_metrics)

            # 混合检索
            hybrid_results = await self.hybrid_search(query)
            hybrid_metrics = self._calculate_metrics(hybrid_results, truth)
            results["hybrid"].append(hybrid_metrics)

        return self._aggregate_results(results)
```

#### 四、问题排查

**诊断流程：**

1. **得分尺度不一致**
   - 现象：向量得分0.8，BM25得分30，直接加权不合理
   - 诊断：统计两种得分的分布范围
   - 解决方案：
     - Min-Max归一化
     - Z-Score标准化
     - 使用RRF（基于排名而非分数）

```python
# 得分归一化处理
class ScoreNormalizer:
    """得分归一化器"""

    def normalize_scores(
        self,
        vector_scores: List[float],
        bm25_scores: List[float]
    ) -> Tuple[List[float], List[float]]:
        """归一化两组得分到[0, 1]区间"""
        # Min-Max归一化
        vec_min, vec_max = min(vector_scores), max(vector_scores)
        bm25_min, bm25_max = min(bm25_scores), max(bm25_scores)

        normalized_vector = [
            (s - vec_min) / (vec_max - vec_min) if vec_max > vec_min else 0.5
            for s in vector_scores
        ]

        normalized_bm25 = [
            (s - bm25_min) / (bm25_max - bm25_min) if bm25_max > bm25_min else 0.5
            for s in bm25_scores
        ]

        return normalized_vector, normalized_bm25
```

2. **融合权重设置不合理**
   - 现象：固定权重0.7/0.3，但某些查询效果差
   - 诊断：分析不同查询类型的最优权重
   - 解决方案：
     - 基于查询特征动态调整
     - A/B测试验证效果
     - 使用机器学习预测最优权重

#### 五、扩展思考

**进阶问题：**

1. **如何实现自适应融合权重学习？**
   - 收集用户反馈（点击、满意度）
   - 训练权重预测模型（Learning to Rank）
   - 在线学习与持续优化

2. **如何处理融合结果的多样性？**
   - MMR（Maximal Marginal Relevance）算法
   - 基于聚类的多样性控制
   - 公平性约束（确保不同来源的结果）

3. **如何在分布式环境下实现高效融合？**
   - 并行检索 + 异步合并
   - 流式融合（边检索边合并）
   - 分布式缓存优化

---

### 题目30：多模态检索的实现方案 ⭐⭐

#### 一、核心原理

**理论阐述：**

多模态检索是现代智能检索系统的重要能力，支持文本、图像、视频等多种模态的跨模态检索。

**核心技术栈：**

1. **多模态Embedding模型**：
   - **CLIP（Contrastive Language-Image Pre-training）**：
     - 文本和图像共享向量空间（512维）
     - 支持以文搜图、以图搜文
     - 中文支持：Chinese-CLIP

   - **ImageBind**：
     - 支持6种模态（文本、图像、音频、视频、热图、IMU）
     - 统一向量空间（1024维）
     - 跨模态对齐能力强

2. **多模态索引架构**：
   - **统一向量空间**：所有模态映射到同一空间
   - **独立向量空间**：各模态独立索引，通过桥接查询
   - **混合架构**：热门模态统一，冷门模态独立

**关键代码示例：**

从KQuery Pro的向量检索中提取多模态支持：

```python
# 多模态检索扩展（基于retriever.py）
class MultimodalRAGEngine:
    """多模态RAG引擎"""

    def __init__(self):
        self.text_encoder = OpenAIEmbeddingService()
        self.image_encoder = CLIPEncoder()  # 图像编码器
        self.rag_engine = RAGEngine()

    async def text_to_image_search(
        self,
        query: str,
        collection_name: str,
        top_k: int = 5
    ) -> List[dict]:
        """以文搜图"""
        # 1. 文本编码
        text_embedding = await self.text_encoder.embed([query])

        # 2. 在图像索引中搜索
        results = await query_document_chunks(
            query_vector=text_embedding[0],
            collection_name=f"{collection_name}_images",
            top_k=top_k
        )

        return results

    async def image_to_text_search(
        self,
        image_path: str,
        collection_name: str,
        top_k: int = 5
    ) -> List[dict]:
        """以图搜文"""
        # 1. 图像编码
        image_embedding = await self.image_encoder.encode_image(image_path)

        # 2. 在文本索引中搜索
        results = await query_document_chunks(
            query_vector=image_embedding,
            collection_name=f"{collection_name}_texts",
            top_k=top_k
        )

        return results
```

#### 二、实现细节

**关键步骤：**

1. **多模态索引构建**

```python
# 多模态索引构建器
class MultimodalIndexBuilder:
    """多模态索引构建器"""

    async def build_index(
        self,
        documents: List[dict],
        collection_name: str
    ):
        """构建多模态索引

        Args:
            documents: 包含文本和图像的文档列表
            collection_name: 集合名称
        """
        text_docs = []
        image_docs = []

        for doc in documents:
            # 文本索引
            if doc.get("text"):
                text_docs.append({
                    "id": f"{doc['id']}_text",
                    "content": doc["text"],
                    "metadata": doc.get("metadata", {})
                })

            # 图像索引
            if doc.get("image_path"):
                image_embedding = await self.image_encoder.encode_image(
                    doc["image_path"]
                )
                image_docs.append({
                    "id": f"{doc['id']}_image",
                    "vector": image_embedding,
                    "metadata": {
                        "original_id": doc["id"],
                        "image_path": doc["image_path"]
                    }
                })

        # 构建索引
        await self._build_text_index(text_docs, f"{collection_name}_texts")
        await self._build_image_index(image_docs, f"{collection_name}_images")
```

2. **跨模态检索实现**

```python
# 跨模态检索器
class CrossModalRetriever:
    """跨模态检索器"""

    async def cross_modal_search(
        self,
        query: Union[str, bytes],
        query_type: str,  # "text" or "image"
        collection_name: str,
        top_k: int = 5
    ) -> List[dict]:
        """跨模态检索

        Args:
            query: 查询（文本或图像）
            query_type: 查询类型
            collection_name: 集合名称
            top_k: 返回数量

        Returns:
            检索结果（包含文本和图像）
        """
        # 编码查询
        if query_type == "text":
            query_embedding = await self.text_encoder.encode_text(query)
        else:
            query_embedding = await self.image_encoder.encode_image(query)

        # 并行检索文本和图像
        text_results = await self._search_text_index(
            query_embedding, f"{collection_name}_texts", top_k
        )
        image_results = await self._search_image_index(
            query_embedding, f"{collection_name}_images", top_k
        )

        # 融合结果
        fused_results = self._fuse_multimodal_results(
            text_results, image_results, top_k
        )

        return fused_results
```

#### 三、场景应用

**基于KQuery Pro的实际案例：**

商品检索场景的多模态实现：

```python
# 商品多模态检索系统
class ProductMultimodalSearch:
    """商品多模态检索系统"""

    async def search_by_image(
        self,
        image_path: str,
        top_k: int = 10
    ) -> List[dict]:
        """以图搜商品"""
        # 1. 图像编码
        image_embedding = await self.clip_encoder.encode_image(image_path)

        # 2. 搜索相似商品（文本描述）
        results = await self.milvus_client.search(
            collection_name="products",
            query_vectors=[image_embedding],
            top_k=top_k
        )

        # 3. 补充商品信息
        enriched_results = []
        for result in results:
            product_info = await self.get_product_info(result["id"])
            enriched_results.append({
                **result,
                "product_name": product_info["name"],
                "price": product_info["price"],
                "image_url": product_info["image_url"]
            })

        return enriched_results

    async def search_by_text_and_image(
        self,
        text: str,
        image_path: str,
        top_k: int = 10
    ) -> List[dict]:
        """文本+图像联合查询"""
        # 1. 编码文本和图像
        text_embedding = await self.clip_encoder.encode_text(text)
        image_embedding = await self.clip_encoder.encode_image(image_path)

        # 2. 融合向量（加权平均）
        fused_embedding = 0.6 * text_embedding + 0.4 * image_embedding

        # 3. 搜索
        results = await self.milvus_client.search(
            collection_name="products",
            query_vectors=[fused_embedding],
            top_k=top_k
        )

        return results
```

#### 四、问题排查

**诊断流程：**

1. **CLIP中文支持不足**
   - 现象：中文查询检索效果差
   - 诊断：测试Chinese-CLIP vs CLIP效果
   - 解决方案：
     - 使用Chinese-CLIP替代
     - 翻译查询为英文再检索
     - 微调CLIP模型

2. **图像编码速度慢**
   - 现象：大批量图像索引构建耗时数小时
   - 诊断：分析编码瓶颈（CPU/GPU利用率）
   - 解决方案：
     - 批量编码（提高吞吐量）
     - 使用GPU加速
     - 异步编码 + 缓存

```python
# 批量图像编码优化
class BatchImageEncoder:
    """批量图像编码器"""

    async def encode_batch(
        self,
        image_paths: List[str],
        batch_size: int = 32
    ) -> List[np.ndarray]:
        """批量编码图像

        Args:
            image_paths: 图像路径列表
            batch_size: 批次大小

        Returns:
            编码结果列表
        """
        embeddings = []

        # 批量处理
        for i in range(0, len(image_paths), batch_size):
            batch = image_paths[i:i+batch_size]

            # 并行编码
            batch_embeddings = await asyncio.gather(
                *[self._encode_single(img) for img in batch]
            )

            embeddings.extend(batch_embeddings)

        return embeddings
```

#### 五、扩展思考

**进阶问题：**

1. **如何实现视频多模态检索？**
   - 视频帧提取 + 图像编码
   - 视频时序建模（如VideoCLIP）
   - 关键帧检索 + 时间范围定位

2. **如何处理多模态结果的融合？**
   - 多模态重排序模型
   - 跨模态一致性检查
   - 用户偏好学习

3. **如何优化多模态检索的实时性？**
   - 向量量化压缩
   - 近似最近邻搜索
   - 多模态缓存策略

---

（由于篇幅限制，我将继续生成剩余题目的参考答案。接下来将生成题目32-55的详细答案，每个题目都将包含核心原理、实现细节、场景应用、问题排查和扩展思考五个维度。）