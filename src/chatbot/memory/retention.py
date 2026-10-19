"""Prune checkpoint history of idle threads.

Every graph step writes a checkpoint, so a thread accumulates dozens per turn. Only the
latest is needed to continue a conversation; older ones are for debugging and time travel.
For threads idle longer than CHECKPOINT_RETENTION_DAYS this keeps the latest checkpoint
(plus its pending writes, which an interrupted run needs to resume) and deletes the rest.

Usage: python -m chatbot.memory.retention [--idle-days N] [--dry-run]

Run it nightly (cron, a Kubernetes CronJob, ...). Deleting a whole thread is the
DELETE /threads/{id} endpoint, not this job.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

from chatbot.config import get_settings
from chatbot.memory.threads import PostgresThreadStore
from chatbot.observability.logs import configure_logging
from chatbot.storage.db import Pool, open_pool

# Checkpoint IDs are time-ordered UUIDv6 strings, so the highest one is the latest.
# This is only safe because the graph has no DeltaChannel state, which would need the
# parent chain back to the last snapshot to reconstruct.
DELETE_OLD_CHECKPOINTS_SQL = """
    DELETE FROM checkpoints c
    WHERE c.thread_id = ANY(%(ids)s)
      AND c.checkpoint_id < (
          SELECT max(l.checkpoint_id) FROM checkpoints l
          WHERE l.thread_id = c.thread_id AND l.checkpoint_ns = c.checkpoint_ns
      )
"""

DELETE_ORPHAN_WRITES_SQL = """
    DELETE FROM checkpoint_writes w
    WHERE w.thread_id = ANY(%(ids)s)
      AND NOT EXISTS (
          SELECT 1 FROM checkpoints c
          WHERE c.thread_id = w.thread_id
            AND c.checkpoint_ns = w.checkpoint_ns
            AND c.checkpoint_id = w.checkpoint_id
      )
"""

# A blob holds one version of one channel (e.g. the message list after step n); keep the
# versions the remaining checkpoints point to.
DELETE_ORPHAN_BLOBS_SQL = """
    DELETE FROM checkpoint_blobs b
    WHERE b.thread_id = ANY(%(ids)s)
      AND NOT EXISTS (
          SELECT 1 FROM checkpoints c, jsonb_each_text(c.checkpoint -> 'channel_versions') v
          WHERE c.thread_id = b.thread_id
            AND c.checkpoint_ns = b.checkpoint_ns
            AND v.key = b.channel
            AND v.value = b.version
      )
"""


async def prune_checkpoints(pool: Pool, thread_ids: Sequence[UUID]) -> int:
    """Keep only the latest checkpoint of each thread. Returns how many were deleted."""
    if not thread_ids:
        return 0
    params = {"ids": [str(t) for t in thread_ids]}
    async with pool.connection() as conn, conn.transaction():
        deleted = (await conn.execute(DELETE_OLD_CHECKPOINTS_SQL, params)).rowcount
        await conn.execute(DELETE_ORPHAN_WRITES_SQL, params)
        await conn.execute(DELETE_ORPHAN_BLOBS_SQL, params)
    return deleted


async def main(idle_days: int, dry_run: bool) -> int:
    settings = get_settings()
    configure_logging(settings)
    pool = await open_pool(settings)
    try:
        cutoff = datetime.now(UTC) - timedelta(days=idle_days)
        thread_ids = await PostgresThreadStore(pool).idle_since(cutoff)
        if dry_run:
            print(f"{len(thread_ids)} threads idle for more than {idle_days} days.")
            return 0
        deleted = await prune_checkpoints(pool, thread_ids)
    finally:
        await pool.close()
    print(f"Pruned {deleted} checkpoints from {len(thread_ids)} threads idle > {idle_days} days.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python -m chatbot.memory.retention")
    parser.add_argument(
        "--idle-days",
        type=int,
        default=get_settings().checkpoint_retention_days,
        help="Prune threads with no activity for this many days",
    )
    parser.add_argument("--dry-run", action="store_true", help="Only count the idle threads")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.idle_days, args.dry_run)))
