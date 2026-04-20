"""HTTP MCP server mounted on the FastAPI app.

Exposes Codex-facing tools (search_photos for now — more to come) over
streamable-HTTP MCP at ``/mcp``.  Using HTTP instead of stdio means the
workspace container doesn't need Python or fastmcp installed — each
Codex session just connects by URL.

Authentication: Codex's `mcp add --url --bearer-token-env-var
POLARIS_WORKSPACE_TOKEN` sends ``Authorization: Bearer <workspace_token>``
on every request.  The :class:`_BearerWorkspaceAuth` middleware validates
the token against the ``workspaces`` table before handing off to the MCP
protocol layer.

Secrets (``UNSPLASH_ACCESS_KEY`` / ``S3_*``) stay server-side.  The
workspace container only knows its own workspace token (injected by
``services/compose.py``).
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP
from sqlalchemy import select
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from polaris_api.config import get_settings
from polaris_api.db import SessionLocal
from polaris_api.models import Workspace
from polaris_api.services import iconify
from polaris_api.services.unsplash import search_and_cache


logger = logging.getLogger(__name__)


mcp = FastMCP("Polaris MCP")


@mcp.tool()
async def search_photos(
    query: str,
    per_page: int = 6,
    orientation: str | None = None,
    color: str | None = None,
    content_filter: str = "low",
) -> list[dict[str, Any]]:
    """Search Unsplash and return photos re-hosted on Polaris S3.

    Use this tool when you need real photographs to embed in a generated
    page (hero image, product shot, mood anchor, etc.).  Every returned
    ``urls.regular`` / ``urls.small`` is a stable Polaris S3 URL — the
    Unsplash CDN has uptime issues and should NOT be hot-linked.

    The ``attribution_text`` / ``attribution_html`` fields MUST be
    rendered near the image (required by the Unsplash API license).

    Args:
        query: Keyword(s), e.g. "mountain landscape", "coffee shop interior".
        per_page: 1–30, default 6.
        orientation: "landscape" | "portrait" | "squarish" | omit.
        color: Unsplash color hint (black_and_white / black / white /
               yellow / orange / red / purple / magenta / green / teal /
               blue) or omit.
        content_filter: "low" (default) | "high".
    """
    settings = get_settings()
    async with SessionLocal() as db:
        photos = await search_and_cache(
            query=query,
            per_page=per_page,
            orientation=orientation,
            color=color,
            content_filter=content_filter,
            session=db,
            settings=settings,
        )
    return photos


# ── Iconify icon search / retrieval ──────────────────────────────────────
# Iconify is a keyless public catalog of 200K+ open-source SVG icons across
# 200+ sets (lucide, heroicons, mdi, tabler, carbon, ri, ...).  These tools
# proxy the four main endpoints of `api.iconify.design` so Codex can find
# and embed icons without guessing path data or making up SVG by hand.


@mcp.tool()
async def get_all_icon_sets() -> Any:
    """List every Iconify icon set with basic metadata.

    Useful for picking a set that fits the project's visual language
    (e.g. `lucide` for minimal line icons, `mdi` for Material, `tabler`
    for uniform stroke width, `heroicons` for the Tailwind team's set).

    Returns the raw Iconify ``/collections`` response.
    """
    return await iconify.list_collections()


@mcp.tool()
async def get_icon_set(set: str) -> Any:  # noqa: A002 (mirror upstream name)
    """Detail about one icon set: total icon count, categories, author.

    Args:
        set: Iconify prefix, e.g. "mdi", "lucide", "heroicons", "tabler".
    """
    return await iconify.get_collection(set)


@mcp.tool()
async def search_icons(
    query: str,
    limit: int = 64,
    start: int | None = None,
    prefix: str | None = None,
) -> Any:
    """Search the Iconify catalog for icons matching ``query``.

    Use this when you need an icon for a button, nav item, status pill,
    or empty state.  Prefer well-known sets (`lucide`, `heroicons`,
    `mdi`, `tabler`, `ri`, `carbon`) for the most consistent visual
    vocabulary; pass ``prefix`` to narrow results to one set.

    Pair with ``get_icon`` to fetch the actual SVG data + framework
    usage snippets for the icon you settle on.

    Args:
        query: Keyword(s), e.g. "home", "chevron right", "credit card".
        limit: 32–999, default 64.  Values below 32 are clamped up to 32
               (Iconify's minimum); above 999 clamped down to 999.
        start: Optional pagination offset.
        prefix: Optional Iconify set prefix (e.g. "lucide") to scope the
                search to one set.
    """
    return await iconify.search(
        query=query, limit=limit, start=start, prefix=prefix
    )


@mcp.tool()
async def get_icon(set: str, icon: str) -> dict[str, Any]:  # noqa: A002
    """Fetch one icon's full data plus ready-to-paste usage snippets.

    Returns a dict with:
      * ``IconifyIcon`` — raw JSON from Iconify (body, width, height,
        viewBox), suitable for libraries that accept an IconifyIcon object.
      * ``css`` — UnoCSS and Tailwind class snippets.
      * ``web`` — per-framework snippets for the Iconify web component,
        Vue, React, Svelte, Astro, and Unplugin Icons.

    Args:
        set: Iconify set prefix, e.g. "lucide".
        icon: Icon name within that set, e.g. "home".
    """
    return await iconify.get_icon_data(set, icon)


class _BearerWorkspaceAuth:
    """Starlette ASGI middleware — gate every MCP request on a valid
    ``Authorization: Bearer <workspace_token>`` header.

    We can't use FastAPI's ``Depends`` / ``Header`` plumbing here because
    the MCP transport is a raw Starlette mount, not a router endpoint.
    Raw ASGI middleware handles it at the right layer.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        auth = _header(scope, b"authorization")
        if not auth or not auth.lower().startswith("bearer "):
            await _json_401(send, "Missing Bearer token")
            return

        token = auth[7:].strip()
        if not token:
            await _json_401(send, "Empty Bearer token")
            return

        async with SessionLocal() as db:
            row = (
                await db.execute(
                    select(Workspace).where(Workspace.workspace_token == token)
                )
            ).scalars().first()
            if row is None:
                logger.warning("MCP: invalid workspace token (len=%d)", len(token))
                await _json_401(send, "Invalid workspace token")
                return
            # Stash workspace id on scope — downstream tools can pull it if
            # we ever need per-workspace scoping.  Currently unused.
            scope.setdefault("state", {})
            scope["state"]["workspace_id"] = row.id  # type: ignore[index]

        await self.app(scope, receive, send)


def _header(scope: Scope, name: bytes) -> str | None:
    for k, v in scope.get("headers") or []:
        if k.lower() == name:
            try:
                return v.decode("latin-1")
            except Exception:
                return None
    return None


async def _json_401(send: Send, detail: str) -> None:
    body = b'{"error":"unauthorized","detail":"' + detail.encode("ascii", "replace") + b'"}'
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def build_mcp_app() -> Any:
    """Return the Starlette ASGI app that hosts the MCP protocol.

    Mounted on the main FastAPI app under ``/mcp`` (see main.py).  The
    client-visible URL is therefore ``{POLARIS_API_URL}/mcp``.
    """
    # `path="/"` strips fastmcp's internal /mcp prefix so that, mounted at
    # FastAPI's /mcp, the external URL stays plain ``/mcp`` (not /mcp/mcp).
    return mcp.http_app(
        path="/",
        transport="streamable-http",
        middleware=[Middleware(_BearerWorkspaceAuth)],
        # Stateless: each request carries its own state (simpler for proxies,
        # zero server-side session tracking).
        stateless_http=True,
    )
