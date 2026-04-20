"""Codex adapter — wraps :class:`PolarisCodexSession` behind the generic
:class:`Agent` contract.

Responsibilities:
  * Maintain a per-workspace WebSocket session cache (codex app-server
    lives inside the workspace container as a long-lived supervisord
    process; reconnecting on every run is wasteful).
  * Ensure the workspace-container's ``$CODEX_HOME/AGENTS.md`` reflects
    the project's active design intent *before* Codex's plan round runs.
  * Translate Codex's item stream (``agentMessage`` / ``plan`` / ...) into
    namespaced events (``codex:agent_message`` / ``codex:plan`` / ...)
    via the shared :class:`EventSink`.
  * Forward ``requestUserInput`` calls through the shared clarification
    pipeline.
  * Persist ``set_project_root`` + kick-off baseline gitignore / git init.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import posixpath
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
from redis.asyncio import Redis

from polaris_agent_core import (
    PolarisAgentConfig,
    PolarisCodexError,
    PolarisCodexSession,
    dyn_response,
)
from polaris_api.services.gitignore_baseline import ensure_baseline_gitignore
from polaris_api.services.workspaces import WorkspaceError, run_git

from polaris_worker.agents.base import (
    Agent,
    AgentKind,
    EventSink,
    RunContext,
    RunOutcome,
    SessionContext,
)
from polaris_worker.clarification import wait_for_answers
from polaris_worker.codex_agents_md import (
    load_active_design_intent,
    render_design_intent_markdown,
    write_codex_home_agents_md,
)
from polaris_worker.polaris_agent_prompt import POLARIS_AGENT_BASE_INSTRUCTIONS
from polaris_worker.queue import session_events_channel

logger = logging.getLogger(__name__)


CODEX_APP_SERVER_PORT = 4455
WORKSPACE_CONTAINER_CWD = "/workspace"


_PLAN_PLAIN_SYSTEM_PROMPT = """You rewrite a coding-assistant's technical
plan into text a NON-TECHNICAL USER can scan in under a minute and
understand what the product will feel like.

The audience is the PROJECT OWNER — someone who commissioned a
website and wants to know "so what does this mean for me".  They do
not read JSX, know what Tailwind is, care which fonts are used, or
want to see hex colors.  They want to know what the page will LOOK
LIKE, what it will DO, and how close it is to what they asked for.

# HARD BANS (violating any of these means a failed rewrite)

Never mention or hint at:
- Framework / library / runtime names: React, Vue, Next, Vite,
  Tailwind, UnoCSS, Framer Motion, shadcn, Node, Bun, npm, Prisma,
  Postgres, Redis, Docker, Playwright, etc.
- CSS syntax or token names: "background", "surface", "primary text",
  "border", "accent", "rounded-lg", "px-4", "flex", etc.
- Color hex codes (`#F5F1E8`), color variables, theme tokens.
- Specific font family names ("Cormorant Garamond", "Noto Sans SC",
  "Inter").  You may say "a warm serif for headings" — never name the
  font.
- Code-level artifacts: component names, type definitions, interface
  shapes, prop lists, "Public Interfaces", "Types", `useState`, JSX
  tags, file paths, import statements.
- Animation timing numbers ("400ms", "ease-out"), pixel measurements.
- Viewport-specific jargon ("sticky header", "100svh", "z-index",
  "breakpoint", "viewport").
- Meta-sections ("Assumptions", "Test Plan", "Summary", "Key Changes",
  "Public Interfaces") — rewrite content into narrative, drop the
  labels.

# WHAT TO WRITE INSTEAD

Describe the USER-FACING EXPERIENCE in plain language.  Think like a
friend explaining what the website will feel like when someone opens
it on their phone.

GOOD (plain): "Scroll down and you'll see three short reasons why
beginners love this school — each with a small illustration."
BAD (technical): "3-4 feature cards in a grid with lucide icons at
24px, stacked vertically on mobile."

GOOD: "The top of the page shows one big photo of a sunlit practice
green, with the club name and a single button that says 'Book a
Trial Lesson'."
BAD: "Hero section uses a full-bleed image background with sticky nav
transitioning to solid on scroll > 80px."

GOOD: "At the bottom, there's a short form — name, phone number, when
you'd like to come — and a button to send it."
BAD: "Form component with fields {name, phone, timeSlot, experience}
in controlled state, validation on blur, idle/submitting/success/error
states."

# STRUCTURE

- Keep the markdown headings ONLY if they describe user-visible
  sections of the page ("At the top", "Getting started section",
  "Form at the bottom" — translated to the input language).  Delete
  headings that refer to implementation work ("Technical foundation",
  "Public interfaces", "Test plan").
