"""Thin async proxy for the public `api.iconify.design` API.

Iconify is a keyless, CDN-backed public service — no auth header, no
secret, no rate limiting in practice.  We don't cache anywhere locally:
their edge cache is already fast, icon payloads are tiny JSON, and the
caller (Codex) takes the data and immediately bakes it into generated
source, so a miss on our side costs one extra HTTP round-trip at most.

Four operations, mirroring the reference `iconify-mcp-server` (TS, stdio):
  * list all collections
  * get one collection's details
  * full-text search
  * get a single icon's full IconifyIcon JSON + per-framework usage
    snippets (unocss / tailwind / web-component / Vue / React / Svelte
    / Astro / Unplugin).

The snippet strings must stay byte-identical to the reference so that
anything Codex learns about Iconify's own MCP applies here.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


ICONIFY_API_BASE = "https://api.iconify.design"
_USER_AGENT = "Polaris Iconify proxy (polaris-dev.xyz)"


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    """One-shot GET on the Iconify API.  Raises on non-2xx."""
    async with httpx.AsyncClient(
        timeout=15.0, headers={"User-Agent": _USER_AGENT}
    ) as client:
        resp = await client.get(
            f"{ICONIFY_API_BASE}{path}", params=params or {}
        )
        resp.raise_for_status()
        return resp.json()


async def list_collections() -> Any:
    """GET /collections — all icon sets, each with basic metadata."""
    logger.info("iconify: list_collections")
    return await _get("/collections")


async def get_collection(prefix: str) -> Any:
    """GET /collection?prefix=<prefix>&info=true — single set detail."""
    logger.info("iconify: get_collection prefix=%s", prefix)
    return await _get("/collection", {"prefix": prefix, "info": "true"})


async def search(
    *,
    query: str,
    limit: int = 64,
    start: int | None = None,
    prefix: str | None = None,
) -> Any:
    """GET /search — text search across all or one icon set.

    Limit is clamped to ``[32, 999]`` to match the upstream MCP reference
    (the API refuses <32 and hard-caps at 999)."""
    clamped = max(32, min(999, int(limit)))
    params: dict[str, Any] = {"query": query, "limit": clamped}
    if start is not None:
        params["start"] = int(start)
    if prefix:
        params["prefix"] = prefix
    logger.info(
        "iconify: search query=%r limit=%d start=%s prefix=%s",
        query, clamped, start, prefix,
    )
    return await _get("/search", params)


async def get_icon_data(icon_set: str, icon: str) -> dict[str, Any]:
    """GET /{set}.json?icons=<icon> + attach framework usage snippets.

    Returns a dict with three top-level keys:
      * ``IconifyIcon`` — raw upstream JSON (body + width/height/viewBox)
      * ``css``        — ``unocss`` / ``tailwindcss`` snippets
      * ``web``        — ``IconifyIconWebComponent`` / ``IconifyForVue``
                         / ``IconifyForReact`` / ``IconifyForSvelte`` /
                         ``AstroIcon`` / ``UnpluginIcons`` snippets
    """
    logger.info("iconify: get_icon_data set=%s icon=%s", icon_set, icon)
    raw = await _get(f"/{icon_set}.json", {"icons": icon})
    return {
        "IconifyIcon": raw,
        "css": {
            "unocss": (
                f'<div class="i-{icon_set}:{icon}" style="color: #fff;"></div>'
            ),
            "tailwindcss": (
                f'<span class="icon-[{icon_set}--{icon}]" '
                f'style="color: #fff;"></span>'
            ),
        },
        "web": {
            "IconifyIconWebComponent": (
                f'<iconify-icon icon="{icon_set}:{icon}" '
                f'width="24" height="24" style="color: #fff"></iconify-icon>'
            ),
            "IconifyForVue": (
                f'<Icon icon="{icon_set}:{icon}" width="24" height="24" '
                f'style="color: #fff" />'
            ),
            "IconifyForReact": (
                f'<Icon icon="{icon_set}:{icon}" width="24" height="24" '
                f'style={{{{color: "#fff"}}}} />'
            ),
            "IconifyForSvelte": (
                f'<Icon icon="{icon_set}:{icon}" width="24" height="24" '
                f'style={{{{color: "#fff"}}}} />'
            ),
            "AstroIcon": f'<Icon name="{icon_set}:{icon}" />',
            "UnpluginIcons": (
                f"import {_capitalize_dash(icon_set)}"
                f"{_capitalize_dash(icon)} from "
                f"'~icons/{icon_set}/{icon}';"
            ),
        },
    }


def _capitalize_dash(text: str) -> str:
    """``"mdi-light"`` → ``"MdiLight"`` — mirror of the reference TS util."""
    return "".join(part.capitalize() for part in text.split("-"))
