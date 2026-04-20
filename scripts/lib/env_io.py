"""Atomic .env read / merge / write.

Format we support is the conservative subset every dotenv lib understands:
``KEY=VALUE`` per line, ``#`` line comments, blank lines.  No ``export``
prefix, no quoting tricks, no ${VAR} interpolation — values are taken
literally to the end of the line.

We do **not** depend on python-dotenv: the wizard needs to (a) preserve
unrelated keys (b) preserve ordering and comments where possible (c)
write atomically.  Re-implementing in 60 lines is cleaner than fighting
a library's "I'll rewrite the whole file my way" semantics.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterator


def parse(text: str) -> dict[str, str]:
    """Parse the contents of a ``.env`` file into ``{key: value}``.

    Lines that don't match ``KEY=VALUE`` are silently dropped.  Last-write
    wins for duplicate keys (matches dotenv convention)."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        out[key] = value
    return out


def read(path: Path) -> dict[str, str]:
    """Load a ``.env`` file as a dict.  Missing file → empty dict."""
    if not path.exists():
        return {}
    return parse(path.read_text(encoding="utf-8"))


def _iter_merged(existing_text: str, updates: dict[str, str]) -> Iterator[str]:
    """Yield rewritten lines: in-place updates first, then any new keys."""
    seen: set[str] = set()
    for raw in existing_text.splitlines():
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            yield line
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            yield f"{key}={updates[key]}"
            seen.add(key)
        else:
            yield line
    new_keys = [k for k in updates.keys() if k not in seen]
    if new_keys:
        # Add a single blank line before appended block, only if file
        # didn't already end with one.
        yield ""
        for k in new_keys:
            yield f"{k}={updates[k]}"


def write(path: Path, updates: dict[str, str], *, mode: int = 0o600) -> None:
    """Atomically write ``updates`` into ``path``.

    Behavior:
      - Existing keys get their value replaced **in place** (preserves
        line position so the file's overall shape doesn't shuffle).
      - New keys are appended to the bottom.
      - Untouched lines (comments, blanks, unrelated keys) pass through
        unchanged.
      - Write goes through a same-directory tempfile + ``os.replace`` so
        readers either see the old content or the new one — never a
        truncated intermediate.
      - File mode is set to 0o600 by default (it's secrets).
    """
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    new_lines = list(_iter_merged(existing, updates))
    body = "\n".join(new_lines)
    if not body.endswith("\n"):
        body += "\n"
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".env.tmp.", dir=parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        # Best-effort cleanup; the caller will see the original error.
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def required_missing(env: dict[str, str], required: list[str]) -> list[str]:
    """Return the subset of ``required`` keys absent or empty in ``env``."""
    return [k for k in required if not env.get(k, "").strip()]
