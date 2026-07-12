"""检索器模块。

RAG 业务编排层 (RAG Engine)，提供高级业务接口。

职责：
- 作为 RAG 系统的对外门面，提供高级业务接口
- 协调数据读写与意图分析
- 处理业务逻辑，如数据预处理、结果组合、外部数据获取等

主要功能：
1. 数据存储：
   - store_documents: 存储文档到 document_chunks 集合
   - store_memories: 存储长期记忆到 memory_records 集合

2. 数据检索：
   - search: 语义检索，支持HyDE和重排序
   - query: 标准查询接口

3. 数据清理：
   - clear_collection: 清空指定集合

4. 统计信息：
   - get_collection_stats: 获取集合统计信息
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.repositories.operations import (
    clear_document_chunks,
    get_collection_stats,
    insert_document_chunk,
    query_document_chunks,
)
from app.services.embedding import OpenAIEmbeddingService
from app.services.hyde_generator import HyDEGenerator
from app.core.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """检索结果（轻量统一格式）。"""

    id: str
    content: str
    score: float
    rerank_score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class RerankerService:
    """重排序服务 - 基于Qwen3-Reranker-8B模型。"""
    
    def __init__(
        self,
        api_base: str = "http://localhost:7929/v1",
        api_key: str = "sk-placeholder",
        model_name: str = "Qwen3-Reranker-8B",
    ):
        """初始化重排序服务。
        
        Args:
            api_base: 重排序API基础URL
            api_key: API密钥
            model_name: 重排序模型名称
        """
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model_name = model_name
        self._initialized = False
        logger.info("重排序服务初始化")
    
    async def rerank(
        self,
        query: str,
        candidates: List[Dict[str, Any]],
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        """对候选文档进行重排序。
        
        Args:
            query: 查询文本
            candidates: 候选文档列表，每个包含 id, content, score
            top_k: 返回的重排序结果数量
            
        Returns:
            重排序后的结果列表，包含 rerank_score 字段
        """
        if not candidates:
            return []
        
        if not self._initialized:
            await self._check_health()
        
        try:
            import aiohttp
            
            url = f"{self.api_base}/rerank"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self.model_name,
                "query": query,
                "documents": [c.get("content", "") for c in candidates],
                "top_n": top_k,
                "return_documents": True,
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=60)
                ) as response:
                    if response.status != 200:
                        text = await response.text()
                        logger.warning(f"Reranker API 返回错误: {response.status}, {text}")
                        return self._fallback_rerank(candidates, top_k)
                    
                    result = await response.json()
            
            reranked_results = []
            for item in result.get("results", []):
                idx = item.get("index")
                score = item.get("rerank_score", 0.0)
                
                if idx is not None and 0 <= idx < len(candidates):
                    candidate = candidates[idx].copy()
                    candidate["rerank_score"] = score
                    reranked_results.append(candidate)
            
            reranked_results.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
            return reranked_results[:top_k]
        
        except Exception as e:
            logger.warning(f"Reranker调用失败，使用本地回退: {e}")
            return self._fallback_rerank(candidates, top_k)
    
    def _fallback_rerank(
        self,
        candidates: List[Dict[str, Any]],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        """本地回退重排序（基于原始分数）。"""
        reranked = []
        for i, c in enumerate(candidates):
            c = c.copy()
            c["rerank_score"] = c.get("score", 0.5) * (1.0 - i * 0.01)
            reranked.append(c)
        
        reranked.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
        return reranked[:top_k]
    
    async def _check_health(self) -> bool:
        """检查重排序服务健康状态。"""
        try:
            import aiohttp
            url = f"{self.api_base}/health"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                    self._initialized = response.status == 200
                    return self._initialized
        except Exception:
            self._initialized = True
            return True


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
        
        Args:
            vector_results: 向量检索结果列表
            bm25_results: BM25检索结果列表
            vector_weight: 向量检索权重
            bm25_weight: BM25检索权重
            top_k: 返回结果数量
            
        Returns:
            融合后的结果列表
        """
        if not vector_results and not bm25_results:
            return []
        
        id_to_result = {}
        
        for i, result in enumerate(vector_results):
            doc_id = result.get("id", f"vec_{i}")
            result = result.copy()
            result["_vector_rank"] = i + 1
            result["_bm25_rank"] = None
            id_to_result[doc_id] = result
        
        for i, result in enumerate(bm25_results):
            doc_id = result.get("id", f"bm25_{i}")
            if doc_id in id_to_result:
                id_to_result[doc_id]["_bm25_rank"] = i + 1
            else:
                result = result.copy()
                result["_vector_rank"] = None
                result["_bm25_rank"] = i + 1
                id_to_result[doc_id] = result
        
        for result in id_to_result.values():
            rrf_score = 0.0
            
            if result["_vector_rank"]:
                rrf_score += vector_weight * self.rrf_k / (self.rrf_k + result["_vector_rank"])
            
            if result["_bm25_rank"]:
                rrf_score += bm25_weight * self.rrf_k / (self.rrf_k + result["_bm25_rank"])
            
            result["rrf_score"] = rrf_score
            del result["_vector_rank"]
            del result["_bm25_rank"]
        
        fused_list = list(id_to_result.values())
        fused_list.sort(key=lambda x: x.get("rrf_score", 0), reverse=True)
        
        return fused_list[:top_k]


