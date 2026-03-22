from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class VectorMemory:
    """
    Stub for vector-based semantic memory (e.g., ChromaDB, Qdrant).
    Used for retrieving relevant past facts or RAG tasks.
    """

    def __init__(self, collection_name: str = "corpclaw_memory"):
        self.collection_name = collection_name
        logger.info("VectorMemory Stub initialized for collection: %s", self.collection_name)

    def add_fact(self, user_id: str, text: str, metadata: dict[str, Any] | None = None) -> None:
        """Store a semantic fact."""
        # TODO: Implement embeddings generation and storage

    def search(self, user_id: str, query: str, limit: int = 3) -> list[str]:
        """Search for relevant facts based on query semantic similarity."""
        # TODO: Implement semantic search against vector DB
        return []
