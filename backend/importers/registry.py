"""Registry for competition importers."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from backend.importers.auto import AutoPlatformImporter
from backend.importers.base import PlatformImporter
from backend.importers.dreamhack import DreamhackImporter
from backend.importers.spec import SpecPlatformImporter
from backend.platforms.specs import load_platform_specs


def iter_platform_importers(
    spec_paths: Iterable[str | Path] = (),
) -> tuple[PlatformImporter, ...]:
    importers: list[PlatformImporter] = [DreamhackImporter()]
    importers.extend(SpecPlatformImporter(spec) for spec in load_platform_specs(spec_paths))
    importers.append(AutoPlatformImporter())
    return tuple(importers)


def pick_importer_for_url(
    url: str,
    *,
    spec_paths: Iterable[str | Path] = (),
) -> PlatformImporter | None:
    normalized = str(url or "").strip()
    for importer in iter_platform_importers(spec_paths):
        if importer.supports_url(normalized):
            return importer
    return None
