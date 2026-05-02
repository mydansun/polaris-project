"""Unit tests for the codex rate-limit projection in ``services/codex_quota``.

The projector reduces codex's verbose ``account/rateLimits/read``
response to the slim shape the chat-pane indicator polls.  Field names
in the codex schema are the kind of thing that breaks silently across
upstream releases — these tests pin the mapping so a typo or rename is
caught at unit-test time, not on a Friday afternoon when the bar
mysteriously goes blank.
"""

from __future__ import annotations

from polaris_api.services.codex_quota import _project_snapshot, _project_window


# ── _project_window ─────────────────────────────────────────────────────


def test_window_extracts_used_percent_and_passes_through_optional_fields():
    out = _project_window(
        {
            "usedPercent": 42,
            "windowDurationMins": 300,
            "resetsAt": 1_770_000_000,
        }
    )
    assert out == {
        "used_percent": 42.0,
        "window_minutes": 300,
        "resets_at": 1_770_000_000,
    }


def test_window_accepts_float_used_percent():
    out = _project_window(
        {"usedPercent": 17.5, "windowDurationMins": 60, "resetsAt": None}
    )
    assert out is not None
    assert out["used_percent"] == 17.5


def test_window_passes_through_null_optionals():
    # Codex sometimes returns ``null`` for windowDurationMins/resetsAt
    # on free tiers — those nulls must be preserved (frontend hides
    # the reset countdown in that case).
    out = _project_window(
        {"usedPercent": 0, "windowDurationMins": None, "resetsAt": None}
    )
    assert out == {
        "used_percent": 0.0,
        "window_minutes": None,
        "resets_at": None,
    }


def test_window_returns_none_when_used_percent_missing():
    # Without usedPercent there's nothing to render — the frontend
    # treats ``None`` as "no quota info available".
    assert _project_window({"windowDurationMins": 300}) is None


def test_window_returns_none_when_used_percent_not_numeric():
    # A string or null usedPercent would otherwise crash the bar's
    # Math.round on the frontend.
    assert _project_window({"usedPercent": "n/a"}) is None
    assert _project_window({"usedPercent": None}) is None


def test_window_returns_none_for_non_dict_input():
    # Codex may emit ``null`` for an entire window when the account
    # has no secondary tier.  Don't crash on it.
    assert _project_window(None) is None
    assert _project_window([]) is None
    assert _project_window("nope") is None


# ── _project_snapshot ───────────────────────────────────────────────────


def test_snapshot_projects_both_windows_and_plan_type():
    raw = {
        "rateLimits": {
            "limitId": "codex",
            "limitName": None,
            "primary": {
                "usedPercent": 14,
                "windowDurationMins": 300,
                "resetsAt": 1_770_000_000,
            },
            "secondary": {
                "usedPercent": 2,
                "windowDurationMins": 10080,
                "resetsAt": 1_770_500_000,
            },
            "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
            "planType": "plus",
            "rateLimitReachedType": None,
        }
    }
    out = _project_snapshot(raw)
    assert out == {
        "primary": {
            "used_percent": 14.0,
            "window_minutes": 300,
            "resets_at": 1_770_000_000,
        },
        "secondary": {
            "used_percent": 2.0,
            "window_minutes": 10080,
            "resets_at": 1_770_500_000,
        },
        "plan_type": "plus",
    }


def test_snapshot_handles_missing_secondary_window():
    # Free-tier accounts only have a primary window — secondary
    # comes back as ``null``; the bar still needs to show primary.
    raw = {
        "rateLimits": {
            "primary": {"usedPercent": 5, "windowDurationMins": 300, "resetsAt": None},
            "secondary": None,
            "planType": "free",
        }
    }
    out = _project_snapshot(raw)
    assert out["primary"] is not None
    assert out["primary"]["used_percent"] == 5.0
    assert out["secondary"] is None
    assert out["plan_type"] == "free"


def test_snapshot_handles_missing_rate_limits_block():
    # If the upstream response shape changes and ``rateLimits`` goes
    # away entirely, we want a safe shape (all None) rather than a
    # KeyError that bubbles to the frontend.
    out = _project_snapshot({})
    assert out == {"primary": None, "secondary": None, "plan_type": None}


def test_snapshot_handles_non_dict_rate_limits():
    # Defensive: if codex ever returns a list or null where we
    # expect a dict, fall back to the empty shape.
    out = _project_snapshot({"rateLimits": None})
    assert out == {"primary": None, "secondary": None, "plan_type": None}
    out = _project_snapshot({"rateLimits": []})
    assert out == {"primary": None, "secondary": None, "plan_type": None}


def test_snapshot_omits_plan_type_when_absent():
    # planType is optional — projection should keep the key with
    # value ``None`` rather than dropping it (frontend always reads
    # the field and the type contract requires it).
    raw = {"rateLimits": {"primary": {"usedPercent": 1}}}
    out = _project_snapshot(raw)
    assert "plan_type" in out
    assert out["plan_type"] is None
