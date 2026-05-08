"""CLI wrapper for ``merge_staging`` — invoked by the api at finalize
time and runnable standalone for manual recovery.

    python -m polaris_worker.replay.merge_cli --fixture path/to/raw/scenario.json [--no-cleanup]

Exits 0 on success, prints the resolved fixture path on stdout.
Exits non-zero on failure with a one-line stderr message.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from polaris_worker.replay.recorder import merge_staging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="polaris-replay-merge")
    parser.add_argument(
        "--fixture",
        required=True,
        help="path to the final raw/<scenario>.json — staging dir is "
        "derived as a sibling .staging-<scenario>/",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="keep the staging dir after a successful merge (for inspection)",
    )
    args = parser.parse_args(argv)

    fixture = Path(args.fixture).resolve()
    try:
        out = merge_staging(fixture, cleanup=not args.no_cleanup)
    except FileNotFoundError as exc:
        print(f"merge failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"merge crashed: {exc}", file=sys.stderr)
        return 1
    print(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
