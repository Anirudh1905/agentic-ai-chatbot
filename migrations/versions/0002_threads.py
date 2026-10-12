"""Thread metadata: who owns each conversation.

The conversation state itself lives in the LangGraph checkpoint tables, which
AsyncPostgresSaver.setup() creates and migrates on API startup.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE threads (
            id         uuid PRIMARY KEY,
            user_id    text NOT NULL,
            title      text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX threads_user_id_updated_at_idx ON threads (user_id, updated_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE threads")
