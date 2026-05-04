"""Email verification code auth — replace GitHub OAuth.

Drop oauth_provider / oauth_subject from users. Add unique constraint on
email. Create verification_codes table for the email login flow.

Revision ID: 0013_email_auth
Revises: 0012_wds
Create Date: 2026-04-16 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0013_email_auth"
down_revision: str | None = "0012_wds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. verification_codes table
    op.create_table(
        "verification_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, index=True),
        sa.Column("code", sa.String(6), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )

    # 2. users: drop oauth columns + old constraint, add email unique
    op.drop_constraint("uq_users_oauth_identity", "users", type_="unique")
    op.drop_column("users", "oauth_provider")
    op.drop_column("users", "oauth_subject")
    op.create_unique_constraint("uq_users_email", "users", ["email"])


def downgrade() -> None:
    op.drop_constraint("uq_users_email", "users", type_="unique")
    op.add_column(
        "users",
        sa.Column("oauth_provider", sa.String(50), server_default="dev", nullable=False),
    )
    op.add_column(
        "users",
        sa.Column("oauth_subject", sa.String(200), server_default="dev", nullable=False),
    )
    op.create_unique_constraint(
        "uq_users_oauth_identity", "users", ["oauth_provider", "oauth_subject"]
    )
    op.drop_table("verification_codes")
