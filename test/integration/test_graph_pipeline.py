from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.integration
class TestGraphPipeline:
    async def test_neo4j_schema_initialized(self, test_env: dict) -> None:
        from neo4j import AsyncGraphDatabase

        driver = AsyncGraphDatabase.driver(
            test_env["NEO4J_URI"],
            auth=(test_env["NEO4J_USER"], test_env["NEO4J_PASSWORD"]),
        )
        async with driver.session() as session:
            result = await session.run("RETURN 1 AS n")
            records = await result.data()
            assert records == [{"n": 1}]
        await driver.close()

    async def test_entity_extraction_to_graph(self, test_env: dict, mock_llm_client: dict) -> None:
        from app.services.graph.graph_builder import GraphBuilderService
        from app.repositories.graph.neo4j_connector import Neo4jConnector, ConnectionConfig

        config = ConnectionConfig(
            uri=test_env["NEO4J_URI"],
            username=test_env["NEO4J_USER"],
            password=test_env["NEO4J_PASSWORD"],
        )
        connector = Neo4jConnector(config=config)
        await connector.connect()

        # 在创建 builder 之前 Mock 实体/关系抽取类，避免真实 LLM 调用
        fake_entities = MagicMock()
        fake_entities.entities = []
        fake_relations = MagicMock()
        fake_relations.relations = []

        with patch("app.services.graph.entity_extraction.EntityExtractionService") as EntSvc, \
             patch("app.services.graph.relation_extraction.RelationExtractionService") as RelSvc:
            ent_instance = MagicMock()
            ent_instance.extract_entities = AsyncMock(return_value=fake_entities)
            EntSvc.return_value = ent_instance

            rel_instance = MagicMock()
            rel_instance.extract_relations = AsyncMock(return_value=fake_relations)
            RelSvc.return_value = rel_instance

            builder = GraphBuilderService(connector=connector, auto_init_schema=False)
            result = await builder.build_graph_from_text(
                text="Kquery 使用 RAG 技术",
                doc_id="doc-test-1",
                kb_id=1,
                use_agent=False,
                detect_communities=False,
            )
            assert result is not None

        await connector.close()
