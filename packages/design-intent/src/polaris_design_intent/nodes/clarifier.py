from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from polaris_design_intent.config import Settings
from polaris_design_intent.prompts.clarifier_system import CLARIFIER_SYSTEM_PROMPT
from polaris_design_intent.state import DesignIntentState

logger = logging.getLogger(__name__)

# Public route targets for the conditional edges in graph.py.
ROUTE_ASK = "clarifier_ask"
ROUTE_PINTEREST = "pinterest"
ROUTE_PALETTE = "palette_step"
ROUTE_LOOP = "clarifier_step"


@tool
def ask_questions(questions: list[dict]) -> str:
    """Ask the user a batch of clarification questions and wait for answers.

    `questions` is a list of dicts: {id, title, description?, choices?, required?}.
    The returned string is a JSON-encoded list of {question_id, answer}.
    """
    # Tool body is not actually executed by our graph — the graph reads the
    # tool_call args from the AIMessage directly and handles the interrupt in
    # the `clarifier_ask` node.  We define the tool only so the LLM knows its
    # schema via bind_tools.
    return "__pending__"


@tool
def propose_color_palette(
    industry: str,
    visual_direction: str,
    audience: str,
    language: str,
    notes: str | None = None,
) -> list[dict]:
    """Generate 5 context-appropriate primary-color options.

    Call this BEFORE asking the user to pick a primary color.  Pass in
    the industry (e.g. "luxury real estate"), visual direction
    (e.g. "architectural cinematic"), audience, and the target language
    ("zh" / "en") for the labels.  The tool returns a list of 5 dicts
    shaped ``{"id", "label", "swatch"}`` — feed them straight into the
    next `ask_questions` call as the color question's `choices`.
    """
    # Body is a stub — the actual color-theory LLM call happens in the
    # `palette_step` graph node; this decorator exists so the clarifier
    # LLM learns the schema via `bind_tools`.
    return []


class PinterestQuerySlots(BaseModel):
    """Structured slot for one Pinterest reference query.

    Word caps are enforced both by the system prompt (soft) and by
    ``_render_query_slots`` at concatenation time (hard truncate).  Only
    ``pageType`` is required — without one we'd surface generic
    homepage refs.  Keep every value in English; Pinterest's index is
    English-leaning and over-long phrases dilute BM25 ranking.
    """

    industry: str | None = Field(
        default=None,
        description="≤ 2 words, English. e.g. 'real estate', 'golf club', 'dental'",
    )
    pageType: str = Field(
        description="REQUIRED, ≤ 2 words, English. e.g. 'landing page', 'pricing', 'homepage'",
    )
    color: str | None = Field(
        default=None,
        description="≤ 1 word, English. e.g. 'white', 'champagne', 'beige'",
    )
    style: str | None = Field(
        default=None,
        description="≤ 1 word, English. e.g. 'luxury', 'minimal', 'cinematic'",
    )


@tool
def emit_design_intent(
    intent: dict, pinterest_queries: list[PinterestQuerySlots]
) -> dict:
    """Finalize the design intent and hand off to reference fetching.

    `intent` must conform to the 18-key DesignIntent schema.
    `pinterest_queries` is a list of 1–3 structured slot objects (see
    :class:`PinterestQuerySlots`).  The graph concatenates non-empty
    slots into a single search string before hitting Pinterest.
    Calling this tool terminates the clarifier loop.
    """
    return {"design_intent": intent, "pinterest_queries": pinterest_queries}


CLARIFIER_TOOLS = [ask_questions, propose_color_palette, emit_design_intent]


def _truncate(value: str | None, max_words: int) -> str:
    """Trim ``value`` to at most ``max_words`` whitespace-separated tokens.

    Returns "" for None / empty / whitespace-only inputs so callers can
    drop the slot entirely.
    """
    if not value:
        return ""
    words = value.strip().split()
    if not words:
        return ""
    return " ".join(words[:max_words])


