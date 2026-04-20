"""add workspace ide url

Revision ID: 0003_workspace_ide_url
Revises: 0002_project_versions
Create Date: 2026-04-13 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_workspace_ide_url"
down_revision: str | None = "0002_project_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("ide_url", sa.String(length=1000), nullable=True))
    op.add_column(
        "workspaces",
        sa.Column(
            "ide_status",
            sa.String(length=50),
            nullable=False,
            server_default="not_configured",
        ),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "ide_status")
    op.drop_column("workspaces", "ide_url")
