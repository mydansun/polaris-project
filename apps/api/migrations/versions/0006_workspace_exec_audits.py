"""add workspace exec audits

Revision ID: 0006_workspace_exec_audits
Revises: 15af4faf772c
Create Date: 2026-04-13 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_workspace_exec_audits"
down_revision: str | Sequence[str] | None = "15af4faf772c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_exec_audits",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column(
            "agent_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id"),
            nullable=True,
        ),
        sa.Column("requested_by", sa.String(length=80), nullable=False),
        sa.Column("command_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cwd", sa.String(length=1000), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("stdout", sa.Text(), nullable=True),
        sa.Column("stderr", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("policy_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("metadata_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_workspace_exec_audits_project_id", "workspace_exec_audits", ["project_id"])
    op.create_index("ix_workspace_exec_audits_workspace_id", "workspace_exec_audits", ["workspace_id"])
    op.create_index("ix_workspace_exec_audits_agent_run_id", "workspace_exec_audits", ["agent_run_id"])


def downgrade() -> None:
    op.drop_index("ix_workspace_exec_audits_agent_run_id", table_name="workspace_exec_audits")
    op.drop_index("ix_workspace_exec_audits_workspace_id", table_name="workspace_exec_audits")
    op.drop_index("ix_workspace_exec_audits_project_id", table_name="workspace_exec_audits")
    op.drop_table("workspace_exec_audits")
