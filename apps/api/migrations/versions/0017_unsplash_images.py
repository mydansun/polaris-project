"""Unsplash image re-hosting dedupe table

One row per (Unsplash photo_id, size) pair we've downloaded and
uploaded to our S3.  Lets the platform-side Unsplash MCP handler skip
re-uploading an image that's already in our bucket.

Revision ID: 0017_unsplash_images
Revises: 0016_session_run_event
Create Date: 2026-04-18 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0017_unsplash_images"
down_revision: str | None = "0016_session_run_event"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "unsplash_images",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Unsplash's opaque photo id (e.g. "abc123xyz").
        sa.Column("photo_id", sa.Text(), nullable=False),
        # "regular" | "small" — per the architecture we don't cache the other
        # sizes (thumb/full/raw).
        sa.Column("size", sa.String(length=20), nullable=False),
        # Object key inside the configured S3 bucket, e.g.
        # "static/images/up/<uuid4>.jpg".
        sa.Column("s3_key", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("photo_id", "size", name="uq_unsplash_photo_size"),
    )
    op.create_index("ix_unsplash_photo_id", "unsplash_images", ["photo_id"])


def downgrade() -> None:
    op.drop_index("ix_unsplash_photo_id", table_name="unsplash_images")
    op.drop_table("unsplash_images")