def _render_query_slots(raw: Any) -> str:
    """Render one ``PinterestQuerySlots`` (or its dict form) into the
    space-separated query string the scraper expects, e.g.
    'real estate landing page white luxury'.

    The ``web design`` suffix is appended later in
    ``pinterest_node._enrich_query`` — keep it out of here so the
    suffix logic stays in one place.

    Empty slots are dropped silently.  Per-slot word caps:
      industry ≤ 2, pageType ≤ 2, color ≤ 1, style ≤ 1.

    Returns "" if the resulting query has no pageType (the only required
    slot).  Caller filters those out.
    """
    if isinstance(raw, BaseModel):
        slots = raw.model_dump()
    elif isinstance(raw, dict):
        slots = raw
    else:
        return ""
    page_type = _truncate(slots.get("pageType"), 2)
    if not page_type:
        return ""
    parts = [
        _truncate(slots.get("industry"), 2),
        page_type,
        _truncate(slots.get("color"), 1),
        _truncate(slots.get("style"), 1),
    ]
    return " ".join(p for p in parts if p)


def _build_model(settings: Settings) -> Any:
    # ``tool_choice="any"`` → OpenAI's ``tool_choice: required``: the model
    # MUST call one of our two tools on every response.  Without this the
    # model sometimes returns natural-language prose (especially on review
    # bounce-backs), which lands in ROUTE_LOOP and re-fires the LLM on
    # the same growing context until LangGraph's recursion cap kicks in.
    return ChatOpenAI(
        model=settings.clarifier_model,
        api_key=settings.openai_api_key,
        temperature=0.3,
    ).bind_tools(CLARIFIER_TOOLS, tool_choice="any")


def _seed_preamble(seed_intent: dict | None) -> str:
    if not seed_intent:
        return ""
    return (
        "\n\nPrior design intent (seed) — the user is re-discovering. Treat these "
        "fields as already known and only ask about what is changing:\n```json\n"
        + json.dumps(seed_intent, ensure_ascii=False, indent=2)
        + "\n```"
    )


def _last_ai_message(messages: list[Any]) -> AIMessage | None:
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            return m
    return None


async def clarifier_step(state: DesignIntentState, settings: Settings) -> dict[str, Any]:
    """LLM call — NO interrupts here.  Split from `clarifier_ask` so interrupt
    replay on resume stays cheap (the ask node has a near-empty body before
    the interrupt)."""
    messages = list(state.get("messages") or [])
    round_count = state.get("round", 0)

    # Seed conversation on first entry.
    if not messages:
        user = (state.get("original_user_message") or "") + _seed_preamble(
            state.get("seed_intent")
        )
        messages = [
            SystemMessage(content=CLARIFIER_SYSTEM_PROMPT),
            HumanMessage(content=user),
        ]

    logger.info(
        "clarifier_step: enter round=%d msgs=%d review_rejections=%d",
        round_count,
        len(messages),
        int(state.get("review_rejections") or 0),
    )

    # Round-cap nudge: once the user has answered 3 rounds of questions, force
    # the next call to emit rather than ask again.
    if round_count >= settings.max_rounds and not _round_cap_already_forced(messages):
        logger.info("clarifier_step: round cap reached, forcing emit")
        messages.append(
            HumanMessage(
                content=(
                    "You have used your 3 question rounds. Call "
                    "`emit_design_intent` now with your best-effort intent. "
                    "Record any unresolved assumptions in `intent.notes`."
                )
            )
        )

    model = _build_model(settings)
    ai_msg: AIMessage = await model.ainvoke(messages)
    messages.append(ai_msg)

    tool_calls = getattr(ai_msg, "tool_calls", None) or []
    chosen = tool_calls[0].get("name") if tool_calls else None
    logger.info(
        "clarifier_step: LLM returned tool=%s (tool_calls=%d, content_chars=%d)",
        chosen or "<none>",
        len(tool_calls),
        len(str(getattr(ai_msg, "content", "") or "")),
    )

    if tool_calls and tool_calls[0].get("name") == "emit_design_intent":
        args = tool_calls[0].get("args") or {}
        intent = args.get("intent") or {}
        raw_queries = args.get("pinterest_queries") or []
        # Render structured slots → flat search strings, drop any with
        # an empty pageType (the renderer returns "" in that case).
        queries = [s for s in (_render_query_slots(q) for q in raw_queries) if s]
        logger.info(
            "clarifier_step: emit_design_intent "
            "(intent_keys=%d, raw_queries=%d, rendered_queries=%d, queries=%s)",
            len(intent),
            len(raw_queries),
            len(queries),
            queries,
        )
        messages.append(
            ToolMessage(
                content=json.dumps(args, ensure_ascii=False),
                tool_call_id=tool_calls[0].get("id", ""),
            )
        )

        # Defective emit: tool called but `intent` is empty.  tool_choice="any"
        # stops the model from returning prose but not from short-circuiting
        # with an empty tool call.  Treat it as a non-emit, nudge the model
        # with concrete feedback, and bail hard after two retries.
        if not intent:
            empty_count = int(state.get("empty_emits") or 0) + 1
            logger.warning(
                "clarifier_step: emit_design_intent had EMPTY intent (attempt %d)",
                empty_count,
            )
            if empty_count > 2:
                raise RuntimeError(
                    "Clarifier stuck: emit_design_intent with empty intent "
                    f"{empty_count} times; aborting discovery run."
                )
            messages.append(
                HumanMessage(
                    content=(
                        "Your last `emit_design_intent` call had an EMPTY "
                        "`intent` object.  You MUST populate at least these "
                        "five keys with concrete strings based on what the "
                        "user already told you:\n"
                        "  - pageType\n  - audience\n  - primaryGoal\n"
                        "  - visualDirection\n  - contentStructure\n\n"
                        "If information is still missing, call `ask_questions` "
                        "instead — do NOT re-emit with an empty intent."
                    )
                )
            )
            return {"messages": messages, "empty_emits": empty_count}

        return {
            "messages": messages,
            "design_intent": intent,
            "pinterest_queries": queries,
        }

    # For ask_questions or unknown, defer to conditional edge routing.
    return {"messages": messages}


