"""PostgresUserStore against a real database with the migrations applied (0003_users)."""

from collections.abc import AsyncIterator

import pytest

from chatbot.accounts.users import EmailTaken, PostgresUserStore
from chatbot.config import Settings
from chatbot.storage.db import Pool, open_pool

pytestmark = pytest.mark.integration


@pytest.fixture
async def pool(database_url: str) -> AsyncIterator[Pool]:
    pool = await open_pool(Settings(database_url=database_url))
    try:
        yield pool
    finally:
        await pool.close()


async def test_create_authenticate_and_get(pool: Pool) -> None:
    users = PostgresUserStore(pool)
    maya = await users.create("Maya@Example.com", "s3cret-pass")
    assert maya.email == "maya@example.com"

    assert await users.authenticate("MAYA@example.com", "s3cret-pass") == maya
    assert await users.authenticate("maya@example.com", "wrong") is None
    assert await users.authenticate("nobody@example.com", "s3cret-pass") is None

    assert await users.get(maya.id) == maya
    assert await users.get("alice") is None  # not a uuid: an external provider's user

    with pytest.raises(EmailTaken):
        await users.create("maya@EXAMPLE.com", "another-pass")

    await users.delete(maya.id)
    assert await users.get(maya.id) is None
    assert await users.authenticate("maya@example.com", "s3cret-pass") is None


async def test_the_password_is_stored_as_an_argon2_hash(pool: Pool) -> None:
    await PostgresUserStore(pool).create("dana@example.com", "s3cret-pass")
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT password_hash FROM users WHERE email = 'dana@example.com'")
        row = await cur.fetchone()
    assert row is not None and row["password_hash"].startswith("$argon2id$")
