"""Review node — LLM quality gate between the clarifier's emit and the
pinterest/compiler stages.

When the clarifier calls ``emit_design_intent``, this node runs a second
LLM pass that grades whether the emitted intent has enough concrete
signal on the five required fields (see prompts/review_system.py).  If
the reviewer rejects, ``design_intent`` is cleared and a guidance
message is appended to the conversation, causing the conditional edge
to route back to ``clarifier_step`` — which will treat this as a new
round and ask the user targeted follow-up questions.

A ``review_rejections`` counter on state prevents infinite clarifier ↔
review deadlocks; after ``MAX_REVIEW_REJECTIONS`` the latest emit is
let through with the reviewer's gaps appended to ``intent.notes``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field

from polaris_design_intent.config import Settings
from polaris_design_intent.prompts.review_system import REVIEW_SYSTEM_PROMPT
from polaris_design_intent.state import DesignIntentState

logger = logging.getLogger(__name__)


# After this many consecutive rejections we stop asking the user more
# questions and let the intent through (with a note).  User-experience
# priority: two review pushbacks max is plenty; more feels naggy.
MAX_REVIEW_REJECTIONS = 2


# Route targets (public, so graph.py can wire them without duplicating
# string literals).
ROUTE_PINTEREST = "pinterest"
ROUTE_BACK_TO_CLARIFIER = "clarifier_step"


class ReviewVerdict(BaseModel):
    """Structured output from the reviewer LLM.

    ``extra='forbid'`` → ``additionalProperties: false`` so OpenAI's
    strict json_schema mode (the LangChain default) accepts the schema.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool = Field(
        description="True only if ALL five required fields pass the bar."
    )
    gaps: list[str] = Field(
        description=(
            "Field names that failed the bar (e.g. ['audience', 'primaryGoal']). "
            "Empty list when ok=true."
        )
    )
    reasons: str = Field(
        description=(
            "One-sentence-per-gap explanation of what's missing plus a "
            "concrete hint for what the clarifier should ask next.  Written "
            "in the user's original language.  Empty string when ok=true."
        )
    )


async def review_node(
    state: DesignIntentState, settings: Settings
) -> dict[str, Any]:
    intent = state.get("design_intent")
    logger.info(
        "review_step: enter (intent_keys=%d, prior_rejections=%d)",
        len(intent) if intent else 0,
        int(state.get("review_rejections") or 0),
    )
    if not intent:
        # Shouldn't happen — conditional edge should only route here when
        # the clarifier just emitted.  Defensive: pass through quietly.
        logger.warning("review_step: no design_intent in state, skipping")
        return {}

    # Defensive short-circuit: if every required key is already empty the
    # rule is obvious, skip the LLM call and reject straight away.
    if not _has_any_required_signal(intent):
        logger.info("review: all required fields empty; rejecting without LLM call")
        return _reject(
            state,
            gaps=["pageType", "audience", "primaryGoal", "visualDirection", "contentStructure"],
            reasons="emit_design_intent produced no usable content for the five required fields.",
        )

    model = ChatOpenAI(
        model=settings.review_model,
        api_key=settings.openai_api_key,
        temperature=0,  # deterministic grading
    ).with_structured_output(ReviewVerdict)

    messages = [
        SystemMessage(content=REVIEW_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                "Evaluate this design intent against the five required fields.\n\n"
                "```json\n"
                + json.dumps(intent, ensure_ascii=False, indent=2)
                + "\n```"
            )
        ),
    ]

    try:
        verdict: ReviewVerdict = await model.ainvoke(messages)  # type: ignore[assignment]
    except Exception:  # noqa: BLE001
        logger.warning("review LLM call failed — letting intent through", exc_info=True)
        return {}

    if verdict.ok:
        logger.info("review: pass")
        return {}

    logger.info("review: reject gaps=%s reasons=%s", verdict.gaps, verdict.reasons)
    return _reject(state, gaps=list(verdict.gaps), reasons=verdict.reasons)


def route_after_review(state: DesignIntentState) -> str:
    """After review_node returns — if design_intent is still present we
    pass (either the reviewer approved, or the rejection cap was hit and
    we annotated + let through).  If it was cleared, bounce back."""
    if state.get("design_intent"):
        logger.info("route_after_review: -> pinterest")
        return ROUTE_PINTEREST
    logger.info("route_after_review: -> clarifier_step (bounce back)")
    return ROUTE_BACK_TO_CLARIFIER


# ── helpers ───────────────────────────────────────────────────────────────


_REQUIRED_KEYS = (
    "pageType",
    "audience",
    "primaryGoal",
    "visualDirection",
    "contentStructure",
)


def _has_any_required_signal(intent: dict[str, Any]) -> bool:
    return any(
        isinstance(intent.get(k), str) and intent[k].strip() for k in _REQUIRED_KEYS
    )


def _reject(
    state: DesignIntentState,
    *,
    gaps: list[str],
    reasons: str,
) -> dict[str, Any]:
    """Build the state delta that bounces back to clarifier_step, OR
    overrides to pass-through when the rejection cap is hit."""
    rejections = int(state.get("review_rejections") or 0) + 1

    if rejections > MAX_REVIEW_REJECTIONS:
        logger.info(
            "review: rejection cap (%s) hit — passing intent through with notes",
            MAX_REVIEW_REJECTIONS,
        )
        intent = dict(state.get("design_intent") or {})
        existing_notes = intent.get("notes") or ""
        cap_note = (
            f"[review] After {rejections - 1} rejection(s), proceeding with "
            f"remaining gaps: {gaps}. Reasons: {reasons}"
        )
        intent["notes"] = (
            existing_notes + ("\n" if existing_notes else "") + cap_note
        ).strip()
        return {
            "design_intent": intent,
            "review_rejections": rejections,
        }

    # Normal rejection: clear design_intent so route_after_review sends
    # control back to clarifier_step; append a guidance HumanMessage that
    # the clarifier's next LLM call will see alongside prior context.
    messages = list(state.get("messages") or [])
    messages.append(
        HumanMessage(
            content=(
                "[review] Your previous emit_design_intent call didn't pass "
                "the quality gate.  These fields need more concrete signal:\n"
                f"  gaps: {gaps}\n"
                f"  reasons: {reasons}\n"
                "Ask the user targeted follow-up questions to fill these "
                "gaps, then call emit_design_intent again."
            )
        )
    )
    return {
        "design_intent": None,
        "messages": messages,
        "review_rejections": rejections,
    }
