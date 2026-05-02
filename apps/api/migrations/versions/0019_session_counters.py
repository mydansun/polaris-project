"""Add per-session activity counters

Two cumulative counters that drive the frontend StatusBar below the
chat input:

- ``file_change_count`` — +1 per inotify close_write / delete / moved_to
  under the project's ``set_project_root`` scope while the Codex
  AgentRun is active.  Source is the worker's per-sink inotifywait
  subprocess; Codex's own ``codex:file_change`` items are rendered in
  chat but do NOT feed this counter.
- ``playwright_call_count`` — +1 per ``codex:mcp_tool_call`` completed
  event whose payload's ``server_name`` is ``"playwright"``.

Both default 0.  Updates land via a 500ms coalescing debounce in
``apps/worker/src/polaris_worker/sink.py``.

Revision ID: 0019_session_counters
Revises: 0018_design_intents_mood_board
Create Date: 2026-04-19 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0019_session_counters"
down_revision: str | None = "0018_design_intents_mood_board"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column(
            "file_change_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "sessions",
        sa.Column(
            "playwright_call_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("sessions", "playwright_call_count")
    op.drop_column("sessions", "file_change_count")
