"""Unit tests for the publish/preview/image naming helpers in
``services/publish.py``.

These names end up as docker compose project names (63-char cap),
container image tags pushed to a registry, and DNS subdomains.  A
silent length regression here breaks ``docker compose up`` with a
cryptic "name too long" failure on real deploys.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from polaris_api.services.publish import (
    compose_project_name,
    image_tag,
    preview_project_name,
    project_domain,
    publish_project_root,
)


_PID = UUID("12345678-90ab-cdef-1234-567890abcdef")


def _settings(**overrides):
    """Minimal Settings-shaped object — only the fields these helpers
    read.  Avoids importing the full pydantic Settings just for naming
    coverage."""
    base = {
        "publish_projects_root": "/srv/publish",
        "registry_url": "registry.local:5000",
        "prod_domain_base": "polaris.app",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ── compose_project_name ────────────────────────────────────────────────


def test_compose_project_name_strips_dashes_and_truncates():
    name = compose_project_name(_PID)
    assert name == "polaris-pub-1234567890abcdef12345678"
    # docker compose project names cap at 63; ours have a 24-char
    # workspace hash, so total stays well under.
    assert len(name) <= 63


def test_compose_project_name_uses_first_24_hex():
    # Truncating from the END would make adjacent project ids collide
    # on the front of the UUID — the helper takes the first 24 hex.
    name = compose_project_name(_PID)
    assert "1234567890abcdef12345678" in name


# ── preview_project_name ────────────────────────────────────────────────


def test_preview_project_name_includes_short_hash():
    name = preview_project_name(_PID, "abcdef0123")
    # Format pinned: prefix + 16 of the project id + first 6 of the hash
    assert name == "polaris-pvw-1234567890abcdef-abcdef"
    assert len(name) <= 63


def test_preview_project_name_truncates_long_short_hash():
    # A full git SHA (40 chars) shouldn't blow the docker name limit.
    name = preview_project_name(_PID, "a" * 40)
    assert name == "polaris-pvw-1234567890abcdef-aaaaaa"


def test_preview_project_name_distinct_from_compose_project_name():
    # Same project, two names — must not collide, otherwise prod and
    # preview deploys overwrite each other.
    pub = compose_project_name(_PID)
    pvw = preview_project_name(_PID, "abcdef")
    assert pub != pvw
    # Different prefix so a name-prefix list filter can distinguish them.
    assert pub.startswith("polaris-pub-")
    assert pvw.startswith("polaris-pvw-")


# ── image_tag ───────────────────────────────────────────────────────────


def test_image_tag_format():
    tag = image_tag(_settings(), _PID, "abc123")
    assert tag == f"registry.local:5000/polaris/{_PID}:abc123"


def test_image_tag_uses_dashed_uuid_form():
    # Unlike compose names, image tags keep the dashed UUID — registry
    # paths don't have the same length cap and dashes are valid in
    # docker reference names.
    tag = image_tag(_settings(), _PID, "abc")
    assert str(_PID) in tag


def test_image_tag_respects_custom_registry():
    tag = image_tag(
        _settings(registry_url="ghcr.io/polaris-team"), _PID, "v1"
    )
    assert tag.startswith("ghcr.io/polaris-team/polaris/")


# ── project_domain ──────────────────────────────────────────────────────


def test_project_domain_pattern():
    assert project_domain(_settings(), _PID) == f"{_PID}.polaris.app"


def test_project_domain_respects_custom_base():
    out = project_domain(_settings(prod_domain_base="example.com"), _PID)
    assert out.endswith(".example.com")
    assert out.startswith(str(_PID))


# ── publish_project_root ────────────────────────────────────────────────


def test_publish_project_root_joins_settings_with_id():
    root = publish_project_root(_settings(), _PID)
    assert root == Path("/srv/publish") / str(_PID)


def test_publish_project_root_returns_path_object():
    # Downstream code uses ``Path`` operators (``/``, ``.exists()``);
    # accidentally returning a raw string would break those.
    root = publish_project_root(_settings(), _PID)
    assert isinstance(root, Path)
