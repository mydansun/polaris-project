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
    workspace_root: str = Field(
        default=str(Path(__file__).resolve().parents[4] / ".data" / "workspaces"),
        validation_alias="POLARIS_WORKSPACE_ROOT",
    )
    workspace_meta_root: str = Field(
        default=str(Path(__file__).resolve().parents[4] / ".data" / "workspace-meta"),
        validation_alias="POLARIS_WORKSPACE_META_ROOT",
    )
    template_root: str = Field(
        default=str(Path(__file__).resolve().parents[4] / "templates" / "default-stack"),
        validation_alias="POLARIS_TEMPLATE_ROOT",
    )
    host_codex_auth_path: str = Field(
        # Host-side path to the Codex auth.json.  Each workspace container
        # bind-mounts this rw to /home/workspace/.codex/auth.json so codex
        # app-server inside uses the same OpenAI credential as host `codex`.
        default_factory=lambda: str(Path.home() / ".codex" / "auth.json"),
        validation_alias="POLARIS_HOST_CODEX_AUTH_PATH",
    )
    ide_public_url_template: str = Field(
        default="https://ide-{workspaceHash}.polaris-dev.xyz",
        validation_alias="POLARIS_IDE_PUBLIC_URL_TEMPLATE",
    )
    workspace_image: str = Field(
        default="polaris/workspace:latest",
        validation_alias="POLARIS_WORKSPACE_IMAGE",
    )
    browser_public_url_template: str = Field(
        default="https://browser-{workspaceHash}.polaris-dev.xyz",
        validation_alias="POLARIS_BROWSER_PUBLIC_URL_TEMPLATE",
    )
    browser_image: str = Field(
        default="polaris/chromium-vnc:latest",
        validation_alias="POLARIS_BROWSER_IMAGE",
    )
    postgres_image: str = Field(
        default="postgres:16-alpine",
        validation_alias="POLARIS_POSTGRES_IMAGE",
    )
    redis_image: str = Field(
        default="redis:7-alpine",
        validation_alias="POLARIS_REDIS_IMAGE",
    )
    browser_session_ttl_minutes: int = Field(
        default=120,
        validation_alias="POLARIS_BROWSER_SESSION_TTL_MINUTES",
    )
    postmark_server_token: str = Field(
        default="",
        validation_alias="POSTMARK_SERVER_TOKEN",
    )
    postmark_message_stream: str = Field(
        default="outbound",
        validation_alias="POSTMARK_MESSAGE_STREAM",
    )
    postmark_from_email: str = Field(
        default="noreply@polaris.dev",
        validation_alias="POSTMARK_FROM_EMAIL",
    )
    invite_code: str = Field(
        default="",
        validation_alias="POLARIS_INVITE_CODE",
    )
    session_secret: str = Field(
        default="polaris-dev-secret-change-me",
        validation_alias="SESSION_SECRET",
    )
    session_ttl_days: int = Field(
        default=7,
        validation_alias="SESSION_TTL_DAYS",
    )
    frontend_url: str = Field(
        default="http://localhost:5173",
        validation_alias="FRONTEND_URL",
    )
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"],
        validation_alias="POLARIS_CORS_ORIGINS",
    )
    # Dev-login shortcut (`GET /auth/dev-login`) is gated behind these two
    # being set.  Empty `dev_user_email` disables the route (404) AND the
    # frontend's "Dev Login" button (via `GET /auth/config`).  Recommended:
    # set only in local dev `.env`; leave empty in every shared / staging
    # / prod environment so the endpoint cannot be abused.
    dev_user_email: str = Field(
        default="",
        validation_alias="POLARIS_DEV_USER_EMAIL",
    )
    dev_user_name: str = Field(
        default="",
        validation_alias="POLARIS_DEV_USER_NAME",
    )
    # Domain base for dev-plane IDE / browser subdomains (ide-<hash>.<domain>).
    # The root `polaris-dev.xyz` origin routing lives in infra/traefik/dynamic/.
    domain: str = Field(
        default="polaris-dev.xyz",
        validation_alias="POLARIS_DOMAIN",
    )
    # Shared external docker network that traefik watches via docker provider.
    # Per-workspace compose (dev) and per-project publish compose (prod) both
    # join it so their traefik.* labels get picked up.
    traefik_public_network_name: str = Field(
        default="traefik-public",
        validation_alias="POLARIS_TRAEFIK_PUBLIC_NETWORK",
    )
    # Domain under which published user projects are served, each at a
    # <uuid>.<prod_domain_base> subdomain resolving to traefik.  The wildcard
    # cert `*.prod.polaris-dev.xyz` must cover this.
    prod_domain_base: str = Field(
        default="prod.polaris-dev.xyz",
        validation_alias="POLARIS_PROD_DOMAIN_BASE",
    )
    # Host-side directory where each published project's durable state
    # lives: source archives per version, secrets.env, compose overrides.
    # Defaults to <repo-root>/.data/projects — all runtime state lives
    # inside the project dir so the entire project is self-contained.
    # Override with POLARIS_PUBLISH_PROJECTS_ROOT for a prod host (e.g.
    # /srv/polaris-projects — sudo-create + chown first).
    publish_projects_root: str = Field(
        default=str(Path(__file__).resolve().parents[4] / ".data" / "projects"),
        validation_alias="POLARIS_PUBLISH_PROJECTS_ROOT",
    )
    # Local docker registry used to shuttle built images from the platform
    # build worker to the "prod" side (same host, for now).  Matches the
    # `registry` service in docker-compose.infra.yaml.
    registry_url: str = Field(
        default="127.0.0.1:5000",
        validation_alias="POLARIS_REGISTRY_URL",
    )
    # Total wall-clock budget for the `docker build` phase of a publish.
    publish_build_timeout_seconds: int = Field(
        default=900,
        validation_alias="POLARIS_PUBLISH_BUILD_TIMEOUT",
    )
    # How long to wait for a smoke-test HTTP 2xx before giving up.
    publish_smoke_timeout_seconds: int = Field(
        default=60,
        validation_alias="POLARIS_PUBLISH_SMOKE_TIMEOUT",
    )
    # URL the platform API exposes to the workspace container so the in-
    # container `polaris` CLI can call back.  Reached via the host-gateway
    # extra_host injected into the workspace compose.
    api_url_for_workspace: str = Field(
        default="http://host.docker.internal:8000",
        validation_alias="POLARIS_API_URL_FOR_WORKSPACE",
    )
    # Repo-root dir that holds per-stack publish scaffolds
    # (Dockerfile / compose.prod.yml / polaris.yaml templates).  Read by
    # publish.py's auto-scaffold fallback when the user clicks Publish
    # without having run `polaris scaffold-publish` inside the workspace.
    publish_templates_root: str = Field(
        default=str(
            Path(__file__).resolve().parents[4] / "infra" / "publish-templates"
        ),
        validation_alias="POLARIS_PUBLISH_TEMPLATES_ROOT",
    )

    # ── S3 / MinIO (image re-hosting target for the Unsplash MCP) ────────
    # Credentials stay platform-side; the workspace MCP never sees them.
    s3_access_key_id: str = Field(default="", validation_alias="S3_ACCESS_KEY_ID")
    s3_secret_access_key: str = Field(default="", validation_alias="S3_SECRET_ACCESS_KEY")
    s3_endpoint: str = Field(default="", validation_alias="S3_ENDPOINT")
    s3_bucket: str = Field(default="polaris", validation_alias="S3_BUCKET")
    # Base URL for anonymous reads of the `static/*` key prefix — object
    # URLs are built as ``{S3_URL_BASE}/{s3_key}``.
    s3_url_base: str = Field(default="", validation_alias="S3_URL_BASE")

    # ── Unsplash (server-side only) ───────────────────────────────────────
    unsplash_access_key: str = Field(
        default="",
        validation_alias="UNSPLASH_ACCESS_KEY",
    )

    # ── Prepublish-audit LLM (optional --deep review) ─────────────────────
    # Cheap-ish default — audit is a text-in/text-out task where the mini
    # model is usually sufficient.  Ops can bump to a flagship via env if
    # quality proves thin.
    openai_secret: str = Field(default="", validation_alias="OPENAI_SECRET")
    audit_model: str = Field(
        default="gpt-5.4-mini", validation_alias="POLARIS_AUDIT_MODEL"
    )

    # ── Run concurrency quota (Redis sorted-set tokens) ────────────────────
    # `max_global_runs` caps total in-flight Sessions across the platform;
    # `max_user_runs` caps them per user.  Acquire happens synchronously in
    # POST /projects/{id}/sessions; release in the worker's orchestrator
    # finally block.  `run_quota_ttl_seconds` is a crash-recovery backstop —
    # an entry's sorted-set score is (now + TTL), and the next acquire's
    # Lua script ZREMRANGEBYSCOREs the expired ones before counting.  TTL
    # should comfortably exceed the worst-case session wall-clock.
    max_global_runs: int = Field(
        default=6, validation_alias="POLARIS_MAX_GLOBAL_RUNS"
    )
    max_user_runs: int = Field(
        default=2, validation_alias="POLARIS_MAX_USER_RUNS"
    )
    run_quota_ttl_seconds: int = Field(
        default=1800, validation_alias="POLARIS_RUN_QUOTA_TTL_SECONDS"
    )

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parents[4] / ".env"),
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
