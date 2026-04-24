"""Registered platform metadata, capabilities, and runtime client builders."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from backend.ctfd import CTFdClient
from backend.platforms.base import (
    CAPABILITY_CONFIRMED,
    CAPABILITY_OPERATOR_ONLY,
    CAPABILITY_UNSUPPORTED,
    PlatformClient,
    normalize_platform_capabilities,
    runtime_mode_from_capabilities,
)
from backend.platforms.specs import find_platform_spec, load_platform_specs

if TYPE_CHECKING:
    from backend.config import Settings


def _dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _humanize_platform_name(platform: str) -> str:
    text = str(platform or "").strip().replace("-", " ").replace("_", " ")
    return " ".join(part.capitalize() for part in text.split()) or "Imported Platform"


RuntimeClientFactory = Callable[[dict[str, Any], "Settings", str], PlatformClient | None]


@dataclass(frozen=True)
class PlatformDescriptor:
    platform: str
    label: str
    capabilities: dict[str, str] = field(default_factory=dict)
    client_factory: RuntimeClientFactory | None = None

    def normalized_capabilities(self) -> dict[str, str]:
        return normalize_platform_capabilities(self.capabilities)


def _build_ctfd_client(_source: dict[str, Any], settings: Settings, cookie_header: str) -> PlatformClient:
    # Prefer the explicit cookie_header arg (passed by build_platform_client
    # from the operator's active GUI state); fall back to settings so the
    # --cookie-file path still works.
    effective_cookie = str(
        cookie_header
        or getattr(settings, "remote_cookie_header", "")
        or ""
    ).strip()
    return CTFdClient(
        base_url=settings.ctfd_url,
        token=settings.ctfd_token,
        username=settings.ctfd_user,
        password=settings.ctfd_pass,
        cookie_header=effective_cookie,
    )


def _build_dreamhack_client(
    source: dict[str, Any],
    _settings: Settings,
    cookie_header: str,
) -> PlatformClient | None:
    from backend.platforms.dreamhack import DreamhackClient

    normalized_cookie = str(cookie_header or "").strip()
    if not normalized_cookie:
        return None
    competition = _dict(source.get("competition"))
    competition_slug = str(competition.get("slug") or "").strip()
    applicant_id = str(source.get("applicant_id") or "").strip()
    if not competition_slug or not applicant_id:
        return None
    return DreamhackClient(
        competition_slug=competition_slug,
        applicant_id=applicant_id,
        cookie_header=normalized_cookie,
        competition_title=str(competition.get("title") or "").strip(),
        competition_url=str(competition.get("url") or "").strip(),
    )


_REGISTERED_PLATFORMS: dict[str, PlatformDescriptor] = {
    "ctfd": PlatformDescriptor(
        platform="ctfd",
        label="CTFd",
        capabilities={
            "import": CAPABILITY_CONFIRMED,
            "poll_solved": CAPABILITY_CONFIRMED,
            "submit_flag": CAPABILITY_CONFIRMED,
            "pull_files": CAPABILITY_CONFIRMED,
        },
        client_factory=_build_ctfd_client,
    ),
    "dreamhack": PlatformDescriptor(
        platform="dreamhack",
        label="Dreamhack",
        capabilities={
            "import": CAPABILITY_CONFIRMED,
            "poll_solved": CAPABILITY_CONFIRMED,
            "submit_flag": CAPABILITY_CONFIRMED,
            "pull_files": CAPABILITY_UNSUPPORTED,
        },
        client_factory=_build_dreamhack_client,
    ),
}


def registered_platforms() -> tuple[PlatformDescriptor, ...]:
    specs = tuple(
        PlatformDescriptor(
            platform=spec.platform,
            label=spec.label,
            capabilities=spec.capabilities,
            client_factory=None,
        )
        for spec in load_platform_specs()
        if spec.platform not in _REGISTERED_PLATFORMS
    )
    return tuple(_REGISTERED_PLATFORMS.values()) + specs


def get_registered_platform(platform: str) -> PlatformDescriptor | None:
    normalized = str(platform or "").strip().lower()
    if not normalized:
        return None
    registered = _REGISTERED_PLATFORMS.get(normalized)
    if registered is not None:
        return registered
    spec = find_platform_spec(normalized)
    if spec is None:
        return None
    return PlatformDescriptor(
        platform=spec.platform,
        label=spec.label,
        capabilities=spec.capabilities,
        client_factory=None,
    )


def resolve_platform_descriptor(platform: str) -> PlatformDescriptor | None:
    normalized = str(platform or "").strip().lower()
    if not normalized:
        return None
    registered = get_registered_platform(normalized)
    if registered is not None:
        return registered
    return PlatformDescriptor(
        platform=normalized,
        label=_humanize_platform_name(normalized),
        capabilities={
            "import": CAPABILITY_CONFIRMED,
            "poll_solved": CAPABILITY_UNSUPPORTED,
            "submit_flag": CAPABILITY_OPERATOR_ONLY,
            "pull_files": CAPABILITY_UNSUPPORTED,
        },
        client_factory=None,
    )


def platform_source_defaults(platform: str) -> dict[str, Any]:
    descriptor = resolve_platform_descriptor(platform)
    if descriptor is None:
        return {}
    capabilities = descriptor.normalized_capabilities()
    return {
        "platform": descriptor.platform,
        "platform_label": descriptor.label,
        "capabilities": capabilities,
        "runtime_mode": runtime_mode_from_capabilities(capabilities),
    }


def normalize_platform_source(value: object) -> dict[str, Any]:
    source = _dict(value)
    platform = str(source.get("platform") or "").strip().lower()
    explicit_capabilities = source.get("capabilities")
    descriptor = resolve_platform_descriptor(platform)
    defaults = descriptor.normalized_capabilities() if descriptor is not None else {}
    capabilities = normalize_platform_capabilities(explicit_capabilities, defaults=defaults)
    normalized = dict(source)
    if descriptor is not None:
        normalized["platform"] = descriptor.platform
        normalized["platform_label"] = str(
            source.get("platform_label") or descriptor.label
        ).strip() or descriptor.label
    elif "platform_label" in normalized and not str(normalized.get("platform_label") or "").strip():
        normalized.pop("platform_label", None)

    if capabilities:
        normalized["capabilities"] = capabilities
        normalized["runtime_mode"] = runtime_mode_from_capabilities(capabilities)
    else:
        normalized.pop("capabilities", None)
        normalized.pop("runtime_mode", None)
    return normalized


def build_registered_platform_client(
    source: object,
    settings: Settings,
    *,
    cookie_header: str = "",
) -> PlatformClient | None:
    source_dict = _dict(source)
    descriptor = get_registered_platform(str(source_dict.get("platform") or ""))
    if descriptor is None or descriptor.client_factory is None:
        return None
    return descriptor.client_factory(source_dict, settings, cookie_header)
