from polaris_design_intent.models import CompiledBrief, DesignIntent, PinterestRef


def test_design_intent_all_keys_optional():
    intent = DesignIntent()
    dumped = intent.model_dump()
    expected_keys = {
        "pageType", "themeMode", "brandName", "productName", "audience",
        "primaryGoal", "coreUseCase", "visualDirection", "contentStructure",
        "narrative", "designSystem", "interactionStyle", "hardConstraints",
        "avoidPatterns", "motionGuidance", "imageryGuidance",
        "implementationRequirements", "notes",
        # Frontend-skill structured tokens
        "typographyPrimary", "typographySecondary", "accentColorHex",
        "heroLayout", "cardPolicy", "motionPlan",
    }
    assert set(dumped.keys()) == expected_keys


def test_design_intent_round_trip():
    payload = {
        "pageType": "landing page",
        "audience": "HNW real-estate buyers",
        "primaryGoal": "book a private tour",
        # String-valued fields accept prose / markdown; nested objects would
        # be rejected now (schema tightened to keep OpenAI strict mode happy).
        "visualDirection": "Editorial, minimal, warm; primary color: white.",
        "hardConstraints": ["no stock photos of generic people"],
        "avoidPatterns": ["card-grid-first layouts"],
    }
    intent = DesignIntent.model_validate(payload)
    assert intent.audience == "HNW real-estate buyers"
    assert intent.hardConstraints == ["no stock photos of generic people"]
    assert intent.visualDirection == "Editorial, minimal, warm; primary color: white."


def test_pinterest_ref_image_fields_optional():
    ref = PinterestRef(id="p1", title="x", max="m", normal="n")
    assert ref.image_b64 is None
    assert ref.mime_type is None


def test_compiled_brief_defaults():
    brief = CompiledBrief(intent=DesignIntent(), brief="hello")
    assert brief.pinterest_refs == []
    assert brief.pinterest_queries == []
