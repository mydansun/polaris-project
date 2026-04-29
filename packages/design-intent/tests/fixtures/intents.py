"""Shared intent fixtures used by multiple integration tests."""

GOLDEN_INTENT = {
    "pageType": "landing page",
    "themeMode": "light",
    "brandName": None,
    "productName": None,
    "audience": "HNW real-estate buyers",
    "primaryGoal": "book a private tour",
    "coreUseCase": None,
    # All "loose" fields are plain strings now (schema tightened so OpenAI
    # strict structured outputs can enforce required fields).
    "visualDirection": "Editorial, minimal, airy; primary color: white.",
    "contentStructure": "1. Hero  2. Trust  3. Lifestyle  4. Showcase  5. CTA",
    "narrative": "airy, restrained, premium",
    "designSystem": None,
    "interactionStyle": None,
    "hardConstraints": [],
    "avoidPatterns": ["generic card-grid-first layouts"],
    "motionGuidance": None,
    "imageryGuidance": None,
    "implementationRequirements": None,
    "notes": None,
}
