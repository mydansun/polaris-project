"""Session / AgentRun / Event abstraction

Drop the Codex-coupled turns / turn_items tables and replace them with the
Polaris-owned Session → AgentRun → Event hierarchy.  Clarifications and
design_intents migrate to reference the new ids.

User confirmed a destructive reset is acceptable; no data-preservation step.

Revision ID: 0016_session_run_event
Revises: 0015_design_intents
Create Date: 2026-04-17 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0016_session_run_event"
down_revision: str | None = "0015_design_intents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Drop legacy turn tables ────────────────────────────────────────
    # Cascade any dependencies that pointed at turn_id so we can rebuild
    # them pointing at session_id / run_id.
    op.execute("DROP TABLE IF EXISTS turn_items CASCADE")
    op.execute("DROP TABLE IF EXISTS turns CASCADE")

    # ── sessions: one row per user message ─────────────────────────────
    op.create_table(
        "sessions",
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
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("user_message", sa.Text(), nullable=False),
        # build_planned | build_direct | discover_then_build
        sa.Column("mode", sa.String(length=30), nullable=False),
        # queued | running | completed | interrupted | failed
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("final_message", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "metadata_jsonb",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "cost_jsonb",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("project_id", "sequence", name="uq_sessions_project_sequence"),
    )
    op.create_index("ix_sessions_project_id", "sessions", ["project_id"])
    op.create_index("ix_sessions_workspace_id", "sessions", ["workspace_id"])

    # ── agent_runs: one row per agent execution inside a session ───────
    op.create_table(
        "agent_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        # codex | discovery (extensible)
        sa.Column("agent_kind", sa.String(length=30), nullable=False),
        # queued | running | completed | failed | skipped
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        # codex_turn_id for codex runs, null otherwise
        sa.Column("external_id", sa.String(length=100), nullable=True),
        sa.Column(
            "input_jsonb",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "output_jsonb",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "session_id", "sequence", name="uq_agent_runs_session_sequence"
        ),
    )
    op.create_index("ix_agent_runs_session_id", "agent_runs", ["session_id"])

    # ── events: atomic progress items inside a run ─────────────────────
    op.create_table(
        "events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        # codex_item_id for codex events, null for discovery synthetic events
        sa.Column("external_id", sa.String(length=100), nullable=True),
        # "codex:agent_message" | "codex:plan" | "discovery:clarifying" | ...
        sa.Column("kind", sa.String(length=80), nullable=False),
        # started | completed | failed
        sa.Column("status", sa.String(length=20), nullable=False, server_default="started"),
        sa.Column(
            "payload_jsonb",
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("run_id", "sequence", name="uq_events_run_sequence"),
    )
    op.create_index("ix_events_run_id", "events", ["run_id"])

    # ── clarifications: rebind from turn → (session, run) ──────────────
    op.drop_index("ix_clarifications_turn_id", table_name="clarifications")
    op.drop_column("clarifications", "turn_id")
    op.add_column(
        "clarifications",
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    op.add_column(
        "clarifications",
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    op.add_column(
        "clarifications",
        sa.Column("agent_kind", sa.String(length=30), nullable=False),
    )
    op.create_index("ix_clarifications_run_id", "clarifications", ["run_id"])

    # ── design_intents: turn_id → session_id ───────────────────────────
    op.drop_index("ix_design_intents_turn_id", table_name="design_intents")
    op.drop_column("design_intents", "turn_id")
    op.add_column(
        "design_intents",
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    op.create_index("ix_design_intents_session_id", "design_intents", ["session_id"])


def downgrade() -> None:
    # Destructive migration — downgrade is not expected to preserve data.
    # Provided only for completeness / test rollback scenarios.
    op.drop_index("ix_design_intents_session_id", table_name="design_intents")
    op.drop_column("design_intents", "session_id")
    op.add_column(
        "design_intents",
        sa.Column(
            "turn_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )

    op.drop_index("ix_clarifications_run_id", table_name="clarifications")
    op.drop_column("clarifications", "agent_kind")
    op.drop_column("clarifications", "run_id")
    op.drop_column("clarifications", "session_id")
    op.add_column(
        "clarifications",
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_clarifications_turn_id", "clarifications", ["turn_id"])

    op.drop_index("ix_events_run_id", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_agent_runs_session_id", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index("ix_sessions_workspace_id", table_name="sessions")
    op.drop_index("ix_sessions_project_id", table_name="sessions")
    op.drop_table("sessions")
    # Legacy turns/turn_items are not restored — they were dropped
    # destructively.  Rollback to a pre-0016 state is a DB wipe.
