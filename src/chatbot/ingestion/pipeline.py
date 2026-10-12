import logging
from dataclasses import dataclass
from pathlib import Path

from langchain_core.embeddings import Embeddings
from psycopg.types.json import Jsonb

from chatbot.config import Settings
from chatbot.db import Conn, Pool
from chatbot.ingestion.chunking import Chunk, chunk_document
from chatbot.ingestion.loaders import is_supported, load

logger = logging.getLogger(__name__)

EMBED_BATCH_SIZE = 64


@dataclass
class DocResult:
    doc_id: str
    added: int = 0
    kept: int = 0
    removed: int = 0


def doc_id_for(path: Path) -> str:
    """Stable ID for a file: its path relative to the working directory when possible."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(Path.cwd()).as_posix()
    except ValueError:
        return resolved.as_posix()


def discover(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if is_supported(root) else []
    return sorted(p for p in root.rglob("*") if p.is_file() and is_supported(p))


async def _embed(embeddings: Embeddings, chunks: list[Chunk]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for start in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[start : start + EMBED_BATCH_SIZE]
        vectors.extend(await embeddings.aembed_documents([c.embed_text for c in batch]))
    return vectors


async def ingest_file(
    conn: Conn, embeddings: Embeddings, settings: Settings, path: Path
) -> DocResult:
    doc = load(path)
    doc_id = doc_id_for(path)
    chunks = chunk_document(doc, settings.chunk_size_tokens, settings.chunk_overlap_tokens)
    result = DocResult(doc_id)

    cur = await conn.execute("SELECT content_hash FROM chunks WHERE doc_id = %s", (doc_id,))
    existing = {row["content_hash"] for row in await cur.fetchall()}
    wanted = {c.content_hash for c in chunks}

    # Only embed chunks whose text changed; unchanged ones are kept as-is.
    new_chunks = [c for c in chunks if c.content_hash not in existing]
    vectors = await _embed(embeddings, new_chunks)

    async with conn.transaction():
        if new_chunks:
            async with conn.cursor() as c:
                await c.executemany(
                    """
                    INSERT INTO chunks (doc_id, source_uri, title, heading, page, chunk_index,
                                        content, embedding, metadata, content_hash)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (doc_id, content_hash) DO NOTHING
                    """,
                    [
                        (
                            doc_id,
                            path.resolve().as_uri(),
                            doc.title,
                            chunk.heading,
                            chunk.page,
                            chunk.index,
                            chunk.content,
                            vector,
                            Jsonb({"format": path.suffix.lower().lstrip(".")}),
                            chunk.content_hash,
                        )
                        for chunk, vector in zip(new_chunks, vectors, strict=True)
                    ],
                )
        kept = [c for c in chunks if c.content_hash in existing]
        if kept:
            async with conn.cursor() as c:
                await c.executemany(
                    "UPDATE chunks SET chunk_index = %s, title = %s"
                    " WHERE doc_id = %s AND content_hash = %s",
                    [(k.index, doc.title, doc_id, k.content_hash) for k in kept],
                )
        stale = list(existing - wanted)
        if stale:
            await conn.execute(
                "DELETE FROM chunks WHERE doc_id = %s AND content_hash = ANY(%s)", (doc_id, stale)
            )

    result.added, result.kept, result.removed = len(new_chunks), len(kept), len(stale)
    return result


async def prune_missing(conn: Conn, root: Path, present: set[str]) -> list[str]:
    """Delete chunks for files under `root` that no longer exist."""
    prefix = doc_id_for(root).rstrip("/") + "/"
    cur = await conn.execute(
        "SELECT DISTINCT doc_id FROM chunks WHERE doc_id LIKE %s",
        (prefix.replace("%", r"\%") + "%",),
    )
    gone = [r["doc_id"] for r in await cur.fetchall() if r["doc_id"] not in present]
    if gone:
        await conn.execute("DELETE FROM chunks WHERE doc_id = ANY(%s)", (gone,))
    return gone


async def ingest_path(
    pool: Pool,
    embeddings: Embeddings,
    settings: Settings,
    root: Path,
    prune: bool = False,
) -> list[DocResult]:
    files = discover(root)
    results = []
    async with pool.connection() as conn:
        for path in files:
            result = await ingest_file(conn, embeddings, settings, path)
            logger.info(
                "%s: +%d added, %d kept, -%d removed",
                result.doc_id,
                result.added,
                result.kept,
                result.removed,
            )
            results.append(result)
        if prune and root.is_dir():
            for doc_id in await prune_missing(conn, root, {r.doc_id for r in results}):
                logger.info("%s: removed (file no longer exists)", doc_id)
    return results
