"""Accounts: an email and an argon2id password hash per user (the `users` table).

The login endpoints create and check accounts, and every request looks its token's user up
here (api/auth.py), so a deleted account's token stops working.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from psycopg.errors import UniqueViolation
from pwdlib import PasswordHash

from chatbot.db import Pool

password_hash = PasswordHash.recommended()  # argon2id
# Checked when the email is unknown, so a miss takes as long as a wrong password and the
# response time doesn't reveal which emails are registered.
DUMMY_HASH = password_hash.hash("not-a-real-password")


@dataclass(frozen=True)
class User:
    id: str
    email: str


class EmailTaken(Exception):
    pass


class UserStore(Protocol):
    async def create(self, email: str, password: str) -> User:
        """Raises EmailTaken if the email is already registered."""
        ...

    async def authenticate(self, email: str, password: str) -> User | None:
        """The user, if the email is registered and the password is theirs."""
        ...

    async def get(self, user_id: str) -> User | None: ...


class PostgresUserStore:
    def __init__(self, pool: Pool) -> None:
        self.pool = pool

    async def create(self, email: str, password: str) -> User:
        try:
            async with self.pool.connection() as conn:
                cur = await conn.execute(
                    "INSERT INTO users (email, password_hash) VALUES (%s, %s) "
                    "RETURNING id::text, email",
                    (email.lower(), password_hash.hash(password)),
                )
                row = await cur.fetchone()
        except UniqueViolation as e:
            raise EmailTaken(email) from e
        assert row is not None
        return User(**row)

    async def authenticate(self, email: str, password: str) -> User | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id::text, email, password_hash FROM users WHERE email = %s",
                (email.lower(),),
            )
            row = await cur.fetchone()
        ok = password_hash.verify(password, row["password_hash"] if row else DUMMY_HASH)
        return User(row["id"], row["email"]) if row and ok else None

    async def get(self, user_id: str) -> User | None:
        try:
            UUID(user_id)
        except ValueError:
            return None  # not one of our ids
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT id::text, email FROM users WHERE id = %s", (user_id,))
            row = await cur.fetchone()
        return User(**row) if row else None
