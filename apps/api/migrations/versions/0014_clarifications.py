"""clarifications table — decouple from turn_items

The in-container `polaris clarify` CLI and the worker's turn-items sink
both write to turn_items, racing on the per-turn sequence unique index.
Move clarifications to their own table so they no longer share the
(turn_id, sequence) namespace.

Revision ID: 0014_clarifications
Revises: 0013_email_auth
Create Date: 2026-04-16 20:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0014_clarifications"
down_revision: str | None = "0013_email_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "clarifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("request_id", sa.String(length=100), nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "turn_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("turns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("questions_jsonb", postgresql.JSONB(), nullable=False),
        sa.Column(
            "answers_jsonb",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("request_id", name="uq_clarifications_request_id"),
    )
    op.create_index(
        "ix_clarifications_project_pending",
        "clarifications",
        ["project_id"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_clarifications_turn_id",
        "clarifications",
        ["turn_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_clarifications_turn_id", table_name="clarifications")
    op.drop_index("ix_clarifications_project_pending", table_name="clarifications")
    op.drop_table("clarifications")
