"""mood_board_step — generate an ORIGINAL landscape mood board image.

Takes the Pinterest chosen reference image (already selected + scored by
the ``references`` node) and the compiler-enriched design intent, then
calls gpt-image-1's ``images.edit`` in "multi-image reference mode" to
produce an ORIGINAL mood board PNG.

Why this exists
---------------
Once the compiler writes the text brief, Codex loses all direct visual
signal — it only has AGENTS.md prose.  This node synthesizes a single
1536×1024 visual anchor that the worker writes into the workspace
container; every Codex turn then gets it as a ``localImage`` input
(see ``codex_app_server.py::run_turn``).

Copyright / privacy notes
-------------------------
- The Pinterest reference bytes are sent to gpt-image-1 ONLY as the
  ``image`` parameter; the prompt explicitly forbids copying subject,
  people, brand, logo, or text from the reference.
- The generated PNG is our own derived work — it replaces the Pinterest
  image everywhere downstream (workspace file, Codex multimodal input).
  Pinterest URLs / bytes never reach Codex, AGENTS.md, SSE, or the
  frontend.
- Failure (rate limit / safety filter / network) → node returns
  ``{"mood_board_b64": None}`` and logs a warning.  Discovery graph
  still completes successfully.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Any

from openai import AsyncOpenAI

from polaris_design_intent.config import Settings
from polaris_design_intent.state import DesignIntentState

logger = logging.getLogger(__name__)


_IMAGE_PROMPT_TEMPLATE = """Create a cohesive visual mood board image \
for a website design direction.

The mood board should be a curated collage of 9 to 12 visual \
references arranged with taste and restraint, like a creative \
director's presentation board.  It is not a final UI mockup and not a \
wireframe.  It should define the visual world of the project through \
photography, color, texture, typography mood, composition, and \
atmosphere.

Include:
- editorial-quality photography
- environmental or lifestyle scenes relevant to the brand
- material and texture references
- typography mood samples
- spatial and compositional inspiration
- a consistent color story

Avoid:
- generic SaaS UI
- dashboard cards
- stock corporate teamwork scenes
- noisy collage layouts
- random unrelated imagery
- literal webpage screenshots unless explicitly requested

Make it feel cohesive, premium, restrained, and presentation-ready.

# Reference image

Treat the ONE reference image I've provided as inspiration for the \
overall palette, composition rhythm, material feel, and typographic \
character.  Draw from its mood — but the final mood board must be \
ORIGINAL.  Do not reproduce any subject, person, product, scene, \
logo, brand, trademark, or readable copy from the reference.

# Project structured inputs

Use these fields to tune the collage's subject matter and color story \
so the board actually fits this specific project, not just the \
reference's vibe:

- Page type: {page_type}
- Audience: {audience}
- Visual direction: {visual_direction}
- Primary accent color: {accent_hex}
- Typography character: {typography_primary}{typography_secondary}
- Hero layout preference: {hero_layout}
- Mood / narrative: {mood}

Canvas: landscape 1536×1024.  Layout: 9-12 distinct tiles / cells of \
varying sizes arranged on a neutral board surface, like pinned \
clippings.  Keep composition calm and balanced — premium editorial \
feel, not cluttered.  All typography in the board must be abstract / \
lorem-ipsum only — no readable brand names, product names, or real \
copy.
"""


def _get(obj: Any, key: str) -> Any:
    """Read a field from either a dict or a pydantic-like object."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


async def mood_board_node(
    state: DesignIntentState, settings: Settings
) -> dict[str, Any]:
    refs = state.get("pinterest_refs") or []
    chosen_ref = next(
        (r for r in refs if _get(r, "image_b64")),
        None,
    )
    if chosen_ref is None:
        logger.info("mood_board: no pinterest ref with image_b64, skipping")
        return {"mood_board_b64": None}

    # Prefer the compiler-enriched intent (richer typography / accent /
    # hero descriptors); fall back to the clarifier-emitted raw intent
    # if compiler hasn't produced a JSON output.
    intent = state.get("compiled_brief_json") or state.get("design_intent") or {}

    ref_b64: str = _get(chosen_ref, "image_b64") or ""
    ref_mime: str = _get(chosen_ref, "mime_type") or "image/jpeg"

    typography_secondary = intent.get("typographySecondary")
    prompt = _IMAGE_PROMPT_TEMPLATE.format(
        page_type=intent.get("pageType") or "website",
        audience=intent.get("audience") or "general audience",
        visual_direction=intent.get("visualDirection") or "clean, modern",
        accent_hex=intent.get("accentColorHex") or "(choose per mood)",
        typography_primary=intent.get("typographyPrimary") or "classical serif",
        typography_secondary=(
            f" + {typography_secondary}" if typography_secondary else ""
        ),
        hero_layout=intent.get("heroLayout") or "full_bleed_image",
        mood=(
            intent.get("narrative")
            or intent.get("visualDirection")
            or "calm, confident"
        ),
    )

    # images.edit expects file-like input; wrap bytes in a named
    # BytesIO so the SDK builds a proper multipart upload.
    try:
        ref_bytes = base64.b64decode(ref_b64)
    except Exception:  # noqa: BLE001
        logger.warning("mood_board: failed to decode reference b64, skipping", exc_info=True)
        return {"mood_board_b64": None}
    ref_file = ("reference.jpg", io.BytesIO(ref_bytes), ref_mime)

    try:
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        logger.info(
            "mood_board: generating (model=%s, size=%s, ref_bytes=%d, prompt_chars=%d)",
            settings.mood_board_image_model,
            settings.mood_board_image_size,
            len(ref_bytes),
            len(prompt),
        )
        resp = await client.images.edit(
            model=settings.mood_board_image_model,
            image=ref_file,
            prompt=prompt,
            size=settings.mood_board_image_size,
            n=1,
        )
        b64 = resp.data[0].b64_json if resp.data else None
        if not b64:
            raise RuntimeError("gpt-image-1 returned empty b64_json")
        logger.info("mood_board: generated %d b64 chars", len(b64))
        return {"mood_board_b64": b64}
    except Exception:  # noqa: BLE001
        logger.warning("mood_board: image gen failed, skipping", exc_info=True)
        return {"mood_board_b64": None}
