"""add deployments table + workspaces.workspace_token

Phase C of the publish pipeline:

* `workspaces.workspace_token` — shared secret the platform injects as env
  into the workspace container so the in-container `polaris` CLI can
  authenticate publish / rollback / status requests back to the platform
  API via the X-Polaris-Workspace-Token header.

* `deployments` — one row per publish attempt.  Immutable except status /
  error / logs / ready_at / superseded_by_id.  Versioning is via the
  existing `project_versions` table (referenced by FK).

Revision ID: 0011_deployments_publish
Revises: 0010_workspace_project_root
Create Date: 2026-04-15 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0011_deployments_publish"
down_revision: str | None = "0010_workspace_project_root"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("workspace_token", sa.String(length=100), nullable=True),
    )
    op.create_index(
        "ix_workspaces_workspace_token",
        "workspaces",
        ["workspace_token"],
        unique=True,
    )

    op.create_table(
        "deployments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_versions.id"),
            nullable=True,
        ),
        sa.Column("git_commit_hash", sa.String(length=100), nullable=True),
        sa.Column("image_tag", sa.String(length=300), nullable=True),
        sa.Column("domain", sa.String(length=300), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="queued"),
        sa.Column("build_log", sa.Text(), nullable=True),
        sa.Column("smoke_log", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "superseded_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("deployments.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_deployments_project_id", "deployments", ["project_id"])
    op.create_index(
        "ix_deployments_project_created",
        "deployments",
        ["project_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_deployments_project_created", table_name="deployments")
    op.drop_index("ix_deployments_project_id", table_name="deployments")
    op.drop_table("deployments")
    op.drop_index("ix_workspaces_workspace_token", table_name="workspaces")
    op.drop_column("workspaces", "workspace_token")
