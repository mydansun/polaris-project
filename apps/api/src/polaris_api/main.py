import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis
from sqlalchemy import text

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

from polaris_api import __version__  # noqa: E402
from polaris_api.config import get_settings  # noqa: E402
from polaris_api.db import SessionLocal  # noqa: E402
from polaris_api.redis_client import get_redis  # noqa: E402
from polaris_api.routes.audit import router as audit_router  # noqa: E402
from polaris_api.routes.auth import router as auth_router  # noqa: E402
from polaris_api.routes.browsers import router as browsers_router  # noqa: E402
from polaris_api.routes.clarify import router as clarify_router  # noqa: E402
from polaris_api.routes.deploy import router as deploy_router  # noqa: E402
from polaris_api.routes.dev_deps import router as dev_deps_router  # noqa: E402
from polaris_api.routes.projects import router as projects_router  # noqa: E402
from polaris_api.routes.sessions import router as sessions_router  # noqa: E402
from polaris_api.routes.unsplash import router as unsplash_router  # noqa: E402
from polaris_api.routes.workspaces import router as workspaces_router  # noqa: E402
from polaris_api.mcp_app import build_mcp_app  # noqa: E402
from polaris_api.schemas import HealthResponse, ReadyResponse  # noqa: E402


mcp_asgi = build_mcp_app()

# FastMCP's Starlette app ships a lifespan that manages the transport;
# FastAPI must inherit it or the MCP session layer never initializes.
app = FastAPI(title="Polaris API", version=__version__, lifespan=mcp_asgi.lifespan)
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router)
app.include_router(projects_router)
app.include_router(sessions_router)
app.include_router(workspaces_router)
app.include_router(browsers_router)
app.include_router(deploy_router)
app.include_router(dev_deps_router)
app.include_router(clarify_router)
app.include_router(unsplash_router)
app.include_router(audit_router)
# MCP over streamable HTTP.  Codex connects here via `mcp add --url
# $POLARIS_API_URL/mcp --bearer-token-env-var POLARIS_WORKSPACE_TOKEN`.
app.mount("/mcp", mcp_asgi)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(service="polaris-api", version=__version__, status="ok")


@app.get("/ready", response_model=ReadyResponse)
async def ready() -> ReadyResponse:
    async with SessionLocal() as session:
        await session.execute(text("select 1"))

    redis: Redis = get_redis()
    try:
        await redis.ping()
    finally:
        await redis.aclose()

    return ReadyResponse(service="polaris-api", database="ok", redis="ok")