async def clarifier_ask(state: DesignIntentState, _settings: Settings) -> dict[str, Any]:
    """Surface questions via `interrupt()`, append answers on resume.

    Body before `interrupt()` is intentionally tiny so that LangGraph's node
    replay on resume is cheap (no LLM calls, no side effects).
    """
    messages = list(state.get("messages") or [])
    round_count = state.get("round", 0)

    last_ai = _last_ai_message(messages)
    if not last_ai or not getattr(last_ai, "tool_calls", None):
        logger.warning("clarifier_ask entered without a pending ask_questions tool call")
        return {"messages": messages}

    tc = last_ai.tool_calls[0]
    questions = (tc.get("args") or {}).get("questions") or []

    logger.info(
        "clarifier_ask: interrupting with %d question(s), round will advance %d -> %d",
        len(questions),
        round_count,
        round_count + 1,
    )

    # Suspends the graph.  On `Command(resume=answers)`, `interrupt()` returns
    # the answers and we fall through.
    answers = interrupt({"kind": "ask_questions", "questions": questions})

    logger.info("clarifier_ask: resumed with %d answer(s)", len(answers or []))

    messages.append(
        ToolMessage(
            content=json.dumps(answers, ensure_ascii=False),
            tool_call_id=tc.get("id", ""),
        )
    )
    return {"messages": messages, "round": round_count + 1}


def _round_cap_already_forced(messages: list[Any]) -> bool:
    """Detect whether we've already injected the round-cap HumanMessage so we
    don't append it on every replay."""
    for m in reversed(messages):
        if isinstance(m, HumanMessage) and "emit_design_intent" in str(m.content):
            return True
        if isinstance(m, AIMessage):
            break
    return False


def route_after_step(state: DesignIntentState) -> str:
    """Conditional edge out of `clarifier_step`."""
    if state.get("design_intent"):
        logger.info("route_after_step: -> review_step (design_intent set)")
        return ROUTE_PINTEREST

    last_ai = _last_ai_message(state.get("messages") or [])
    if last_ai is None:
        logger.warning("route_after_step: -> clarifier_step LOOP (no AI message)")
        return ROUTE_LOOP
    tcs = getattr(last_ai, "tool_calls", None) or []
    if tcs and tcs[0].get("name") == "ask_questions":
        logger.info("route_after_step: -> clarifier_ask")
        return ROUTE_ASK
    if tcs and tcs[0].get("name") == "propose_color_palette":
        logger.info("route_after_step: -> palette_step")
        return ROUTE_PALETTE
    # No tool call, unknown tool, or emit_design_intent that was defective
    # (empty intent — we cleared it upstream and appended corrective feedback).
    # Loop back so the model can try again.
    called = tcs[0].get("name") if tcs else "<no tool>"
    logger.warning(
        "route_after_step: -> clarifier_step LOOP (called=%s, design_intent empty/missing)",
        called,
    )
    return ROUTE_LOOP


# ── palette_step ───────────────────────────────────────────────────────────
# Dedicated node that materializes the `propose_color_palette` tool.  The
# clarifier LLM calls the tool with the project context it has gathered;
# this node runs a focused color-theorist LLM call and appends the result
# as a ToolMessage so the clarifier sees it on the next turn and can feed
# the five options into `ask_questions`.

