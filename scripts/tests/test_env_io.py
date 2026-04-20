"""Tests for scripts/lib/env_io.py — atomic .env merge + parse round-trips."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from lib import env_io


def test_parse_simple_lines():
    text = "A=1\nB=hello world\n"
    assert env_io.parse(text) == {"A": "1", "B": "hello world"}


def test_parse_comments_and_blanks_dropped():
    text = "# a comment\n\nKEY=value\n   # another\n"
    assert env_io.parse(text) == {"KEY": "value"}


def test_parse_equals_in_value_preserved():
    text = "URL=postgres://u:p@host:5432/db?sslmode=require\n"
    assert env_io.parse(text)["URL"] == (
        "postgres://u:p@host:5432/db?sslmode=require"
    )


def test_parse_lines_without_equals_dropped():
    text = "garbage line\nA=1\n=novalueforthiskey\n"
    assert env_io.parse(text) == {"A": "1"}


def test_read_missing_file_returns_empty(tmp_path: Path):
    assert env_io.read(tmp_path / "no.env") == {}


def test_write_creates_file_with_mode(tmp_path: Path):
    p = tmp_path / ".env"
    env_io.write(p, {"FOO": "bar", "BAZ": "qux"})
    assert p.read_text() == "\nFOO=bar\nBAZ=qux\n"
    # On POSIX the mode bits include 0o600
    mode = os.stat(p).st_mode & 0o777
    assert mode == 0o600


def test_write_preserves_existing_lines_and_comments(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text(
        "# top comment\nFOO=old\n\n# section\nBAR=keepme\n",
        encoding="utf-8",
    )
    env_io.write(p, {"FOO": "new", "NEW": "added"})
    out = p.read_text()
    # FOO replaced in place, BAR untouched, comments preserved, NEW appended
    assert "# top comment" in out
    assert "FOO=new" in out
    assert "FOO=old" not in out
    assert "BAR=keepme" in out
    assert "# section" in out
    assert out.rstrip().endswith("NEW=added")


def test_write_is_atomic_no_tmpfile_left_behind(tmp_path: Path):
    p = tmp_path / ".env"
    env_io.write(p, {"A": "1"})
    siblings = list(tmp_path.iterdir())
    # Only the final .env should remain; no `.env.tmp.*` debris.
    assert siblings == [p]


def test_required_missing():
    env = {"A": "1", "B": "", "C": "  "}
    assert env_io.required_missing(env, ["A", "B", "C", "D"]) == [
        "B",
        "C",
        "D",
    ]


def test_round_trip_after_write(tmp_path: Path):
    p = tmp_path / ".env"
    env_io.write(p, {"KEY": "val=with=equals", "X": "1"})
    parsed = env_io.read(p)
    assert parsed == {"KEY": "val=with=equals", "X": "1"}
