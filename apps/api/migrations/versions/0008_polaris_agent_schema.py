"""polaris_agent schema: projects.codex_thread_id + turns + turn_items, drop agent_runs/agent_steps

Replaces the three-agent orchestration schema (agent_runs holding one fire-and-forget
PM request plus a short fixed-length agent_steps list) with a conversational model
aligned to Codex's own thread/turn/item hierarchy:

  Project  ──1:1──►  codex_thread_id        (one Codex thread per project)
      │
      └── 1:N ──►  Turn  (one per user message; = one Codex turn)
                     │
                     └── 1:N ──►  TurnItem  (one per Codex item: plan, command
                                             exec, file change, mcp tool call,
                                             dynamic tool call, agent message…)

workspace_exec_audits keeps its row-per-exec audit, but its FK is renamed
`agent_run_id → turn_id`.

Revision ID: 0008_polaris_agent_schema
Revises: 0007_drop_workspace_ide_port
Create Date: 2026-04-14 21:50:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0008_polaris_agent_schema"
down_revision: str | Sequence[str] | None = "0007_drop_workspace_ide_port"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. projects.codex_thread_id ------------------------------------------------
    op.add_column(
        "projects",
        sa.Column("codex_thread_id", sa.String(length=100), nullable=True),
    )

    # 2. turns --------------------------------------------------------------------
    op.create_table(
        "turns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
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
        sa.Column("codex_turn_id", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("final_message", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("cost_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("metadata_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_turns_project_id", "turns", ["project_id"])
    op.create_index("ix_turns_workspace_id", "turns", ["workspace_id"])
    op.create_index(
        "ix_turns_project_sequence",
        "turns",
        ["project_id", "sequence"],
        unique=True,
    )
    op.create_index(
        "ix_turns_project_created",
        "turns",
        ["project_id", sa.text("created_at DESC")],
    )

    # 3. turn_items ---------------------------------------------------------------
    op.create_table(
        "turn_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "turn_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("turns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("codex_item_id", sa.String(length=100), nullable=True),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="started"),
        sa.Column("payload_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
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
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_turn_items_turn_id", "turn_items", ["turn_id"])
    op.create_index(
        "ix_turn_items_sequence",
        "turn_items",
        ["turn_id", "sequence"],
        unique=True,
    )
    op.create_index("ix_turn_items_kind", "turn_items", ["kind"])
    op.create_index(
        "ix_turn_items_codex_id",
        "turn_items",
        ["codex_item_id"],
        postgresql_where=sa.text("codex_item_id IS NOT NULL"),
    )

    # 4. workspace_exec_audits: rename agent_run_id → turn_id, rebuild FK --------
    # drop the old FK constraint first (named automatically by postgres)
    op.execute(
        "ALTER TABLE workspace_exec_audits "
        "DROP CONSTRAINT IF EXISTS workspace_exec_audits_agent_run_id_fkey"
    )
    op.alter_column(
        "workspace_exec_audits",
        "agent_run_id",
        new_column_name="turn_id",
    )
    op.create_foreign_key(
        "workspace_exec_audits_turn_id_fkey",
        "workspace_exec_audits",
        "turns",
        ["turn_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_index(
        "ix_workspace_exec_audits_agent_run_id",
        table_name="workspace_exec_audits",
        if_exists=True,
    )
    op.create_index(
        "ix_workspace_exec_audits_turn_id",
        "workspace_exec_audits",
        ["turn_id"],
    )

    # 5. drop legacy agent_runs / agent_steps ------------------------------------
    op.drop_table("agent_steps")
    op.drop_table("agent_runs")


def downgrade() -> None:
    # Re-create agent_runs / agent_steps skeletons (no data migration).
    op.create_table(
        "agent_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("agent_type", sa.String(length=50), nullable=False, server_default="pm"),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="queued"),
        sa.Column("input_summary", sa.Text(), nullable=False),
        sa.Column("output_summary", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cost_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("metadata_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
    )
    op.create_table(
        "agent_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("step_type", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="pending"),
        sa.Column("payload_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("run_id", "step_index", name="uq_agent_steps_run_step_index"),
    )
    op.execute(
        "ALTER TABLE workspace_exec_audits "
        "DROP CONSTRAINT IF EXISTS workspace_exec_audits_turn_id_fkey"
    )
    op.drop_index("ix_workspace_exec_audits_turn_id", table_name="workspace_exec_audits", if_exists=True)
    op.alter_column("workspace_exec_audits", "turn_id", new_column_name="agent_run_id")
    op.create_foreign_key(
        "workspace_exec_audits_agent_run_id_fkey",
        "workspace_exec_audits",
        "agent_runs",
        ["agent_run_id"],
        ["id"],
    )
    op.create_index(
        "ix_workspace_exec_audits_agent_run_id",
        "workspace_exec_audits",
        ["agent_run_id"],
    )
    op.drop_index("ix_turn_items_codex_id", table_name="turn_items", if_exists=True)
    op.drop_index("ix_turn_items_kind", table_name="turn_items")
    op.drop_index("ix_turn_items_sequence", table_name="turn_items")
    op.drop_index("ix_turn_items_turn_id", table_name="turn_items")
    op.drop_table("turn_items")
    op.drop_index("ix_turns_project_created", table_name="turns")
    op.drop_index("ix_turns_project_sequence", table_name="turns")
    op.drop_index("ix_turns_workspace_id", table_name="turns")
    op.drop_index("ix_turns_project_id", table_name="turns")
    op.drop_table("turns")
    op.drop_column("projects", "codex_thread_id")
