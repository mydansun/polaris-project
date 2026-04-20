"""drop workspaces.ide_port column

The IDE is no longer exposed as a host port: nginx routes
https://ide-<workspaceHash>.polaris.test directly to the container on the
shared polaris-internal network, so the per-workspace port field is dead data.

Revision ID: 0007_drop_workspace_ide_port
Revises: 0006_workspace_exec_audits
Create Date: 2026-04-14 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0007_drop_workspace_ide_port"
down_revision: str | Sequence[str] | None = "0006_workspace_exec_audits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("workspaces", "ide_port")


def downgrade() -> None:
    op.add_column("workspaces", sa.Column("ide_port", sa.Integer(), nullable=True))
