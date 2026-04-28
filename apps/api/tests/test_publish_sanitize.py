"""Unit tests for ``sanitize_prod_compose``.

The helper strips host-published ports from the user's compose.prod.yml
before the publish pipeline hands it to docker.  Port 80/443 would
collide with the platform's Traefik; we strip ALL host publishes (not
just 80/443) since Traefik handles external routing via the
``traefik-public`` network.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import yaml

from polaris_api.services.publish import sanitize_prod_compose


def _write_compose(dir_: Path, text: str) -> None:
    (dir_ / "compose.prod.yml").write_text(textwrap.dedent(text).lstrip())


def _read_services(dir_: Path) -> dict:
    return yaml.safe_load((dir_ / "compose.prod.yml").read_text())["services"]


def test_strips_short_syntax_80_80(tmp_path: Path):
    _write_compose(
        tmp_path,
        """\
        services:
          web:
            build: .
            ports:
              - "80:80"
        """,
    )
    log: list[str] = []
    sanitize_prod_compose(tmp_path, log)
    assert "ports" not in _read_services(tmp_path)["web"]
    assert any("web" in line and "ports" in line for line in log)


def test_strips_non_80_host_port_too(tmp_path: Path):
    # Policy: strip ALL host publishes, not just 80 — traefik never
    # needs host-side exposure.
    _write_compose(
        tmp_path,
        """\
        services:
          web:
            ports: ["3000:3000"]
        """,
    )
    sanitize_prod_compose(tmp_path, [])
    assert "ports" not in _read_services(tmp_path)["web"]


def test_strips_long_syntax_dict(tmp_path: Path):
    _write_compose(
        tmp_path,
        """\
        services:
          web:
            ports:
              - published: 80
                target: 80
        """,
    )
    sanitize_prod_compose(tmp_path, [])
    assert "ports" not in _read_services(tmp_path)["web"]


def test_preserves_expose(tmp_path: Path):
    _write_compose(
        tmp_path,
        """\
        services:
          web:
            expose: ["80"]
        """,
    )
    sanitize_prod_compose(tmp_path, [])
    assert _read_services(tmp_path)["web"]["expose"] == ["80"]


def test_only_affects_services_with_ports(tmp_path: Path):
    _write_compose(
        tmp_path,
        """\
        services:
          web:
            ports: ["80:80"]
          worker:
            command: ["python", "worker.py"]
        """,
    )
    sanitize_prod_compose(tmp_path, [])
    svcs = _read_services(tmp_path)
    assert "ports" not in svcs["web"]
    assert "command" in svcs["worker"]  # untouched


def test_noop_when_no_ports(tmp_path: Path):
    original = textwrap.dedent(
        """\
        services:
          web:
            build: .
        """
    ).lstrip()
    (tmp_path / "compose.prod.yml").write_text(original)
    log: list[str] = []
    sanitize_prod_compose(tmp_path, log)
    assert (tmp_path / "compose.prod.yml").read_text() == original
    assert log == []


def test_noop_when_file_missing(tmp_path: Path):
    log: list[str] = []
    sanitize_prod_compose(tmp_path, log)
    assert log == []


def test_logs_and_skips_on_invalid_yaml(tmp_path: Path):
    (tmp_path / "compose.prod.yml").write_text("::: not yaml :::\n")
    log: list[str] = []
    sanitize_prod_compose(tmp_path, log)
    assert any("invalid YAML" in line for line in log)
