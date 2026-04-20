from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = Field(
        default="postgresql+asyncpg://root:123456@127.0.0.1:5432/polaris",
        validation_alias="POLARIS_DATABASE_URL",
    )
    redis_url: str = Field(
        default="redis://127.0.0.1:6379/0",
        validation_alias="POLARIS_REDIS_URL",
    )
    consumer_group: str = Field(
        default="polaris-workers",
        validation_alias="POLARIS_WORKER_CONSUMER_GROUP",
    )
    consumer_name: str = Field(
        default="worker-1",
        validation_alias="POLARIS_WORKER_CONSUMER_NAME",
    )
    codex_model: str = Field(
        validation_alias="POLARIS_CODEX_MODEL",
    )
    # Second LLM pass that rewrites each Codex plan message into a
    # non-technical user-friendly version.  Runs on ``on_item_completed``
    # for codex:plan events; a 1–2s blocking call per plan.
    codex_plan_plain_model: str = Field(
        # Flagship by default — the mini variant still leaked framework
        # names, CSS tokens, type definitions, and font specifics into the
        # "plain" version.  Plain-language rewriting is a taste judgment
        # task where the extra parameters pay off.
        default="gpt-5.4",
        validation_alias="POLARIS_CODEX_PLAN_PLAIN_MODEL",
    )
    openai_api_key: str = Field(
        default="",
        validation_alias="OPENAI_SECRET",
    )
    # Total wall-clock cap on one turn.  Generous on purpose — complex
    # scaffold + validate turns legitimately take minutes.  Liveness is
    # detected via WebSocket ping/pong, not an idle timer.
    codex_turn_timeout_seconds: float = Field(
        default=900,
        validation_alias="POLARIS_CODEX_TURN_TIMEOUT_SECONDS",
    )
    codex_liveness_check_interval_seconds: float = Field(
        default=30,
        validation_alias="POLARIS_CODEX_LIVENESS_CHECK_INTERVAL_SECONDS",
    )
    # Background scavenger: workspaces with no turn activity for this
    # long have their compose runtime brought down.  User code, codex
    # sessions, and dependency-service volumes all persist across the
    # stop — reopening the project re-creates the containers.  Set to
    # 0 to disable the scavenger entirely.
    idle_workspace_timeout_seconds: float = Field(
        default=3600,
        validation_alias="POLARIS_IDLE_WORKSPACE_TIMEOUT_SECONDS",
    )
    idle_workspace_scan_interval_seconds: float = Field(
        default=300,
        validation_alias="POLARIS_IDLE_WORKSPACE_SCAN_INTERVAL_SECONDS",
    )
    codex_approval_policy: str = Field(
        # Codex runs inside the workspace container = our real sandbox, so
        # auto-accept every tool call.  The container boundary + per-tenant
        # auth.json is the trust model.
        default="never",
        validation_alias="POLARIS_CODEX_APPROVAL_POLICY",
    )

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parents[4] / ".env"),
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def asyncpg_url(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
