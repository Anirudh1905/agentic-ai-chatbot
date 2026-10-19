"""Bring the database up to date, once per deploy, before the new version starts.

Usage: python -m chatbot.migrate

1. Alembic (`migrations/`): chunks, threads, users.
2. The checkpointer's and the store's own tables, which LangGraph versions itself
   (`setup()`), including the memories' vector index.

Each step is idempotent, so running it again is harmless. In Kubernetes it's the migrate Job,
which the API and the worker wait for; locally `make migrate` and Tilt run it. No process
creates tables at start (Phase 9).
"""

import asyncio
import logging
import os
import sys

from alembic import command
from alembic.config import Config
from langchain_core.embeddings import DeterministicFakeEmbedding

from chatbot.config import get_settings
from chatbot.observability.logs import configure_logging
from chatbot.storage.db import open_pool
from chatbot.storage.langgraph import build_checkpointer, build_store

logger = logging.getLogger("chatbot.migrate")


async def setup_langgraph() -> None:
    settings = get_settings()
    pool = await open_pool(settings)
    try:
        await build_checkpointer(pool).setup()
        # Creating the tables never embeds anything, so this needs no model key.
        embed = DeterministicFakeEmbedding(size=settings.embedding_dimensions)
        await build_store(pool, settings, embed).setup()
    finally:
        await pool.close()


def main() -> int:
    configure_logging(get_settings())
    command.upgrade(Config(os.environ.get("ALEMBIC_CONFIG", "alembic.ini")), "head")
    logger.info("alembic: at head")
    asyncio.run(setup_langgraph())
    logger.info("checkpointer and store tables: up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
