"""python -m chatbot.migrate: Alembic, then the checkpointer's and the store's own tables, so no
process creates tables at start (Phase 9). The database fixture runs it once; it runs again
here, which must be harmless."""

import subprocess
import sys

import psycopg
import pytest

pytestmark = pytest.mark.integration

TABLES = {
    "chunks",
    "threads",
    "users",
    "checkpoints",
    "checkpoint_writes",
    "checkpoint_blobs",
    "store",
    "store_vectors",
}


def test_migrate_creates_every_table_and_can_run_again(database_url: str) -> None:
    env = {"DATABASE_URL": database_url, "PATH": ""}
    again = subprocess.run(
        [sys.executable, "-m", "chatbot.migrate"], env=env, capture_output=True, text=True
    )
    assert again.returncode == 0, again.stderr

    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
    assert {r[0] for r in rows} >= TABLES
