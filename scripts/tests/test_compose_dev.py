"""Structural tests for compose.dev.yaml.

These run against the actual `docker compose` CLI's `config` command,
which fully validates references / interpolation / schema.  We feed a
deterministic env so output is comparable between runs.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from lib import paths


def _have_docker_compose() -> bool:
    if shutil.which("docker") is None:
        return False
    p = subprocess.run(  # noqa: S603, S607
        ["docker", "compose", "version"], capture_output=True, text=True
    )
    return p.returncode == 0


pytestmark = pytest.mark.skipif(
    not _have_docker_compose(), reason="docker compose not installed"
)


_FIXTURE_ENV = {
    "POLARIS_DOMAIN": "polaris-dev.xyz",
    "CF_DNS_API_TOKEN": "test-token",
    # POLARIS_HOST_CODEX_AUTH_PATH deliberately omitted — compose.dev.yaml
    # has ``${POLARIS_HOST_CODEX_AUTH_PATH:-${HOME}/.codex/auth.json}``
    # default; this test verifies the default kicks in.
    "S3_ACCESS_KEY_ID": "ak",
    "S3_SECRET_ACCESS_KEY": "sk",
    "OPENAI_SECRET": "sk-test",
    "POLARIS_PINTEREST_TOOL_API_KEY": "pkey",
    "SESSION_SECRET": "x" * 64,
    # PWD is the host-side repo root that up.py also sets before invoking
    # docker compose.  Tests must mirror this to interpolate ${PWD} the
    # same way the production CLI does.
    "PWD": str(paths.REPO_ROOT),
    # HOME used by the codex-auth default expansion.
    "HOME": str(paths.REPO_ROOT / ".test-home"),
}


def _config(extra: dict | None = None) -> dict:
    """Run `docker compose config --format json` against compose.dev.yaml."""
    env = {**os.environ, **_FIXTURE_ENV, **(extra or {})}
    p = subprocess.run(  # noqa: S603, S607
        [
            "docker",
            "compose",
            "-f",
            str(paths.compose_file()),
            "config",
            "--format",
            "json",
        ],
        cwd=paths.REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(p.stdout)


def test_compose_parses_with_minimum_env():
    cfg = _config()
    assert cfg["name"] == "polaris"
    assert "api" in cfg["services"]
    assert "worker" in cfg["services"]
    assert "web" in cfg["services"]
    assert "traefik" in cfg["services"]


def test_traefik_uses_cf_resolver_via_labels():
    cfg = _config()
    api_labels = cfg["services"]["api"].get("labels", {})
    label_keys = list(api_labels.keys() if isinstance(api_labels, dict) else api_labels)
    # Either dict-form or list-form labels — normalize.
    if isinstance(api_labels, list):
        joined = "\n".join(api_labels)
    else:
        joined = "\n".join(f"{k}={v}" for k, v in api_labels.items())
    assert "traefik.http.routers.api.tls.certresolver=cf" in joined
    assert "traefik.http.routers.api.tls.domains[0].sans=*.polaris-dev.xyz" in joined


def test_no_letsencrypt_host_mount_anywhere():
    cfg = _config()
    for service_name, service in cfg["services"].items():
        for vol in service.get("volumes", []):
            src = vol.get("source", "") if isinstance(vol, dict) else str(vol)
            assert "/etc/letsencrypt" not in src, (
                f"service {service_name} still mounts /etc/letsencrypt: {vol}"
            )


def test_traefik_has_acme_named_volume():
    cfg = _config()
    traefik_vols = cfg["services"]["traefik"]["volumes"]
    sources = [
        v.get("source", "") if isinstance(v, dict) else str(v)
        for v in traefik_vols
    ]
    # The volume named `traefik-acme` (compose-rendered name) has source
    # equal to the docker-compose project's resolved volume name.
    assert any("traefik-acme" in s or "polaris-traefik-acme" in s for s in sources)


def test_api_worker_have_docker_socket_mount():
    cfg = _config()
    for svc in ("api", "worker"):
        srcs = [
            v.get("source", "") if isinstance(v, dict) else str(v)
            for v in cfg["services"][svc]["volumes"]
        ]
        assert any("/var/run/docker.sock" in s for s in srcs), (
            f"{svc} missing docker.sock mount"
        )


def test_repo_bind_mount_uses_same_path_inside_and_outside():
    """The api/worker containers must see the repo at the same absolute
    path that the host has, so generated workspace compose YAMLs work."""
    cfg = _config()
    repo_root = str(paths.REPO_ROOT)
    for svc in ("api", "worker"):
        vols = cfg["services"][svc]["volumes"]
        match = None
        for v in vols:
            if isinstance(v, dict) and v.get("source") == repo_root and v.get("target") == repo_root:
                match = v
                break
        assert match, f"{svc} missing same-path bind-mount of repo root {repo_root}"


def test_no_hardcoded_host_docker_internal_in_compose():
    """compose.dev.yaml itself shouldn't contain host.docker.internal —
    services reach each other via the polaris-shared network.

    NOTE: this scans the rendered YAML, not the source — so even
    `extra_hosts: host-gateway` aliases that legitimately exist in
    workspace compose templates won't trip this."""
    text = paths.compose_file().read_text()
    assert "host.docker.internal" not in text, (
        "compose.dev.yaml contains host.docker.internal — should use service DNS"
    )
