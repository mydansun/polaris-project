"""Discovery adapter — wraps :func:`polaris_design_intent.run_design_intent`
behind the generic :class:`Agent` contract.

Maps the LangGraph pipeline's three node transitions (clarifier / pinterest /
compiler) to ``discovery:*`` events on our :class:`EventSink` **as they
happen** via a LangChain callback, bridges the ``ask_questions`` tool to the
shared clarification channel via ``RunContext.user_input_fn``, and persists
the final design-intent row.

Output (persisted to ``agent_runs.output_jsonb`` by the orchestrator):
  - ``brief``:  ``CompiledBrief.brief`` — the English design brief; the
    orchestrator's ``threading_forward`` promotes this to the next codex
    run's ``user_message``.
  - ``intent``: 18-key structured design intent, also persisted to the
    ``design_intents`` table (one active row per project).
  - ``pinterest_refs`` / ``pinterest_queries``: full refs + queries used.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import UUID

import base64
from uuid import uuid4 as _uuid4

from langchain_core.callbacks import AsyncCallbackHandler

from polaris_api.config import get_settings as _api_get_settings
from polaris_api.services.s3 import public_url as _s3_public_url
from polaris_api.services.s3 import upload_bytes as _s3_upload_bytes

from polaris_design_intent import CompiledBrief, run_design_intent

from polaris_worker.agents.base import (
    Agent,
    AgentKind,
    EventSink,
    RunContext,
    RunOutcome,
    SessionContext,
)
from polaris_worker.codex_agents_md import (
    MOOD_BOARD_CONTAINER_PATH,  # noqa: F401 — re-exported for callers that compose paths
    _workspace_container_name,
    write_mood_board_to_workspace,
)

logger = logging.getLogger(__name__)


# Node name → discovery event kind mapping.  Both clarifier nodes share a
# single "clarifying" event; advancing into references / compiled / moodboard
# implicitly closes earlier events.  The public event kind deliberately
# avoids the word "pinterest" — that's an implementation detail the user
# shouldn't care about, so we surface it as the generic "references" stage.
_NODE_TO_KIND: dict[str, str] = {
    "clarifier_step":  "discovery:clarifying",
    "clarifier_ask":   "discovery:clarifying",
    "review_step":     "discovery:clarifying",  # still "clarifying" bucket
    "pinterest":       "discovery:references",
    "compiler":        "discovery:compiled",
    "mood_board_step": "discovery:moodboard",
}

# Sequence of event kinds.  Entering a later-sequence node closes any
# earlier-sequence events that are still open.
_KIND_SEQUENCE = [
    "discovery:clarifying",
    "discovery:references",
    "discovery:compiled",
    "discovery:moodboard",
]


async def _upload_mood_board_to_s3(image_b64: str) -> str | None:
    """Decode the mood-board PNG and push it to our S3 under
    ``static/images/moodboard/<uuid>.png``.  Returns the anonymous-public
    URL, or None on failure (caller treats None as 'mood board absent')."""
    try:
        settings = _api_get_settings()
        if not settings.s3_endpoint or not settings.s3_url_base:
            logger.warning(
                "mood_board: S3 not configured — mood board will not be uploaded"
            )
            return None
        key = f"static/images/moodboard/{_uuid4()}.png"
        data = base64.b64decode(image_b64)
        await _s3_upload_bytes(
            key=key, data=data, content_type="image/png", settings=settings
        )
        url = _s3_public_url(key=key, settings=settings)
        logger.info("mood_board: uploaded %d bytes to %s", len(data), url)
        return url
    except Exception:  # noqa: BLE001
        logger.warning("mood_board: S3 upload failed", exc_info=True)
        return None


class _DiscoveryProgressHandler(AsyncCallbackHandler):
    """Translate LangGraph node lifecycle events into discovery sink events.

    LangChain's callback model fires ``on_chain_start`` / ``on_chain_end``
    for every runnable including our graph nodes — we filter by name.

    Policy:
      - First time we see a node mapped to kind K, emit ``event_started`` for K.
      - Moving into a later-sequence kind closes (emit_event_completed) any
        earlier-sequence kinds still open.
      - ``finalize_all()`` must be called after the graph returns to close
        whatever is still open (typically `discovery:compiled`).
    """

    def __init__(self, sink: EventSink, log: logging.LoggerAdapter | logging.Logger):
        self._sink = sink
        self._log = log
        self._started: dict[str, Any] = {}   # kind -> event_id
        self._completed: set[str] = set()

    async def _close_earlier(self, kind: str) -> None:
        target_idx = _KIND_SEQUENCE.index(kind)
        for i in range(target_idx):
            prev = _KIND_SEQUENCE[i]
            if prev in self._started and prev not in self._completed:
                await self._sink.emit_event_completed(
                    event_id=self._started[prev],
                    external_id=prev,  # match the started event's external_id
                    payload={"stage": "closed_by_next"},
                    status="completed",
                )
                self._completed.add(prev)
                self._log.info("discovery event completed (auto): %s", prev)

    async def on_chain_start(  # type: ignore[override]
        self,
        serialized: dict[str, Any] | None,
        inputs: dict[str, Any] | None,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        # LangGraph node names appear either in kwargs["name"] or in tags.
        name = kwargs.get("name")
        if not name and isinstance(serialized, dict):
            name = serialized.get("name")
        if not isinstance(name, str):
            return
        kind = _NODE_TO_KIND.get(name)
        if kind is None:
            return
        await self._close_earlier(kind)
        if kind in self._started:
            return  # already open; dedupe clarifier_step / clarifier_ask re-entries
        # external_id=kind keeps the started + completed events associated
        # with the SAME frontend bubble (useSessionEventHandler matches by
        # external_id) so the UI shows one row that transitions from blue
        # dot → green check instead of two separate messages.
        eid = await self._sink.emit_event_started(
            kind=kind, external_id=kind, payload={}
        )
        self._started[kind] = eid
        self._log.info("discovery event started: %s (from node=%s)", kind, name)

    async def on_chain_end(  # type: ignore[override]
        self,
        outputs: dict[str, Any] | None,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        name = kwargs.get("name")
        if not isinstance(name, str):
            return
        kind = _NODE_TO_KIND.get(name)
        # Only close `compiler` here — the rest get closed by forward progress.
        if kind != "discovery:compiled" or kind not in self._started:
            return
        if kind in self._completed:
            return
        await self._sink.emit_event_completed(
            event_id=self._started[kind],
            external_id=kind,
            payload={},
            status="completed",
        )
        self._completed.add(kind)
        self._log.info("discovery event completed: %s (from node=%s)", kind, name)

    async def finalize_all(
        self,
        *,
        status: str = "completed",
        overrides: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Close whatever is still open.  Called by the agent after the
        graph returns (or raises).

        ``overrides`` — per-kind extra payload fields merged into the
        final event.  The agent uses this to attach the mood_board S3
        URL onto the ``discovery:compiled`` event so the frontend can
        render it in a dedicated card.
        """
        overrides = overrides or {}
        for kind in _KIND_SEQUENCE:
            if kind in self._started and kind not in self._completed:
                payload: dict[str, Any] = {"stage": "finalized"}
                if kind in overrides:
                    payload.update(overrides[kind])
                await self._sink.emit_event_completed(
                    event_id=self._started[kind],
                    external_id=kind,
                    payload=payload,
                    status=status,
                )
                self._completed.add(kind)
                self._log.info("discovery event finalized: %s (%s)", kind, status)


