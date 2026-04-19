import pytest

from backend.agents.claude_advisor import ADVISOR_MODEL as CLAUDE_ADVISOR_MODEL
from backend.agents.codex_advisor import ADVISOR_MODEL as CODEX_ADVISOR_MODEL
from backend.cost_tracker import FALLBACK_PRICING
from backend.models import CONTEXT_WINDOWS, DEFAULT_MODELS, VISION_MODELS, provider_from_spec


def test_default_models_use_codex_and_gemini_only() -> None:
    assert "gemini/gemini-2.5-flash" in DEFAULT_MODELS
    assert "gemini/gemini-2.5-flash-lite" in DEFAULT_MODELS
    assert "gemini/gemini-2.5-pro" in DEFAULT_MODELS
    assert "codex/gpt-5.4" in DEFAULT_MODELS
    assert "codex/gpt-5.4-mini" in DEFAULT_MODELS
    assert "codex/gpt-5.3-codex" in DEFAULT_MODELS
    assert "codex/gpt-5.3-codex-spark" in DEFAULT_MODELS
    assert len(DEFAULT_MODELS) == 7
    assert all(not spec.startswith("claude-sdk/") for spec in DEFAULT_MODELS)


def test_direct_api_providers_are_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported provider 'azure'"):
        provider_from_spec("azure/gpt-5.4")


def test_advisor_defaults_use_mini_and_sonnet() -> None:
    assert CODEX_ADVISOR_MODEL == "gpt-5.4-mini"
    assert CLAUDE_ADVISOR_MODEL == "claude-sonnet-4-6"


def test_sonnet_4_6_has_context_vision_and_fallback_pricing() -> None:
    assert CONTEXT_WINDOWS["claude-sonnet-4-6"] == 1_000_000
    assert "claude-sonnet-4-6" in VISION_MODELS
    assert FALLBACK_PRICING["claude-sonnet-4-6"] == {
        "input": 3.00,
        "cached_input": 0.30,
        "output": 15.00,
    }
