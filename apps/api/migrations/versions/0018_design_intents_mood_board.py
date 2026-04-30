"""Add mood_board_url column to design_intents

Stores the anonymous-public S3 URL of the generated mood board PNG
(produced by the ``mood_board_step`` LangGraph node, uploaded after
discovery completes).  Two consumers:

- Codex — ``CodexAgent.run`` passes the URL as an ``{type: "image",
  url}`` entry in every ``turn/start``'s ``input`` array so Codex has
  a persistent visual anchor across all turns of the project.
- Frontend — the ``discovery:compiled`` SSE event includes this URL
  so the chat can render the mood board in a dedicated card.

Nullable: graceful degradation when image generation fails or the
Pinterest reference was unavailable.

Revision ID: 0018_design_intents_mood_board
Revises: 0017_unsplash_images
Create Date: 2026-04-18 22:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0018_design_intents_mood_board"
down_revision: str | None = "0017_unsplash_images"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "design_intents",
        sa.Column("mood_board_url", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("design_intents", "mood_board_url")
