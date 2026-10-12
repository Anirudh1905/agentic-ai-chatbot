"""Usage: python -m chatbot.ingestion PATH [--prune]"""

import argparse
import asyncio
import sys
from pathlib import Path

from chatbot.config import get_settings
from chatbot.db import open_pool
from chatbot.embeddings import build_embeddings
from chatbot.ingestion.pipeline import ingest_path
from chatbot.observability import configure_logging


async def main(path: Path, prune: bool) -> int:
    settings = get_settings()
    configure_logging(settings)
    pool = await open_pool(settings)
    try:
        results = await ingest_path(pool, build_embeddings(settings), settings, path, prune)
    finally:
        await pool.close()
    added = sum(r.added for r in results)
    print(
        f"Ingested {len(results)} files: {added} chunks embedded, "
        f"{sum(r.kept for r in results)} unchanged, {sum(r.removed for r in results)} removed."
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python -m chatbot.ingestion")
    parser.add_argument("path", type=Path, help="File or directory to ingest")
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Delete chunks for files under PATH that no longer exist",
    )
    args = parser.parse_args()
    if not args.path.exists():
        sys.exit(f"Not found: {args.path}")
    sys.exit(asyncio.run(main(args.path, args.prune)))
