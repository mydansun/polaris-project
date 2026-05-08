"""Pydantic schemas for replay fixtures (raw + annotated layers).

Two distinct shapes:

  * ``RawFixture`` — what the recorder writes verbatim during a live
    run.  No commentary, just facts: the user actions in concrete form
    + a11y snapshots, plus the agent IO (codex frames + design-intent
    node outputs).  This is the source of truth and must be immutable
    after recording (we hash its events array into ``raw_hash``).

  * ``AnnotatedFixture`` — derived from ``RawFixture`` by an offline
    pass that fills in ``semantic`` + ``intent`` + ``narrative`` +
    ``key_invariants``.  Carries ``raw_hash`` so the integrity test
    can refuse stale annotations after raw is re-recorded.

Schema version is part of every fixture so we can evolve safely:
parsers reject unknown majors, and tooling emits the current version.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SCHEMA_VERSION = 1


# ── Raw layer ──────────────────────────────────────────────────────────


class RawA11ySnapshot(BaseModel):
    """A11y snapshot taken at action time, used by the offline annotator.

    Free-form on purpose — the annotator picks fields it understands.
    Today we know we want ``target`` (role / name / testid) and a
    short ``ancestors`` chain; future annotators can mine more.
    """

    model_config = ConfigDict(extra="allow")

    target: dict[str, Any] = Field(default_factory=dict)
    ancestors: list[dict[str, Any]] = Field(default_factory=list)
    siblings_text: list[str] = Field(default_factory=list)


class RawUserAction(BaseModel):
    """One observed user-side action.

    Concrete-only.  ``concrete`` is a free-form dict because the shape
    differs per ``kind`` — a click has selector + viewport_xy, an
    answer_clarification has request_id + answers, a wait has only a
    waypoint name.  The annotator branches on ``kind``.
    """

    t: float = Field(ge=0, description="Seconds since recording start")
    kind: str = Field(min_length=1)
    concrete: dict[str, Any] = Field(default_factory=dict)
    a11y_snapshot: RawA11ySnapshot | None = None


class RawCodexFrame(BaseModel):
    """One JSON-RPC frame in the codex transcript."""

    t: float = Field(ge=0)
    direction: Literal["in", "out"]
    frame: dict[str, Any]


class RawDesignIntentNode(BaseModel):
    """One design-intent graph node observation."""

    t: float = Field(ge=0)
    node: str = Field(min_length=1)
    output: dict[str, Any] = Field(default_factory=dict)


class RawAgentIO(BaseModel):
    codex_frames: list[RawCodexFrame] = Field(default_factory=list)
    design_intent_nodes: list[RawDesignIntentNode] = Field(default_factory=list)


class RawFixture(BaseModel):
    version: int = Field(ge=1)
    scenario: str = Field(min_length=1, max_length=80)
    recorded_at: str = Field(description="ISO 8601")
    user_actions: list[RawUserAction] = Field(default_factory=list)
    agent_io: RawAgentIO = Field(default_factory=RawAgentIO)

    @model_validator(mode="after")
    def _check_version(self) -> "RawFixture":
        if self.version != SCHEMA_VERSION:
            raise ValueError(
                f"raw fixture schema version mismatch: file says "
                f"v{self.version}, code is v{SCHEMA_VERSION}"
            )
        return self


def compute_raw_hash(raw: RawFixture) -> str:
    """Stable hash over the immutable bits of a raw fixture.

    Used by ``AnnotatedFixture.raw_hash`` to detect drift.  The hash
    deliberately excludes ``recorded_at`` (cosmetic timestamp) and any
    ``a11y_snapshot`` field whose contents are large but irrelevant to
    the action timeline — re-snapshotting a11y trees from the same run
    shouldn't invalidate the annotation.
    """
    payload = {
        "scenario": raw.scenario,
        "user_actions": [
            {"t": ev.t, "kind": ev.kind, "concrete": ev.concrete}
            for ev in raw.user_actions
        ],
        "agent_io": {
            "codex_frames": [
                {"t": f.t, "direction": f.direction, "frame": f.frame}
                for f in raw.agent_io.codex_frames
            ],
            "design_intent_nodes": [
                {"t": n.t, "node": n.node, "output": n.output}
                for n in raw.agent_io.design_intent_nodes
            ],
        },
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


# ── Annotated layer ────────────────────────────────────────────────────


class AnnotatedSemantic(BaseModel):
    """Derived semantic info — recorded once during annotation."""

    model_config = ConfigDict(extra="allow")

    target_kind: str | None = None
    target_label: str | None = None
    element_role: str | None = None
    container: str | None = None


class AnnotatedClarificationRound(BaseModel):
    """Per-question annotation for an ``answer_clarification`` action."""

    question_id: str
    question_title: str
    question_topic: str | None = None
    selected_by_id: str | None = None
    selected_by_label: str | None = None
    selected_by_index: int | None = None
    free_text: str | None = None
    rationale: str | None = None


class AnnotatedAction(BaseModel):
    """Raw action augmented with semantic / intent layers."""

    t: float
    kind: str
    concrete: dict[str, Any] = Field(default_factory=dict)
    semantic: AnnotatedSemantic | None = None
    intent: str | None = Field(
        default=None,
        description=(
            "Short English phrase describing why the user did this; required "
            "for non-trivial actions (click / type / answer_clarification)."
        ),
    )
    rationale: str | None = Field(
        default=None,
        description=(
            "Longer Chinese commentary on the choice — context the AI used "
            "to pick this option.  Required for clarification answers."
        ),
    )
    clarification_rounds: list[AnnotatedClarificationRound] | None = None


class AnnotatedFixture(BaseModel):
    version: int = Field(ge=1)
    scenario: str
    raw_hash: str = Field(min_length=64, max_length=64)
    narrative: list[str] = Field(min_length=3)
    key_invariants: list[str] = Field(min_length=3)
    actions: list[AnnotatedAction] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_version_and_narrative(self) -> "AnnotatedFixture":
        if self.version != SCHEMA_VERSION:
            raise ValueError(
                f"annotated fixture schema version mismatch: file says "
                f"v{self.version}, code is v{SCHEMA_VERSION}"
            )
        for line in self.narrative:
            if len(line.strip()) < 10:
                raise ValueError(
                    "narrative lines must each be at least 10 chars — "
                    "use real sentences, not stubs"
                )
        return self
