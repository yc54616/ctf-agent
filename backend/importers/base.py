"""Shared platform importer interfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class ImportAuth:
    mode: str = "none"
    cookie_header: str = ""
    cookie_file: str = ""
    session_ref: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.cookie_header or self.session_ref)


@dataclass
class CompetitionImportResult:
    platform: str
    competition_dir: Path
    title: str
    source_url: str
    auth_mode: str
    imported_at: str
    challenge_entries: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class PlatformImporter(Protocol):
    platform: str

    def supports_url(self, url: str) -> bool:
        """Return True when the importer knows how to handle the URL."""

    async def import_competition(
        self,
        url: str,
        auth: ImportAuth,
        root: str | Path,
        *,
        refresh: bool = False,
    ) -> CompetitionImportResult:
        """Fetch, parse, and persist a competition under ``root``."""
