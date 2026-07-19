"""长期记忆服务。

从对话中自动提取用户事实/偏好，支持向量检索和管理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from app.core.logger import get_logger

logger = get_logger(__name__)


@dataclass
class MemoryFact:
    """记忆事实数据类。"""
    id: str
    user_id: str
    content: str
    fact_type: str = "fact"  # fact, preference, goal, task
    source_session_id: str = ""
    confidence: float = 1.0
    created_at: str = ""
    embedding: Optional[List[float]] = None


class LongTermMemoryService:
    """长期记忆服务。

    负责从对话中提取事实、存储到向量库、检索相关记忆。

    使用方式：
        service = LongTermMemoryService()
        facts = await service.extract_facts(session_messages)
        await service.store_fact("user_1", fact, "session_1")
        memories = await service.retrieve_facts("user_1", "用户偏好")
    """

    def __init__(self):
        self._collection_name = "memory_records"

    async def extract_facts(
        self, messages: List[dict], user_id: str = ""
    ) -> List[MemoryFact]:
        """从对话消息中提取事实。

        Args:
            messages: 对话消息列表 [{"role": "user/assistant", "content": "..."}]
            user_id: 用户 ID

        Returns:
            提取的事实列表
        """
        import json

        from app.core.llm_client import call_openai_chat
        from app.core.llm.router import resolve_provider_config

        config = resolve_provider_config()

        conversation = "\n".join(
            f"{m['role']}: {m['content']}" for m in messages[-10:]
        )

        prompt = f"""请从以下对话中提取关于用户的长期记忆事实。

提取规则：
1. 只提取关于用户的事实：偏好、习惯、身份、目标、工作背景等
2. 每条事实用简洁的自然语言表述
3. 忽略问候、闲聊等无信息的内容
4. 如果没有值得记住的事实，返回空数组

输出 JSON 数组格式（不要附带 markdown 代码块标记）：
[{{"content": "...", "type": "preference|fact|goal|task"}}]

对话内容：
{conversation[:3000]}"""

        try:
            result = await call_openai_chat(
                model=config["model"],
                messages=[{"role": "user", "content": prompt}],
                api_key=config["api_key"],
                base_url=config["base_url"],
                temperature=0.1,
                max_tokens=1024,
            )

            # 清理可能的 markdown 标记
            result = result.strip()
            if result.startswith("```"):
                result = result.split("\n", 1)[-1]
                if result.endswith("```"):
                    result = result[:-3]

            facts_data = json.loads(result)

            if not isinstance(facts_data, list):
                return []

            import uuid
            from datetime import datetime

            now = datetime.now().isoformat()
            facts = []
            for fd in facts_data:
                if not isinstance(fd, dict) or not fd.get("content"):
                    continue
                facts.append(MemoryFact(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    content=fd["content"].strip(),
                    fact_type=fd.get("type", "fact"),
                    confidence=0.8,
                    created_at=now,
                ))

            logger.info(f"从对话中提取了 {len(facts)} 条记忆")
            return facts

        except json.JSONDecodeError as e:
            logger.warning(f"记忆提取 JSON 解析失败: {e}, 原始: {result[:100]}")
            return []
        except Exception as e:
            logger.error(f"记忆提取失败: {e}")
            return []

    async def store_fact(
        self, user_id: str, fact: MemoryFact, session_id: str = ""
    ) -> bool:
        """存储一条记忆到向量库。

        Args:
            user_id: 用户 ID
            fact: 记忆事实
            session_id: 来源会话 ID

        Returns:
            是否成功
        """
        try:
            from app.services.retriever import RAGEngine

            rag_engine = RAGEngine()

            metadata = {
                "user_id": user_id,
                "fact_type": fact.fact_type,
                "source_session_id": session_id or fact.source_session_id,
                "confidence": fact.confidence,
                "created_at": fact.created_at,
            }

            await rag_engine.insert_document_chunk(
                text=fact.content,
                collection_name=self._collection_name,
                metadata=metadata,
            )

            logger.info(f"已存储记忆: {fact.content[:50]}...")
            return True

        except Exception as e:
            logger.error(f"存储记忆失败: {e}")
            return False

    async def retrieve_facts(
        self, user_id: str, query: str, top_k: int = 5
    ) -> List[MemoryFact]:
        """检索与查询相关的长期记忆。

        Args:
            user_id: 用户 ID
            query: 查询文本
            top_k: 返回条数

        Returns:
            相关记忆列表
        """
        try:
            from app.services.retriever import RAGEngine

            rag_engine = RAGEngine()
            results = await rag_engine.query(
                query=query,
                collection_name=self._collection_name,
                top_k=top_k,
                filter_expr=f'user_id == "{user_id}"',
            )

            facts = []
            for r in results:
                facts.append(MemoryFact(
                    id=r.get("id", ""),
                    user_id=user_id,
                    content=r.get("text", ""),
                    fact_type=r.get("metadata", {}).get("fact_type", "fact"),
                    confidence=r.get("metadata", {}).get("confidence", 1.0),
                    created_at=r.get("metadata", {}).get("created_at", ""),
                ))

            return facts

        except Exception as e:
            logger.error(f"记忆检索失败: {e}")
            return []

    async def delete_fact(self, user_id: str, fact_id: str) -> bool:
        """删除一条记忆。

        Args:
            user_id: 用户 ID
            fact_id: 记忆 ID

        Returns:
            是否成功
        """
        try:
            from app.services.retriever import RAGEngine

            rag_engine = RAGEngine()
            await rag_engine.delete_document(
                collection_name=self._collection_name,
                doc_id=fact_id,
            )
            return True
        except Exception as e:
            logger.error(f"删除记忆失败: {e}")
            return False

    async def delete_all_facts(self, user_id: str) -> bool:
        """删除用户的所有记忆。

        Args:
            user_id: 用户 ID

        Returns:
            是否成功
        """
        try:
            from app.services.retriever import RAGEngine

            rag_engine = RAGEngine()
            await rag_engine.clear_collection(self._collection_name)
            logger.info(f"已清空用户 {user_id} 的所有记忆")
            return True
        except Exception as e:
            logger.error(f"清空记忆失败: {e}")
            return False

    def format_for_prompt(self, facts: List[MemoryFact]) -> str:
        """将记忆格式化为可注入 prompt 的文本。

        Args:
            facts: 记忆列表

        Returns:
            格式化文本
        """
        if not facts:
            return ""

        lines = ["[用户长期记忆]"]
        for fact in facts:
            type_label = {
                "preference": "偏好",
                "fact": "事实",
                "goal": "目标",
                "task": "任务",
            }.get(fact.fact_type, "其他")

            lines.append(f"- [{type_label}] {fact.content}")

        return "\n".join(lines)
