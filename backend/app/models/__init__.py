"""KQuery Pro 数据模型层。

按领域拆分，避免同名冲突。
"""

from app.models.common import StandardResponse
from app.models.document import (
    DocumentProcessResult,
    DocumentProcessRequest,
    DocumentBatchRequest,
    TextAddRequest,
    TextAddResult,
)
from app.models.query import (
    QueryResult,
    QueryRequest,
    CollectionStats,
    RetrievalResult,
    CollectionCreateRequest,
    CollectionDeleteRequest,
)
from app.models.graph import (
    BuildGraphRequest,
    BuildGraphFromCollectionRequest,
    SearchEntitiesRequest,
    SubgraphRequest,
    VisualizationRequest,
    PathQueryRequest,
)
from app.models.graphrag import (
    HybridSearchRequest,
    AskRequest,
    EntityExtractionRequest,
)
from app.models.chat import ChatRequest
from app.models.lineage import DataLineage, DataLineageNode, DataLineageEdge

__all__ = [
    # common
    "StandardResponse",
    # document
    "DocumentProcessResult",
    "DocumentProcessRequest",
    "DocumentBatchRequest",
    "TextAddRequest",
    "TextAddResult",
    # query
    "QueryResult",
    "QueryRequest",
    "CollectionStats",
    "RetrievalResult",
    "CollectionCreateRequest",
    "CollectionDeleteRequest",
    # graph
    "BuildGraphRequest",
    "BuildGraphFromCollectionRequest",
    "SearchEntitiesRequest",
    "SubgraphRequest",
    "VisualizationRequest",
    "PathQueryRequest",
    # graphrag
    "HybridSearchRequest",
    "AskRequest",
    "EntityExtractionRequest",
    # chat
    "ChatRequest",
    # lineage
    "DataLineage",
    "DataLineageNode",
    "DataLineageEdge",
]
