# Kquery 检索链路

## 双路召回

关键词路用 BM25，语义路用向量检索。每条查询（原文 / 改写 / HyDE / 子问题）都分别跑双路，
每路默认取前 20 条候选。任一路失败会被降级并记录到 `degraded_routes`，不会拖垮整次检索。

## 融合与精排

RRF 融合按名次计算，RRF 常数 k 默认 60，融合候选池默认 20 条。
融合结果交给重排模型（gte-rerank-v2）精排，取最终 Top-K。
重排失败会降级为融合序并标记 `reranked=False`，绝不让重排阻断检索。

## 切块策略

默认切块参数是 chunk_size 300、overlap 50（按字符计）。
可选策略有 recursive、semantic、parent_child、raptor 四种，参数可热配置。
切块产出的 chunk_id 形如 `{doc_id}#{seq}`，重建索引时保持不变。

## 评测

检索质量用 recall@k、nDCG@10、MRR 度量，标注格式是 query_id / chunk_id / grade 三元组。
