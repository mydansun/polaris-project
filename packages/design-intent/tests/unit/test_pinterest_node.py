import pytest

from polaris_design_intent.nodes import pinterest as pinterest_mod
from polaris_design_intent.nodes.pinterest import _enrich_query, pinterest_node


def test_enrich_query_appends_suffix(monkeypatch):
    monkeypatch.setattr(pinterest_mod, "_QUERY_SUFFIX", "web design")
    assert _enrich_query("real estate landing page") == "real estate landing page web design"


def test_enrich_query_dedupe_case_insensitive(monkeypatch):
    monkeypatch.setattr(pinterest_mod, "_QUERY_SUFFIX", "web design")
    assert _enrich_query("SaaS dashboard web design") == "SaaS dashboard web design"
    assert _enrich_query("portfolio WEB DESIGN inspiration") == "portfolio WEB DESIGN inspiration"


def test_enrich_query_passes_empty_through(monkeypatch):
    monkeypatch.setattr(pinterest_mod, "_QUERY_SUFFIX", "web design")
    assert _enrich_query("") == ""
    assert _enrich_query("   ") == ""


def test_enrich_query_noop_when_suffix_empty(monkeypatch):
    monkeypatch.setattr(pinterest_mod, "_QUERY_SUFFIX", "")
    assert _enrich_query("real estate landing") == "real estate landing"


@pytest.mark.asyncio
async def test_pinterest_node_merges_and_dedupes(settings, mock_pinterest_transport):
    state = {
        "pinterest_queries": [
            "real estate landing page white",
            "real estate website beige luxury",
        ]
    }
    result = await pinterest_node(state, settings)
    ids = [r["id"] for r in result["pinterest_refs"]]
    # 4 from the first query + 1 unique from the second (pin-002 was a duplicate)
    assert ids == ["pin-001", "pin-002", "pin-003", "pin-004", "pin-005"]


@pytest.mark.asyncio
async def test_pinterest_node_hands_single_image_to_compiler(settings, mock_pinterest_transport):
    """Only the single chosen ref retains image_b64; others stripped."""
    state = {"pinterest_queries": ["real estate landing page white"]}
    result = await pinterest_node(state, settings)
    refs = result["pinterest_refs"]
    encoded = [r for r in refs if r["image_b64"] is not None]
    assert len(encoded) == 1
    # With stub scorer the first encoded gets score=5 → chosen
    assert encoded[0]["id"] == "pin-001"
    assert encoded[0]["score"] == 5.0


@pytest.mark.asyncio
async def test_pinterest_node_threshold_first_match_wins(settings, mock_pinterest_transport, monkeypatch):
    """When multiple refs score >= threshold, the FIRST such ref (post-shuffle
    order) is chosen — not the highest one."""

    async def all_four_plus(*, refs, queries, settings):
        # Everyone with an image scores 4.5, in input order
        out = []
        for r in refs:
            if r.image_b64 is None:
                out.append(r)
            else:
                out.append(r.model_copy(update={"score": 4.5, "score_reason": "s"}))
        return out

    monkeypatch.setattr(pinterest_mod, "score_images_batched", all_four_plus)
    settings.image_score_threshold = 4.0
    state = {"pinterest_queries": ["real estate landing page white"]}
    result = await pinterest_node(state, settings)
    chosen = [r for r in result["pinterest_refs"] if r["image_b64"] is not None]
    assert len(chosen) == 1
    assert chosen[0]["id"] == "pin-001"  # first encoded = first qualifier


@pytest.mark.asyncio
async def test_pinterest_node_below_threshold_picks_max(settings, mock_pinterest_transport, monkeypatch):
    """All scores below threshold → picks the single highest-scoring."""

    async def low_scores(*, refs, queries, settings):
        # descending 3.5 / 2.5 / 1.5 / 0.5
        out = []
        cur = 3.5
        for r in refs:
            if r.image_b64 is None:
                out.append(r)
            else:
                out.append(r.model_copy(update={"score": cur, "score_reason": "low"}))
                cur = max(0.0, cur - 1.0)
        return out

    monkeypatch.setattr(pinterest_mod, "score_images_batched", low_scores)
    settings.image_score_threshold = 4.0
    state = {"pinterest_queries": ["real estate landing page white"]}
    result = await pinterest_node(state, settings)
    chosen = [r for r in result["pinterest_refs"] if r["image_b64"] is not None]
    assert len(chosen) == 1
    # pin-001 is the first encoded so it got 3.5 (max) → chosen
    assert chosen[0]["id"] == "pin-001"
    assert chosen[0]["score"] == 3.5


@pytest.mark.asyncio
async def test_pinterest_node_tolerates_empty_queries(settings):
    result = await pinterest_node({"pinterest_queries": []}, settings)
    assert result == {"pinterest_refs": []}


@pytest.mark.asyncio
async def test_pinterest_node_truncates_to_max_refs(settings, mock_pinterest_transport):
    settings.max_refs = 2
    state = {"pinterest_queries": ["real estate landing page white"]}
    result = await pinterest_node(state, settings)
    assert len(result["pinterest_refs"]) == 2


@pytest.mark.asyncio
async def test_pinterest_node_scorer_failure_still_returns_refs(settings, mock_pinterest_transport, monkeypatch):
    """Scorer exception path: pipeline falls back to first encoded ref."""

    async def blow_up(*, refs, queries, settings):
        # Simulate score_images_batched's "exception → unscored" path by
        # returning refs with image_b64 but score=None.
        return list(refs)

    monkeypatch.setattr(pinterest_mod, "score_images_batched", blow_up)
    state = {"pinterest_queries": ["real estate landing page white"]}
    result = await pinterest_node(state, settings)
    refs = result["pinterest_refs"]
    # At least one image should still be present (fallback path)
    encoded = [r for r in refs if r["image_b64"] is not None]
    assert len(encoded) == 1