- Prefer short paragraphs and compact bullet lists.
- The whole rewrite should be READABLE IN 60-90 SECONDS.  If the
  original is longer, cut implementation details, not user-visible
  ones.
- Match the input language exactly (Chinese → Chinese, English →
  English).  Do not translate.
- Do not add preamble / sign-off.  Output ONLY the rewritten markdown.

If a section of the original has NO user-visible outcome (e.g. pure
tech stack setup, file structure, type definitions), delete it rather
than trying to translate it."""


async def _translate_plan_to_plain(
    text: str,
    *,
    settings: Any,
    log: logging.Logger | logging.LoggerAdapter,
) -> str | None:
    """Call the configured plan-plain model to rewrite a Codex plan for
    non-technical users.  Returns the rewritten markdown, or None on
    failure (caller falls back to technical-only rendering)."""
    if not text or not text.strip():
        return None
    api_key = getattr(settings, "openai_api_key", "") or ""
    model_name = getattr(settings, "codex_plan_plain_model", "") or ""
    if not api_key or not model_name:
        log.warning(
            "plan translation skipped — no API key or model configured"
        )
        return None
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_openai import ChatOpenAI

        log.info("plan: translating to plain (chars=%d, model=%s)", len(text), model_name)
        model = ChatOpenAI(
            model=model_name,
            api_key=api_key,
            temperature=0.3,
        )
        resp = await model.ainvoke(
            [
                SystemMessage(content=_PLAN_PLAIN_SYSTEM_PROMPT),
                HumanMessage(content=f"原始 plan / original plan:\n\n{text}"),
            ]
        )
        plain = str(getattr(resp, "content", "") or "").strip()
        log.info("plan: translated (chars=%d)", len(plain))
        return plain or None
    except Exception:  # noqa: BLE001
        log.warning(
            "plan translation failed — falling back to technical only",
            exc_info=True,
        )
        return None


SET_PROJECT_ROOT_TOOL: dict[str, Any] = {
    "name": "set_project_root",
    "description": (
        "Tell the IDE which directory to open for this project. Call "
        "exactly once, immediately after scaffolding (or confirming the "
        "layout of) the project. `path` must be an absolute path under "
        "/workspace — normally `/workspace`, or `/workspace/<subdir>` if "
        "the scaffolder placed the project in a subdirectory."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}


FOCUS_BROWSER_TOOL: dict[str, Any] = {
    "name": "focus_browser",
    "description": (
        "Flip the user's right-side panel to the live preview browser "
        "(VNC into the chromium container) so they can watch the "
        "automation happen.  Call this ONCE immediately before the first "
        "playwright MCP interaction in a turn — `browser_navigate`, "
        "`browser_click`, `browser_fill`, `browser_snapshot`, etc.  "
        "No-op if the panel is already on the browser.  `reason` is "
        "optional one-line context for server logs; not shown to the user."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "reason": {"type": "string"},
        },
        "additionalProperties": False,
    },
}


# ── Codex item → event kind projection ────────────────────────────────────
# Maps Codex's native ``item.type`` to our agent-prefixed Event kind.
# ``userMessage`` / ``contextCompaction`` → skipped (not user-facing).

_KIND_MAP: dict[str, str | None] = {
    "agentMessage": "codex:agent_message",
    "plan": "codex:plan",
    "reasoning": "codex:reasoning",
    "commandExecution": "codex:command_execution",
    "fileChange": "codex:file_change",
    "mcpToolCall": "codex:mcp_tool_call",
    "dynamicToolCall": "codex:dynamic_tool_call",
    "webSearch": "codex:web_search",
    "userMessage": None,
    "contextCompaction": None,
}


def _map_kind(codex_type: Any) -> str | None:
    if not isinstance(codex_type, str):
        return None
    if codex_type in _KIND_MAP:
        return _KIND_MAP[codex_type]
    return "codex:other"


def _codex_item_payload(item: dict[str, Any]) -> dict[str, Any]:
    """Trim a Codex item to a DB-friendly payload keyed per item type."""
    codex_type = item.get("type")
    if codex_type == "agentMessage":
        return {"text": item.get("text"), "phase": item.get("phase")}
    if codex_type == "plan":
        return {"text": item.get("text")}
    if codex_type == "reasoning":
        return {"summary": item.get("summary"), "content": item.get("content")}
    if codex_type == "commandExecution":
        return {
            "command": item.get("command"),
            "cwd": item.get("cwd"),
            "status": item.get("status"),
            "exit_code": item.get("exitCode"),
            "output": item.get("aggregatedOutput"),
            "duration_ms": item.get("durationMs"),
        }
    if codex_type == "fileChange":
        raw_changes = item.get("changes") or []
        paths: list[str] = []
        additions = 0
        deletions = 0
        for ch in raw_changes:
            if not isinstance(ch, dict):
                continue
            path = ch.get("path")
            if isinstance(path, str):
                paths.append(path)
            diff = ch.get("diff")
            if isinstance(diff, str):
                for line in diff.splitlines():
                    if line.startswith("+++") or line.startswith("---"):
                        continue
                    if line.startswith("+"):
                        additions += 1
                    elif line.startswith("-"):
                        deletions += 1
        return {
            "paths": paths,
            "additions": additions,
            "deletions": deletions,
            "changes": raw_changes,
        }
    if codex_type == "mcpToolCall":
        return {
            "server": item.get("server"),
            "tool": item.get("tool"),
            "status": item.get("status"),
            "arguments": item.get("arguments"),
            "result": item.get("result"),
            "error": item.get("error"),
            "duration_ms": item.get("durationMs"),
        }
    if codex_type == "dynamicToolCall":
        return {
            "tool": item.get("tool"),
            "status": item.get("status"),
            "success": item.get("success"),
            "arguments": item.get("arguments"),
            "content_items": item.get("contentItems"),
            "duration_ms": item.get("durationMs"),
        }
    if codex_type == "webSearch":
        return {"query": item.get("query"), "action": item.get("action")}
    return {"type": codex_type, "raw": item}


# ── Session cache ──────────────────────────────────────────────────────────

_sessions: dict[UUID, PolarisCodexSession] = {}
_session_locks: dict[UUID, asyncio.Lock] = {}


def _workspace_container_name(workspace_id: UUID) -> str:
    hash_id = str(workspace_id).replace("-", "")[:24]
    return f"polaris-ws-{hash_id}"


def _session_lock(workspace_id: UUID) -> asyncio.Lock:
    lock = _session_locks.get(workspace_id)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[workspace_id] = lock
    return lock


async def _resolve_container_ip(
    workspace_id: UUID, *, max_wait_seconds: float = 30.0
) -> str:
    """Look up the workspace container's IP.  Retries for a short window so
    we can start concurrently with ``ensure_workspace_runtime`` on the API
    side."""
    container = _workspace_container_name(workspace_id)
    deadline = asyncio.get_running_loop().time() + max_wait_seconds
    last_err = ""
    while True:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "inspect",
            "--format",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
            container,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            for candidate in stdout.decode().split():
                candidate = candidate.strip()
                if candidate:
                    return candidate
            last_err = "container has no IP addresses assigned"
        else:
            last_err = stderr.decode(errors="replace").strip()
        if asyncio.get_running_loop().time() >= deadline:
            raise PolarisCodexError(
                f"workspace container {container} not reachable after "
                f"{max_wait_seconds:.0f}s: {last_err}. "
                "Make sure the workspace runtime has been started "
                "(open the project in the UI to trigger it)."
            )
        await asyncio.sleep(1.0)


async def _get_or_open_session(
    workspace_id: UUID, settings: Any
) -> PolarisCodexSession:
    async with _session_lock(workspace_id):
        ip = await _resolve_container_ip(workspace_id)
        ws_url = f"ws://{ip}:{CODEX_APP_SERVER_PORT}/"

        existing = _sessions.get(workspace_id)
        if existing is not None:
            if existing.is_alive() and existing.ws_url == ws_url:
                return existing
            reason = (
                "dead ws"
                if not existing.is_alive()
                else f"ip moved {existing.ws_url} -> {ws_url}"
            )
            logger.info(
                "cached Codex session for workspace %s stale (%s); reopening",
                workspace_id,
                reason,
            )
            await _drop_session(workspace_id)

        config = PolarisAgentConfig(
            ws_url=ws_url,
            cwd=WORKSPACE_CONTAINER_CWD,
            base_instructions=POLARIS_AGENT_BASE_INSTRUCTIONS,
            dynamic_tools=[SET_PROJECT_ROOT_TOOL, FOCUS_BROWSER_TOOL],
            # Bound before each run; see CodexAgent.run().
            dynamic_tool_handler=None,
            user_input_handler=None,
            model=settings.codex_model,
            sandbox_mode=None,
            approval_policy=settings.codex_approval_policy,
            turn_timeout_seconds=settings.codex_turn_timeout_seconds,
            liveness_check_interval_seconds=settings.codex_liveness_check_interval_seconds,
        )
        session = PolarisCodexSession(config)
        await session.start()
        _sessions[workspace_id] = session
        logger.info("opened Codex session for workspace %s at %s", workspace_id, ws_url)
        return session


async def _drop_session(workspace_id: UUID) -> None:
    session = _sessions.pop(workspace_id, None)
    if session is None:
        return
    with contextlib.suppress(Exception):
        await session.close()


# ── Dynamic tool handler (set_project_root only, for now) ─────────────────


def _normalize_project_root(path: str) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    norm = posixpath.normpath(path)
    if norm != "/workspace" and not norm.startswith("/workspace/"):
        return None
    return norm


# inotifywait regex that excludes noise directories we don't want to
# count against the StatusBar file-change counter:
#   - any path segment starting with "." (`.git`, `.idea`, `.vscode`,
#     `.venv`, `.next`, `.cache`, `.pnpm-store`, …)
#   - common build / dependency output dirs without a leading dot
# The `(^|/)` guard anchors the segment; the trailing `(/|$)` prevents
# accidental partial-name matches (e.g. a folder called "distributor"
# would not be matched by the "dist" rule).
_WATCHER_EXCLUDE_REGEX = (
    r"(^|/)(\.[^/]+|node_modules|dist|build|__pycache__|coverage|target)(/|$)"
)


async def _watch_project_files(
    *,
    container: str,
    project_root: str,
    sink: Any,  # DbEventSink — loose type to avoid extra imports
    log: logging.Logger | logging.LoggerAdapter,
) -> None:
    """Long-running docker-exec that streams inotify close_write / delete
    / moved_to events under ``project_root`` (inside ``container``).

    Every output line becomes one file-change delta on the sink's
    500ms-coalesced counter.  The counter is by-event, not by unique
    path — repeated edits of the same file bump it N times.

    Exits naturally when:
      - the subprocess is cancelled (normal case, at run end)
      - inotifywait itself exits (container stopped, fs unmounted, …)
      - stream decode errors (treated as fatal; restart-on-next-run)
    """
    # Quote the path — project roots can contain spaces in principle;
    # shlex.quote handles it.  The regex escape of dots is fine: inside
    # POSIX extended regex a dot matches any char, but the extra strings
    # we match are literal enough that ambiguity is a non-issue.
    import shlex
    cmd = (
        "inotifywait -m -r -q "
        f"--exclude '{_WATCHER_EXCLUDE_REGEX}' "
        f"-e close_write -e delete -e moved_to "
        f"--format '%w%f' "
        f"{shlex.quote(project_root)}"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", container, "sh", "-c", cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        log.warning("inotify watcher: `docker` binary missing on host; skipping")
        return
    log.info(
        "inotify watcher started (container=%s, scope=%s)",
        container, project_root,
    )
    try:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            # We don't use the decoded path — counter is pure bump-count.
            # But decode anyway so a stray non-UTF-8 byte can't wedge the
            # stream (surrogateescape → decoded, just noise).
            _ = raw.decode("utf-8", errors="replace").rstrip()
            await sink.bump_file_delta(1)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.warning("inotify watcher stream error", exc_info=True)
    finally:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        log.info("inotify watcher stopped (container=%s)", container)


async def _ensure_project_git(project_root: Path) -> None:
    """git init + baseline commit at the real project root.  Called from the
    ``set_project_root`` handler once Codex has declared where the project
    lives (scaffolders refuse to run in a non-empty cwd, so ``.git`` is
    intentionally missing until now)."""
    if (project_root / ".git").exists():
        return
    await run_git(project_root, "init", "-b", "main")
    await run_git(project_root, "config", "user.email", "dev@polaris.local")
    await run_git(project_root, "config", "user.name", "Polaris")
    await run_git(project_root, "add", "-A")
    await run_git(
        project_root,
        "commit",
        "--allow-empty",
        "-m",
        "polaris: initial scaffold",
    )


def _build_dynamic_tool_handler(
    *,
    conn: asyncpg.Connection,
    conn_lock: asyncio.Lock,
    redis: Redis,
    session_id: UUID,
    workspace_id: UUID,
    on_project_root_set: Any = None,  # Callable[[str], None] | None
):
    async def handler(
        name: str, args: dict[str, Any], _raw: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            if name == "set_project_root":
                norm = _normalize_project_root(args.get("path", ""))
                if norm is None:
                    return dyn_response(
                        False, {"error": "path must be absolute under /workspace"}
                    )
                async with conn_lock:
                    repo_path_row = await conn.fetchrow(
                        "UPDATE workspaces SET project_root=$1 WHERE id=$2 "
                        "RETURNING repo_path",
                        norm,
                        workspace_id,
                    )
                if repo_path_row is not None and repo_path_row["repo_path"]:
                    host_repo = Path(repo_path_row["repo_path"])
                    subdir = norm.removeprefix("/workspace").lstrip("/")
                    host_project_root = host_repo / subdir if subdir else host_repo
                    try:
                        if host_project_root.is_dir():
                            ensure_baseline_gitignore(host_project_root)
                            await _ensure_project_git(host_project_root)
                    except (OSError, WorkspaceError) as exc:
                        logger.warning(
                            "post-scaffold seeding failed for %s: %s",
                            host_project_root,
                            exc,
                        )
                await redis.publish(
                    session_events_channel(session_id),
                    json.dumps(
                        {
                            "session_id": str(session_id),
                            "kind": "project_root_changed",
                            "path": norm,
                        }
                    ),
                )
                logger.info("workspace %s project_root set to %s", workspace_id, norm)
                if on_project_root_set is not None:
                    try:
                        on_project_root_set(norm)
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "on_project_root_set callback failed (workspace=%s)",
                            workspace_id,
                            exc_info=True,
                        )
                return dyn_response(True, {"path": norm})

            if name == "focus_browser":
                reason = str(args.get("reason") or "")[:200]
                await redis.publish(
                    session_events_channel(session_id),
                    json.dumps(
                        {
                            "session_id": str(session_id),
                            "kind": "browser_focus_requested",
                            "reason": reason,
                        }
                    ),
                )
                logger.info(
                    "focus_browser requested for workspace %s (reason=%r)",
                    workspace_id,
                    reason,
                )
                return dyn_response(True, {})

            return dyn_response(False, {"error": f"unknown tool {name!r}"})
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "dynamic tool %s failed for workspace %s", name, workspace_id
            )
            return dyn_response(
                False, {"error": f"{type(exc).__name__}: {exc}"}
            )

    return handler


# ── request_user_input handler ────────────────────────────────────────────


def _build_user_input_handler(
    *,
    redis: Redis,
    session_id: UUID,
    run_id: UUID,
):
    """Translate Codex's request_user_input payload to our
    ClarificationQuestion shape, delegate to ``wait_for_answers``, then
    translate back to Codex's answer format."""

    async def handler(
        questions: list[dict[str, Any]], _params: dict[str, Any]
    ) -> dict[str, Any]:
        # Codex shape:    { id, header, question, options: [{ label, description }] }
        # Frontend shape: { id, title, description?, choices: [{ id, label, summary? }], ... }
        frontend_questions: list[dict[str, Any]] = []
        for q in questions:
            choices: list[dict[str, Any]] = []
            for i, opt in enumerate(q.get("options") or []):
                choices.append(
                    {
                        "id": opt.get("label", f"opt_{i}"),
                        "label": opt.get("label", ""),
                        "summary": opt.get("description"),
                    }
                )
            frontend_questions.append(
                {
                    "id": q.get("id", f"q_{len(frontend_questions)}"),
                    "title": q.get("question") or q.get("header") or "",
                    "description": q.get("header") if q.get("question") else None,
                    "required": True,
                    "choices": choices,
                    "allow_override_text": True,
                    "override_label": "Or tell Polaris what to do",
                }
            )

        result = await wait_for_answers(
            redis=redis,
            session_id=session_id,
            run_id=run_id,
            questions=frontend_questions,
            source="codex",
        )
        raw_answers = (result or {}).get("answers") or {}
        codex_answers: dict[str, Any] = {}
        for qid, ans in raw_answers.items():
            if isinstance(ans, dict):
                value = ans.get("override_text") or ans.get("selected_choice") or ""
                codex_answers[qid] = {"answers": [value]}
            elif isinstance(ans, str):
                codex_answers[qid] = {"answers": [ans]}
        return codex_answers

    return handler


