"""Tests for the Iconify service proxy.

Strategy: respx mocks `api.iconify.design` — the service has no DB or
S3 side-effects so tests are just request-shape + response-mapping
assertions."""

from __future__ import annotations

import httpx
import pytest
import respx

from polaris_api.services import iconify


@pytest.mark.asyncio
async def test_list_collections_passthrough():
    with respx.mock() as router:
        route = router.get("https://api.iconify.design/collections").mock(
            return_value=httpx.Response(200, json={"mdi": {"name": "Material Design Icons"}})
        )
        out = await iconify.list_collections()
    assert route.called
    assert out == {"mdi": {"name": "Material Design Icons"}}


@pytest.mark.asyncio
async def test_get_collection_sends_prefix_and_info():
    with respx.mock() as router:
        route = router.get("https://api.iconify.design/collection").mock(
            return_value=httpx.Response(200, json={"prefix": "mdi", "total": 7000})
        )
        out = await iconify.get_collection("mdi")
    assert route.called
    # Both ``prefix=mdi`` and ``info=true`` must be on the wire.
    sent_url = str(route.calls[0].request.url)
    assert "prefix=mdi" in sent_url
    assert "info=true" in sent_url
    assert out["total"] == 7000


@pytest.mark.asyncio
async def test_search_clamps_limit_below_minimum():
    with respx.mock() as router:
        route = router.get("https://api.iconify.design/search").mock(
            return_value=httpx.Response(200, json={"icons": []})
        )
        await iconify.search(query="home", limit=5)
    sent_url = str(route.calls[0].request.url)
    # 5 → 32 (Iconify's min)
    assert "limit=32" in sent_url
    assert "query=home" in sent_url


@pytest.mark.asyncio
async def test_search_clamps_limit_above_maximum():
    with respx.mock() as router:
        route = router.get("https://api.iconify.design/search").mock(
            return_value=httpx.Response(200, json={"icons": []})
        )
        await iconify.search(query="home", limit=2000)
    sent_url = str(route.calls[0].request.url)
    assert "limit=999" in sent_url


@pytest.mark.asyncio
async def test_search_omits_optional_params_when_missing():
    """start / prefix stay out of the query string unless explicitly passed."""
    with respx.mock() as router:
        route = router.get("https://api.iconify.design/search").mock(
            return_value=httpx.Response(200, json={"icons": []})
        )
        await iconify.search(query="home", limit=64)
    sent_url = str(route.calls[0].request.url)
    assert "start=" not in sent_url
    assert "prefix=" not in sent_url


@pytest.mark.asyncio
async def test_search_includes_optional_params_when_passed():
    with respx.mock() as router:
        route = router.get("https://api.iconify.design/search").mock(
            return_value=httpx.Response(200, json={"icons": []})
        )
        await iconify.search(query="home", limit=64, start=100, prefix="lucide")
    sent_url = str(route.calls[0].request.url)
    assert "start=100" in sent_url
    assert "prefix=lucide" in sent_url


@pytest.mark.asyncio
async def test_get_icon_data_wraps_raw_plus_snippets():
    with respx.mock() as router:
        router.get("https://api.iconify.design/lucide.json").mock(
            return_value=httpx.Response(
                200,
                json={"prefix": "lucide", "icons": {"home": {"body": "<path/>"}}},
            )
        )
        out = await iconify.get_icon_data("lucide", "home")

    # Raw payload preserved
    assert out["IconifyIcon"]["icons"]["home"]["body"] == "<path/>"

    # CSS snippets
    assert 'i-lucide:home' in out["css"]["unocss"]
    assert 'icon-[lucide--home]' in out["css"]["tailwindcss"]

    # Web component / framework snippets
    assert '<iconify-icon icon="lucide:home"' in out["web"]["IconifyIconWebComponent"]
    assert '<Icon icon="lucide:home"' in out["web"]["IconifyForVue"]
    assert '<Icon icon="lucide:home"' in out["web"]["IconifyForReact"]
    assert '<Icon name="lucide:home" />' == out["web"]["AstroIcon"]
    assert out["web"]["UnpluginIcons"] == (
        "import LucideHome from '~icons/lucide/home';"
    )


@pytest.mark.asyncio
async def test_get_icon_data_capitalizes_dashed_set_and_icon_names():
    """Dashes in set or icon become CamelCase in the Unplugin import."""
    with respx.mock() as router:
        router.get("https://api.iconify.design/mdi-light.json").mock(
            return_value=httpx.Response(200, json={"prefix": "mdi-light", "icons": {}})
        )
        out = await iconify.get_icon_data("mdi-light", "home-variant")
    assert out["web"]["UnpluginIcons"] == (
        "import MdiLightHomeVariant from '~icons/mdi-light/home-variant';"
    )


@pytest.mark.asyncio
async def test_http_error_bubbles_up():
    """500 from Iconify → HTTPStatusError (fastmcp converts to MCP error)."""
    with respx.mock() as router:
        router.get("https://api.iconify.design/collections").mock(
            return_value=httpx.Response(500, text="server down")
        )
        with pytest.raises(httpx.HTTPStatusError):
            await iconify.list_collections()
