"""Thin async S3 client helper — used by any service that wants to
upload user-visible assets to our MinIO bucket.

Design choices:
  * One module-level :class:`aioboto3.Session` — aioboto3 requires a
    per-task ``client(...)`` context manager anyway, so sharing the
    Session is safe and reuses credentials loading.
  * Upload helper always sets ``ACL=public-read`` because the only
    caller today writes into the ``static/*`` prefix, which is
    configured for anonymous reads on MinIO.  If you later add other
    prefixes tighten this up.
  * ``public_url`` builds ``{S3_URL_BASE}/{key}`` — the `S3_URL_BASE`
    env (e.g. ``https://polaris.s3.polaris-dev.xyz``) already points at
    the anonymously-served bucket root.
"""

from __future__ import annotations

import logging
from functools import lru_cache

import aioboto3

from polaris_api.config import Settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _session() -> aioboto3.Session:
    return aioboto3.Session()


async def upload_bytes(
    *,
    key: str,
    data: bytes,
    content_type: str,
    settings: Settings,
) -> None:
    """Put ``data`` at ``key`` in the configured bucket.  Raises on failure."""
    session = _session()
    async with session.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
    ) as client:
        await client.put_object(
            Bucket=settings.s3_bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            ACL="public-read",
        )
    logger.info(
        "s3: uploaded %s (%d bytes, %s)",
        key,
        len(data),
        content_type,
    )


def public_url(*, key: str, settings: Settings) -> str:
    return f"{settings.s3_url_base.rstrip('/')}/{key.lstrip('/')}"
