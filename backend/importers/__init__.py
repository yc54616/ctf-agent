"""Platform competition importers."""

from backend.importers.auto import AutoPlatformImporter
from backend.importers.base import CompetitionImportResult, ImportAuth, PlatformImporter
from backend.importers.dreamhack import DreamhackImporter
from backend.importers.registry import iter_platform_importers, pick_importer_for_url
from backend.importers.spec import SpecPlatformImporter

__all__ = [
    "AutoPlatformImporter",
    "CompetitionImportResult",
    "DreamhackImporter",
    "ImportAuth",
    "PlatformImporter",
    "SpecPlatformImporter",
    "iter_platform_importers",
    "pick_importer_for_url",
]
