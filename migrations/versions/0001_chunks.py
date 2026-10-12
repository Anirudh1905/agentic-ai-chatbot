"""Knowledge base chunks with vector and full-text indexes.

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Must match Settings.embedding_dimensions. Changing the embedding model to one with a
# different size needs a new migration and a full re-ingest.
EMBEDDING_DIMENSIONS = 1536


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(f"""
        CREATE TABLE chunks (
            id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            doc_id       text NOT NULL,
            source_uri   text NOT NULL,
            title        text NOT NULL,
            heading      text,
            page         int,
            chunk_index  int NOT NULL,
            content      text NOT NULL,
            content_tsv  tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
            embedding    vector({EMBEDDING_DIMENSIONS}) NOT NULL,
            metadata     jsonb NOT NULL DEFAULT '{{}}',
            content_hash text NOT NULL,
            created_at   timestamptz NOT NULL DEFAULT now(),
            UNIQUE (doc_id, content_hash)
        )
    """)
    op.execute(
        "CREATE INDEX chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute("CREATE INDEX chunks_content_tsv_idx ON chunks USING gin (content_tsv)")
    op.execute("CREATE INDEX chunks_doc_id_idx ON chunks (doc_id)")


def downgrade() -> None:
    op.execute("DROP TABLE chunks")