class TimeDecayScorer:
    """时间衰减排序器。
    
    根据文档时间进行衰减排序，越新的文档排名越前。
    """
    
    def __init__(self, half_life_days: float = 30.0):
        """初始化时间衰减排序器。
        
        Args:
            half_life_days: 半衰期（天），文档重要性随时间减半的时间
        """
        self.half_life_days = half_life_days
        logger.info(f"时间衰减排序器初始化完成 (半衰期={half_life_days}天)")
    
    def score(
        self,
        results: List[Dict[str, Any]],
        time_field: str = "created_at",
        base_score: float = 1.0,
    ) -> List[Dict[str, Any]]:
        """对结果应用时间衰减。
        
        Args:
            results: 检索结果列表
            time_field: 时间字段名
            base_score: 基础分数
            
        Returns:
            应用时间衰减后的结果列表
        """
        from datetime import datetime, timezone
        
        current_time = datetime.now(timezone.utc)
        
        scored_results = []
        for result in results:
            result = result.copy()
            created_at = result.get("metadata", {}).get(time_field)
            
            if created_at:
                if isinstance(created_at, str):
                    try:
                        created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    except ValueError:
                        created_at = None
            
            if created_at:
                try:
                    age_days = (current_time - created_at.replace(tzinfo=timezone.utc)).days
                    decay_factor = 0.5 ** (age_days / self.half_life_days)
                except Exception:
                    decay_factor = 1.0
            else:
                decay_factor = 1.0
            
            original_score = result.get("score", base_score)
            result["time_decay_score"] = original_score * decay_factor
            result["_decay_factor"] = decay_factor
            
            scored_results.append(result)
        
        scored_results.sort(key=lambda x: x.get("time_decay_score", 0), reverse=True)
        return scored_results


