"""Tests for ``polaris_worker.clarification.wait_for_answers``.

Two things matter here and break in different ways:

1. **DB persistence.** When the caller supplies ``conn``/``conn_lock``/
   ``project_id``, a ``Clarification`` row is inserted *before* the
   SSE fires.  Without it, chat replay via ``GET /sessions/{id}`` shows
   no clarification history (the bug we shipped earlier in the day).
2. **Receive loop.** When the user POSTs answers to the per-session
   clarification channel, the helper returns ``{request_id, answers}``;
   on timeout or interrupt it returns ``{}``.

Both are exercised against fakes — no real asyncpg, no real Redis —
so this file stays fast (sub-second) and offline.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

import pytest

from polaris_worker.clarification import wait_for_answers
from polaris_worker.queue import (
    clarification_channel,
    session_control_channel,
    session_events_channel,
)


# ── Fakes ──────────────────────────────────────────────────────────────


class _FakePubSub:
    """Redis pubsub stub.  Queue messages with ``feed`` from the test
    body; ``get_message`` consumes them one at a time and otherwise
    returns ``None`` so the helper's loop times out cleanly.
    """

    def __init__(self) -> None:
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.closed = False
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def subscribe(self, *channels: str) -> None:
        self.subscribed.extend(channels)

    async def unsubscribe(self, *channels: str) -> None:
        self.unsubscribed.extend(channels)

    async def aclose(self) -> None:
        self.closed = True

    async def get_message(
        self, ignore_subscribe_messages: bool = False, timeout: float | None = None
    ) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(
                self._queue.get(), timeout=min(0.05, timeout or 0.05)
            )
        except asyncio.TimeoutError:
            return None

    def feed(self, channel: str, data: str) -> None:
        # The real redis-py pubsub messages have ``type/channel/data``
        # fields; ``_get_pubsub_message`` only inspects channel + data.
        self._queue.put_nowait({"type": "message", "channel": channel, "data": data})


class _FakeRedis:
    def __init__(self) -> None:
        self.published: list[dict[str, str]] = []
        self._pubsub = _FakePubSub()

    async def publish(self, channel: str, payload: str) -> None:
        self.published.append({"channel": channel, "payload": payload})

    def pubsub(self) -> _FakePubSub:
        return self._pubsub


class _FakeConn:
    """Records every ``execute`` call.  Mirrors only the ``execute`` slice
    of asyncpg.Connection that ``wait_for_answers`` actually uses."""

    def __init__(self) -> None:
        self.executed: list[dict[str, Any]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append({"sql": sql, "args": args})
        return "INSERT 0 1"


# ── Persistence: with conn ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persists_clarification_when_conn_provided():
    conn = _FakeConn()
    redis = _FakeRedis()
    session_id = uuid4()
    run_id = uuid4()
    project_id = uuid4()
    questions = [
        {
            "id": "q1",
            "title": "Pick a vibe",
            "choices": [{"id": "calm", "label": "Calm"}],
        }
    ]

    out = await wait_for_answers(
        redis=redis,
        session_id=session_id,
        run_id=run_id,
        questions=questions,
        source="discovery",
        timeout_seconds=0,  # exit the loop immediately after publish
        conn=conn,
        conn_lock=asyncio.Lock(),
        project_id=project_id,
    )

    assert out == {}  # timed out cleanly
    assert len(conn.executed) == 1, conn.executed
    sql = conn.executed[0]["sql"]
    assert "INSERT INTO clarifications" in sql
    assert "ON CONFLICT (request_id) DO NOTHING" in sql

    args = conn.executed[0]["args"]
    # Positional order pinned by the SQL: id, request_id, project_id,
    # session_id, run_id, agent_kind, questions_json.
    row_id, request_id, persisted_pid, persisted_sid, persisted_rid, agent_kind, questions_json = args
    assert isinstance(row_id, UUID)
    assert isinstance(request_id, str) and len(request_id) > 0
    assert persisted_pid == project_id
    assert persisted_sid == session_id
    assert persisted_rid == run_id
    assert agent_kind == "discovery"
    assert json.loads(questions_json) == questions


@pytest.mark.asyncio
async def test_explicit_request_id_is_used_in_insert():
    conn = _FakeConn()
    redis = _FakeRedis()
    out = await wait_for_answers(
        redis=redis,
        session_id=uuid4(),
        run_id=uuid4(),
        questions=[],
        source="codex",
        request_id="caller-supplied-id",
        timeout_seconds=0,
        conn=conn,
        conn_lock=asyncio.Lock(),
        project_id=uuid4(),
    )
    assert out == {}
    args = conn.executed[0]["args"]
    assert args[1] == "caller-supplied-id"

    # And the SSE payload carries the same id — frontend keys the
    # ClarificationCard off this request_id, so DB and SSE must agree.
    events = [p for p in redis.published if "events" in p["channel"]]
    assert events, redis.published
    payload = json.loads(events[0]["payload"])
    assert payload["request"]["request_id"] == "caller-supplied-id"


@pytest.mark.asyncio
async def test_generated_request_id_is_consistent_across_db_and_sse():
    conn = _FakeConn()
    redis = _FakeRedis()
    out = await wait_for_answers(
        redis=redis,
        session_id=uuid4(),
        run_id=uuid4(),
        questions=[],
        source="codex",
        timeout_seconds=0,
        conn=conn,
        conn_lock=asyncio.Lock(),
        project_id=uuid4(),
    )
    assert out == {}
    db_request_id = conn.executed[0]["args"][1]
    sse_payload = json.loads(redis.published[0]["payload"])
    sse_request_id = sse_payload["request"]["request_id"]
    # The generated id must be reused — otherwise the API can never
    # match the answer back to the persisted row.
    assert db_request_id == sse_request_id


@pytest.mark.asyncio
async def test_codex_source_is_persisted_as_agent_kind():
    conn = _FakeConn()
    redis = _FakeRedis()
    await wait_for_answers(
        redis=redis,
        session_id=uuid4(),
        run_id=uuid4(),
        questions=[],
        source="codex",
        timeout_seconds=0,
        conn=conn,
        conn_lock=asyncio.Lock(),
        project_id=uuid4(),
    )
    assert conn.executed[0]["args"][5] == "codex"


@pytest.mark.asyncio
async def test_questions_serialized_with_unicode_content():
    # Questions can carry CJK text — the json.dumps must not be
    # ascii-only or it would mangle prompts back to the user.
    conn = _FakeConn()
    redis = _FakeRedis()
    await wait_for_answers(
        redis=redis,
        session_id=uuid4(),
        run_id=uuid4(),
        questions=[{"id": "q1", "title": "选择风格"}],
        source="codex",
        timeout_seconds=0,
        conn=conn,
        conn_lock=asyncio.Lock(),
        project_id=uuid4(),
    )
    questions_json = conn.executed[0]["args"][6]
    assert json.loads(questions_json) == [{"id": "q1", "title": "选择风格"}]


# ── Persistence: without conn ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_db_call_when_conn_omitted():
    redis = _FakeRedis()
    out = await wait_for_answers(
        redis=redis,
        session_id=uuid4(),
        run_id=uuid4(),
        questions=[],
        timeout_seconds=0,
    )
    assert out == {}
    # SSE still fires — the helper degrades to the legacy ephemeral
    # mode rather than refusing to operate.
    assert len(redis.published) == 1


@pytest.mark.asyncio
async def test_no_db_call_when_only_some_of_the_three_supplied():
    # The contract is "all three or none" — supplying just conn
    # without a lock or project_id must NOT attempt the INSERT
    # (would NPE).
    conn = _FakeConn()
    redis = _FakeRedis()
    await wait_for_answers(
        redis=redis,
        session_id=uuid4(),
        run_id=uuid4(),
        questions=[],
        timeout_seconds=0,
        conn=conn,
        # conn_lock and project_id deliberately omitted
    )
    assert conn.executed == []


# ── Receive loop ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_returns_answers_when_published_to_clarification_channel():
    redis = _FakeRedis()
    session_id = uuid4()
    run_id = uuid4()
    rid = "req-receive-1"

    async def feed_answer():
        # Brief delay so wait_for_answers is already subscribed.
        await asyncio.sleep(0.02)
        redis.pubsub().feed(
            clarification_channel(session_id),
            json.dumps(
                {
                    "request_id": rid,
                    "run_id": str(run_id),
                    "answers": {"q1": {"selected_choice": "calm"}},
                }
            ),
        )

    feeder = asyncio.create_task(feed_answer())
    out = await wait_for_answers(
        redis=redis,
        session_id=session_id,
        run_id=run_id,
        questions=[],
        source="codex",
        request_id=rid,
        timeout_seconds=2,
    )
    await feeder

    assert out == {
        "request_id": rid,
        "answers": {"q1": {"selected_choice": "calm"}},
    }
    # Acknowledgement event published back so the frontend dismisses
    # the modal.
    ack_payloads = [
        json.loads(p["payload"])
        for p in redis.published
        if "events" in p["channel"]
    ]
    assert any(
        ev.get("kind") == "clarification_answered" and ev.get("request_id") == rid
        for ev in ack_payloads
    ), ack_payloads


@pytest.mark.asyncio
async def test_ignores_messages_for_a_different_request_id():
    # Two clarifications can be in flight on the same session (rare
    # but legal); the helper must ignore answers tagged for a sibling
    # request.
    redis = _FakeRedis()
    session_id = uuid4()
    run_id = uuid4()
    rid = "req-mine"

    async def feeder():
        await asyncio.sleep(0.02)
        redis.pubsub().feed(
            clarification_channel(session_id),
            json.dumps(
                {
                    "request_id": "req-someone-else",
                    "run_id": str(run_id),
                    "answers": {"q1": {"selected_choice": "x"}},
                }
            ),
        )

    asyncio.create_task(feeder())
    # Short timeout — should hit the deadline because the only
    # message we got was for a different request.
    out = await wait_for_answers(
        redis=redis,
        session_id=session_id,
        run_id=run_id,
        questions=[],
        request_id=rid,
        timeout_seconds=1,
    )
    assert out == {}


@pytest.mark.asyncio
async def test_returns_empty_when_interrupt_arrives_on_control_channel():
    # User clicked Stop while the modal was up — control channel
    # gets a message and the helper bails out with an empty dict
    # rather than blocking the agent forever.
    redis = _FakeRedis()
    session_id = uuid4()

    async def interrupt():
        await asyncio.sleep(0.02)
        redis.pubsub().feed(
            session_control_channel(session_id), json.dumps({"kind": "interrupt"})
        )

    asyncio.create_task(interrupt())
    out = await wait_for_answers(
        redis=redis,
        session_id=session_id,
        run_id=uuid4(),
        questions=[],
        timeout_seconds=2,
    )
    assert out == {}


@pytest.mark.asyncio
async def test_publishes_to_correct_channels_for_session():
    # Sanity — the SSE event lands on the per-session events channel
    # (not the clarification channel, not a global broadcast).
    redis = _FakeRedis()
    session_id = uuid4()
    await wait_for_answers(
        redis=redis,
        session_id=session_id,
        run_id=uuid4(),
        questions=[],
        timeout_seconds=0,
    )
    expected = session_events_channel(session_id)
    assert redis.published[0]["channel"] == expected


@pytest.mark.asyncio
async def test_pubsub_unsubscribes_and_closes_on_exit():
    # Resource hygiene: a stuck pubsub subscription is a slow leak
    # the worker carries until container restart.  Verify the cleanup
    # path runs.
    redis = _FakeRedis()
    session_id = uuid4()
    await wait_for_answers(
        redis=redis,
        session_id=session_id,
        run_id=uuid4(),
        questions=[],
        timeout_seconds=0,
    )
    pubsub = redis.pubsub()
    assert clarification_channel(session_id) in pubsub.subscribed
    assert session_control_channel(session_id) in pubsub.subscribed
    assert clarification_channel(session_id) in pubsub.unsubscribed
    assert pubsub.closed is True
