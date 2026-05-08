"""Replay variant of :func:`run_design_intent`.

Drop-in replacement for the worker's discovery agent: same signature,
same return type, no LLM calls.  Walks the recorded
``design_intent_nodes`` from a raw fixture and:

  * Calls ``user_input_fn`` at each ``clarifier_ask`` node, passing the
    questions extracted from the recorded ``messages``.  This drives
    the same SSE clarification card the real run would have shown, so
    the frontend / Playwright driver can answer them as recorded.
  * Replays the SSE-shape progress events through the supplied
    ``callbacks`` (LangChain handlers) so the chat shows the references
    gallery / mood-board / "design brief" bubbles exactly as during a
    live run.
  * Returns a :class:`CompiledBrief` reconstructed from the recorded
    ``compiler`` node's output.

Network calls are blocked by ``polaris_agent_core.replay_guard``;
this runner deliberately avoids constructing any LLM client so the
only path that ever raises is the unintended-fallthrough path.
"""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path
from typing import Any

from polaris_design_intent.models import CompiledBrief, DesignIntent, PinterestRef
from polaris_design_intent.tools.user_input import UserInputFn

logger = logging.getLogger(__name__)


# ── Fixture loading ────────────────────────────────────────────────────


def load_raw_fixture(path: Path) -> dict[str, Any]:
    """Read a raw fixture from disk, transparently un-gzipping ``.json.gz``."""
    if path.suffix == ".gz":
        with gzip.open(path, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    return json.loads(path.read_text(encoding="utf-8"))


# ── Question extraction from recorded messages ─────────────────────────


def _questions_from_clarifier_ask(node_output: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the structured ``ask_questions`` payload out of a recorded
    ``clarifier_ask`` node's messages.

    Real graph: ``clarifier_step`` ends with an ``AIMessage`` whose
    ``tool_calls[0]`` is ``ask_questions(questions=[...])``.  ``clarifier_ask``
    then calls ``interrupt(questions)``.  The recorder captured the messages
    list but not the interrupt arg directly, so we reach back into the
    last AIMessage's tool_calls to recover the structured questions.
    """
    messages = node_output.get("messages") or []
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        # _make_jsonable wraps LangChain BaseMessage as
        # {"_message_type": ..., "content": ...}; pydantic-style dump
        # keeps tool_calls at the top level too.  Try both shapes.
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls and isinstance(msg.get("content"), dict):
            tool_calls = msg["content"].get("tool_calls") or []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            if tc.get("name") == "ask_questions":
                args = tc.get("args") or {}
                qs = args.get("questions") or []
                if isinstance(qs, list):
                    return [q for q in qs if isinstance(q, dict)]
    return []


# ── Compiler output → CompiledBrief reconstruction ─────────────────────


def _compiled_brief_from_recording(
    nodes: list[dict[str, Any]],
) -> CompiledBrief:
    """Stitch the recorded compiler/mood_board/pinterest outputs back
    into a CompiledBrief instance.

    Worker code downstream calls ``brief.intent``, ``brief.brief``,
    ``brief.mood_board_b64``, ``brief.pinterest_refs`` — we have to
    return all of them or the post-discovery wiring (write AGENTS.md,
    write mood_board.png, persist design_intent row) breaks.
    """
    compiler_output: dict[str, Any] = {}
    pinterest_refs: list[dict[str, Any]] = []
    pinterest_queries: list[str] = []
    mood_board_b64: str | None = None

    for node in nodes:
        name = node.get("node")
        out = node.get("output") or {}
        if name == "compiler" and isinstance(out, dict):
            compiler_output = out
        elif name == "pinterest_score" and isinstance(out, dict):
            refs = out.get("pinterest_refs")
            if isinstance(refs, list):
                pinterest_refs = [r for r in refs if isinstance(r, dict)]
        elif name == "clarifier_step" and isinstance(out, dict):
            qs = out.get("pinterest_queries")
            if isinstance(qs, list):
                pinterest_queries = [str(q) for q in qs]
        elif name == "mood_board_step" and isinstance(out, dict):
            b64 = out.get("mood_board_b64")
            if isinstance(b64, str) and b64:
                mood_board_b64 = b64

    # compiler returns two flat keys (NOT a nested CompiledBriefSchema):
    #   compiled_brief_json   = compiled.intent.model_dump()  (DesignIntent dict)
    #   compiled_brief_prompt = compiled.brief                (the brief text)
    # Don't be fooled by the "_json" suffix — that's just legacy naming.
    intent_dict = compiler_output.get("compiled_brief_json") or {}
    brief_text = compiler_output.get("compiled_brief_prompt") or ""

    try:
        intent = DesignIntent(**intent_dict)
    except Exception as exc:  # noqa: BLE001
        logger.warning("replay: DesignIntent reconstruction failed (%s); using empty", exc)
        intent = DesignIntent()

    refs = []
    for r in pinterest_refs:
        try:
            refs.append(PinterestRef(**r))
        except Exception:  # noqa: BLE001
            # The real graph trims fields between pinterest_score and
            # compiler; recorded refs may lack image_b64 etc.  Best-effort.
            continue

    return CompiledBrief(
        intent=intent,
        brief=brief_text,
        pinterest_refs=refs,
        pinterest_queries=pinterest_queries,
        mood_board_b64=mood_board_b64,
    )


# ── Callback simulation (optional SSE fidelity) ────────────────────────


async def _fire_chain_start(
    callbacks: list[Any] | None,
    *,
    node_name: str,
    inputs: dict[str, Any] | None,
) -> None:
    """Invoke each callback's on_chain_start as the real graph would.

    Discovery's progress handler reacts to node-level chain starts and
    emits the chat-pane SSE bubbles.  We pass ``inputs`` so the
    handler's special-cases (compiler reading pinterest_refs to close
    the references bubble with final scored data) still work.
    """
    if not callbacks:
        return
    for cb in callbacks:
        on_start = getattr(cb, "on_chain_start", None)
        if on_start is None:
            continue
        try:
            await on_start(
                serialized=None,
                inputs=inputs or {},
                run_id=None,
                parent_run_id=None,
                tags=[],
                metadata={},
                name=node_name,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("replay callback on_chain_start(%s) failed: %s", node_name, exc)


async def _fire_chain_end(
    callbacks: list[Any] | None,
    *,
    node_name: str,
    outputs: dict[str, Any] | None,
) -> None:
    if not callbacks:
        return
    for cb in callbacks:
        on_end = getattr(cb, "on_chain_end", None)
        if on_end is None:
            continue
        try:
            await on_end(
                outputs or {},
                run_id=None,
                parent_run_id=None,
                tags=[],
                name=node_name,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("replay callback on_chain_end(%s) failed: %s", node_name, exc)


# ── Public entry ───────────────────────────────────────────────────────


async def replay_run_design_intent(
    *,
    fixture_path: Path,
    user_input_fn: UserInputFn,
    callbacks: list[Any] | None = None,
) -> CompiledBrief:
    """Replay a recorded design-intent flow.

    Args mirror the subset of :func:`run_design_intent` the worker
    actually uses in its discovery agent.  The orchestrator's existing
    progress handler / clarification plumbing keeps working — this
    function fires the same callbacks and calls the same user-input
    function.

    Idempotent: re-invoking against the same fixture produces the same
    CompiledBrief (ids in pinterest refs are stable from recording).
    """
    fixture = load_raw_fixture(fixture_path)
    nodes = fixture.get("agent_io", {}).get("design_intent_nodes", []) or []
    if not nodes:
        raise RuntimeError(
            f"replay fixture {fixture_path.name!r} has no design_intent_nodes — "
            "this fixture wasn't recorded with the design-intent tap, can't replay"
        )

    # Walk nodes in recorded order.  We accumulate outputs into a
    # running ``state`` so the next node's chain_start gets a state
    # snapshot the progress handler can mine for refs / queries.
    state: dict[str, Any] = {}
    for idx, node in enumerate(nodes):
        name = node.get("node") or ""
        out = node.get("output") or {}

        # Fire chain_start *before* applying this node's output — the
        # handler reads the prior-state ``inputs`` (e.g. compiler reads
        # pinterest_refs from the state at compiler-entry, which is the
        # state after pinterest_score returned).
        await _fire_chain_start(callbacks, node_name=name, inputs=dict(state))

        if name == "clarifier_ask":
            # Drive the real clarification flow: the user_input_fn
            # publishes an SSE event + blocks on Redis until the
            # frontend POSTs answers.  We don't use the return value;
            # the user's UI clicks (driven by Playwright reading
            # user_actions) are the source of truth for this turn.
            questions = _questions_from_clarifier_ask(out)
            if questions:
                logger.info(
                    "replay: clarifier_ask round %d, %d question(s)",
                    out.get("round", -1),
                    len(questions),
                )
                try:
                    await user_input_fn(questions)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "replay: user_input_fn raised at clarifier_ask round=%d: %s",
                        out.get("round", -1),
                        exc,
                    )

        # Merge this node's output into running state.
        if isinstance(out, dict):
            for k, v in out.items():
                state[k] = v

    # Fire chain_end on the final node so the progress handler's
    # finalize logic runs (closes any open SSE bubbles).
    final_name = nodes[-1].get("node") if nodes else None
    if final_name:
        final_out = nodes[-1].get("output") or {}
        await _fire_chain_end(callbacks, node_name=final_name, outputs=final_out)

    return _compiled_brief_from_recording(nodes)
