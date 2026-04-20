from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class PinterestClient:
    """Thin async wrapper around the Pinterest-scraper HTTP service.

    The service contract is:
      POST /query {"query": str, "hops": 1|2|3}
        -> {"query": str, "hops": int, "count": int,
            "results": [{"id": str, "title": str, "max": url, "normal": url}]}
      GET  /health -> {"ok": bool, "last_check_at": str}

    Separately, `max` / `normal` URLs are public images served out of S3 (MinIO);
    `download_image` does a plain httpx GET.

    The client owns its own httpx.AsyncClient per instance — callers either use
    it as an async context manager or invoke `aclose()` themselves.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> "PinterestClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def query(self, query: str, *, hops: int = 1) -> list[dict[str, Any]]:
        """POST /query and return `results` list.  Raises on non-2xx."""
        url = f"{self._base_url}/query"
        resp = await self._client.post(url, json={"query": query, "hops": hops})
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", []) or []

    async def download_image(self, url: str) -> tuple[bytes, str]:
        """Fetch raw image bytes + content-type.  Timeouts and network errors
        propagate — callers decide whether to swallow them."""
        resp = await self._client.get(url)
        resp.raise_for_status()
        mime = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        return resp.content, mime

    async def health(self) -> dict[str, Any]:
        resp = await self._client.get(f"{self._base_url}/health")
        resp.raise_for_status()
        return resp.json()
