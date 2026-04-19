"""Model resolution for CLI-backed solver providers."""

from __future__ import annotations

from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from backend.config import Settings

# Default model specs — codex and gemini use custom solver backends
DEFAULT_MODELS: list[str] = [
    "gemini/gemini-2.5-flash",
    "gemini/gemini-2.5-flash-lite",
    "gemini/gemini-2.5-pro",
    "codex/gpt-5.4",
    "codex/gpt-5.4-mini",
    "codex/gpt-5.3-codex",
    "codex/gpt-5.3-codex-spark",
]

# Context window sizes (tokens)
CONTEXT_WINDOWS: dict[str, int] = {
    "us.anthropic.claude-opus-4-7-v1": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "us.anthropic.claude-opus-4-6-v1": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "gemini-2.5-flash": 1_000_000,
    "gemini-2.5-flash-lite": 1_000_000,
    "gemini-2.5-pro": 1_000_000,
    "gpt-5.4": 1_000_000,
    "gpt-5.2-codex": 1_000_000,
    "gpt-5.1-codex-max": 1_000_000,
    "gpt-5.4-mini": 400_000,
    "gpt-5.3-codex": 1_000_000,
    "gpt-5.3-codex-spark": 128_000,
    "gpt-5.2": 1_000_000,
    "gpt-5.1-codex-mini": 400_000,
    "gemini-3-flash-preview": 1_000_000,
}

# Models that support vision
VISION_MODELS: set[str] = {
    "us.anthropic.claude-opus-4-7-v1",
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "us.anthropic.claude-opus-4-6-v1",
    "claude-opus-4-6",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gemini-3-flash-preview",
}

SUPPORTED_PROVIDERS: set[str] = {
    "claude-sdk",
    "codex",
    "gemini",
    "google",
}


def resolve_model(spec: str, settings: Settings) -> Model:
    """Direct API model resolution is no longer supported."""
    provider = provider_from_spec(spec)
    raise ValueError(
        f"Provider '{provider}' uses its own solver backend. "
        f"Direct API providers are not supported for {spec}."
    )


def resolve_model_settings(spec: str) -> ModelSettings:
    """Return default model settings for custom solver backends."""
    provider_from_spec(spec)
    return ModelSettings(max_tokens=128_000)


def model_id_from_spec(spec: str) -> str:
    """Extract just the model ID from a spec (strips effort suffix)."""
    parts = spec.split("/")
    return parts[1] if len(parts) >= 2 else spec


def provider_from_spec(spec: str) -> str:
    """Extract the provider from a spec and enforce the supported set."""
    provider = spec.split("/", 1)[0]
    if provider not in SUPPORTED_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_PROVIDERS))
        raise ValueError(
            f"Unsupported provider '{provider}' in model spec '{spec}'. "
            f"Supported providers: {supported}."
        )
    return provider


def effort_from_spec(spec: str) -> str | None:
    """Extract effort level from a spec like 'claude-sdk/claude-opus-4-6/max'."""
    parts = spec.split("/")
    if len(parts) >= 3 and parts[2] in ("low", "medium", "high", "max"):
        return parts[2]
    return None


def supports_vision(spec: str) -> bool:
    """Check if a model spec supports vision."""
    return model_id_from_spec(spec) in VISION_MODELS


def context_window(spec: str) -> int:
    """Get context window size for a model spec."""
    return CONTEXT_WINDOWS.get(model_id_from_spec(spec), 200_000)
