"""The single source of truth for every env var the platform reads.

All three CLIs (build / up / down), the wizard, the validators, and the
README all consult this list.  No duplicate metadata lives in
``.env.example`` or scattered docstrings.

Each entry declares:
  * ``key``: the env var name (also the .env line key)
  * ``required``: must be non-empty for ``up.py`` to start the platform
  * ``secret``: prompt should mask input + omit from logs
  * ``default``: shown as the suggested answer in the wizard; ``None``
    means the wizard insists on user input
  * ``help``:  one-line guidance shown above the prompt
  * ``validator``: optional callable ``(value, env) -> ValidationResult``
    invoked after the user enters a value.  Receives the **whole env
    dict** so validators that depend on cross-fields (CF token needs
    POLARIS_DOMAIN to look up the zone) work without parameter
    threading.
"""

import secrets
from dataclasses import dataclass
from typing import Callable, Optional

from . import validators
from .validators import ValidationResult


@dataclass(frozen=True)
class Field:
    key: str
    required: bool
    secret: bool
    default: Optional[str]
    help: str
    validator: Optional[Callable[[str, dict[str, str]], ValidationResult]] = None


def _gen_session_secret() -> str:
    return secrets.token_hex(32)


# ── validators that need cross-field context ────────────────────────────


def _validate_domain(value: str, _env: dict[str, str]) -> ValidationResult:
    return validators.domain_format(value)


def _validate_cf_token(value: str, env: dict[str, str]) -> ValidationResult:
    zone = env.get("POLARIS_DOMAIN", "polaris-dev.xyz").strip() or "polaris-dev.xyz"
    return validators.cf_token(value, zone)


def _validate_pinterest(value: str, env: dict[str, str]) -> ValidationResult:
    base = env.get(
        "POLARIS_PINTEREST_TOOL_BASE", "https://pint-polaris-infra.miyuko.name"
    )
    return validators.pinterest_token(value, base)


def _validate_openai(value: str, _env: dict[str, str]) -> ValidationResult:
    return validators.openai_key(value)


# ── The actual catalog ───────────────────────────────────────────────────


FIELDS: list[Field] = [
    Field(
        key="POLARIS_DOMAIN",
        required=True,
        secret=False,
        default="polaris-dev.xyz",
        help="Wildcard root used for ide-*/browser-*/published subdomains. "
        "Must be a domain whose DNS lives on Cloudflare (DNS-01 challenge).",
        validator=_validate_domain,
    ),
    Field(
        key="CF_DNS_API_TOKEN",
        required=True,
        secret=True,
        default=None,
        help="Cloudflare API token, scoped to Zone:DNS:Edit + Zone:Zone:Read.",
        validator=_validate_cf_token,
    ),
    Field(
        key="OPENAI_SECRET",
        required=True,
        secret=True,
        default=None,
        help="OpenAI API key used by the design-intent pipeline + audit.",
        validator=_validate_openai,
    ),
    Field(
        key="POLARIS_PINTEREST_TOOL_API_KEY",
        required=True,
        secret=True,
        default=None,
        help="X-API-Key for the Pinterest scraper at pint-polaris-infra.miyuko.name.",
        validator=_validate_pinterest,
    ),
    Field(
        key="POLARIS_PINTEREST_TOOL_BASE",
        required=False,
        secret=False,
        default="https://pint-polaris-infra.miyuko.name",
        help="Pinterest scraper endpoint base URL.",
    ),
    Field(
        key="SESSION_SECRET",
        required=True,
        secret=True,
        default=None,  # auto-generate via the wizard's "leave blank → generate"
        help="64-byte hex string for cookie signing. Leave blank to auto-generate.",
    ),
    Field(
        key="POLARIS_DEV_USER_EMAIL",
        required=False,
        secret=False,
        default="dev@polaris.local",
        help="Enables the dev-login shortcut. Leave blank for shared/staging hosts.",
    ),
    Field(
        key="POLARIS_DEV_USER_NAME",
        required=False,
        secret=False,
        default="Polaris Dev",
        help="Display name for the dev-login user.",
    ),
    Field(
        key="UNSPLASH_ACCESS_KEY",
        required=False,
        secret=True,
        default="",
        help="Unsplash access key (server-side only). Empty disables search_photos MCP.",
    ),
    Field(
        key="POLARIS_PROD_DOMAIN_BASE",
        required=False,
        secret=False,
        # Wizard fills this with ``prod.<POLARIS_DOMAIN>`` after the
        # domain is entered (see ``autogenerate_blank``).  Override
        # only if your published-projects subtree lives under a
        # different label, e.g. ``live.example.com``.
        default=None,
        help="Subdomain base for published projects "
        "(<uuid>.<base>).  Leave blank to derive `prod.<POLARIS_DOMAIN>`.",
    ),
    Field(
        key="POLARIS_HOST_CODEX_AUTH_PATH",
        required=False,
        secret=False,
        # Runtime default expands ~ in the current user's HOME — see
        # ``runtime_default``.  ``up.py`` will create an empty stub file
        # at this path if it doesn't exist, so the bind-mount never fails;
        # workspace codex sees an empty/invalid auth.json and prompts the
        # user to authenticate.  Once the user runs ``codex login`` on the
        # host (or inside any workspace), the bind-mount propagates the
        # populated auth.json everywhere.
        default=None,
        help="Host path to codex auth.json (default: ~/.codex/auth.json). "
        "Mounted into workspace containers; up.py auto-creates it if missing.",
    ),
]


def runtime_default(field: Field) -> str | None:
    """Field defaults that depend on the runtime environment (e.g. expand
    ``~``).  Centralizing here so the wizard's ``_resolve_default`` path
    can call this without knowing about each field's quirks."""
    import os

    if field.key == "POLARIS_HOST_CODEX_AUTH_PATH":
        return os.path.expanduser("~/.codex/auth.json")
    return field.default


def by_key(key: str) -> Field | None:
    for f in FIELDS:
        if f.key == key:
            return f
    return None


def required_keys(_env: dict[str, str]) -> list[str]:
    """Keys whose absence/empty value blocks ``up.py``.

    Currently no conditional logic — every required field is unconditional.
    Argument kept for API stability in case we add a future mode.
    """
    return [f.key for f in FIELDS if f.required]


def autogenerate_blank(env: dict[str, str]) -> dict[str, str]:
    """Fill in blanks for fields whose default depends on another
    field's value or is computed at runtime."""
    out: dict[str, str] = {}
    if not env.get("SESSION_SECRET", "").strip() or env.get(
        "SESSION_SECRET"
    ) == "polaris-dev-secret-change-me":
        out["SESSION_SECRET"] = _gen_session_secret()
    # POLARIS_PROD_DOMAIN_BASE defaults to `prod.<POLARIS_DOMAIN>` so
    # that compose.dev.yaml can interpolate it without nested
    # substitution (which docker compose v2 doesn't support reliably).
    if not env.get("POLARIS_PROD_DOMAIN_BASE", "").strip():
        domain = env.get("POLARIS_DOMAIN", "").strip()
        if domain:
            out["POLARIS_PROD_DOMAIN_BASE"] = f"prod.{domain}"
    return out