class DiscoveryAgent(Agent):
    """Drives one design-intent LangGraph run."""

    kind: AgentKind = AgentKind.discovery

    def __init__(self) -> None:
        # Set when ``run`` is driving a LangGraph invocation.  The session
        # control channel's interrupt messages land in ``handle_control``
        # which cancels this task; `run` catches CancelledError and
        # returns a RunOutcome(status="interrupted").
        self._active_task: asyncio.Task[CompiledBrief] | None = None

    async def run(
        self,
        session: SessionContext,
        run: RunContext,
        sink: EventSink,
    ) -> RunOutcome:
        user_message: str = run.input.get("user_message") or ""
        seed_intent: dict | None = run.input.get("seed_intent")
        log = logging.LoggerAdapter(
            logger,
            {
                "session_id": str(session.session_id),
                "run_id": str(run.run_id),
            },
        )

        log.info(
            "discovery: start (msg_chars=%d, has_seed=%s)",
            len(user_message),
            seed_intent is not None,
        )

        progress = _DiscoveryProgressHandler(sink, log)

        async def _invoke() -> CompiledBrief:
            return await run_design_intent(
                project_id=str(session.project_id),
                turn_id=str(run.run_id),  # LangGraph thread_id — just needs uniqueness
                user_message=user_message,
                seed_intent=seed_intent,
                user_input_fn=run.user_input_fn,
                callbacks=[progress],
            )

        self._active_task = asyncio.create_task(_invoke())
        try:
            brief: CompiledBrief = await self._active_task
        except asyncio.CancelledError:
            log.info("discovery cancelled by interrupt")
            await progress.finalize_all(status="failed")
            return RunOutcome(
                status="interrupted",
                output={},
                error=None,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("discovery graph failed")
            await progress.finalize_all(status="failed")
            return RunOutcome(
                status="failed",
                output={},
                error=f"discovery: {type(exc).__name__}: {exc}",
            )
        finally:
            self._active_task = None

        # If the mood_board node produced a PNG, we do two things:
        #   1. Upload to S3 for frontend display (card in chat bubble).
        #   2. Write the bytes into the workspace container so every
        #      Codex turn can attach it as a local `image`/`localImage`
        #      input (no network dependency at turn time).
        # Both fail-soft — if S3 upload dies the card just won't show;
        # if the container write dies Codex simply runs without a
        # visual anchor this project.
        mood_board_url: str | None = None
        if brief.mood_board_b64:
            mood_board_url = await _upload_mood_board_to_s3(brief.mood_board_b64)
            try:
                await write_mood_board_to_workspace(
                    container=_workspace_container_name(session.workspace_id),
                    image_b64=brief.mood_board_b64,
                )
            except Exception:  # noqa: BLE001
                log.warning(
                    "mood_board: workspace write failed — Codex turns will "
                    "run without the image input",
                    exc_info=True,
                )

        # Graph returned — close any event still open (typically the last
        # stage, :moodboard).  The S3 URL (for frontend display) rides
        # along on the moodboard event so the frontend card can render
        # without another round-trip.
        overrides: dict[str, dict[str, Any]] = {}
        if mood_board_url:
            overrides["discovery:moodboard"] = {"mood_board_url": mood_board_url}
        await progress.finalize_all(status="completed", overrides=overrides)

        intent_nonempty = sum(
            1 for v in brief.intent.model_dump().values() if v
        )
        log.info(
            "discovery: graph done (intent_nonempty=%d/%d, pinterest_refs=%d, brief_chars=%d, mood_board=%s)",
            intent_nonempty,
            len(brief.intent.model_dump()),
            len(brief.pinterest_refs),
            len(brief.brief),
            "yes" if mood_board_url else "no",
        )

        # Persist the design_intent row (supersede prior active row).
        await _persist_design_intent(
            conn=session.conn,
            conn_lock=session.conn_lock,
            project_id=session.project_id,
            session_id=session.session_id,
            brief=brief,
            mood_board_url=mood_board_url,
        )
        log.info("discovery: persisted design_intent row")

        intent_dump = brief.intent.model_dump()
        refs_dump = [r.model_dump() for r in brief.pinterest_refs]
        queries_list = list(brief.pinterest_queries)

        return RunOutcome(
            status="completed",
            output={
                "brief": brief.brief,
                "intent": intent_dump,
                "pinterest_refs": refs_dump,
                "pinterest_queries": queries_list,
            },
        )

    async def handle_control(self, event: dict[str, Any]) -> None:
        """Best-effort cooperative cancel — invoked by the worker-side
        session control consumer (``_consume_session_control`` in the
        orchestrator) when the user hits the Stop button.

        Cancels the in-flight ``run_design_intent`` task.  LangGraph
        propagates ``asyncio.CancelledError`` through its node awaits so
        the pending LLM / HTTP / gpt-image-1 call exits at its next await
        point.  ``run`` catches the CancelledError and returns
        RunOutcome(status="interrupted").
        """
        if event.get("kind") != "interrupt":
            return
        task = self._active_task
        if task is None or task.done():
            return
        task.cancel()


async def _persist_design_intent(
    *,
    conn: Any,
    conn_lock: Any,
    project_id: UUID,
    session_id: UUID,
    brief: CompiledBrief,
    mood_board_url: str | None = None,
) -> None:
    """Insert a new active design_intents row and supersede the prior one,
    atomically."""
    intent_json = json.dumps(brief.intent.model_dump())
    refs_json = json.dumps([r.model_dump() for r in brief.pinterest_refs])
    queries_json = json.dumps(list(brief.pinterest_queries))

    async with conn_lock:
        async with conn.transaction():
            await conn.execute(
                "UPDATE design_intents SET status='superseded' "
                "WHERE project_id=$1 AND status='active'",
                project_id,
            )
            await conn.execute(
                "INSERT INTO design_intents "
                "(project_id, session_id, intent_jsonb, compiled_brief, "
                " pinterest_refs_jsonb, pinterest_queries_jsonb, "
                " mood_board_url, status) "
                "VALUES ($1, $2, $3::jsonb, $4, $5::jsonb, $6::jsonb, $7, 'active')",
                project_id,
                session_id,
                intent_json,
                brief.brief,
                refs_json,
                queries_json,
                mood_board_url,
            )
