"""Unit tests for the webview-hostname helpers in ``services/compose.py``.

These are tiny pure functions that produce strings the rest of the stack
(Theia env, Traefik labels, LE cert) depends on character-by-character.
A 1-byte regression here means images don't load in the IDE, or the
wildcard cert fails to match.  Worth pinning.
"""

from __future__ import annotations

import re
from uuid import UUID

from polaris_api.services.compose import (
    _webview_host_regexp,
    webview_external_endpoint,
    webview_short_hash,
)


_WS = UUID("12345678-90ab-cdef-1234-567890abcdef")
_DOMAIN = "polaris-dev.xyz"


def test_short_hash_is_first_12_hex_chars():
    # 36-char dashed UUID → 32 hex; we keep the first 12.
    assert webview_short_hash(_WS) == "1234567890ab"


def test_short_hash_length_invariant():
    # The whole point: 12 chars, no more, no less — the rest of the
    # hostname is sized assuming this constant.
    assert len(webview_short_hash(_WS)) == 12


def test_external_endpoint_carries_uuid_placeholder():
    # Theia substitutes ``{{uuid}}`` per-webview at runtime; the rest
    # of the label is workspace-scoped.  Format must be exact — Theia
    # won't tolerate a stray space or capitalization change.
    out = webview_external_endpoint(_WS, _DOMAIN)
    assert out == "wv-{{uuid}}-1234567890ab.polaris-dev.xyz"


def test_external_endpoint_first_label_under_dns_limit():
    # DNS label cap is 63 chars.  The leftmost label is everything
    # before the first dot.  We use the longest reasonable v4/v5 UUID
    # Theia would produce (36 chars) for the worst case.
    template = webview_external_endpoint(_WS, _DOMAIN)
    rendered = template.replace("{{uuid}}", "x" * 36)
    first_label = rendered.split(".", 1)[0]
    assert len(first_label) <= 63, f"first label is {len(first_label)} chars: {first_label}"


def test_external_endpoint_uses_single_left_label_for_wildcard_cert():
    # RFC 6125 wildcard certs only match ONE label at the leftmost
    # position — ``*.polaris-dev.xyz`` matches ``foo.polaris-dev.xyz``
    # but not ``a.b.polaris-dev.xyz``.  Concretely: the leftmost
    # label of the rendered hostname (everything up to the first
    # dot) must contain no dots itself.
    rendered = webview_external_endpoint(_WS, _DOMAIN).replace(
        "{{uuid}}", "x" * 36
    )
    leftmost_label = rendered.split(".", 1)[0]
    assert "." not in leftmost_label, (
        f"leftmost label {leftmost_label!r} has dots — wildcard cert "
        f"won't cover this hostname"
    )


def test_host_regexp_is_v3_syntax():
    # Traefik v3 takes raw Go regex inside HostRegexp(`...`).  The v2
    # named-group form ``{name:regex}`` would be silently misparsed.
    out = _webview_host_regexp(_WS, _DOMAIN)
    assert out.startswith("HostRegexp(`")
    assert out.endswith("`)")
    # No v2 placeholder syntax.
    assert "{name:" not in out and "}" not in out.split("`")[1]


def test_host_regexp_escapes_domain_dots():
    # Unescaped ``.`` in a Go regex matches any character — that would
    # let ``foo-Xpolaris-devXxyz`` (with arbitrary separators) sneak
    # through and match the wrong workspaces.  Every domain dot must
    # appear backslash-escaped.
    out = _webview_host_regexp(_WS, _DOMAIN)
    body = out[len("HostRegexp(`") : -len("`)")]
    # Every dot in the body should be escaped (preceded by ``\``).
    for i, ch in enumerate(body):
        if ch != ".":
            continue
        assert i > 0 and body[i - 1] == "\\", (
            f"unescaped dot at index {i} in regex body {body!r}"
        )
    # And every domain component must appear behind an escaped dot.
    for component in _DOMAIN.split("."):
        assert f"\\.{component}" in body


def test_host_regexp_anchors_on_short_hash():
    # The pattern must be specific to THIS workspace.  Other
    # workspaces share the wildcard hostname pattern; only the
    # 12-char short hash distinguishes them.  Regression guard:
    # ensure the short hash appears between the UUID body and the
    # domain.
    out = _webview_host_regexp(_WS, _DOMAIN)
    assert webview_short_hash(_WS) in out


def test_host_regexp_matches_real_theia_url():
    # End-to-end: render the regex and verify it matches a sample
    # rendered hostname Theia would produce.  This is the test that
    # would have caught both v2/v3 syntax mistakes AND missing dot
    # escapes in one shot.
    out = _webview_host_regexp(_WS, _DOMAIN)
    body = out[len("HostRegexp(`") : -len("`)")]
    pattern = re.compile(body)
    sample_uuid = "abcdef12-3456-7890-abcd-ef1234567890"
    sample_host = (
        f"wv-{sample_uuid}-{webview_short_hash(_WS)}.{_DOMAIN}"
    )
    assert pattern.match(sample_host), f"{body!r} did not match {sample_host!r}"
    # Negative: a different workspace's short hash must NOT match.
    other = "wv-" + sample_uuid + "-cafebabecafe." + _DOMAIN
    assert pattern.match(other) is None


def test_host_regexp_rejects_extra_label():
    # Defense in depth: someone routing ``foo.wv-...polaris-dev.xyz``
    # (with an extra leading label) shouldn't match — the wildcard
    # cert wouldn't cover them anyway.
    out = _webview_host_regexp(_WS, _DOMAIN)
    body = out[len("HostRegexp(`") : -len("`)")]
    pattern = re.compile(body)
    bad = f"foo.wv-{'a' * 36}-{webview_short_hash(_WS)}.{_DOMAIN}"
    assert pattern.match(bad) is None
