import pytest

from backend.models import DEFAULT_MODELS, provider_from_spec


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
