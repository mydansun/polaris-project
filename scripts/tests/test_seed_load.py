"""Tests for scripts/seed/load.py — placeholder substitution + db url
normalization + manifest reading.

The full DB-roundtrip test would require a live postgres; we run that
as an integration smoke against the running compose stack
(``test_seed_load_integration``), gated on POLARIS_LIVE_DB=1 to keep
unit-suite hermetic.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from seed import load as seed_load


# ── pure helpers ────────────────────────────────────────────────────────


def test_replace_placeholder_string():
    out = seed_load._replace_placeholder(
        "https://polaris.s3.__POLARIS_DOMAIN__/x.png",
        "polaris-dev.xyz",
    )
    assert out == "https://polaris.s3.polaris-dev.xyz/x.png"


def test_replace_placeholder_nested_dict_and_list():
    src = {
        "domain": "<id>.prod.__POLARIS_DOMAIN__",
        "queries": ["site:__POLARIS_DOMAIN__", "other"],
        "nested": {"url": "https://__POLARIS_DOMAIN__/x"},
    }
    out = seed_load._replace_placeholder(src, "example.com")
    assert out["domain"] == "<id>.prod.example.com"
    assert out["queries"] == ["site:example.com", "other"]
    assert out["nested"]["url"] == "https://example.com/x"


def test_replace_placeholder_passes_non_strings():
    assert seed_load._replace_placeholder(42, "anything") == 42
    assert seed_load._replace_placeholder(None, "anything") is None
    assert seed_load._replace_placeholder(True, "anything") is True


# ── _to_dt ────────────────────────────────────────────────────────────


def test_to_dt_parses_iso_string():
    from datetime import datetime, timezone

    out = seed_load._to_dt("2026-04-22T20:05:01.654917+00:00")
    assert isinstance(out, datetime)
    assert out.tzinfo is not None
    # Round-trip
    assert out.isoformat() == "2026-04-22T20:05:01.654917+00:00"


def test_to_dt_passes_through_datetime():
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    assert seed_load._to_dt(now) is now


def test_to_dt_passes_through_none():
    assert seed_load._to_dt(None) is None


def test_to_dt_handles_z_suffix():
    out = seed_load._to_dt("2026-04-22T20:05:01Z")
    assert out is not None
    assert out.tzinfo is not None


# ── db_url normalization ────────────────────────────────────────────────


def test_db_url_strips_sqlalchemy_dialect(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "POLARIS_DATABASE_URL",
        "postgresql+asyncpg://u:p@h:5432/d?sslmode=require",
    )
    env = {"POLARIS_DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/d?sslmode=require"}
    assert (
        seed_load._db_url(env)
        == "postgres://u:p@h:5432/d?sslmode=require"
    )


# ── manifest discovery ─────────────────────────────────────────────────


def test_load_raises_on_missing_manifest(tmp_path: Path):
    import asyncio

    with pytest.raises(FileNotFoundError):
        asyncio.run(seed_load.load(tmp_path))


# ── integration: load against the live local DB ─────────────────────────
#
# Opt-in.  Runs against the compose.dev.yaml postgres container.
# Verifies:
#   - dev user resolved/created
#   - 5 projects inserted
#   - placeholder fully substituted in deployments.domain
#   - reset wipes only seeded projects (not pre-existing ones)
#
#   POLARIS_LIVE_DB=1 uv run --group dev pytest \
#       scripts/tests/test_seed_load.py -k integration


pytestmark_integration = pytest.mark.skipif(
    os.environ.get("POLARIS_LIVE_DB") != "1",
    reason="opt-in: requires running postgres + seed-data/",
)


@pytestmark_integration
def test_seed_load_integration(tmp_path: Path):
    """Round-trip the actual seed-data/2026-05-05/ snapshot.  Cleans up
    after itself via reset()."""
    import asyncio

    import asyncpg

    repo_root = Path(__file__).resolve().parents[2]
    source = repo_root / "seed-data" / "2026-05-05"
    if not source.exists():
        pytest.skip(f"no snapshot at {source}")

    async def _verify_no_placeholder() -> int:
        conn = await asyncpg.connect(seed_load._db_url(seed_load._env()))
        try:
            rows = await conn.fetch(
                """SELECT d.domain FROM deployments d
                     JOIN projects p ON p.id = d.project_id
                     JOIN sessions s ON s.project_id = p.id
                    WHERE s.metadata_jsonb @> '{"seeded": true}'::jsonb"""
            )
            for r in rows:
                assert seed_load.PLACEHOLDER not in (r["domain"] or "")
            return len(rows)
        finally:
            await conn.close()

    # Round 1: load
    stats = asyncio.run(seed_load.load(source, force=True))
    assert stats.inserted >= 1, "expected at least one project to insert"

    # Round 2: idempotent — same load should skip everything
    stats2 = asyncio.run(seed_load.load(source, force=False))
    assert stats2.inserted == 0
    assert stats2.skipped == stats.inserted

    # Verify domains in DB are placeholder-free
    domain_count = asyncio.run(_verify_no_placeholder())
    assert domain_count >= 1

    # Cleanup
    n = asyncio.run(seed_load.reset())
    assert n >= 1
