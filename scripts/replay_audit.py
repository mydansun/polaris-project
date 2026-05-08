"""Replay fixture audit — sanity-check a recorded raw fixture.

Reads a fixture file (``.json`` or ``.json.gz``), runs minimum-coverage
checks needed for replay (clarification rounds, codex turn lifecycle,
mood-board, file_change presence, terminal status), and prints a
human-readable summary.

Usage::

    python scripts/replay_audit.py tests/fixtures/replay/raw/golf-landing-page.json.gz

Exit code is non-zero when a required invariant fails — handy in CI to
catch a re-recorded fixture that lost coverage between protocol bumps.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


def load_raw_fixture(path: Path) -> dict[str, Any]:
    if path.suffix == ".gz":
        with gzip.open(path, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    return json.loads(path.read_text(encoding="utf-8"))


def _completed_items(frames: list[dict]) -> list[dict]:
    out: list[dict] = []
    for f in frames:
        fr = f.get("frame", {})
        if fr.get("method") == "item/completed":
            item = fr.get("params", {}).get("item")
            if isinstance(item, dict):
                out.append(item)
    return out


def _item_type(item: dict) -> str:
    """Codex item type lives at ``type`` in the item payload (not ``kind``).
    Older transcripts may use either; check both for forward-compat."""
    return item.get("type") or item.get("kind") or "?"


def audit(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Returns (failures, warnings).  Failures break replay invariants
    we depend on; warnings are observable oddities worth flagging."""
    failures: list[str] = []
    warnings: list[str] = []

    ua = data.get("user_actions", [])
    agent_io = data.get("agent_io", {})
    frames = agent_io.get("codex_frames", [])
    nodes = agent_io.get("design_intent_nodes", [])

    # ── User actions ──
    clar_actions = [a for a in ua if a.get("kind") == "answer_clarification"]
    if len(clar_actions) < 3:
        failures.append(
            f"only {len(clar_actions)} clarification answer(s); golf scenario "
            "expected at least 3 (one per round)"
        )

    distinct_questions = {a["concrete"].get("question_id") for a in clar_actions}
    if len(distinct_questions) < 3:
        warnings.append(
            f"only {len(distinct_questions)} distinct question_ids — recording "
            "may have skipped a round"
        )

    if not any(a.get("kind") == "click" and "plan-approve" in str(a.get("concrete", {})) for a in ua):
        warnings.append("no plan-approve click recorded — build phase wasn't triggered")

    # ── Codex frames ──
    methods = Counter(f.get("frame", {}).get("method") for f in frames)
    started = methods.get("item/started", 0)
    completed = methods.get("item/completed", 0)
    if started != completed:
        failures.append(
            f"item/started ({started}) != item/completed ({completed}) — "
            "imbalanced lifecycle, replayer would hang waiting for completion"
        )

    turn_completed = methods.get("turn/completed", 0)
    if turn_completed < 1:
        failures.append("no turn/completed frame — codex never finished a turn")

    # Final turn status
    final_turn_status = None
    for f in reversed(frames):
        if f.get("frame", {}).get("method") == "turn/completed":
            final_turn_status = (
                f.get("frame", {})
                .get("params", {})
                .get("turn", {})
                .get("status")
            )
            break
    if final_turn_status != "completed":
        failures.append(
            f"final turn status is {final_turn_status!r}, expected 'completed'"
        )

    # Item-type coverage
    items = _completed_items(frames)
    item_types = Counter(_item_type(i) for i in items)
    for required in ("plan", "fileChange", "agentMessage"):
        if item_types.get(required, 0) < 1:
            failures.append(
                f"no completed item of type={required!r} — replay would miss "
                "this UI surface"
            )

    # ── Design-intent ──
    di_kinds = Counter(n.get("node") for n in nodes)
    for required in ("compiler", "mood_board_step"):
        if di_kinds.get(required, 0) < 1:
            failures.append(
                f"design-intent node {required!r} never fired — discovery did "
                "not complete"
            )

    return failures, warnings


def summary(data: dict[str, Any]) -> str:
    ua = data.get("user_actions", [])
    frames = data.get("agent_io", {}).get("codex_frames", [])
    nodes = data.get("agent_io", {}).get("design_intent_nodes", [])
    items = _completed_items(frames)
    methods = Counter(f.get("frame", {}).get("method") for f in frames)
    item_types = Counter(_item_type(i) for i in items)
    di_kinds = Counter(n.get("node") for n in nodes)

    lines: list[str] = []
    lines.append(f"scenario: {data.get('scenario')}")
    lines.append(f"recorded_at: {data.get('recorded_at')}")
    lines.append(f"wall time: {ua[-1]['t']:.1f}s" if ua else "(no user actions)")
    lines.append("")
    lines.append(f"user actions ({len(ua)}):")
    for k, n in Counter(a.get("kind") for a in ua).most_common():
        lines.append(f"  {k}: {n}")
    lines.append("")
    lines.append(f"codex frames ({len(frames)}):")
    for m, n in methods.most_common(8):
        lines.append(f"  {m}: {n}")
    if len(methods) > 8:
        lines.append(f"  … and {len(methods) - 8} more methods")
    lines.append("")
    lines.append(f"completed item types ({sum(item_types.values())} items):")
    for t, n in item_types.most_common():
        lines.append(f"  {t}: {n}")
    lines.append("")
    lines.append(f"design-intent nodes ({len(nodes)}):")
    for k, n in di_kinds.most_common():
        lines.append(f"  {k}: {n}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="replay-audit")
    parser.add_argument("fixture", help="path to raw/<scenario>.json[.gz]")
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="just print the summary, don't run invariant checks",
    )
    args = parser.parse_args(argv)

    path = Path(args.fixture)
    if not path.exists():
        print(f"error: fixture not found at {path}", file=sys.stderr)
        return 2
    data = load_raw_fixture(path)

    print(summary(data))
    if args.summary_only:
        return 0

    failures, warnings = audit(data)
    print()
    if warnings:
        print("warnings:")
        for w in warnings:
            print(f"  ⚠ {w}")
    if failures:
        print("failures:")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("✓ all invariants passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
