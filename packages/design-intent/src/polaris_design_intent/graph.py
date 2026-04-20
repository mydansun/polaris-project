from __future__ import annotations

import logging
from functools import partial
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from polaris_design_intent.config import Settings, get_settings
from polaris_design_intent.models import CompiledBrief, DesignIntent, PinterestRef
from polaris_design_intent.nodes.clarifier import (
    ROUTE_ASK,
    ROUTE_LOOP,
    ROUTE_PALETTE,
    ROUTE_PINTEREST,
    clarifier_ask,
    clarifier_step,
    palette_step,
    route_after_step,
)
from polaris_design_intent.nodes.compiler import compiler_node
from polaris_design_intent.nodes.mood_board import mood_board_node
from polaris_design_intent.nodes.pinterest import pinterest_node
from polaris_design_intent.nodes.review import (
    ROUTE_BACK_TO_CLARIFIER,
    ROUTE_PINTEREST as REVIEW_ROUTE_PINTEREST,
    review_node,
    route_after_review,
)
from polaris_design_intent.state import DesignIntentState
from polaris_design_intent.tools.user_input import UserInputFn

logger = logging.getLogger(__name__)


def build_graph(
    settings: Settings | None = None,
    *,
    checkpointer: Any | None = None,
) -> Any:
    """Assemble the design-intent LangGraph.

    Nodes:
      - clarifier_step:  LLM turn of the clarifier loop (no interrupts)
      - clarifier_ask:   interrupts with questions, resumes with answers
      - palette_step:    dedicated color-theorist LLM call that materializes
                         the clarifier's `propose_color_palette` tool
      - pinterest:       fetches reference images, base64-encodes top-N
      - compiler:        multimodal LLM call, produces CompiledBrief
      - mood_board_step: gpt-image-1 ``images.edit`` call using the
                         Pinterest chosen image as visual reference;
                         produces an ORIGINAL mood board PNG for Codex

    The caller supplies the checkpointer.  Tests use MemorySaver; the worker
    entry point plumbs an AsyncPostgresSaver.  If omitted, we fall back to
    MemorySaver — fine for one-shot runs without durable resume.
    """
    settings = settings or get_settings()
    checkpointer = checkpointer or MemorySaver()

    graph = StateGraph(DesignIntentState)

    graph.add_node("clarifier_step", partial(clarifier_step, settings=settings))
    graph.add_node("clarifier_ask", partial(clarifier_ask, _settings=settings))
    graph.add_node("palette_step", partial(palette_step, settings=settings))
    graph.add_node("review_step", partial(review_node, settings=settings))
    graph.add_node("pinterest", partial(pinterest_node, settings=settings))
    graph.add_node("compiler", partial(compiler_node, settings=settings))
    graph.add_node("mood_board_step", partial(mood_board_node, settings=settings))

    graph.add_edge(START, "clarifier_step")
    graph.add_conditional_edges(
        "clarifier_step",
        route_after_step,
        {
            ROUTE_ASK: "clarifier_ask",
            # After emit → gate through review_step, NOT straight into
            # pinterest.  If review rejects, it clears design_intent and
            # routes back here for another clarification round.
            ROUTE_PINTEREST: "review_step",
            ROUTE_PALETTE: "palette_step",
            ROUTE_LOOP: "clarifier_step",
        },
    )
    graph.add_edge("clarifier_ask", "clarifier_step")
    graph.add_edge("palette_step", "clarifier_step")
    graph.add_conditional_edges(
        "review_step",
        route_after_review,
        {
            REVIEW_ROUTE_PINTEREST: "pinterest",
            ROUTE_BACK_TO_CLARIFIER: "clarifier_step",
        },
    )
    graph.add_edge("pinterest", "compiler")
    graph.add_edge("compiler", "mood_board_step")
    graph.add_edge("mood_board_step", END)

    return graph.compile(checkpointer=checkpointer)


def _extract_interrupt_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    """LangGraph surfaces in-flight interrupts via the `__interrupt__` key on
    the invoke return value.  Normalize to the first interrupt's value dict."""
    interrupts = result.get("__interrupt__") if isinstance(result, dict) else None
    if not interrupts:
        return None
    first = interrupts[0]
    # Interrupt objects expose `.value`; dict-shaped fallbacks use `["value"]`.
    value = getattr(first, "value", None)
    if value is None and isinstance(first, dict):
        value = first.get("value")
    return value if isinstance(value, dict) else None


async def run_design_intent(
    *,
    project_id: str,
    turn_id: str,
    user_message: str,
    user_input_fn: UserInputFn,
    seed_intent: dict | None = None,
    settings: Settings | None = None,
    checkpointer: Any | None = None,
    callbacks: list[Any] | None = None,
) -> CompiledBrief:
    """Main entry point invoked by the worker.

    Runs the graph with a stable thread_id so LangGraph can persist and resume
    through clarification rounds.  Each interrupt surfaces a question batch to
    `user_input_fn`; its answers are handed back via `Command(resume=...)`.

    ``callbacks`` — optional LangChain CallbackHandlers.  Used by the worker
    side to observe per-node transitions (discovery:clarifying / pinterest /
    compiled) in real time instead of receiving them batched after the graph
    finishes.
    """
    settings = settings or get_settings()
    graph = build_graph(settings, checkpointer=checkpointer)
    config: dict[str, Any] = {
        "configurable": {"thread_id": f"design_intent:{turn_id}"},
        "callbacks": callbacks or [],
    }

    initial: DesignIntentState = {
        "project_id": project_id,
        "turn_id": turn_id,
        "original_user_message": user_message,
        "seed_intent": seed_intent,
        "messages": [],
        "round": 0,
        "pinterest_refs": [],
        "pinterest_queries": [],
    }

    logger.info(
        "run_design_intent: start project=%s turn=%s msg_chars=%d has_seed=%s",
        project_id,
        turn_id,
        len(user_message or ""),
        seed_intent is not None,
    )
    result: dict[str, Any] = await graph.ainvoke(initial, config=config)

    resume_iter = 0
    while payload := _extract_interrupt_payload(result):
        resume_iter += 1
        questions = payload.get("questions") or []
        logger.info(
            "run_design_intent: interrupt #%d surfacing %d question(s) to user",
            resume_iter,
            len(questions),
        )
        answers = await user_input_fn(questions)
        logger.info(
            "run_design_intent: resuming graph with %d answer(s)",
            len(answers or []),
        )
        result = await graph.ainvoke(Command(resume=answers), config=config)

    logger.info(
        "run_design_intent: graph finished after %d interrupt(s)", resume_iter
    )

    intent_dict = result.get("compiled_brief_json") or {}
    brief_text = result.get("compiled_brief_prompt") or ""
    pinterest_refs = [
        PinterestRef.model_validate(r) for r in (result.get("pinterest_refs") or [])
    ]
    # Strip image_b64 from the returned refs — it's only needed inside the
    # compiler call.  Callers get URLs + mime type only.
    for ref in pinterest_refs:
        ref.image_b64 = None

    return CompiledBrief(
        intent=DesignIntent.model_validate(intent_dict),
        brief=brief_text,
        pinterest_refs=pinterest_refs,
        pinterest_queries=result.get("pinterest_queries") or [],
        mood_board_b64=result.get("mood_board_b64") or None,
    )
