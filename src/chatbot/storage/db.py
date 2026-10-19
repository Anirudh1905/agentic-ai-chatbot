from pgvector.psycopg import register_vector_async
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from chatbot.config import Settings

type Conn = AsyncConnection[DictRow]
type Pool = AsyncConnectionPool[Conn]


async def _configure(conn: Conn) -> None:
    await register_vector_async(conn)


async def open_pool(settings: Settings) -> Pool:
    pool: Pool = AsyncConnectionPool(
        settings.database_url,
        connection_class=AsyncConnection[DictRow],
        min_size=1,
        max_size=10,
        kwargs={"row_factory": dict_row, "autocommit": True},
        configure=_configure,
        open=False,
    )
    await pool.open(wait=True, timeout=10)
    return pool
