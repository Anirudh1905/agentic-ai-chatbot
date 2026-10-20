"""Thread metadata (owner, title, last activity). Messages live in the checkpointer."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from chatbot.storage.db import Pool

TITLE_CHARS = 80


@dataclass(frozen=True)
class Thread:
    id: UUID
    user_id: str
    title: str | None
    created_at: datetime
    updated_at: datetime


def make_title(message: str) -> str:
    title = " ".join(message.split())
    return title if len(title) <= TITLE_CHARS else title[: TITLE_CHARS - 1] + "…"


class ThreadStore(Protocol):
    async def create(self, thread_id: UUID, user_id: str, title: str | None) -> Thread: ...

    async def claim(self, thread_id: UUID, user_id: str, title: str | None) -> Thread | None:
        """Create the thread for `user_id` if it's new, or mark it active if they own it.

        Returns None if another user owns it.
        """
        ...

    async def get(self, thread_id: UUID, user_id: str) -> Thread | None:
        """The thread, or None if it doesn't exist or belongs to someone else."""
        ...

    async def list_for_user(self, user_id: str, limit: int) -> list[Thread]: ...

    async def delete(self, thread_id: UUID) -> None: ...

    async def lease(self, thread_id: UUID, user_id: str, run_id: UUID, seconds: float) -> bool:
        """Take the thread's run lease for `run_id` (Phase 9, api/leases.py). False if another
        run holds one that hasn't expired, or the thread isn't the user's."""
        ...

    async def renew(self, thread_id: UUID, run_id: UUID, seconds: float) -> bool:
        """Extend the lease by `seconds` from now. False if `run_id` no longer holds it."""
        ...

    async def release(self, thread_id: UUID, run_id: UUID) -> None:
        """Give the lease up, if `run_id` still holds it."""
        ...

    async def ids_for_user(self, user_id: str) -> list[UUID]:
        """Every thread of the user's (deleting their data)."""
        ...

    async def idle_since(self, cutoff: datetime) -> list[UUID]: ...

    async def idle_between(self, since: datetime, until: datetime) -> list[Thread]:
        """Threads last active between `since` and `until`, oldest first (so a user's are
        pushed in the order they happened)."""
        ...


COLUMNS = "id, user_id, title, created_at, updated_at"


class PostgresThreadStore:
    def __init__(self, pool: Pool) -> None:
        self.pool = pool

    async def create(self, thread_id: UUID, user_id: str, title: str | None) -> Thread:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"INSERT INTO threads (id, user_id, title) VALUES (%s, %s, %s) RETURNING {COLUMNS}",
                (thread_id, user_id, title),
            )
            row = await cur.fetchone()
        assert row is not None
        return Thread(**row)

    async def claim(self, thread_id: UUID, user_id: str, title: str | None) -> Thread | None:
        # One statement, so two first messages racing on a new thread can't both create it.
        # The WHERE makes the conflict a no-op (no row returned) for someone else's thread.
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"""
                INSERT INTO threads (id, user_id, title) VALUES (%s, %s, %s)
                ON CONFLICT (id) DO UPDATE
                    SET updated_at = now(), title = coalesce(threads.title, EXCLUDED.title)
                    WHERE threads.user_id = EXCLUDED.user_id
                RETURNING {COLUMNS}
                """,
                (thread_id, user_id, title),
            )
            row = await cur.fetchone()
        return Thread(**row) if row else None

    async def get(self, thread_id: UUID, user_id: str) -> Thread | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {COLUMNS} FROM threads WHERE id = %s AND user_id = %s",
                (thread_id, user_id),
            )
            row = await cur.fetchone()
        return Thread(**row) if row else None

    async def list_for_user(self, user_id: str, limit: int) -> list[Thread]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {COLUMNS} FROM threads WHERE user_id = %s "
                "ORDER BY updated_at DESC LIMIT %s",
                (user_id, limit),
            )
            rows = await cur.fetchall()
        return [Thread(**r) for r in rows]

    async def delete(self, thread_id: UUID) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM threads WHERE id = %s", (thread_id,))

    async def lease(self, thread_id: UUID, user_id: str, run_id: UUID, seconds: float) -> bool:
        # One conditional UPDATE, so two runs racing for the thread can't both take it.
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                UPDATE threads SET run_id = %s, run_until = now() + make_interval(secs => %s)
                WHERE id = %s AND user_id = %s AND (run_until IS NULL OR run_until < now())
                RETURNING id
                """,
                (run_id, seconds, thread_id, user_id),
            )
            return await cur.fetchone() is not None

    async def renew(self, thread_id: UUID, run_id: UUID, seconds: float) -> bool:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE threads SET run_until = now() + make_interval(secs => %s) "
                "WHERE id = %s AND run_id = %s RETURNING id",
                (seconds, thread_id, run_id),
            )
            return await cur.fetchone() is not None

    async def release(self, thread_id: UUID, run_id: UUID) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE threads SET run_id = NULL, run_until = NULL WHERE id = %s AND run_id = %s",
                (thread_id, run_id),
            )

    async def ids_for_user(self, user_id: str) -> list[UUID]:
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT id FROM threads WHERE user_id = %s", (user_id,))
            rows = await cur.fetchall()
        return [r["id"] for r in rows]

    async def idle_since(self, cutoff: datetime) -> list[UUID]:
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT id FROM threads WHERE updated_at < %s", (cutoff,))
            rows = await cur.fetchall()
        return [r["id"] for r in rows]

    async def idle_between(self, since: datetime, until: datetime) -> list[Thread]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {COLUMNS} FROM threads WHERE updated_at >= %s AND updated_at < %s "
                "ORDER BY updated_at",
                (since, until),
            )
            rows = await cur.fetchall()
        return [Thread(**r) for r in rows]
