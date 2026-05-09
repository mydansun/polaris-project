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
    # Total wall-clock cap on one turn.  Now an absolute cost-cap
    # ceiling rather than the active gate — the inactivity timeout
    # below catches stuck turns first, so this only fires for runaway
    # loops that DO keep producing notifications for hours.
    codex_turn_timeout_seconds: float = Field(
        default=7200,
        validation_alias="POLARIS_CODEX_TURN_TIMEOUT_SECONDS",
    )
    # The "real" turn timeout: kill if no notification (reasoning step,
    # command execution, tool call, etc.) arrives for this long.  A
    # productive turn never trips it; a stuck turn (hung syscall,
    # internal loop, unresponsive model) goes silent and gets killed.
    codex_inactivity_timeout_seconds: float = Field(
        default=600,
        validation_alias="POLARIS_CODEX_INACTIVITY_TIMEOUT_SECONDS",
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
    # Maximum number of Sessions this worker process runs concurrently.
    # Mirrors the API-side ``max_global_runs`` quota knob (same env var) so
    # the worker's actual fan-out matches the cap the API enforces at
    # ``acquire_run_slot``.  Each in-flight Session holds one asyncpg pool
    # connection for its lifetime — pool size is sized off this value.
    max_global_runs: int = Field(
        default=6,
        validation_alias="POLARIS_MAX_GLOBAL_RUNS",
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