PALETTE_SYSTEM_PROMPT = """You are a color theorist for a website-builder.

Given an industry, visual direction, and audience, propose EXACTLY 5
primary-color options forming a coherent palette for THIS project's mood.

Rules:
- Return ONLY a JSON array of 5 objects: {"id","label","swatch"}.
- `swatch` MUST be a 7-char #RRGGBB hex (no alpha, no shorthand).
- `id` is a short ascii slug (lowercase + underscore), unique across the 5.
- `label` in the user's target `language` ("zh" or "en"), 1–3 words.
- Include at least one light/neutral base AND one darker anchor; the
  other three should feel tailored to the industry/mood.
- No explanation, no code fence — just the JSON array."""


_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


# If the LLM returns malformed or empty output, fall back to this neutral
# palette so the clarifier loop can keep moving.  These five also happen
# to be the original hard-coded defaults — safe middle-of-the-road choices.
_FALLBACK_PALETTE: list[dict[str, str]] = [
    {"id": "white",      "label": "White",      "swatch": "#FFFFFF"},
    {"id": "warm_white", "label": "Warm white", "swatch": "#F5F1E8"},
    {"id": "sage",       "label": "Sage",       "swatch": "#A6B89F"},
    {"id": "navy",       "label": "Navy",       "swatch": "#1E2A3A"},
    {"id": "charcoal",   "label": "Charcoal",   "swatch": "#2B2B2B"},
]


def _parse_and_validate_palette(raw: str) -> list[dict]:
    """Parse a JSON array of 5 {id,label,swatch} dicts from a raw model
    response.  Tolerates a leading/trailing ```json``` code fence (some
    models add them despite the instruction).  Returns ``_FALLBACK_PALETTE``
    on any parse/validation failure — the clarifier loop must not deadlock."""
    stripped = raw.strip()
    if stripped.startswith("```json"):
        stripped = stripped[len("```json"):].strip()
    elif stripped.startswith("```"):
        stripped = stripped[3:].strip()
    if stripped.endswith("```"):
        stripped = stripped[:-3].strip()
    try:
        parsed = json.loads(stripped)
        if not isinstance(parsed, list) or len(parsed) != 5:
            raise ValueError("not a 5-item list")
        for item in parsed:
            if not isinstance(item, dict):
                raise ValueError("non-dict item")
            if not _HEX_RE.match(str(item.get("swatch", ""))):
                raise ValueError(f"bad swatch: {item!r}")
            if not item.get("id") or not item.get("label"):
                raise ValueError(f"missing id/label: {item!r}")
        return parsed
    except Exception:
        logger.warning("palette_step: LLM output invalid — using fallback palette")
        return _FALLBACK_PALETTE


async def palette_step(state: DesignIntentState, settings: Settings) -> dict[str, Any]:
    """Run one color-theorist LLM call, append its output as a ToolMessage."""
    messages = list(state.get("messages") or [])
    last_ai = _last_ai_message(messages)
    if last_ai is None or not getattr(last_ai, "tool_calls", None):
        logger.warning("palette_step entered without a pending tool call")
        return {"messages": messages}

    tc = last_ai.tool_calls[0]
    args = tc.get("args") or {}
    logger.info(
        "palette_step: generating palette for industry=%r direction=%r audience=%r lang=%s",
        args.get("industry"),
        args.get("visual_direction"),
        args.get("audience"),
        args.get("language"),
    )

    model = ChatOpenAI(
        model=settings.compiler_model,
        api_key=settings.openai_api_key,
        temperature=0.7,
    )
    try:
        resp = await model.ainvoke(
            [
                SystemMessage(content=PALETTE_SYSTEM_PROMPT),
                HumanMessage(content=json.dumps(args, ensure_ascii=False)),
            ]
        )
        palette = _parse_and_validate_palette(str(resp.content or ""))
    except Exception:  # noqa: BLE001
        logger.warning("palette_step: LLM call raised — using fallback", exc_info=True)
        palette = _FALLBACK_PALETTE

    logger.info("palette_step: returning %d colors", len(palette))
    messages.append(
        ToolMessage(
            content=json.dumps(palette, ensure_ascii=False),
            tool_call_id=tc.get("id", ""),
        )
    )
    return {"messages": messages}
