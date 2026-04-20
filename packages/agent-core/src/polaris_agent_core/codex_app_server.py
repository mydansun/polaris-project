"""Codex app-server client for the single-agent (polaris_agent) architecture.

Codex runs inside each workspace container (supervisor-managed `codex
app-server --listen ws://0.0.0.0:4455`).  Workers on the host connect to it
via the per-workspace docker bridge using the container's internal IP.

One ``PolarisCodexSession`` owns a long-lived WebSocket connection per
workspace.  Every project gets one Codex thread persisted on disk in the
workspace's ``codex-home`` named volume, so ``thread/resume`` works across
worker restarts:

    worker turn → session.ensure_thread(existing_id)
                → session.run_turn(thread, user_message, sink)
                → sink gets projected items → turn_items rows

All of Codex's real tools run inside the workspace container (its built-in
``exec_command`` + ``apply_patch``, plus the Playwright MCP child process
config'd in ``/home/workspace/.codex/config.toml``).  The session does not
inject any dynamic tools — the container is the sandbox.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.protocol import State


ServerRequestHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class PolarisCodexError(RuntimeError):
    """Any failure from the Codex app-server transport / protocol."""


class TurnTimeoutError(PolarisCodexError):
    """A turn was cut short because it hit the wall-clock budget."""

    def __init__(
        self,
        *,
        elapsed_seconds: float,
        budget_seconds: float,
    ) -> None:
        self.elapsed_seconds = elapsed_seconds
        self.budget_seconds = budget_seconds
        msg = (
            f"Turn total timeout: {elapsed_seconds:.0f}s exceeded the "
            f"{budget_seconds:.0f}s wall-clock budget"
        )
        super().__init__(msg)


class ConnectionLostError(PolarisCodexError):
    """The WebSocket connection to codex app-server dropped mid-turn."""

    def __init__(self, elapsed_seconds: float) -> None:
        self.elapsed_seconds = elapsed_seconds
        super().__init__(
            f"Codex app-server connection lost after {elapsed_seconds:.0f}s"
        )


# ─────────────────────────────────────────────────────────────────────────────
#  JSON-RPC WebSocket client
# ─────────────────────────────────────────────────────────────────────────────


class _JsonRpcWebSocketClient:
    """JSON-RPC 2.0 over a single WebSocket connection.

    Messages are newline-delimited JSON objects (one per ws frame).  Pending
    request futures are keyed by numeric id; notifications are pushed onto
    an ``asyncio.Queue`` for the session to consume.  Server-initiated
    requests (approval prompts, etc.) are dispatched to
    ``server_request_handler``.
    """

    def __init__(
        self,
        ws_url: str,
        server_request_handler: ServerRequestHandler | None = None,
        *,
        open_timeout: float = 30.0,
    ) -> None:
        self._ws_url = ws_url
        self._server_request_handler = server_request_handler
        self._open_timeout = open_timeout
        self._ws: ClientConnection | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._next_id = 1
        self._recent_errors: deque[str] = deque(maxlen=50)

    @property
    def last_errors(self) -> str | None:
        if not self._recent_errors:
            return None
        return "\n".join(self._recent_errors)

    def is_alive(self) -> bool:
        """True iff the underlying WebSocket is still OPEN.

        Used by the worker to reject stale cached sessions (e.g., after
        the workspace container was recreated) *before* the next turn
        wastes time trying to send on a closed pipe.
        """
        return self._ws is not None and self._ws.state == State.OPEN

    async def start(self) -> None:
        """Connect to codex app-server, retrying on ECONNREFUSED.

        The supervisor-managed codex-app-server inside the workspace
        container starts slightly after the container itself is up.  In the
        race window `websockets.connect` raises ``ConnectionRefusedError``
        (errno 111).  Retry briefly so the first turn after ``docker compose
        up`` doesn't have to be resent.
        """
        deadline = asyncio.get_running_loop().time() + self._open_timeout
        last_exc: Exception | None = None
        while True:
            try:
                self._ws = await asyncio.wait_for(
                    websockets.connect(
                        self._ws_url,
                        max_size=None,  # Codex can send large item payloads
                        ping_interval=30,
                        ping_timeout=20,
                    ),
                    timeout=5.0,
                )
                break
            except (ConnectionRefusedError, OSError) as exc:
                last_exc = exc
                if asyncio.get_running_loop().time() >= deadline:
                    raise PolarisCodexError(
                        f"Codex app-server WebSocket connect failed at "
                        f"{self._ws_url} after {self._open_timeout:.0f}s: {exc}"
                    ) from exc
                await asyncio.sleep(1.0)
            except (asyncio.TimeoutError, websockets.WebSocketException) as exc:
                raise PolarisCodexError(
                    f"Codex app-server WebSocket connect failed at "
                    f"{self._ws_url}: {exc}"
                ) from exc
        self._reader_task = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(PolarisCodexError("Codex session closed"))
        self._pending.clear()

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._ws is None:
            raise PolarisCodexError("Codex WebSocket not connected")
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        await self._ws.send(json.dumps(message))
        response = await future
        if "error" in response:
            error = response["error"]
            msg_text = error.get("message") if isinstance(error, dict) else str(error)
            raise PolarisCodexError(f"{method} failed: {msg_text}")
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self._ws is None:
            raise PolarisCodexError("Codex WebSocket not connected")
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._ws.send(json.dumps(message))

    async def next_notification(self) -> dict[str, Any]:
        return await self._notifications.get()

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                text = raw.decode() if isinstance(raw, bytes) else raw
                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    self._recent_errors.append(f"(bad frame) {text[:200]}")
                    continue
                if not isinstance(message, dict):
                    continue
                mid = message.get("id")
                if mid is not None and "method" in message:
                    await self._handle_server_request(mid, message)
                    continue
                if mid is not None and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if not fut.done():
                        fut.set_result(message)
                    continue
                if "method" in message:
                    await self._notifications.put(message)
        except websockets.ConnectionClosed:
            pass
        finally:
            err = PolarisCodexError("Codex app-server closed the WebSocket")
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(err)
            self._pending.clear()

    async def _handle_server_request(self, request_id: Any, message: dict[str, Any]) -> None:
        method = message.get("method")
        if not isinstance(method, str):
            await self._write_error(request_id, -32600, "method must be string")
            return
        if self._server_request_handler is None:
            await self._write_error(
                request_id, -32001, f"polaris does not handle server request {method!r}"
            )
            return
        params = message.get("params") or {}
        if not isinstance(params, dict):
            await self._write_error(request_id, -32602, f"{method!r} params must be object")
            return
        try:
            result = await self._server_request_handler(method, params)
        except Exception as exc:  # noqa: BLE001
            await self._write_error(request_id, -32002, str(exc))
            return
        await self._write_result(request_id, result)

    async def _write_result(self, request_id: Any, result: dict[str, Any]) -> None:
        if self._ws is None:
            return
        await self._ws.send(
            json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})
        )

    async def _write_error(self, request_id: Any, code: int, message: str) -> None:
        if self._ws is None:
            return
        await self._ws.send(
            json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
            )
        )


# ─────────────────────────────────────────────────────────────────────────────
#  polaris_agent configuration + turn sink protocol
# ─────────────────────────────────────────────────────────────────────────────


DynamicToolHandler = Callable[[str, dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class PolarisAgentConfig:
    """Static per-session configuration for PolarisCodexSession.

    `ws_url` points at the codex app-server running inside the workspace
    container (e.g. ``ws://172.18.0.5:4455/``).  `cwd` is the path Codex
    should start threads in — **from the container's perspective**
    (``/workspace``), NOT the host bind-mount path.

    `dynamic_tools` registers client-executed tools at thread/start (Codex
    experimental API).  When Codex invokes one, the server-request
    ``item/tool/call`` is routed to ``dynamic_tool_handler(name, args,
    raw_params) -> response_dict``.  Expected response shape:
    ``{"success": bool, "contentItems": [{"type": "inputText", "text":
    "..."}]}``.  The ``_dyn_response`` helper builds that for you.
    """

    ws_url: str
    cwd: str = "/workspace"
    base_instructions: str = ""
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    dynamic_tools: list[dict[str, Any]] = field(default_factory=list)
    dynamic_tool_handler: DynamicToolHandler | None = None
    model: str | None = None
    # Codex honors config.toml defaults if these are omitted.  We override at
    # thread/start time only when a workspace needs a per-session twist.
    sandbox_mode: str | None = None
    approval_policy: str = "never"
    # Handler for Codex's built-in request_user_input tool.  Called when
    # Codex sends item/tool/requestUserInput.  Must return a dict mapping
    # question_id → {"answers": ["selected option or free text"]}.
    # If None, the tool call returns an error telling Codex to proceed
    # without user input.
    user_input_handler: Callable[[list[dict[str, Any]], dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    # Total wall-clock budget for one turn: hard cap no matter how
    # productive the agent is.  Generous by default — complex scaffold +
    # verify turns legitimately take several minutes.
    turn_timeout_seconds: float = 900
    # How often to probe WebSocket liveness when no notifications arrive.
    # The websockets library already sends ping/pong (30s/20s), so this
    # interval only controls how quickly we *notice* a dead connection
    # and raise ConnectionLostError.
    liveness_check_interval_seconds: float = 30


class TurnItemSink(Protocol):
    async def on_turn_started(self, codex_turn_id: str) -> None: ...
    async def on_item_started(self, item: dict[str, Any]) -> None: ...
    async def on_item_completed(self, item: dict[str, Any]) -> None: ...
    async def on_agent_message_delta(self, text: str) -> None: ...
    async def on_turn_completed(self, status: str, error: str | None) -> None: ...


# ─────────────────────────────────────────────────────────────────────────────
#  PolarisCodexSession
# ─────────────────────────────────────────────────────────────────────────────


class PolarisCodexSession:
    def __init__(self, config: PolarisAgentConfig) -> None:
        self._config = config
        self._client: _JsonRpcWebSocketClient | None = None

    @classmethod
    @contextlib.asynccontextmanager
    async def open(cls, config: PolarisAgentConfig):
        session = cls(config)
        await session.start()
        try:
            yield session
        finally:
            await session.close()

    def is_alive(self) -> bool:
        """Cheap liveness probe for the worker-side session cache."""
        return self._client is not None and self._client.is_alive()

    @property
    def ws_url(self) -> str:
        """The ws URL this session was opened against — lets the worker
        detect container IP changes and drop stale sessions."""
        return self._config.ws_url

    async def start(self) -> None:
        self._client = _JsonRpcWebSocketClient(
            ws_url=self._config.ws_url,
            server_request_handler=self._handle_server_request,
        )
        await self._client.start()
        await self._client.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "polaris-worker",
                    "title": "Polaris Worker",
                    "version": "0.0.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await self._client.notify("initialized")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def ensure_thread(self, existing_thread_id: str | None) -> str:
        assert self._client is not None
        if existing_thread_id:
            try:
                return await self._resume_thread(existing_thread_id)
            except PolarisCodexError:
                pass
        return await self._start_thread()

    async def _start_thread(self) -> str:
        assert self._client is not None
        params: dict[str, Any] = {
            "cwd": self._config.cwd,
            "approvalPolicy": self._config.approval_policy,
            "ephemeral": False,
            "serviceName": "polaris-worker",
            "baseInstructions": self._config.base_instructions,
        }
        if self._config.sandbox_mode:
            params["sandbox"] = self._config.sandbox_mode
        if self._config.model:
            params["model"] = self._config.model
        if self._config.mcp_servers:
            params["config"] = {"mcp_servers": dict(self._config.mcp_servers)}
        if self._config.dynamic_tools:
            params["dynamicTools"] = list(self._config.dynamic_tools)
        resp = await self._client.request("thread/start", params)
        tid = (resp.get("thread") or {}).get("id")
        if not isinstance(tid, str) or not tid:
            raise PolarisCodexError("thread/start did not return a thread id")
        return tid

    async def _resume_thread(self, thread_id: str) -> str:
        assert self._client is not None
        params: dict[str, Any] = {
            "threadId": thread_id,
            "approvalPolicy": self._config.approval_policy,
            "baseInstructions": self._config.base_instructions,
        }
        if self._config.sandbox_mode:
            params["sandbox"] = self._config.sandbox_mode
        if self._config.model:
            params["model"] = self._config.model
        if self._config.mcp_servers:
            params["config"] = {"mcp_servers": dict(self._config.mcp_servers)}
        if self._config.dynamic_tools:
            # Resupply on every resume — Codex persists these in rollout
            # metadata but we keep the contract explicit for safety.
            params["dynamicTools"] = list(self._config.dynamic_tools)
        resp = await self._client.request("thread/resume", params)
        returned = (resp.get("thread") or {}).get("id")
        if returned != thread_id:
            raise PolarisCodexError(
                f"thread/resume returned mismatched id {returned!r}"
            )
        return thread_id

    async def run_turn(
        self,
        *,
        thread_id: str,
        user_message: str,
        project_id: UUID,
        workspace_id: UUID,
        turn_id: UUID,
        sink: TurnItemSink,
        mode: str = "plan",
        local_image_paths: list[str] | None = None,
    ) -> None:
        """Start a Codex turn.

        ``local_image_paths`` — optional filesystem paths inside the
        workspace container attached to the turn as
        ``{"type": "localImage", "path": ...}`` entries on the Codex
        ``TurnStartParams.input`` array.  Currently used for the
        project's mood board so every turn has a persistent visual
        anchor; Codex reads the file directly from the workspace
        (no network fetch).
        """
        assert self._client is not None
        input_items: list[dict[str, Any]] = [
            {"type": "text", "text": user_message, "text_elements": []}
        ]
        for path in local_image_paths or []:
            if path:
                input_items.append({"type": "localImage", "path": path})
        turn_params: dict[str, Any] = {
            "threadId": thread_id,
            "input": input_items,
        }
        # collaborationMode: "plan" enables request_user_input + plan-first;
        # "default" executes directly.  Always pass it so the model is explicit.
        turn_params["collaborationMode"] = {
            "mode": mode,
            "settings": {
                "model": self._config.model,
                "reasoningEffort": "medium",
            },
        }
        await self._client.request("turn/start", turn_params)
        try:
            await self._consume_turn(thread_id=thread_id, sink=sink)
        except TurnTimeoutError as exc:
            with contextlib.suppress(Exception):
                await self._client.request("turn/interrupt", {"threadId": thread_id})
            await sink.on_turn_completed("failed", str(exc))
        except ConnectionLostError as exc:
            await sink.on_turn_completed("failed", str(exc))

    async def interrupt(self, thread_id: str) -> None:
        assert self._client is not None
        with contextlib.suppress(Exception):
            await self._client.request("turn/interrupt", {"threadId": thread_id})

    async def steer(self, thread_id: str, additional_text: str) -> None:
        assert self._client is not None
        await self._client.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": additional_text, "text_elements": []}],
            },
        )

    async def _consume_turn(self, *, thread_id: str, sink: TurnItemSink) -> None:
        """Drain Codex notifications for this turn.

        Two safeguards prevent the turn from running forever:

        - **total timeout** (``turn_timeout_seconds``): absolute wall-clock
          cap regardless of productivity.
        - **liveness check**: when no notification arrives within
          ``liveness_check_interval_seconds``, probe ``is_alive()`` on the
          WebSocket.  If the connection dropped (container restart, network
          partition, codex crash), raise ``ConnectionLostError`` immediately.
          If the connection is still healthy, keep waiting — the agent is
          simply busy with a long-running operation (npm install, docker
          build, etc.).

        This replaces the old idle-timeout approach which would kill
        perfectly healthy turns during long shell commands.
        """
        assert self._client is not None
        loop = asyncio.get_running_loop()
        start = loop.time()
        total_budget = self._config.turn_timeout_seconds
        check_interval = self._config.liveness_check_interval_seconds

        while True:
            now = loop.time()
            elapsed = now - start
            if elapsed >= total_budget:
                raise TurnTimeoutError(
                    elapsed_seconds=elapsed,
                    budget_seconds=total_budget,
                )

            remaining = total_budget - elapsed
            wait = max(0.01, min(check_interval, remaining))
            try:
                msg = await asyncio.wait_for(
                    self._client.next_notification(), timeout=wait
                )
            except asyncio.TimeoutError:
                # No notification within the check interval — probe the
                # WebSocket.  If still alive, Codex is just busy; loop back
                # and keep waiting.  If dead, bail out immediately.
                if not self._client.is_alive():
                    raise ConnectionLostError(
                        elapsed_seconds=loop.time() - start
                    )
                continue
            method = msg.get("method")
            params = msg.get("params") or {}
            if not isinstance(method, str) or not isinstance(params, dict):
                continue

            msg_thread = params.get("threadId")
            if isinstance(msg_thread, str) and msg_thread != thread_id:
                continue

            if method == "item/started":
                item = params.get("item")
                if isinstance(item, dict):
                    await sink.on_item_started(item)
                continue

            if method == "item/completed":
                item = params.get("item")
                if isinstance(item, dict):
                    await sink.on_item_completed(item)
                continue

            if method == "item/agentMessage/delta":
                delta = params.get("delta")
                if isinstance(delta, str):
                    await sink.on_agent_message_delta(delta)
                continue

            if method == "turn/started":
                turn = params.get("turn") or {}
                if isinstance(turn, dict):
                    tid = turn.get("id")
                    if isinstance(tid, str):
                        await sink.on_turn_started(tid)
                continue

            if method == "turn/completed":
                turn = params.get("turn") or {}
                status = "completed"
                error: str | None = None
                if isinstance(turn, dict):
                    raw = turn.get("status")
                    if raw == "interrupted":
                        status = "interrupted"
                    elif raw == "failed":
                        status = "failed"
                        err = turn.get("error") or {}
                        if isinstance(err, dict):
                            msg_text = err.get("message")
                            if isinstance(msg_text, str):
                                error = msg_text
                await sink.on_turn_completed(status, error)
                return

    async def _handle_server_request(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Reply to Codex-side approval prompts.

        The container IS our sandbox, so we auto-accept everything except
        requests we don't know how to reply to (those raise).  Auto-accept
        on `exec_command` is the critical change vs the host-side variant,
        where we declined because Codex had no real container to run in.
        """
        if method in ("item/commandExecution/requestApproval", "execCommandApproval"):
            return {"decision": "accept"}
        if method == "item/fileChange/requestApproval":
            return {"decision": "accept"}
        if method == "item/permissions/requestApproval":
            return {"permissions": {"fileSystem": None, "network": None}, "scope": "turn"}
        if method == "mcpServer/elicitation/request":
            return {"action": "accept"}
        if method == "item/dynamicToolCall/requestApproval":
            return {"decision": "accept"}
        if method == "item/tool/requestUserInput":
            questions = params.get("questions", [])
            if self._config.user_input_handler is not None:
                answers = await self._config.user_input_handler(questions, params)
                return {"answers": answers}
            # No handler — tell Codex to proceed with defaults.
            return {"answers": {}}
        if method == "item/tool/call":
            tool = params.get("tool")
            args = params.get("arguments")
            if not isinstance(args, dict):
                args = {}
            if self._config.dynamic_tool_handler is None:
                return _dyn_response(False, {"error": "no dynamic tool handler"})
            if not isinstance(tool, str):
                return _dyn_response(False, {"error": "missing tool name"})
            return await self._config.dynamic_tool_handler(tool, args, params)
        raise PolarisCodexError(f"unsupported Codex server request {method!r}")


# ─────────────────────────────────────────────────────────────────────────────
#  helpers
# ─────────────────────────────────────────────────────────────────────────────


def parse_command(command: str) -> list[str]:
    """Split a command string into argv (kept for backward compat in tests)."""
    tokens = shlex.split(command)
    if not tokens:
        raise PolarisCodexError("command is empty")
    return tokens


def _dyn_response(success: bool, payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a dict payload as the Codex dynamic-tool response envelope."""
    return {
        "success": success,
        "contentItems": [
            {"type": "inputText", "text": json.dumps(payload, default=str)}
        ],
    }
