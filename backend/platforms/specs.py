"""Declarative platform spec loading and URL matching."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from backend.platforms.base import normalize_platform_capabilities

_SPEC_SUFFIXES = {".json", ".yml", ".yaml"}


def _dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    text = str(value or "").strip()
    return (text,) if text else ()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_platform_spec_locations() -> tuple[Path, ...]:
    root = _project_root()
    return (
        root / ".ctf-platforms",
        root / "platform-specs",
    )


def _iter_spec_files(paths: Iterable[str | Path]) -> list[Path]:
    discovered: list[Path] = []
    seen: set[Path] = set()
    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve()
        if not path.exists():
            continue
        if path.is_file():
            if path.suffix.lower() in _SPEC_SUFFIXES and path not in seen:
                seen.add(path)
                discovered.append(path)
            continue
        for child in sorted(path.rglob("*")):
            if child.is_file() and child.suffix.lower() in _SPEC_SUFFIXES and child not in seen:
                seen.add(child)
                discovered.append(child)
    return discovered


def _load_spec_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    payload = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text) or {}
    return _dict(payload)


@dataclass(frozen=True)
class PlatformMatchSpec:
    domains: tuple[str, ...] = ()
    url_patterns: tuple[str, ...] = ()

    def matches_url(self, url: str) -> bool:
        normalized_url = str(url or "").strip()
        if not normalized_url:
            return False
        parsed = urlsplit(normalized_url)
        hostname = str(parsed.hostname or "").strip().lower()
        if self.domains:
            matched_domain = any(
                hostname == domain or hostname.endswith(f".{domain}")
                for domain in self.domains
            )
            if not matched_domain:
                return False
        if self.url_patterns:
            return any(pattern in normalized_url for pattern in self.url_patterns)
        return bool(self.domains)

    @classmethod
    def from_dict(cls, value: object) -> PlatformMatchSpec:
        payload = _dict(value)
        return cls(
            domains=tuple(domain.lower() for domain in _strings(payload.get("domains"))),
            url_patterns=_strings(payload.get("url_patterns")),
        )


@dataclass(frozen=True)
class PlatformImportRegexSpec:
    competition_slug_regex: str = ""
    competition_title_regex: str = ""
    challenge_regex: str = ""

    @classmethod
    def from_dict(cls, value: object) -> PlatformImportRegexSpec:
        payload = _dict(value)
        return cls(
            competition_slug_regex=str(payload.get("competition_slug_regex") or "").strip(),
            competition_title_regex=str(payload.get("competition_title_regex") or "").strip(),
            challenge_regex=str(payload.get("challenge_regex") or "").strip(),
        )


@dataclass(frozen=True)
class PlatformSpec:
    platform: str
    label: str
    match: PlatformMatchSpec = field(default_factory=PlatformMatchSpec)
    capabilities: dict[str, str] = field(default_factory=dict)
    import_regex: PlatformImportRegexSpec = field(default_factory=PlatformImportRegexSpec)
    auth_mode: str = "none"
    path: Path | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, path: Path | None = None) -> PlatformSpec | None:
        platform = str(payload.get("platform") or "").strip().lower()
        if not platform:
            return None
        label = str(payload.get("label") or platform).strip()
        return cls(
            platform=platform,
            label=label or platform,
            match=PlatformMatchSpec.from_dict(payload.get("match")),
            capabilities=normalize_platform_capabilities(payload.get("capabilities")),
            import_regex=PlatformImportRegexSpec.from_dict(payload.get("import")),
            auth_mode=str(_dict(payload.get("auth")).get("mode") or "none").strip() or "none",
            path=path,
        )

    def matches_url(self, url: str) -> bool:
        return self.match.matches_url(url)


def load_platform_specs(paths: Iterable[str | Path] = ()) -> tuple[PlatformSpec, ...]:
    search_paths = list(default_platform_spec_locations())
    search_paths.extend(Path(path) for path in paths)
    specs: list[PlatformSpec] = []
    for spec_file in _iter_spec_files(search_paths):
        try:
            payload = _load_spec_file(spec_file)
        except Exception:
            continue
        spec = PlatformSpec.from_dict(payload, path=spec_file)
        if spec is not None:
            specs.append(spec)
    return tuple(specs)


def find_platform_spec_for_url(
    url: str,
    *,
    paths: Iterable[str | Path] = (),
) -> PlatformSpec | None:
    normalized_url = str(url or "").strip()
    for spec in load_platform_specs(paths):
        if spec.matches_url(normalized_url):
            return spec
    return None


def find_platform_spec(
    platform: str,
    *,
    paths: Iterable[str | Path] = (),
) -> PlatformSpec | None:
    normalized = str(platform or "").strip().lower()
    if not normalized:
        return None
    for spec in load_platform_specs(paths):
        if spec.platform == normalized:
            return spec
    return None


def compile_platform_regex(pattern: str) -> re.Pattern[str] | None:
    text = str(pattern or "").strip()
    if not text:
        return None
    try:
        return re.compile(text, re.S)
    except re.error:
        return None