# ── The sink that translates Codex items into our EventSink ───────────────


class _CodexTurnSink:
    """Adapter implementing agent-core's ``TurnItemSink`` protocol; forwards
    each callback to our generic ``EventSink``.

    Tracks:
      * ``external_id`` (Codex's turn id) — surfaced on RunOutcome so the
        orchestrator can persist it into ``agent_runs.external_id``.
      * ``final_message`` — last ``codex:agent_message`` text, used for
        Session.final_message.
      * ``failure_reason`` — translated error from Codex (timeout / wire
        disconnect) enriched with last-activity snapshot.
    """

    def __init__(
        self,
        *,
        sink: EventSink,
        conn: asyncpg.Connection,
        conn_lock: asyncio.Lock,
        redis: Redis,
        session_id: UUID,
        workspace_id: UUID,
        log: logging.Logger | logging.LoggerAdapter,
        settings: Any,
    ) -> None:
        self._sink = sink
        self._conn = conn
        self._conn_lock = conn_lock
        self._redis = redis
        self._session_id = session_id
        self._workspace_id = workspace_id
        self._log = log
        self._settings = settings
        self._events_by_codex_id: dict[str, UUID] = {}
        self._started_at = time.monotonic()
        self._last_activity_at = self._started_at
        self._last_item_kind: str | None = None
        self._item_counts: dict[str, int] = {}
        self._final_message: str | None = None
        self._external_id: str | None = None
        self._fallback_project_root_attempted = False

    # Accessors for CodexAgent after run_turn finishes.
    @property
    def final_message(self) -> str | None:
        return self._final_message

    @property
    def external_id(self) -> str | None:
        return self._external_id

    def _record(self, kind: str) -> None:
        self._last_activity_at = time.monotonic()
        self._last_item_kind = kind
        self._item_counts[kind] = self._item_counts.get(kind, 0) + 1

    def compose_timeout_reason(self, base_error: str) -> str:
        counts = ", ".join(f"{k}={v}" for k, v in sorted(self._item_counts.items()))
        parts = [base_error.rstrip(".") + "."]
        if self._last_item_kind is None:
            parts.append(
                "Codex never emitted a single item — likely stuck waiting on "
                "the upstream model or a tool-call handshake."
            )
        else:
            parts.append(f"Last activity: {self._last_item_kind}.")
        if counts:
            parts.append(f"Events this run: {counts}.")
        return " ".join(parts)

    async def on_turn_started(self, codex_turn_id: str) -> None:
        self._external_id = codex_turn_id
        self._log.info("codex turn started (external_id=%s)", codex_turn_id)

    async def on_item_started(self, item: dict[str, Any]) -> None:
        kind = _map_kind(item.get("type"))
        if kind is None:
            return
        codex_id = item.get("id") if isinstance(item.get("id"), str) else None
        payload = _codex_item_payload(item)
        self._record(kind)
        event_id = await self._sink.emit_event_started(
            kind=kind, external_id=codex_id, payload=payload,
        )
        if codex_id is not None:
            self._events_by_codex_id[codex_id] = event_id

    async def on_item_completed(self, item: dict[str, Any]) -> None:
        kind = _map_kind(item.get("type"))
        if kind is None:
            return
        codex_id = item.get("id") if isinstance(item.get("id"), str) else None
        payload = _codex_item_payload(item)
        self._record(kind)

        # For plan items, run a second LLM pass (gpt-5.4-mini by default)
        # to produce a non-technical version.  Blocking ~1–2s per plan;
        # the frontend Tabs component switches between the two.
        if kind == "codex:plan":
            plain = await _translate_plan_to_plain(
                str(payload.get("text") or ""),
                settings=self._settings,
                log=self._log,
            )
            if plain:
                payload["text_plain"] = plain

        event_id = self._events_by_codex_id.get(codex_id) if codex_id else None
        if event_id is None:
            # Codex completed an item we never saw start — emit a started +
            # completed pair so the row exists.
            event_id = await self._sink.emit_event_started(
                kind=kind, external_id=codex_id, payload=payload,
            )
        await self._sink.emit_event_completed(
            event_id=event_id,
            external_id=codex_id,
            payload=payload,
            status="completed",
        )

        if kind == "codex:agent_message":
            text = payload.get("text")
            if isinstance(text, str) and text:
                self._final_message = text
        if kind == "codex:file_change":
            await self._maybe_infer_project_root(payload)
        # StatusBar counter: playwright MCP calls.  We count completions
        # (one tick per finished tool call), regardless of whether the
        # call succeeded — failed browser_click / timeout still
        # represents "agent did a testing step".
        if kind == "codex:mcp_tool_call" and payload.get("server") == "playwright":
            await self._sink.bump_playwright_delta(1)

    async def _maybe_infer_project_root(self, payload: dict[str, Any]) -> None:
        """Safety net — if Codex writes files without calling
        set_project_root, infer the root from the shallowest common top-level
        under /workspace."""
        if self._fallback_project_root_attempted:
            return
        self._fallback_project_root_attempted = True

        async with self._conn_lock:
            row = await self._conn.fetchrow(
                "SELECT project_root FROM workspaces WHERE id=$1",
                self._workspace_id,
            )
        if row is None or row["project_root"] is not None:
            return

        paths = payload.get("paths")
        if not isinstance(paths, list):
            return
        top_level: set[str] = set()
        for p in paths:
            if isinstance(p, str) and p.startswith("/workspace/"):
                rest = p[len("/workspace/") :]
                first = rest.split("/", 1)[0]
                if first:
                    top_level.add(first)
        if not top_level:
            return
        inferred = (
            f"/workspace/{next(iter(top_level))}"
            if len(top_level) == 1
            else "/workspace"
        )
        async with self._conn_lock:
            await self._conn.execute(
                "UPDATE workspaces SET project_root=$1 WHERE id=$2 "
                "AND project_root IS NULL",
                inferred,
                self._workspace_id,
            )
        await self._redis.publish(
            session_events_channel(self._session_id),
            json.dumps(
                {
                    "session_id": str(self._session_id),
                    "kind": "project_root_changed",
                    "path": inferred,
                }
            ),
        )
        self._log.info(
            "workspace %s project_root auto-inferred to %s",
            self._workspace_id,
            inferred,
        )

    async def on_agent_message_delta(self, text: str) -> None:
        await self._sink.emit_message_delta(text=text)

    async def on_turn_completed(
        self, status: str, error: str | None
    ) -> None:
        # Orchestrator finalizes the agent_runs row and session status
        # externally — we just surface the final status/error via the
        # accessors set by run() below.
        if status == "failed" and error and (
            "Turn total timeout" in error or "connection lost" in error.lower()
        ):
            error = self.compose_timeout_reason(error)
        elapsed = time.monotonic() - self._started_at
        counts = ", ".join(
            f"{k}={v}" for k, v in sorted(self._item_counts.items())
        ) or "no events"
        self._log.info(
            "codex turn %s in %.1fs (%s)%s",
            status,
            elapsed,
            counts,
            f" — {error}" if error else "",
        )
        self._completion_status = status
        self._completion_error = error


