"""Pinterest node should tolerate individual query / image failures."""

from __future__ import annotations

import httpx
import pytest
import respx

from polaris_design_intent.nodes.pinterest import pinterest_node


@pytest.mark.integration
@pytest.mark.asyncio
async def test_one_query_500_others_succeed(settings):
    settings.max_images_to_compiler = 0  # skip image downloads for this test
    with respx.mock(base_url="http://pinterest.test") as router:
        def handler(request):
            import json as _j

            body = _j.loads(request.content or b"{}")
            if body.get("query") == "bad":
                return httpx.Response(500)
            return httpx.Response(
                200,
                json={
                    "query": body.get("query"),
                    "hops": 1,
                    "count": 1,
                    "results": [
                        {"id": f"p-{body['query']}", "title": "t",
                         "max": "m", "normal": "n"},
                    ],
                },
            )

        router.post("/query").mock(side_effect=handler)

        state = {"pinterest_queries": ["good", "bad", "another-good"]}
        result = await pinterest_node(state, settings)

    ids = [r["id"] for r in result["pinterest_refs"]]
    assert ids == ["p-good", "p-another-good"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_image_download_failure_keeps_url_only(settings):
    settings.max_images_to_compiler = 2
    with respx.mock() as router:
        router.post("http://pinterest.test/query").mock(
            return_value=httpx.Response(
                200,
                json={
                    "query": "q",
                    "hops": 1,
                    "count": 2,
                    "results": [
                        {"id": "p1", "title": "t1",
                         "max": "https://example.com/a.jpg",
                         "normal": "https://example.com/a-n.jpg"},
                        {"id": "p2", "title": "t2",
                         "max": "https://example.com/b.jpg",
                         "normal": "https://example.com/b-n.jpg"},
                    ],
                },
            )
        )
        # First image fetch 500s, second is a tiny fake PNG.
        router.get("https://example.com/a.jpg").mock(return_value=httpx.Response(500))
        router.get("https://example.com/b.jpg").mock(
            return_value=httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})
        )

        state = {"pinterest_queries": ["q"]}
        result = await pinterest_node(state, settings)

    refs = result["pinterest_refs"]
    assert refs[0]["image_b64"] is None  # failed fetch — URL preserved
    assert refs[1]["image_b64"] is not None  # succeeded
