"""design_intents — structured product+design brief per project

Holds the LangGraph pre-agent's output: the 18-key `intent_jsonb`, the
compiled English `compiled_brief`, and the list of Pinterest refs used as
visual seeds. One row per discover turn; the current one is marked
`status='active'`, prior rows are flipped to `'superseded'` atomically
when the user re-discovers.

Revision ID: 0015_design_intents
Revises: 0014_clarifications
Create Date: 2026-04-17 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0015_design_intents"
down_revision: str | None = "0014_clarifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "design_intents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
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
        sa.Column("intent_jsonb", postgresql.JSONB(), nullable=False),
        sa.Column("compiled_brief", sa.Text(), nullable=False),
        sa.Column(
            "pinterest_refs_jsonb",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "pinterest_queries_jsonb",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # At most one active row per project. Partial unique index lets us keep
    # historical superseded rows without fighting the index.
    op.create_index(
        "ux_design_intents_project_active",
        "design_intents",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_design_intents_turn_id",
        "design_intents",
        ["turn_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_design_intents_turn_id", table_name="design_intents")
    op.drop_index("ux_design_intents_project_active", table_name="design_intents")
    op.drop_table("design_intents")
