"""add workspace_dep_services (dev-time dependency slots)

Keeps dev-time deps (postgres / redis) out of the workspace compose file
so turning one on/off never re-renders the workspace service and never
restarts the workspace container — which would kill the Codex session
that called `polaris dev-up`.  Each row = one docker container attached to
the workspace's per-project network via `--network-alias <service>`.

Revision ID: 0012_wds
Revises: 0011_deployments_publish
Create Date: 2026-04-15 15:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0012_wds"
down_revision: str | None = "0011_deployments_publish"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_dep_services",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("service", sa.String(length=50), nullable=False),
        sa.Column("container_name", sa.String(length=100), nullable=False),
        sa.Column("volume_name", sa.String(length=100), nullable=False),
        sa.Column("image", sa.String(length=200), nullable=False),
        sa.Column("network", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="starting"),
        sa.Column("env_jsonb", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("workspace_id", "service", name="uq_wds_workspace_service"),
    )
    op.create_index("ix_wds_workspace_id", "workspace_dep_services", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_wds_workspace_id", table_name="workspace_dep_services")
    op.drop_table("workspace_dep_services")
