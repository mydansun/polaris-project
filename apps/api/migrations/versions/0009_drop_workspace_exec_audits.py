"""drop workspace_exec_audits table

Codex now runs inside each workspace container and uses its own built-in
exec_command + apply_patch for all tool calls.  The host-side audited
`workspace_exec` dynamic tool (which produced this table's rows) is gone,
so the table has no writers.  turn_items.commandExecution now provides the
equivalent observability (what commands Codex ran during which turn), so
we drop the audit table entirely instead of leaving a dead artifact.

Revision ID: 0009_drop_workspace_exec_audits
Revises: 0008_polaris_agent_schema
Create Date: 2026-04-14 23:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0009_drop_workspace_exec_audits"
down_revision: str | None = "0008_polaris_agent_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        "ix_workspace_exec_audits_project_id",
        table_name="workspace_exec_audits",
        if_exists=True,
    )
    op.drop_index(
        "ix_workspace_exec_audits_workspace_id",
        table_name="workspace_exec_audits",
        if_exists=True,
    )
    op.drop_index(
        "ix_workspace_exec_audits_turn_id",
        table_name="workspace_exec_audits",
        if_exists=True,
    )
    op.drop_table("workspace_exec_audits")


def downgrade() -> None:
    op.create_table(
        "workspace_exec_audits",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column(
            "turn_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("turns.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("requested_by", sa.String(length=80), nullable=False),
        sa.Column("command_jsonb", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("cwd", sa.String(length=1000), nullable=False, server_default="/workspace"),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="running"),
        sa.Column("exit_code", sa.Integer, nullable=True),
        sa.Column("stdout", sa.Text, nullable=True),
        sa.Column("stderr", sa.Text, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("policy_jsonb", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("metadata_jsonb", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_workspace_exec_audits_project_id",
        "workspace_exec_audits",
        ["project_id"],
    )
    op.create_index(
        "ix_workspace_exec_audits_workspace_id",
        "workspace_exec_audits",
        ["workspace_id"],
    )
    op.create_index(
        "ix_workspace_exec_audits_turn_id",
        "workspace_exec_audits",
        ["turn_id"],
    )
