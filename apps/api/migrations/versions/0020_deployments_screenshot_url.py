"""Add deployments.screenshot_url

Captures a Playwright screenshot of the smoke-tested prod URL once the
deployment reaches ``status='ready'``.  Surfaced on the HomePage card
so users see what they shipped.

Revision ID: 0020_deployments_screenshot_url
Revises: 0019_session_counters
Create Date: 2026-05-05
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision: str = "0020_deployments_screenshot_url"
down_revision: str | None = "0019_session_counters"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "deployments",
        sa.Column("screenshot_url", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("deployments", "screenshot_url")
