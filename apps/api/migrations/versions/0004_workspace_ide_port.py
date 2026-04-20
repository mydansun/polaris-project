"""add workspace ide port

Revision ID: 0004_workspace_ide_port
Revises: 0003_workspace_ide_url
Create Date: 2026-04-13 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_workspace_ide_port"
down_revision: str | None = "0003_workspace_ide_url"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("ide_port", sa.Integer(), nullable=True))
    op.create_index("ix_workspaces_ide_port", "workspaces", ["ide_port"])


def downgrade() -> None:
    op.drop_index("ix_workspaces_ide_port", table_name="workspaces")
    op.drop_column("workspaces", "ide_port")
