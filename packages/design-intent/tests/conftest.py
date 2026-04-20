"""Shared test fixtures for polaris-design-intent.

Design rule: no network, no OpenAI keys.  Unit and integration tests stay
reproducible by using respx for HTTP and a scripted fake chat model for LLM.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest
import respx
from httpx import Response
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from polaris_design_intent.config import Settings

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def settings() -> Settings:
    """A Settings instance with in-test defaults.  Override individual fields
    in-place as needed."""
    s = Settings()
    s.openai_api_key = "sk-test"
    s.pinterest_base_url = "http://pinterest.test"
    s.max_rounds = 3
    s.pinterest_hops = 1
    s.max_refs = 12
    s.max_images_to_compiler = 2  # keep tests small
    return s


@pytest.fixture
def pinterest_responses() -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / "pinterest_responses.json").read_text())


@pytest.fixture
def mock_pinterest_transport(pinterest_responses: dict[str, Any]):
    """respx router covering both `POST /query` and image GETs.

    `/query` responses are keyed by query string — any unknown query returns
    an empty result list.  Image GETs return a tiny fake PNG.  Test cases that
    want specific failures re-route individual mocks after entering this
    fixture.
    """
    with respx.mock() as router:
        def query_handler(request):
            body = json.loads(request.content or b"{}")
            query = body.get("query", "")
            for _, resp in pinterest_responses.items():
                if resp.get("query") == query:
                    return Response(200, json=resp)
            return Response(200, json={"query": query, "hops": 1, "count": 0, "results": []})

        router.post("http://pinterest.test/query").mock(side_effect=query_handler)
        # Any image GET returns deterministic fake bytes — tests only care that
        # base64 encoding succeeded, not that the payload is a real image.
        fake_image = b"FAKE_IMAGE_BYTES_FOR_TESTS"
        router.route(url__regex=r"https?://example\.com/.*").mock(
            return_value=Response(200, content=fake_image, headers={"content-type": "image/png"})
        )
        yield router


# ── Fake LLMs ─────────────────────────────────────────────────────────────


def scripted_chat_model(
    responses: list[AIMessage],
) -> GenericFakeChatModel:
    """A chat model that replays a pre-built list of AIMessages in order.

    Useful when you want to exercise a specific clarifier path (e.g. first
    round asks, second round emits).  `bind_tools` on this model returns
    itself so the tool schema is ignored — the test drives tool_calls by
    constructing them directly on AIMessage.
    """
    return _ScriptedFakeChatModel(messages=iter(responses))


class _ScriptedFakeChatModel(GenericFakeChatModel):
    """Subclass that ignores `bind_tools` and `with_structured_output`.

    We can't use GenericFakeChatModel directly because tool-binding on real
    ChatOpenAI returns a RunnableBinding which is what our production nodes
    invoke.  For tests, we short-circuit: bind_tools / with_structured_output
    both return self, so the scripted messages are served regardless.
    """

    def bind_tools(self, *_args, **_kwargs):  # noqa: D401
        return self

    def with_structured_output(self, schema, **_kwargs):  # type: ignore[override]
        # Wrap so .ainvoke() returns a schema instance constructed from the
        # next scripted AIMessage's content (expected to be JSON).
        parent = self

        class _StructuredWrapper:
            async def ainvoke(self, _messages):  # noqa: D401
                msg = next(parent.messages)
                payload = json.loads(msg.content)
                return schema.model_validate(payload)

        return _StructuredWrapper()


@pytest.fixture(autouse=True)
def _no_query_enrichment(monkeypatch):
    """Disable the "web design" suffix for every test.  Fixtures key
    Pinterest responses on the raw query strings; patching the suffix
    to empty keeps them matching without touching any JSON / call site.
    Individual tests that want to exercise enrichment patch the module
    constant back themselves.
    """
    from polaris_design_intent.nodes import pinterest as _pinterest_mod

    monkeypatch.setattr(_pinterest_mod, "_QUERY_SUFFIX", "")


@pytest.fixture(autouse=True)
def _no_shuffle(monkeypatch):
    """Deterministic ref order for every test.  pinterest_node shuffles in
    production to avoid position-bias in the scorer, but tests assert on
    ordering so we pin it.
    """
    from polaris_design_intent.nodes import pinterest as _pinterest_mod

    monkeypatch.setattr(_pinterest_mod.random, "shuffle", lambda _xs: None)


@pytest.fixture(autouse=True)
def _stub_image_scorer(monkeypatch):
    """Replace the batched image scorer in pinterest_node globally.

    Default behavior: assign descending scores (5, 4, 3, ...) to every
    ref that has ``image_b64``, leaving others untouched.  Individual
    tests can monkey-patch with a different stub or restore the real one.
    """
    from polaris_design_intent.nodes import pinterest as _pinterest_mod

    async def _fake(*, refs, queries, settings):
        out = []
        cur = 5.0
        for r in refs:
            if r.image_b64 is None:
                out.append(r)
                continue
            out.append(r.model_copy(update={"score": cur, "score_reason": "stub"}))
            cur = max(0.0, cur - 1.0)
        return out

    monkeypatch.setattr(_pinterest_mod, "score_images_batched", _fake)


@pytest.fixture
def stub_user_input_fn() -> Callable[..., Any]:
    """Factory for stub UserInputFn.

    Usage:
        uif = stub_user_input_fn([[{"question_id":"q1","answer":"saas"}]])
    The returned async callable pops one answer set per call.
    """

    def _factory(answer_batches: list[list[dict]]):
        iterator = iter(answer_batches)

        async def _uif(_questions):
            try:
                return next(iterator)
            except StopIteration as e:  # noqa: BLE001
                raise AssertionError("stub_user_input_fn ran out of scripted answers") from e

        return _uif

    return _factory
