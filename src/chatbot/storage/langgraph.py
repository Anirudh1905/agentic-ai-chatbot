"""LangGraph's own Postgres tables: the checkpointer (every graph step) and the store (long-term
memories, with a vector index). Built the same way by the API, the worker and the migrate
command; only `python -m chatbot.migrate` creates their tables (`setup()`)."""

from langchain_core.embeddings import Embeddings
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore

from chatbot.config import Settings
from chatbot.storage.db import Pool


def build_checkpointer(pool: Pool) -> AsyncPostgresSaver:
    return AsyncPostgresSaver(pool)


def build_store(pool: Pool, settings: Settings, embeddings: Embeddings) -> AsyncPostgresStore:
    """Long-term memory (memory/long_term/), searched by meaning. Temporary items get a TTL:
    reads skip them once expired, and the sweeper deletes them."""
    return AsyncPostgresStore(
        pool,
        index={
            "dims": settings.embedding_dimensions,
            "embed": embeddings,
            "fields": ["content"],
        },
        ttl={"refresh_on_read": False, "omit_expired": True, "sweep_interval_minutes": 60},
    )