# ── The Agent ──────────────────────────────────────────────────────────────


class CodexAgent(Agent):
    """Agent adapter that drives a single Codex run for the orchestrator.

    Steps per ``run()``:
      1. Write the project's active design intent (if any) to the workspace
         container's ``$CODEX_HOME/AGENTS.md`` via ``docker exec``.  This
         runs BEFORE opening the Codex WS so it's visible to Codex's very
         first plan-round read.
      2. Open (or reuse) the ``PolarisCodexSession`` for this workspace.
      3. Bind per-run tool/user-input handlers (session_id + run_id
         captured in closures).
      4. ``ensure_thread`` + ``run_turn``.  The sink adapter translates
         Codex items into ``codex:*`` events via our EventSink.
      5. Return RunOutcome: external_id=codex_turn_id, final_message,
         output includes the last agent_message text for handoff.
    """

    kind: AgentKind = AgentKind.codex

    def __init__(self) -> None:
        self._active_session: PolarisCodexSession | None = None
        self._active_thread_id: str | None = None

    async def run(
        self,
        session: SessionContext,
        run: RunContext,
        sink: EventSink,
    ) -> RunOutcome:
        user_message: str = run.input.get("user_message") or ""
        codex_mode: str = run.input.get("codex_mode") or "plan"
        log = logging.LoggerAdapter(
            logger,
            {
                "session_id": str(session.session_id),
                "run_id": str(run.run_id),
                "workspace_id": str(session.workspace_id),
            },
        )

        # Step 1 — AGENTS.md global scope.  Silent no-op if project has
        # no active design intent (never ran discovery).  The rendered
        # markdown includes an absolute path to the mood_board PNG
        # when one exists, so Codex is aware of the file without us
        # having to attach it as a multimodal input on every turn.
        try:
            brief = await load_active_design_intent(
                session.conn, session.conn_lock, session.project_id
            )
            if brief is not None:
                rendered = render_design_intent_markdown(brief)
                await write_codex_home_agents_md(
                    container=_workspace_container_name(session.workspace_id),
                    content=rendered,
                )
        except Exception:  # noqa: BLE001
            log.warning("AGENTS.md write failed — continuing anyway", exc_info=True)

        # Step 2 — open (or reuse) the WS session.
        try:
            codex_session = await _get_or_open_session(
                session.workspace_id, session.settings
            )
        except PolarisCodexError as exc:
            log.exception("failed to open Codex session")
            return RunOutcome(
                status="failed",
                output={},
                error=f"codex connect: {exc}",
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("unexpected error opening Codex session")
            return RunOutcome(
                status="failed",
                output={},
                error=f"{type(exc).__name__}: {exc}",
            )

        # Step 3 — bind per-run handlers.
        # File-watcher task bookkeeping — started in response to
        # `set_project_root` (or immediately below if the workspace
        # already has one).  Cancelled and flushed in the `finally` of
        # Step 4 so the last 500ms of fs activity isn't lost.
        watcher_tasks: set[asyncio.Task] = set()
        watcher_started = False

        def _spawn_watcher(project_root: str) -> None:
            nonlocal watcher_started
            if watcher_started:
                return
            watcher_started = True
            task = asyncio.create_task(
                _watch_project_files(
                    container=_workspace_container_name(session.workspace_id),
                    project_root=project_root,
                    sink=sink,
                    log=log,
                )
            )
            watcher_tasks.add(task)
            task.add_done_callback(watcher_tasks.discard)

        codex_session._config.dynamic_tool_handler = _build_dynamic_tool_handler(
            conn=session.conn,
            conn_lock=session.conn_lock,
            redis=session.redis,
            session_id=session.session_id,
            workspace_id=session.workspace_id,
            on_project_root_set=_spawn_watcher,
        )
        codex_session._config.user_input_handler = _build_user_input_handler(
            redis=session.redis,
            session_id=session.session_id,
            run_id=run.run_id,
        )

        # If the workspace already has a project_root (2nd+ turn), start
        # the watcher immediately — we'd otherwise miss any fs activity
        # before Codex re-declared it (which it typically won't).
        async with session.conn_lock:
            pr_row = await session.conn.fetchrow(
                "SELECT project_root FROM workspaces WHERE id=$1",
                session.workspace_id,
            )
        if pr_row is not None and pr_row["project_root"]:
            _spawn_watcher(pr_row["project_root"])

        # Step 4 — run the turn.
        codex_sink = _CodexTurnSink(
            sink=sink,
            conn=session.conn,
            conn_lock=session.conn_lock,
            redis=session.redis,
            session_id=session.session_id,
            workspace_id=session.workspace_id,
            log=log,
            settings=session.settings,
        )

        async with session.conn_lock:
            project_row = await session.conn.fetchrow(
                "SELECT codex_thread_id FROM projects WHERE id=$1",
                session.project_id,
            )
        existing_thread_id: str | None = (
            project_row["codex_thread_id"] if project_row is not None else None
        )

        try:
            thread_id = await codex_session.ensure_thread(existing_thread_id)
            self._active_session = codex_session
            self._active_thread_id = thread_id
            if thread_id != existing_thread_id:
                async with session.conn_lock:
                    await session.conn.execute(
                        "UPDATE projects SET codex_thread_id=$1 WHERE id=$2",
                        thread_id,
                        session.project_id,
                    )
            # We deliberately do NOT pass the mood_board as a per-turn
            # `localImage` input.  The PNG is referenced by absolute path
            # in AGENTS.md ("Mood board" section); Codex can read it on
            # demand if it needs visual detail, but we don't burn vision
            # tokens on every turn for what's really a one-time mood cue.
            await codex_session.run_turn(
                thread_id=thread_id,
                user_message=user_message,
                project_id=session.project_id,
                workspace_id=session.workspace_id,
                turn_id=run.run_id,  # agent-core param name; holds our run_id
                sink=codex_sink,  # type: ignore[arg-type]
                mode=codex_mode,
            )
        except PolarisCodexError as exc:
            log.exception("polaris_agent session failure")
            await _drop_session(session.workspace_id)
            return RunOutcome(
                status="failed",
                output={},
                error=str(exc),
                external_id=codex_sink.external_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("unexpected error driving codex turn")
            await _drop_session(session.workspace_id)
            return RunOutcome(
                status="failed",
                output={},
                error=f"{type(exc).__name__}: {exc}",
                external_id=codex_sink.external_id,
            )
        finally:
            self._active_session = None
            self._active_thread_id = None
            # Cancel file watchers + flush any pending StatusBar deltas
            # so the last 500ms of fs/playwright activity is persisted
            # and surfaced in the final SSE frame before run teardown.
            for task in list(watcher_tasks):
                task.cancel()
            for task in list(watcher_tasks):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            with contextlib.suppress(Exception):
                await sink.finalize_stats()

        status = getattr(codex_sink, "_completion_status", "completed")
        error = getattr(codex_sink, "_completion_error", None)
        if status == "failed":
            return RunOutcome(
                status="failed",
                output={"final_message": codex_sink.final_message} if codex_sink.final_message else {},
                error=error,
                external_id=codex_sink.external_id,
                final_message=codex_sink.final_message,
            )
        if status == "interrupted":
            # Codex WS received `turn/interrupt` (see codex_app_server.py
            # around L602 — raw item "interrupted" → sink status
            # "interrupted").  Propagate it so the orchestrator finalises
            # the session as interrupted rather than silently completed.
            return RunOutcome(
                status="interrupted",
                output={"final_message": codex_sink.final_message} if codex_sink.final_message else {},
                error=error,
                external_id=codex_sink.external_id,
                final_message=codex_sink.final_message,
            )

        return RunOutcome(
            status="completed",
            output={"final_message": codex_sink.final_message}
            if codex_sink.final_message
            else {},
            external_id=codex_sink.external_id,
            final_message=codex_sink.final_message,
        )

    async def handle_control(self, event: dict[str, Any]) -> None:
        """Called by the session-level control consumer for interrupt/steer.

        Best-effort: if the WS session is gone we silently ignore."""
        kind = event.get("kind")
        session = self._active_session
        thread_id = self._active_thread_id
        if session is None or thread_id is None:
            return
        if kind == "interrupt":
            with contextlib.suppress(Exception):
                await session.interrupt(thread_id)
        elif kind == "steer":
            text = event.get("message")
            if isinstance(text, str) and text.strip():
                with contextlib.suppress(Exception):
                    await session.steer(thread_id, text)
