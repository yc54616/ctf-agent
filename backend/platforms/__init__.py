"""Runtime platform clients and helpers."""

from __future__ import annotations

from backend.platforms.base import (
    CAPABILITY_CONFIRMED,
    CAPABILITY_OPERATOR_ONLY,
    CAPABILITY_UNSUPPORTED,
    PLATFORM_CAPABILITY_KEYS,
    RUNTIME_MODE_FULL_REMOTE,
    RUNTIME_MODE_IMPORT_ONLY,
    RUNTIME_MODE_OPERATOR_ONLY,
    CompositePlatformClient,
    NullPlatformClient,
    PlatformClient,
    SubmitResult,
    normalize_platform_capabilities,
    platform_label,
    runtime_mode_from_capabilities,
)
from backend.platforms.browser import BrowserPlatformClient
from backend.platforms.catalog import (
    PlatformDescriptor,
    build_registered_platform_client,
    get_registered_platform,
    normalize_platform_source,
    platform_source_defaults,
    registered_platforms,
    resolve_platform_descriptor,
)
from backend.platforms.factory import build_platform_client

__all__ = [
    "CAPABILITY_CONFIRMED",
    "CAPABILITY_OPERATOR_ONLY",
    "CAPABILITY_UNSUPPORTED",
    "CompositePlatformClient",
    "BrowserPlatformClient",
    "DreamhackClient",
    "NullPlatformClient",
    "PlatformClient",
    "PLATFORM_CAPABILITY_KEYS",
    "PlatformDescriptor",
    "RUNTIME_MODE_FULL_REMOTE",
    "RUNTIME_MODE_IMPORT_ONLY",
    "RUNTIME_MODE_OPERATOR_ONLY",
    "SubmitResult",
    "build_registered_platform_client",
    "build_platform_client",
    "get_registered_platform",
    "normalize_platform_capabilities",
    "normalize_platform_source",
    "platform_label",
    "platform_source_defaults",
    "registered_platforms",
    "resolve_platform_descriptor",
    "runtime_mode_from_capabilities",
]


def __getattr__(name: str) -> object:
    if name == "DreamhackClient":
        from backend.platforms.dreamhack import DreamhackClient

        return DreamhackClient
    raise AttributeError(name)
