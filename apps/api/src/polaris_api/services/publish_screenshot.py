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
from pathlib import Path
from uuid import UUID

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
# fonts swap in and lazy-loaded hero images settle.  Tuned conservative.
VIRTUAL_TIME_BUDGET_MS = 4_000


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
    """
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