class RAGEngine:
    """RAG 业务编排层。

    作为 RAG 系统的对外门面，提供基础操作方法：
    - DocumentChunk 操作：insert_document_chunk, query_document_chunks, clear_document_chunks
    - 检索功能：search, query
    - 管理功能：clear_collection, get_collection_stats
    """

    def __init__(
        self,
        enable_hyde: bool = True,
        enable_rerank: bool = True,
        enable_rrf: bool = True,
        enable_time_decay: bool = True,
    ):
        """初始化 RAGEngine。

        Args:
            enable_hyde: 是否启用HyDE（假设性文档嵌入）
            enable_rerank: 是否启用重排序
            enable_rrf: 是否启用RRF融合
            enable_time_decay: 是否启用时间衰减
        """
        self.enable_hyde = enable_hyde
        self.enable_rerank = enable_rerank
        self.enable_rrf = enable_rrf
        self.enable_time_decay = enable_time_decay
        self._hyde_generator: Optional[HyDEGenerator] = None
        self._reranker: Optional[RerankerService] = None
        self._rrf_engine: Optional[RRFusionEngine] = None
        self._time_decay_scorer: Optional[TimeDecayScorer] = None

        logger.info("RAG引擎初始化完成")

    def _get_hyde_generator(self) -> HyDEGenerator:
        """获取HyDE生成器实例。"""
        if self._hyde_generator is None:
            self._hyde_generator = HyDEGenerator()
            logger.info("HyDE生成器初始化完成")
        return self._hyde_generator

    def _get_reranker(self) -> RerankerService:
        """获取重排序服务实例。"""
        if self._reranker is None:
            from app.core.settings import get_settings
            settings = get_settings()
            self._reranker = RerankerService(
                api_base=settings.llm.rerank_api_base,
                api_key=settings.llm.rerank_api_key or "sk-placeholder",
                model_name=settings.llm.rerank_model_name,
            )
            logger.info("重排序服务初始化完成")
        return self._reranker

    def _get_rrf_engine(self) -> RRFusionEngine:
        """获取RRF融合引擎实例。"""
        if self._rrf_engine is None:
            from app.core.settings import get_settings
            settings = get_settings()
            self._rrf_engine = RRFusionEngine(rrf_k=settings.rag.rerank_rrf_k)
            logger.info("RRF融合引擎初始化完成")
        return self._rrf_engine

    def _get_time_decay_scorer(self) -> TimeDecayScorer:
        """获取时间衰减排序器实例。"""
        if self._time_decay_scorer is None:
            from app.core.settings import get_settings
            settings = get_settings()
            self._time_decay_scorer = TimeDecayScorer(
                half_life_days=settings.rag.time_decay_half_life_days
            )
            logger.info("时间衰减排序器初始化完成")
        return self._time_decay_scorer

    async def search(
        self,
        query: str,
        collection_name: str,
        top_k: int = 5,
        enable_hyde: bool = True,
    ) -> List[RetrievalResult]:
        """语义检索（支持HyDE、重排序、RRF融合、时间衰减）。

        Args:
            query: 查询文本
            collection_name: 向量集合名称
            top_k: 返回结果数量
            enable_hyde: 是否启用HyDE

        Returns:
            检索结果列表
        """
        try:
            search_query = query
            if enable_hyde and self.enable_hyde:
                hyde_generator = self._get_hyde_generator()
                hyde_result = await hyde_generator.generate(query)
                if hyde_result.success and hyde_result.hypothetical_doc:
                    search_query = hyde_result.hypothetical_doc
                    logger.info(f"使用HyDE生成的查询: {search_query[:50]}...")

            fetch_k = top_k * 3
            vector_results = await query_document_chunks(
                query_text=search_query,
                collection_name=collection_name,
                top_k=fetch_k,
            )

            candidates = []
            for result in vector_results:
                candidates.append({
                    "id": result.get("id", ""),
                    "content": result.get("content", result.get("text", "")),
                    "score": result.get("score", 0.0),
                    "metadata": result.get("metadata", {}),
                })

            if self.enable_rerank and len(candidates) >= 2:
                reranker = self._get_reranker()
                reranked = await reranker.rerank(query, candidates, top_k=top_k)
                candidates = reranked
            else:
                candidates = candidates[:top_k]

            if self.enable_time_decay:
                scorer = self._get_time_decay_scorer()
                candidates = scorer.score(candidates)

            retrieval_results = []
            for result in candidates:
                retrieval_results.append(
                    RetrievalResult(
                        id=result.get("id", ""),
                        content=result.get("content", result.get("text", "")),
                        score=result.get("score", 0.0),
                        rerank_score=result.get("rerank_score"),
                        metadata=result.get("metadata", {}),
                    )
                )

            logger.info(f"检索完成，返回 {len(retrieval_results)} 个结果")
            return retrieval_results

        except Exception as e:
            logger.error(f"检索失败: {query}, 错误: {e}", exc_info=True)
            return []

    async def hybrid_search(
        self,
        query: str,
        collection_name: str,
        top_k: int = 5,
        bm25_weight: float = 0.4,
    ) -> List[RetrievalResult]:
        """混合检索（向量 + BM25 + RRF融合）。

        Args:
            query: 查询文本
            collection_name: 向量集合名称
            top_k: 返回结果数量
            bm25_weight: BM25检索权重（剩余权重分配给向量检索）

        Returns:
            融合后的检索结果列表
        """
        try:
            vector_k = top_k * 3
            vector_results = await query_document_chunks(
                query_text=query,
                collection_name=collection_name,
                top_k=vector_k,
            )

            vector_candidates = []
            for result in vector_results:
                vector_candidates.append({
                    "id": result.get("id", ""),
                    "content": result.get("content", result.get("text", "")),
                    "score": result.get("score", 0.0),
                    "metadata": result.get("metadata", {}),
                })

            bm25_candidates = self._bm25_search(query, vector_candidates)

            rrf_engine = self._get_rrf_engine()
            fused = rrf_engine.fuse(
                vector_results=vector_candidates,
                bm25_results=bm25_candidates,
                vector_weight=1.0 - bm25_weight,
                bm25_weight=bm25_weight,
                top_k=top_k,
            )

            if self.enable_rerank and len(fused) >= 2:
                reranker = self._get_reranker()
                reranked = await reranker.rerank(query, fused, top_k=top_k)
                fused = reranked

            retrieval_results = []
            for result in fused:
                meta = result.get("metadata", {}) or {}
            retrieval_results.append(
                RetrievalResult(
                        id=result.get("id", ""),
                        content=result.get("content", ""),
                        score=result.get("rrf_score", result.get("score", 0.0)),
                        rerank_score=result.get("rerank_score"),
                        metadata=meta,
                    )
                )

            logger.info(f"混合检索完成，返回 {len(retrieval_results)} 个结果")
            return retrieval_results

        except Exception as e:
            logger.error(f"混合检索失败: {query}, 错误: {e}", exc_info=True)
            return []

    def _bm25_search(
        self,
        query: str,
        candidates: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """简单的BM25风格搜索（基于词频）。
        
        注意：这是简化的本地实现，生产环境应使用真实的BM25索引。
        """
        if not candidates:
            return []
        
        import re
        
        query_terms = re.findall(r"[\w]+", query.lower())
        if not query_terms:
            return []
        
        scored = []
        for i, candidate in enumerate(candidates):
            content = candidate.get("content", "").lower()
            content_terms = re.findall(r"[\w]+", content)
            
            if not content_terms:
                continue
            
            term_count = sum(1 for term in query_terms if term in content_terms)
            score = term_count / len(query_terms)
            
            result = candidate.copy()
            result["bm25_score"] = score
            result["_bm25_rank"] = i + 1
            scored.append(result)
        
        scored.sort(key=lambda x: x.get("bm25_score", 0), reverse=True)
        return scored[:20]

    async def query(
        self,
        query: str,
        collection_name: str,
        top_k: int = 5,
    ) -> List[RetrievalResult]:
        """标准查询接口。

        Args:
            query: 查询文本
            collection_name: 向量集合名称
            top_k: 返回结果数量

        Returns:
            检索结果列表
        """
        return await self.search(
            query=query,
            collection_name=collection_name,
            top_k=top_k,
            enable_hyde=False,
        )

    async def insert_document_chunk(
        self,
        text: str,
        collection_name: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """插入文档切片。

        Args:
            text: 文本内容
            collection_name: 集合名称
            metadata: 元数据

        Returns:
            bool: 插入是否成功
        """
        try:
            embedding_service = OpenAIEmbeddingService()
            vector = await embedding_service.embed([text])

            success = await insert_document_chunk(
                collection_name=collection_name,
                text=text,
                vector=vector[0],
                metadata=metadata or {},
            )

            if success:
                logger.info(f"文档块插入成功: {text[:50]}...")
            else:
                logger.warning(f"文档块插入失败: {text[:50]}...")

            return success

        except Exception as e:
            logger.error(f"插入文档块失败: {e}", exc_info=True)
            return False

    async def clear_collection(self, collection_name: str) -> bool:
        """清空指定集合的所有数据。

        Args:
            collection_name: 集合名称

        Returns:
            bool: 是否成功
        """
        try:
            success = await clear_document_chunks(collection_name)
            if success:
                logger.info(f"集合清空完成: '{collection_name}'")
            else:
                logger.warning(f"集合清空失败: '{collection_name}'")
            return success
        except Exception as e:
            logger.error(f"清空集合失败: '{collection_name}', 错误: {e}", exc_info=True)
            return False

    async def get_collection_stats(self, collection_name: str) -> Dict[str, Any]:
        """获取集合统计信息。

        Args:
            collection_name: 集合名称

        Returns:
            Dict[str, Any]: 统计信息
        """
        try:
            stats = await get_collection_stats(collection_name)
            logger.info(f"获取集合统计成功: '{collection_name}', 文档数: {stats.get('count', 0)}")
            return stats
        except Exception as e:
            logger.error(f"获取集合统计失败: '{collection_name}', 错误: {e}", exc_info=True)
            return {
                "collection_name": collection_name,
                "count": 0,
                "error": str(e),
            }