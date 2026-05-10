"""Capture a screenshot of a freshly-published site and stash it on S3.

Called by the publish pipeline right after smoke probes succeed — at
that point the prod URL is serving real content (and the cert chain
through traefik is already valid).  We render headless, upload, and
write the public URL onto the deployment row.

Design notes
------------
* Headless Chromium is invoked via subprocess rather than a Python
  Playwright wrapper.  The api container ships ``chromium`` from
  ``apt`` (~150MB) — adding the ``playwright`` PyPI package would
  also bring in its own copy of chromium and roughly double that.
  ``chromium --headless=new --screenshot=...`` is exactly what we
  need.

* ``--no-sandbox`` is required because the api process runs as root
  inside the container and Chromium's user-namespace sandbox conflicts
  with that.  This is fine for our use case: we're navigating to a
  URL we just deployed — same trust boundary as everything else the
  api does.

* The whole step is **best-effort** — a screenshot failure must not
  fail the publish.  Caller wraps the call in try/except.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import tempfile
import time
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from polaris_api.config import Settings
from polaris_api.models import Deployment
from polaris_api.services import s3 as s3_mod


logger = logging.getLogger(__name__)


# Screenshots land at this S3 prefix.  Mirrors the layout used by the
# Unsplash MCP (static/images/...) so an existing public-read bucket
# policy on `static/*` covers us.
S3_KEY_TEMPLATE = "static/images/deployments/{deployment_id}.png"

# Viewport: 1280×720 matches the seed-time backfill we did via the
# Playwright MCP, and is HomePage-card friendly (3:2-ish crop fits the
# card aspect ratio without big letterboxing).
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 720

# Chromium's per-page wall-clock budget.  Render-and-shoot for a
# typical landing page completes well under this; longer-running JS
# animations / network stalls are abandoned (still produces a usable
# above-the-fold shot).
CHROMIUM_TIMEOUT_SECONDS = 25.0

# Wait this many ms after page load before screenshotting.  Lets web-
# fonts swap in and lazy-loaded hero images settle.  4s was too short
# for Next.js + Prisma pages whose hero images come from a remote S3
# bucket — capture fired before the optimized ``next/image`` requests
# finished and the screenshot was a near-blank cream skeleton.  10s
# covers typical landing pages without making simple ones noticeably
# slower (chromium exits when virtual time elapses).
VIRTUAL_TIME_BUDGET_MS = 10_000

# Pre-screenshot HTTP probe: poll the public URL until it returns 200
# (after following redirects).  Catches the brief window between
# `dep.status = ready` (we set this BEFORE traefik has noticed the new
# container) and the public URL actually serving content.  Saves the
# downstream chromium launch + 25s timeout when the probe never
# resolves.
PROBE_TIMEOUT_SECONDS = 30.0
PROBE_INTERVAL_SECONDS = 2.0
PROBE_REQUEST_TIMEOUT_SECONDS = 5.0


def _chromium_command(url: str, output: Path) -> list[str]:
    """Build the chromium CLI invocation.  Pulled out so unit tests
    can assert the exact flags without spinning the binary."""
    return [
        "chromium",
        "--headless=new",
        # Container runs as root → user-namespace sandbox doesn't apply.
        "--no-sandbox",
        # Headless doesn't have a GPU; skip the dance of trying.
        "--disable-gpu",
        # Network sandbox: harmless to drop; we trust the URL we just
        # smoke-tested.
        "--disable-dev-shm-usage",
        f"--window-size={VIEWPORT_WIDTH},{VIEWPORT_HEIGHT}",
        # `virtual-time-budget` advances the page clock until ${ms}
        # have passed; combined with `--run-all-compositor-stages-
        # before-draw` it deterministically waits for fonts + lazy
        # content before snapshotting.
        f"--virtual-time-budget={VIRTUAL_TIME_BUDGET_MS}",
        "--run-all-compositor-stages-before-draw",
        # Hide the noisy chrome / scrollbars from the capture.
        "--hide-scrollbars",
        f"--screenshot={output}",
        url,
    ]


async def _run_chromium(url: str, output: Path) -> None:
    """Spawn chromium; raise on non-zero exit OR timeout OR missing
    output file (chromium sometimes succeeds-then-aborts before flush)."""
    cmd = _chromium_command(url, output)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=CHROMIUM_TIMEOUT_SECONDS)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"chromium screenshot of {url!r} timed out") from None
    if proc.returncode != 0:
        raise RuntimeError(
            f"chromium exit {proc.returncode} for {url!r}"
        )
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError(f"chromium produced no file at {output}")


async def _wait_until_ready(
    url: str,
    *,
    timeout_s: float = PROBE_TIMEOUT_SECONDS,
    interval_s: float = PROBE_INTERVAL_SECONDS,
    request_timeout_s: float = PROBE_REQUEST_TIMEOUT_SECONDS,
) -> bool:
    """Poll ``url`` until it returns HTTP 200 (after following redirects)
    or ``timeout_s`` elapses.  Returns True on first 200, False on
    timeout.  Connection errors / TLS issues / non-200 statuses all
    treated as "not ready yet" and retried until the timeout.

    ``verify=False`` because freshly-issued LE certs may have OCSP / CT
    sync lag; chromium will see the cert as valid (browsers accept LE)
    and we trust the URL we just deployed regardless.  ``follow_redirects``
    so apps that redirect ``/`` → ``/home`` are detected by their final
    page status, matching what chromium would render.
    """
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(
        verify=False,
        follow_redirects=True,
        timeout=request_timeout_s,
    ) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return True
            except httpx.HTTPError:
                # Connection refused / DNS / TLS still being set up —
                # keep polling until the deadline.
                pass
            await asyncio.sleep(interval_s)
    return False


async def capture_and_record(
    *,
    deployment_id: UUID,
    url: str,
    db: AsyncSession,
    settings: Settings,
) -> str | None:
    """Snapshot ``url``, upload to S3, write ``screenshot_url`` onto
    the deployment row.  Returns the public URL on success or ``None``
    if any step failed — caller logs and moves on.

    Best-effort: every exception is swallowed and logged.  The publish
    pipeline must NOT fail because the screenshot step did.

    Probes the URL with httpx until it returns 200 before launching
    chromium.  This avoids a 25s wasted chromium timeout when the URL
    isn't actually serving (cert not yet propagated, traefik hasn't
    picked up the new container, etc.) and lets the fallback chain
    (mood_board_url → placeholder card on the frontend) kick in faster.
    """
    if not await _wait_until_ready(url):
        logger.info(
            "publish: %s never returned 200 within %.0fs; skipping screenshot "
            "(frontend will fall back to mood board / placeholder)",
            url, PROBE_TIMEOUT_SECONDS,
        )
        return None

    key = S3_KEY_TEMPLATE.format(deployment_id=str(deployment_id))
    # tempfile in /tmp — chromium writes here, we read+upload, delete.
    # secrets.token_hex avoids collisions when two publishes overlap.
    tmp_path = Path(tempfile.gettempdir()) / f"polaris-shot-{secrets.token_hex(4)}.png"
    try:
        await _run_chromium(url, tmp_path)
        data = tmp_path.read_bytes()
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("publish: screenshot capture failed for %s: %s", url, exc)
        return None
    finally:
        tmp_path.unlink(missing_ok=True)

    try:
        await s3_mod.upload_bytes(
            key=key,
            data=data,
            content_type="image/png",
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("publish: screenshot upload failed for %s: %s", url, exc)
        return None

    public_url = s3_mod.public_url(key=key, settings=settings)
    try:
        await db.execute(
            update(Deployment)
            .where(Deployment.id == deployment_id)
            .values(screenshot_url=public_url)
        )
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("publish: screenshot DB write failed for %s: %s", url, exc)
        return None

    logger.info(
        "publish: screenshot captured for deployment %s (%d bytes) → %s",
        deployment_id, len(data), public_url,
    )
    return public_url
