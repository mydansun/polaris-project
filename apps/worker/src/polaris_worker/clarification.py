"""Shared clarification Q&A plumbing — reused by every agent that raises
user-facing questions (Codex's ``requestUserInput``, the discovery agent's
``ask_questions`` tool, and any future agent).

Both agents surface questions through the same SSE event + ``/clarify/*``
API routes; the only difference is the ``source`` tag carried in the SSE
payload so the UI can label the card.  Routing uses the per-session Redis
clarification channel (``clarification_channel`` in queue.py); each
question batch carries a ``run_id`` so the worker can bind answers to
exactly the run that asked.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal
from uuid import UUID, uuid4

from redis.asyncio import Redis

from polaris_worker.queue import (
    clarification_channel,
    session_control_channel,
    session_events_channel,
)

logger = logging.getLogger(__name__)

ClarificationSource = Literal["codex", "discovery"]


async def _get_pubsub_message(pubsub) -> dict | None:  # type: ignore[type-arg]
    """Pull one non-subscribe message from Redis pubsub, polling at 100ms."""
    while True:
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        if msg is not None:
            return msg
        await asyncio.sleep(0.1)


async def wait_for_answers(
    *,
    redis: Redis,
    session_id: UUID,
    run_id: UUID,
    questions: list[dict[str, Any]],
    source: ClarificationSource = "codex",
    request_id: str | None = None,
    timeout_seconds: int = 600,
) -> dict[str, dict[str, Any]]:
    """Publish a ``clarification_requested`` SSE event and block on the
    per-session Redis clarification channel until the user POSTs answers
    via ``/projects/{id}/clarify/response`` (or we time out / get the
    session interrupted via its control channel).

    ``questions`` is the frontend ``ClarificationQuestion`` shape (see
    ``packages/shared-types/src/index.ts``).  Caller must translate
    agent-specific question shapes into this format.

    Returns ``{request_id, answers}`` on success, ``{}`` on timeout or
    interrupt.
    """
    request_id = request_id or str(uuid4())

    await redis.publish(
        session_events_channel(session_id),
        json.dumps(
            {
                "session_id": str(session_id),
                "run_id": str(run_id),
                "kind": "clarification_requested",
                "request": {
                    "request_id": request_id,
                    "questions": questions,
                    "source": source,
                },
            }
        ),
    )

    logger.info(
        "clarification: source=%s session=%s run=%s request_id=%s qcount=%d",
        source,
        session_id,
        run_id,
        request_id,
        len(questions),
    )

    clarification_ch = clarification_channel(session_id)
    control_ch = session_control_channel(session_id)
    pubsub = redis.pubsub()
    await pubsub.subscribe(clarification_ch, control_ch)
    try:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                logger.warning(
                    "clarification timed out: source=%s request_id=%s",
                    source,
                    request_id,
                )
                return {}
            try:
                msg = await asyncio.wait_for(
                    _get_pubsub_message(pubsub), timeout=min(remaining, 5.0)
                )
            except asyncio.TimeoutError:
                continue
            if msg is None:
                continue
            channel = msg.get("channel")
            if isinstance(channel, bytes):
                channel = channel.decode()
            if channel == control_ch:
                logger.info("clarification interrupted via control channel")
                return {}
            if channel != clarification_ch:
                continue
            data = msg.get("data")
            if isinstance(data, bytes):
                data = data.decode()
            if not isinstance(data, str):
                continue
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            if payload.get("request_id") != request_id:
                continue
            # If the API pinned a specific run_id and it doesn't match us,
            # let the other run pick it up (belt-and-suspenders; there
            # shouldn't be two concurrent runs on one session in v1).
            reply_run_id = payload.get("run_id")
            if reply_run_id is not None and str(run_id) != reply_run_id:
                continue
            answers = payload.get("answers") or {}
            await redis.publish(
                session_events_channel(session_id),
                json.dumps(
                    {
                        "session_id": str(session_id),
                        "run_id": str(run_id),
                        "kind": "clarification_answered",
                        "request_id": request_id,
                    }
                ),
            )
            logger.info(
                "clarification answered: source=%s request_id=%s",
                source,
                request_id,
            )
            return {"request_id": request_id, "answers": answers}
    finally:
        await pubsub.unsubscribe(clarification_ch, control_ch)
        await pubsub.aclose()


def build_design_intent_user_input_fn(
    *,
    redis: Redis,
    session_id: UUID,
    run_id: UUID,
    source: str = "discovery",
):
    """Adapter that plugs :func:`wait_for_answers` into the LangGraph
    clarifier's ``UserInputFn`` protocol (``list[dict] -> list[dict]``).

    The clarifier sends ``{id, title, description?, choices: list[str],
    required?}`` dicts; we translate to the frontend ``ClarificationQuestion``
    shape, await answers, then flatten back to
    ``[{question_id, answer}, ...]`` for the graph.

    Also usable by CodexAgent after translating Codex's ``requestUserInput``
    question shape to the frontend shape — pass ``source="codex"``.
    """

    async def _fn(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        frontend_questions: list[dict[str, Any]] = []
        for q in questions:
            raw_choices = q.get("choices") or []
            # Accept either a list of plain strings or already-structured
            # {id, label, summary?} dicts (Codex translator passes the latter).
            choices: list[dict[str, Any]] = []
            for c in raw_choices:
                if isinstance(c, str):
                    choices.append({"id": c, "label": c, "summary": None, "swatch": None})
                elif isinstance(c, dict):
                    choices.append(
                        {
                            "id": c.get("id") or c.get("label") or "",
                            "label": c.get("label") or "",
                            "summary": c.get("summary"),
                            "swatch": c.get("swatch"),
                        }
                    )
            frontend_questions.append(
                {
                    "id": q.get("id") or f"q_{len(frontend_questions)}",
                    "title": q.get("title") or q.get("question") or "",
                    "description": q.get("description") or q.get("header"),
                    "required": bool(q.get("required", True)),
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
            source=source,  # type: ignore[arg-type]
        )
        raw_answers = (result or {}).get("answers") or {}

        flat: list[dict[str, Any]] = []
        for qid, ans in raw_answers.items():
            if isinstance(ans, dict):
                value = ans.get("override_text") or ans.get("selected_choice") or ""
            else:
                value = str(ans)
            flat.append({"question_id": qid, "answer": value})
        return flat

    return _fn
