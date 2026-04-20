from __future__ import annotations

from typing import TypedDict

from langchain_core.messages import AnyMessage


class DesignIntentState(TypedDict, total=False):
    """State threaded through the LangGraph nodes.

    `total=False` — every key is optional; nodes populate what they own.
    Dict rather than Pydantic because LangGraph state reducers prefer TypedDict
    and we serialize this to a checkpointer anyway.
    """

    # --- call-site inputs ---
    project_id: str
    turn_id: str
    original_user_message: str
    seed_intent: dict | None  # prior active DesignIntent on re-discover

    # --- clarifier loop ---
    messages: list[AnyMessage]
    round: int  # number of ask_questions calls consumed

    # --- clarifier outputs ---
    design_intent: dict | None  # DesignIntent.model_dump()
    pinterest_queries: list[str]

    # --- review_step bookkeeping ---
    # Number of times review has rejected the clarifier's emit and sent it
    # back for another round.  Capped to prevent clarifier ↔ review
    # deadlocks; after the cap we let the intent through with a note.
    review_rejections: int

    # --- clarifier defect counter ---
    # Number of times the clarifier called emit_design_intent with an
    # empty `intent` object.  tool_choice="any" forces a tool call every
    # turn, but the model can dodge by emitting with `intent={}`.  We
    # count those, corrective-prompt the first two, and raise on #3 to
    # stop bleeding OpenAI calls.
    empty_emits: int

    # --- pinterest node output ---
    pinterest_refs: list[dict]  # [PinterestRef.model_dump(), ...]

    # --- compiler output ---
    compiled_brief_json: dict | None
    compiled_brief_prompt: str | None

    # --- mood_board_step output ---
    # Base64-encoded PNG from gpt-image-1's images.edit call, using the
    # Pinterest chosen image as a visual reference.  None on generation
    # failure (safety filter / rate limit / network) — downstream just
    # skips the file write and Codex runs without an image input.
    mood_board_b64: str | None
