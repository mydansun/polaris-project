"""add workspaces.project_root

Codex owns the project layout inside /workspace now (scaffolders choose
whether to emit into `.` or into a named subdir like `my-app/`).  The
agent reports the intended IDE entry point via a dynamic tool
`set_project_root`, and we persist it here so subsequent page loads know
which folder to open VSCode at.  NULL = "not yet decided" (editor pane
shows a skeleton while a turn is in flight; falls back to /workspace
when the turn completes without the agent setting a root).

Revision ID: 0010_workspace_project_root
Revises: 0009_drop_workspace_exec_audits
Create Date: 2026-04-15 00:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0010_workspace_project_root"
down_revision: str | None = "0009_drop_workspace_exec_audits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("project_root", sa.String(length=1000), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "project_root")
