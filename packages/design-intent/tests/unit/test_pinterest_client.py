import json

import httpx
import pytest
import respx

from polaris_design_intent.tools.pinterest_client import PinterestClient


@pytest.mark.asyncio
async def test_query_posts_with_correct_body():
    with respx.mock(base_url="http://pinterest.test") as router:
        route = router.post("/query").mock(
            return_value=httpx.Response(
                200,
                json={
                    "query": "real estate landing page white",
                    "hops": 1,
                    "count": 1,
                    "results": [
                        {"id": "p1", "title": "t", "max": "m", "normal": "n"},
                    ],
                },
            )
        )
        async with PinterestClient("http://pinterest.test") as client:
            results = await client.query("real estate landing page white", hops=1)
    assert route.called
    body = json.loads(route.calls[0].request.content)
    assert body == {"query": "real estate landing page white", "hops": 1}
    assert results == [{"id": "p1", "title": "t", "max": "m", "normal": "n"}]


@pytest.mark.asyncio
async def test_query_returns_empty_on_missing_results_key():
    with respx.mock(base_url="http://pinterest.test") as router:
        router.post("/query").mock(return_value=httpx.Response(200, json={"query": "x"}))
        async with PinterestClient("http://pinterest.test") as client:
            results = await client.query("x")
    assert results == []


@pytest.mark.asyncio
async def test_download_image_returns_bytes_and_mime():
    png = b"\x89PNG\r\n\x1a\n"
    with respx.mock() as router:
        router.get("https://example.com/img.png").mock(
            return_value=httpx.Response(
                200, content=png, headers={"content-type": "image/png; charset=binary"}
            )
        )
        async with PinterestClient("http://pinterest.test") as client:
            data, mime = await client.download_image("https://example.com/img.png")
    assert data == png
    assert mime == "image/png"


@pytest.mark.asyncio
async def test_query_raises_on_5xx():
    with respx.mock(base_url="http://pinterest.test") as router:
        router.post("/query").mock(return_value=httpx.Response(500))
        async with PinterestClient("http://pinterest.test") as client:
            with pytest.raises(httpx.HTTPStatusError):
                await client.query("x")
