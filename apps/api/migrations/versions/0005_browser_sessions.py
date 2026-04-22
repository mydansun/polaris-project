"""add browser sessions

Revision ID: 0005_browser_sessions
Revises: 0004_workspace_ide_port
Create Date: 2026-04-13 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_browser_sessions"
down_revision: str | None = "0004_workspace_ide_port"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "browser_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("vnc_url", sa.String(length=1000), nullable=True),
        sa.Column("context_metadata_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_browser_sessions_project_id", "browser_sessions", ["project_id"])
    op.create_index("ix_browser_sessions_workspace_id", "browser_sessions", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_browser_sessions_workspace_id", table_name="browser_sessions")
    op.drop_index("ix_browser_sessions_project_id", table_name="browser_sessions")
    op.drop_table("browser_sessions")
