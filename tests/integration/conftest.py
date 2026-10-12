"""Integration tests run against a real Postgres + pgvector.

Each test module gets a throwaway database with the migrations applied, dropped afterwards.
Skipped when Postgres isn't reachable, unless REQUIRE_DB=1 (set in CI).
"""

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[2]
ADMIN_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://chatbot:chatbot@localhost:5433/chatbot"
)


@pytest.fixture(scope="module")
def database_url() -> Iterator[str]:
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as e:
        if os.environ.get("REQUIRE_DB") == "1":
            raise
        pytest.skip(f"Postgres not reachable at {ADMIN_URL}: {e}")

    name = f"chatbot_test_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    url = ADMIN_URL.rsplit("/", 1)[0] + f"/{name}"
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env={**os.environ, "DATABASE_URL": url},
            check=True,
            capture_output=True,
        )
        yield url
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()
