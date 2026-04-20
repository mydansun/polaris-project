from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DesignIntent(BaseModel):
    """Structured design brief keys mirroring the user-supplied compiler prompt.

    Every previously-loose field (``visualDirection`` / ``contentStructure``
    / ``narrative`` / ``designSystem`` / ``interactionStyle`` /
    ``motionGuidance`` / ``imageryGuidance`` / ``implementationRequirements``)
    is now ``str | None``.  The LLM writes prose / markdown there instead of
    nested objects — this keeps the JSON schema compatible with OpenAI's
    strict structured-output mode (which requires ``additionalProperties:
    false`` on every nested object, and our old ``dict | list | str | None``
    unions emitted anonymous objects that violated it).

    ``extra='forbid'`` turns into ``additionalProperties: false`` on the
    emitted JSON Schema — required by strict mode.  Do not reorder or
    rename keys: the compiler system prompt enumerates them exactly and
    downstream consumers key off them verbatim.
    """

    model_config = ConfigDict(extra="forbid")

    pageType: str | None = None
    themeMode: Literal["light", "dark", "auto"] | None = None
    brandName: str | None = None
    productName: str | None = None
    audience: str | None = None
    primaryGoal: str | None = None
    coreUseCase: str | None = None
    visualDirection: str | None = None
    contentStructure: str | None = None
    narrative: str | None = None
    designSystem: str | None = None
    interactionStyle: str | None = None
    hardConstraints: list[str] = Field(default_factory=list)
    avoidPatterns: list[str] = Field(default_factory=list)
    motionGuidance: str | None = None
    imageryGuidance: str | None = None
    implementationRequirements: str | None = None
    notes: str | None = None

    # ── Frontend-skill structured tokens ───────────────────────────────
    # These were added to push the compiler LLM away from hand-wavy
    # ("modern sans", "a blueish accent") output and toward choices that
    # Codex can implement directly.  All optional — `None` / `[]` means
    # "unconstrained", and the compiler fills them per the prompt's
    # "Frontend Craft Fields" section.
    typographyPrimary: str | None = None
    typographySecondary: str | None = None
    accentColorHex: str | None = None
    heroLayout: Literal[
        "full_bleed_image",
        "full_bleed_gradient",
        "split",
        "minimal",
        "poster",
        "no_hero",
    ] | None = None
    cardPolicy: Literal[
        "cardless_default",
        "cards_for_interactive_only",
        "card_grid_ok",
    ] | None = None
    motionPlan: list[str] = Field(default_factory=list)


class PinterestRef(BaseModel):
    """One Pinterest result, optionally with a base64-encoded image payload.

    `image_b64` and `mime_type` are only populated for the subset of refs that
    will be fed as multimodal inputs to the compiler node.  URLs are always
    preserved so downstream consumers (AGENTS.md, UI) can link out.
    """

    id: str
    title: str
    max: str
    normal: str
    mime_type: str | None = None
    image_b64: str | None = None
    score: float | None = None
    score_reason: str | None = None


class CompiledBrief(BaseModel):
    """Output of the compiler node.  `intent` is the structured 18-key object;
    `brief` is the design brief (in the user's language) consumed by Codex.

    `mood_board_b64` is populated by the downstream `mood_board_step` node
    — a base64 PNG that the worker writes into the workspace container so
    Codex can consume it as a `localImage` turn input.  None if image
    generation failed or no Pinterest reference was available."""

    intent: DesignIntent
    brief: str
    pinterest_refs: list[PinterestRef] = Field(default_factory=list)
    pinterest_queries: list[str] = Field(default_factory=list)
    mood_board_b64: str | None = None
