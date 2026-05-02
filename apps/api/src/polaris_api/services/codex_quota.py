"""Read codex 5-hour rate-limit usage for a workspace.

The codex app-server inside each workspace container exposes a JSON-RPC
method ``account/rateLimits/read`` that returns the same numbers Codex
shows when a user types ``/status``.  We open a short-lived WebSocket
from the api container, run initialize → request, then close.

Result is cached in Redis (``codex:quota:{workspace_id}``, 30 s TTL) so
the chat-pane indicator can poll once a minute without hammering codex
or holding a long-lived session inside the api process.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import UUID

import websockets
from redis.asyncio import Redis

logger = logging.getLogger(__name__)

CODEX_APP_SERVER_PORT = 4455
CACHE_TTL_SECONDS = 30
WS_TIMEOUT_SECONDS = 5.0


def _workspace_container_name(workspace_id: UUID) -> str:
    hash_id = str(workspace_id).replace("-", "")[:24]
    return f"polaris-ws-{hash_id}"


async def _resolve_workspace_ip(workspace_id: UUID) -> str | None:
    """Look up the workspace container's IP on ``polaris-shared``.

    Returns None if the container isn't running or hasn't joined the
    shared network yet.  Callers treat that as "quota not available";
    the indicator simply hides until a workspace is up.
    """
    container = _workspace_container_name(workspace_id)
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "inspect",
            "--format",
            '{{with index .NetworkSettings.Networks "polaris-shared"}}'
            "{{.IPAddress}}{{end}}",
            container,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
    except (asyncio.TimeoutError, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    ip = stdout.decode().strip()
    return ip or None


async def _request_rate_limits(ws_url: str) -> dict[str, Any] | None:
    """Open a one-shot codex JSON-RPC session and call account/rateLimits/read.

    The codex app-server requires an `initialize` handshake before any
    other request; we send it, fire-and-forget the `initialized`
    notification, then issue the rate-limits read.  All within a 5-second
    budget — a healthy local WS round-trip is sub-100 ms.
    """
    try:
        async with websockets.connect(
            ws_url,
            max_size=2**20,
            open_timeout=WS_TIMEOUT_SECONDS,
            close_timeout=1.0,
            ping_interval=None,
        ) as ws:
            async def call(method: str, params: dict[str, Any] | None, request_id: int) -> dict[str, Any]:
                msg: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
                if params is not None:
                    msg["params"] = params
                await ws.send(json.dumps(msg))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=WS_TIMEOUT_SECONDS)
                    text = raw.decode() if isinstance(raw, bytes) else raw
                    try:
                        parsed = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict) and parsed.get("id") == request_id:
                        return parsed
                    # Notifications and unrelated messages are ignored.

            await call(
                "initialize",
                {
                    "clientInfo": {
                        "name": "polaris-api",
                        "title": "Polaris API",
                        "version": "0.0.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
                1,
            )
            await ws.send(json.dumps({"jsonrpc": "2.0", "method": "initialized"}))
            response = await call("account/rateLimits/read", None, 2)
            if "error" in response:
                err = response["error"]
                logger.info("codex rate-limits read returned error: %s", err)
                return None
            result = response.get("result")
            return result if isinstance(result, dict) else None
    except (asyncio.TimeoutError, OSError, websockets.WebSocketException) as exc:
        logger.info("codex rate-limits read failed: %s", exc)
        return None


def _project_window(window: Any) -> dict[str, Any] | None:
    if not isinstance(window, dict):
        return None
    used = window.get("usedPercent")
    if not isinstance(used, (int, float)):
        return None
    return {
        "used_percent": float(used),
        "window_minutes": window.get("windowDurationMins"),
        "resets_at": window.get("resetsAt"),
    }


def _project_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Reduce the raw codex response to just what the chat indicator needs."""
    rate_limits = snapshot.get("rateLimits") or {}
    if not isinstance(rate_limits, dict):
        rate_limits = {}
    return {
        "primary": _project_window(rate_limits.get("primary")),
        "secondary": _project_window(rate_limits.get("secondary")),
        "plan_type": rate_limits.get("planType"),
    }


async def get_codex_quota(
    *, workspace_id: UUID, redis: Redis
) -> dict[str, Any] | None:
    """Cached entry point used by the route handler."""
    cache_key = f"codex:quota:{workspace_id}"
    cached = await redis.get(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            pass

    ip = await _resolve_workspace_ip(workspace_id)
    if not ip:
        return None
    snapshot = await _request_rate_limits(f"ws://{ip}:{CODEX_APP_SERVER_PORT}/")
    if snapshot is None:
        return None
    payload = _project_snapshot(snapshot)
    await redis.set(cache_key, json.dumps(payload), ex=CACHE_TTL_SECONDS)
    return payload
