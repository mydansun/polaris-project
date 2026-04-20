from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field

from polaris_design_intent.config import Settings
from polaris_design_intent.models import DesignIntent, PinterestRef
from polaris_design_intent.prompts.compiler_system import COMPILER_SYSTEM_PROMPT
from polaris_design_intent.state import DesignIntentState

logger = logging.getLogger(__name__)


class CompiledBriefSchema(BaseModel):
    """Wrapper schema for the compiler's two-field structured output.

    ``extra='forbid'`` → ``additionalProperties: false`` on the JSON Schema,
    required by OpenAI's strict structured-output mode.  Both fields are
    required (no defaults) so the LLM cannot omit ``brief``.
    """

    model_config = ConfigDict(extra="forbid")

    intent: DesignIntent = Field(description="The structured 18-key design intent object.")
    brief: str = Field(
        description=(
            "The final compiled design brief, ready to hand to a frontend "
            "generation model. Multi-paragraph text in the user's original "
            "language (mirror Chinese if user wrote Chinese, etc.). No "
            "meta-commentary, no JSON, no process notes."
        )
    )


def _build_human_content(
    *,
    intent_json: dict,
    refs: list[PinterestRef],
    original_user_message: str,
) -> list[dict[str, Any]]:
    """Build the multimodal user message — a text block followed by one
    image_url block per reference (data URL with base64 image bytes)."""
    # Titles summary doubles as an anchor the LLM can cite in the brief.
    titles_lines = [
        f"#{i+1}. {ref.title or '(no title)'}"
        for i, ref in enumerate(refs)
        if ref.image_b64
    ]
    original_block = (
        "Original user request — the compiled brief in Part 2 MUST be written\n"
        "in the same natural language as this text (mirror Chinese / English /\n"
        "etc. exactly).  The structured JSON keys in Part 1 stay in English,\n"
        "but their string values should also use this language where they\n"
        "describe style, narrative, or copy:\n\n"
        "```\n"
        + (original_user_message or "(no original message provided)")
        + "\n```\n\n"
    )
    text_block = {
        "type": "text",
        "text": (
            original_block
            + "Structured product + design intent collected from the user:\n\n"
            + "```json\n"
            + json.dumps(intent_json, ensure_ascii=False, indent=2)
            + "\n```\n\n"
            + (
                "Below are the Pinterest visual references attached as images. "
                "Use them as high-priority inputs for visual direction, composition, "
                "spacing, typography mood, color, and material feel. You may cite them "
                "in the compiled brief as 'reference #N'.\n\n"
                "References:\n" + "\n".join(titles_lines)
                if titles_lines
                else "No visual references are attached for this compilation."
            )
            + "\n\nNow populate the single JSON object with BOTH `intent` (the "
            + "18-key structured object) AND `brief` (the compiled design "
            + "brief as a single string, in the user's language).  Both "
            + "fields are required."
        ),
    }

    blocks: list[dict[str, Any]] = [text_block]
    for ref in refs:
        if not ref.image_b64:
            continue
        mime = ref.mime_type or "image/jpeg"
        blocks.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{ref.image_b64}"},
            }
        )
    return blocks


async def compiler_node(state: DesignIntentState, settings: Settings) -> dict[str, Any]:
    """Run the second-compilation step.

    Takes the design_intent + pinterest_refs from state, calls gpt-5.4 with the
    user-provided brief-compiler system prompt, and returns a structured
    CompiledBriefSchema populated with `intent` + `brief`.
    """
    intent_json = state.get("design_intent") or {}
    ref_dicts = state.get("pinterest_refs") or []
    refs = [PinterestRef.model_validate(r) for r in ref_dicts]
    original_user_message = state.get("original_user_message") or ""
    image_count = sum(1 for r in refs if r.image_b64)
    logger.info(
        "compiler: enter (intent_keys=%d, refs=%d, images=%d, model=%s)",
        len(intent_json),
        len(refs),
        image_count,
        settings.compiler_model,
    )

    # Strict structured output (json_schema mode — the LangChain default).
    # Requires every object to have ``additionalProperties: false`` and every
    # declared field to be populated; satisfied by DesignIntent / CompiledBriefSchema's
    # ``extra='forbid'`` configs and by typing every intent field as a
    # non-union primitive (str | None, Literal, list[str]).  This guarantees
    # ``brief`` is always present in the response.
    model = ChatOpenAI(
        model=settings.compiler_model,
        api_key=settings.openai_api_key,
        temperature=0.3,
    ).with_structured_output(CompiledBriefSchema)

    messages = [
        SystemMessage(content=COMPILER_SYSTEM_PROMPT),
        HumanMessage(
            content=_build_human_content(
                intent_json=intent_json,
                refs=refs,
                original_user_message=original_user_message,
            ),
        ),
    ]

    compiled: CompiledBriefSchema = await model.ainvoke(messages)  # type: ignore[assignment]
    logger.info(
        "compiler: done (brief_chars=%d)", len(compiled.brief or "")
    )

    return {
        "compiled_brief_json": compiled.intent.model_dump(),
        "compiled_brief_prompt": compiled.brief,
    }
