"""Prepublish audit — LLM-assisted deep review of the user's publish
configuration.

Called by the workspace CLI's ``polaris prepublish-audit --deep``.  The
CLI uploads the user's polaris.yaml + Dockerfile + package.json scripts;
we hand them to a cheap-ish model with a tight prompt and return any
concrete runtime failures the LLM predicts.  Static rules in the CLI
cover the common traps (bare node bins) — this catches semantic
mismatches that regex can't (port disagreement, missing scripts,
non-idempotent migrations).

Auth accepts the same two paths as ``routes/deploy.py``: session cookie
OR X-Polaris-Workspace-Token, both resolve to the owning Project.
"""
from __future__ import annotations

import logging
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from polaris_api.config import Settings, get_settings
from polaris_api.db import get_session
from polaris_api.routes.deploy import _resolve_project_access
from polaris_api.schemas import AuditIssue, AuditRequest, AuditResponse
from polaris_api.services.audit_prompt import (
    AUDIT_SYSTEM_PROMPT,
    clamp,
    format_audit_inputs,
    parse_audit_response,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["audit"])


_OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"


async def _call_openai_chat(
    *, model: str, api_key: str, system: str, user: str, timeout: float = 30.0
) -> str:
    """Direct REST call to OpenAI chat completions.  Kept minimal so the
    api package doesn't need the full ``langchain-openai`` stack (which
    lives in the worker / design-intent deps, not here)."""
    payload = {
        "model": model,
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(_OPENAI_CHAT_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        return ""
    return str((choices[0].get("message") or {}).get("content") or "")


@router.post(
    "/projects/{project_id}/prepublish-audit",
    response_model=AuditResponse,
)
async def prepublish_audit(
    project_id: UUID,
    payload: AuditRequest,
    request: Request,
    x_polaris_workspace_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AuditResponse:
    await _resolve_project_access(
        request, project_id, session, settings, x_polaris_workspace_token
    )

    # Clamp inputs before handing to the model — dev deps on a large
    # project can push package.json past several hundred KB, and
    # Dockerfiles can be long.
    polaris_yaml = clamp(payload.polaris_yaml)
    dockerfile = clamp(payload.dockerfile)
    scripts = payload.package_json_scripts or {}

    if not settings.openai_secret:
        logger.warning("prepublish-audit requested but OPENAI_SECRET not set")
        return AuditResponse(issues=[])

    user_msg = format_audit_inputs(polaris_yaml, dockerfile, scripts)

    try:
        raw = await _call_openai_chat(
            model=settings.audit_model,
            api_key=settings.openai_secret,
            system=AUDIT_SYSTEM_PROMPT,
            user=user_msg,
        )
    except Exception:  # noqa: BLE001
        logger.warning("prepublish-audit LLM call failed", exc_info=True)
        return AuditResponse(issues=[])

    issues = [AuditIssue(**item) for item in parse_audit_response(raw)]
    logger.info(
        "prepublish-audit: project=%s model=%s issues=%d (errors=%d)",
        project_id,
        settings.audit_model,
        len(issues),
        sum(1 for it in issues if it.severity == "error"),
    )
    return AuditResponse(issues=issues)
