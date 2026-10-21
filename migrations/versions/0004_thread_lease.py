"""The per-thread run lease (Phase 10): one run at a time per thread, across every API pod.

`run_id` is the run holding the lease, `run_until` when it expires unless renewed
(api/leases.py). Both are NULL while no run is going.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE threads ADD COLUMN run_id uuid, ADD COLUMN run_until timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE threads DROP COLUMN run_id, DROP COLUMN run_until")
