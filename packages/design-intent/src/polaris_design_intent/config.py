from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Env-sourced settings for the design-intent LangGraph pipeline.

    Reads `.env` from the monorepo root so standalone callers (tests,
    ad-hoc scripts) and the worker process both see the same values.
    Upstream note — we used to instantiate a plain class with
    ``os.environ.get(...)``, which silently fell back to defaults when
    the worker was launched under a supervisor that didn't export
    `.env` to the shell.  `BaseSettings` closes that gap.

    Model split by role:
      * compiler — quality-critical multimodal step producing the
        final design brief; flagship model.
      * clarifier — orchestrates 3 rounds of tool calls against a long
        conversation.  Flagship by default: the mini variant was
        observed emitting `emit_design_intent` with an empty `intent`
        object under load (tool_choice="any" forces *a* tool call but
        not a populated one), burning tokens in the ROUTE_LOOP.
      * review — structured grading call on a small JSON blob; stays on
        -mini where cost matters more than nuance.
    Per-role env vars let you override any of these.
    """

    openai_api_key: str = Field(default="", validation_alias="OPENAI_SECRET")

    compiler_model: str = Field(
        default="gpt-5.4",
        validation_alias="POLARIS_DESIGN_INTENT_COMPILER_MODEL",
    )
    clarifier_model: str = Field(
        default="gpt-5.4",
        validation_alias="POLARIS_DESIGN_INTENT_CLARIFIER_MODEL",
    )
    review_model: str = Field(
        default="gpt-5.4-mini",
        validation_alias="POLARIS_DESIGN_INTENT_REVIEW_MODEL",
    )
    scorer_model: str = Field(
        default="gpt-5.4-mini",
        validation_alias="POLARIS_DESIGN_INTENT_SCORER_MODEL",
    )

    # Mood board generator: one images.edit call per discovery run.
    # gpt-image-1 is the current flagship image gen; landscape 1536x1024
    # lines up with typical web hero proportions.
    mood_board_image_model: str = Field(
        default="gpt-image-1",
        validation_alias="POLARIS_DESIGN_INTENT_MOOD_BOARD_IMAGE_MODEL",
    )
    mood_board_image_size: str = Field(
        default="1536x1024",
        validation_alias="POLARIS_DESIGN_INTENT_MOOD_BOARD_SIZE",
    )

    pinterest_base_url: str = Field(
        default="http://polaris-dev.xyz:9801",
        validation_alias="POLARIS_PINTEREST_TOOL_BASE",
    )
    max_rounds: int = Field(
        default=3, validation_alias="POLARIS_DESIGN_INTENT_MAX_ROUNDS"
    )
    pinterest_hops: int = Field(
        default=1, validation_alias="POLARIS_DESIGN_INTENT_PINTEREST_HOPS"
    )
    # Downed from 12 → 6 for the batched scorer: multimodal token cost
    # scales with image count, and 6 candidates is enough to consistently
    # surface a match score >= 4 without blowing budget.
    max_refs: int = Field(
        default=6, validation_alias="POLARIS_DESIGN_INTENT_MAX_REFS"
    )
    # Pipeline now forces a single image into the compiler (the best-scored
    # one).  This env var remains as an upper bound; effectively capped at 1
    # downstream.  Kept for future multi-image compile experiments.
    max_images_to_compiler: int = Field(
        default=6, validation_alias="POLARIS_DESIGN_INTENT_MAX_IMAGES_TO_COMPILER"
    )
    image_score_threshold: float = Field(
        default=4.0, validation_alias="POLARIS_DESIGN_INTENT_IMAGE_SCORE_THRESHOLD"
    )
    # Must match api/worker defaults so a bare clone-and-run picks up the
    # same dev Postgres that docker-compose.infra.yaml spins up.
    database_url: str = Field(
        default="postgresql+asyncpg://root:123456@127.0.0.1:5432/polaris",
        validation_alias="POLARIS_DATABASE_URL",
    )

    model_config = SettingsConfigDict(
        # packages/design-intent/src/polaris_design_intent/config.py
        #   parents[0] polaris_design_intent/
        #   parents[1] src/
        #   parents[2] design-intent/
        #   parents[3] packages/
        #   parents[4] <repo root>
        env_file=str(Path(__file__).resolve().parents[4] / ".env"),
        extra="ignore",
    )


def get_settings() -> Settings:
    return Settings()
