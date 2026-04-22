"""Heuristic importer fallback for arbitrary competition URLs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
import yaml

from backend.automation_profile import automation_profile_path, write_automation_profile
from backend.challenge_config import infer_connection, render_connection_info
from backend.importers.base import CompetitionImportResult, ImportAuth, PlatformImporter
from backend.platforms.catalog import platform_source_defaults
from backend.prompts import infer_flag_guard_from_texts

USER_AGENT = "Mozilla/5.0 (compatible; ctf-agent auto-import)"
_TITLE_PATTERNS = (
    re.compile(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](?P<title>[^"\']+)["\']',
        re.I,
    ),
    re.compile(
        r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\'](?P<title>[^"\']+)["\']',
        re.I,
    ),
    re.compile(r"<title[^>]*>(?P<title>.*?)</title>", re.I | re.S),
    re.compile(r"<h1[^>]*>(?P<title>.*?)</h1>", re.I | re.S),
)
_BLOCK_PATTERNS = (
    re.compile(
        r"<(?P<tag>li|article|section|div)[^>]*(?:class|id)=[\"'][^\"']*(?:challenge|problem|task|quest|mission|puzzle)[^\"']*[\"'][^>]*>(?P<html>.*?)</(?P=tag)>",
        re.I | re.S,
    ),
    re.compile(
        r"<(?P<tag>li|article|section)[^>]*(?:data-category|data-challenge|data-problem)[^>]*>(?P<html>.*?)</(?P=tag)>",
        re.I | re.S,
    ),
)
_FALLBACK_LINK_RE = re.compile(
    r"<a[^>]*href=[\"'](?P<href>[^\"']*(?:challenge|problem|task|quest|mission|puzzle)[^\"']*)[\"'][^>]*>(?P<name>.*?)</a>",
    re.I | re.S,
)
_ANCHOR_RE = re.compile(
    r"<a[^>]*href=[\"'](?P<href>[^\"']+)[\"'][^>]*>(?P<name>.*?)</a>",
    re.I | re.S,
)
_HEADING_RE = re.compile(r"<h[1-4][^>]*>(?P<name>.*?)</h[1-4]>", re.I | re.S)
_DESC_PATTERNS = (
    re.compile(
        r"<(?:div|p|span)[^>]*(?:class|id)=[\"'][^\"']*(?:desc|description|summary|content|body)[^\"']*[\"'][^>]*>(?P<text>.*?)</(?:div|p|span)>",
        re.I | re.S,
    ),
    re.compile(r"<p[^>]*>(?P<text>.*?)</p>", re.I | re.S),
)
_CATEGORY_ATTR_RE = re.compile(
    r"(?:data-category|data-tag|data-type)=[\"'](?P<category>[^\"']+)[\"']",
    re.I,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _slugify(value: str) -> str:
    text = re.sub(r"[^0-9A-Za-z._-]+", "_", str(value or "").strip())
    text = re.sub(r"_+", "_", text).strip("._")
    return text or "competition"


def _clean_text(value: object) -> str:
    text = unescape(str(value or ""))
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = _TAG_RE.sub(" ", text)
    text = text.replace("\xa0", " ")
    return _WS_RE.sub(" ", text).strip()


def _humanize_hostname(hostname: str) -> str:
    text = str(hostname or "").strip().replace("-", " ").replace(".", " ")
    return " ".join(part.capitalize() for part in text.split()) or "Imported Platform"


def _platform_key_from_url(url: str) -> str:
    hostname = str(urlsplit(url).hostname or "").strip().lower()
    normalized = re.sub(r"[^0-9a-z]+", "-", hostname).strip("-")
    return normalized or "generic-web"


def _title_from_html(html: str, *, fallback: str) -> str:
    for pattern in _TITLE_PATTERNS:
        match = pattern.search(html)
        if match is None:
            continue
        title = _clean_text(match.group("title"))
        if title:
            return title
    return fallback


def _competition_slug(url: str, title: str) -> str:
    path = str(urlsplit(url).path or "").rstrip("/")
    tail = path.rsplit("/", 1)[-1].strip()
    if tail:
        return _slugify(tail)
    return _slugify(title)


def _auth_payload(auth: ImportAuth) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    mode = str(auth.mode or "").strip()
    cookie_file = str(auth.cookie_file or "").strip()
    session_ref = str(auth.session_ref or "").strip()
    if mode:
        payload["mode"] = mode
    if cookie_file:
        payload["cookie_file"] = cookie_file
    if session_ref:
        payload["session_ref"] = session_ref
    return payload


def _join_url(base_url: str, href: str) -> str:
    return urljoin(base_url, str(href or "").strip())


def _block_category(html: str) -> str:
    match = _CATEGORY_ATTR_RE.search(html)
    return _clean_text(match.group("category")) if match else ""


def _block_name_and_href(html: str, *, base_url: str) -> tuple[str, str]:
    for pattern in (_HEADING_RE, _ANCHOR_RE):
        match = pattern.search(html)
        if match is None:
            continue
        name = _clean_text(match.groupdict().get("name", ""))
        href = _join_url(base_url, match.groupdict().get("href", "")) if "href" in match.groupdict() else ""
        if name:
            return name, href
    return "", ""


def _block_description(html: str, *, fallback_name: str) -> str:
    for pattern in _DESC_PATTERNS:
        match = pattern.search(html)
        if match is None:
            continue
        text = _clean_text(match.group("text"))
        if text and text != fallback_name:
            return text
    text = _clean_text(html)
    if text.startswith(fallback_name):
        text = text[len(fallback_name) :].strip(" :-")
    return text


@dataclass(frozen=True)
class _ChallengeCandidate:
    name: str
    url: str
    category: str = ""
    description: str = ""


def _block_candidates(html: str, *, base_url: str) -> list[_ChallengeCandidate]:
    candidates: list[_ChallengeCandidate] = []
    seen: set[tuple[str, str]] = set()
    for pattern in _BLOCK_PATTERNS:
        for match in pattern.finditer(html):
            block_html = str(match.group("html") or "")
            name, challenge_url = _block_name_and_href(block_html, base_url=base_url)
            if not name:
                continue
            description = _block_description(block_html, fallback_name=name)
            category = _block_category(match.group(0)) or _block_category(block_html)
            key = (name.lower(), challenge_url)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                _ChallengeCandidate(
                    name=name,
                    url=challenge_url,
                    category=category,
                    description=description,
                )
            )
    return candidates


def _fallback_candidates(html: str, *, base_url: str) -> list[_ChallengeCandidate]:
    candidates: list[_ChallengeCandidate] = []
    seen: set[tuple[str, str]] = set()
    for match in _FALLBACK_LINK_RE.finditer(html):
        name = _clean_text(match.group("name"))
        if not name:
            continue
        challenge_url = _join_url(base_url, match.group("href"))
        key = (name.lower(), challenge_url)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(_ChallengeCandidate(name=name, url=challenge_url))
    return candidates


def _heuristic_candidates(html: str, *, base_url: str) -> list[_ChallengeCandidate]:
    candidates = _block_candidates(html, base_url=base_url)
    if candidates:
        return candidates
    return _fallback_candidates(html, base_url=base_url)


class AutoPlatformImporter(PlatformImporter):
    platform = "auto"

    def supports_url(self, url: str) -> bool:
        parsed = urlsplit(str(url or "").strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    async def import_competition(
        self,
        url: str,
        auth: ImportAuth,
        root: str | Path,
        *,
        refresh: bool = False,
    ) -> CompetitionImportResult:
        normalized_url = str(url or "").strip()
        if not self.supports_url(normalized_url):
            raise RuntimeError(f"Unsupported competition URL: {url}")

        public_headers = {"User-Agent": USER_AGENT}
        auth_headers = dict(public_headers)
        if auth.enabled:
            auth_headers["Cookie"] = auth.cookie_header

        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            public_html = await self._fetch_text(client, normalized_url, headers=public_headers)
            authed_html = ""
            if auth.enabled:
                authed_html = await self._fetch_text(client, normalized_url, headers=auth_headers)

        html = authed_html or public_html
        platform_key = _platform_key_from_url(normalized_url)
        title = _title_from_html(html, fallback=platform_key)
        competition_slug = _competition_slug(normalized_url, title)
        competition_dir = Path(root).resolve() / competition_slug
        cache_dir = competition_dir / ".source-cache"
        if competition_dir.exists() and not refresh:
            raise RuntimeError(
                f"{competition_dir} already exists. Re-run with --refresh to update source metadata."
            )

        competition_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "competition-public.html").write_text(public_html, encoding="utf-8")
        if authed_html:
            (cache_dir / "competition-authenticated.html").write_text(authed_html, encoding="utf-8")

        challenge_entries: list[dict[str, Any]] = []
        warnings: list[str] = []
        source_defaults = platform_source_defaults(platform_key)
        auth_payload = _auth_payload(auth)
        profile_ref = str(automation_profile_path(competition_dir))
        remote_profile = {
            "version": 1,
            "platform": platform_key,
            "platform_label": source_defaults.get(
                "platform_label",
                _humanize_hostname(str(urlsplit(normalized_url).hostname or "")),
            ),
            "competition_url": normalized_url,
            "runtime_mode": source_defaults.get("runtime_mode", ""),
            "capabilities": source_defaults.get("capabilities", {}),
            "mode": "import_only",
            "challenge_hints": [],
        }
        candidates = _heuristic_candidates(html, base_url=normalized_url)
        if not candidates:
            warnings.append(
                "Auto importer could not confidently identify challenge cards; imported competition metadata only."
            )
        for candidate in candidates:
            metadata = self._challenge_metadata(
                candidate,
                platform_key=platform_key,
                title=title,
                competition_slug=competition_slug,
                competition_url=normalized_url,
                source_defaults=source_defaults,
                auth_payload=auth_payload,
                profile_ref=profile_ref,
            )
            challenge_slug = _slugify(metadata["name"])
            challenge_dir = competition_dir / challenge_slug
            challenge_dir.mkdir(parents=True, exist_ok=True)
            (challenge_dir / "metadata.yml").write_text(
                yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            (cache_dir / f"challenge-{challenge_slug}.json").write_text(
                json.dumps(
                    {
                        "name": candidate.name,
                        "url": candidate.url,
                        "category": candidate.category,
                        "description": candidate.description,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            remote_profile["challenge_hints"].append(
                {
                    "name": metadata["name"],
                    "category": metadata["category"],
                    "challenge_url": metadata["source"]["challenge_url"],
                }
            )
            challenge_entries.append(
                {
                    "name": metadata["name"],
                    "slug": challenge_slug,
                    "path": challenge_slug,
                    "challenge_url": metadata["source"]["challenge_url"],
                    "solved": False,
                    "writeup_submitted": False,
                    "remote_attached": source_defaults.get("runtime_mode", "") == "full_remote",
                }
            )

        imported_at = datetime.now(UTC).isoformat()
        write_automation_profile(competition_dir, remote_profile)
        manifest = {
            "platform": platform_key,
            "platform_label": source_defaults.get(
                "platform_label",
                _humanize_hostname(str(urlsplit(normalized_url).hostname or "")),
            ),
            "capabilities": source_defaults.get("capabilities", {}),
            "runtime_mode": source_defaults.get("runtime_mode", ""),
            "source_url": normalized_url,
            "title": title,
            "competition_slug": competition_slug,
            "imported_at": imported_at,
            "auth_mode": auth.mode,
            "import_mode": "heuristic_auto",
            "challenge_entries": challenge_entries,
            "remote": {
                "runtime_mode": source_defaults.get("runtime_mode", ""),
                "profile_ref": profile_ref,
            },
        }
        if auth_payload:
            manifest["auth"] = auth_payload
        if warnings:
            manifest["warnings"] = warnings
        (competition_dir / "competition.yml").write_text(
            yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return CompetitionImportResult(
            platform=platform_key,
            competition_dir=competition_dir,
            title=title,
            source_url=normalized_url,
            auth_mode=auth.mode,
            imported_at=imported_at,
            challenge_entries=challenge_entries,
            warnings=warnings,
        )

    async def _fetch_text(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str],
    ) -> str:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.text

    def _challenge_metadata(
        self,
        candidate: _ChallengeCandidate,
        *,
        platform_key: str,
        title: str,
        competition_slug: str,
        competition_url: str,
        source_defaults: dict[str, Any],
        auth_payload: dict[str, Any],
        profile_ref: str,
    ) -> dict[str, Any]:
        description = str(candidate.description or "").strip()
        connection = infer_connection(description, candidate.url)
        flag_format, flag_regex = infer_flag_guard_from_texts(description)
        metadata: dict[str, Any] = {
            "name": candidate.name,
            "category": candidate.category,
            "description": description,
            "value": 0,
            "tags": [candidate.category] if candidate.category else [],
            "connection_info": render_connection_info(connection, fallback=candidate.url),
            "solves": 0,
            "flag_format": flag_format,
            "flag_regex": flag_regex,
            "source": {
                **source_defaults,
                "competition": {
                    "slug": competition_slug,
                    "title": title,
                    "url": competition_url,
                },
                "challenge_url": candidate.url,
                "status": {
                    "solved": False,
                    "writeup_submitted": False,
                },
                "import_mode": "heuristic_auto",
                "remote": {
                    "runtime_mode": source_defaults.get("runtime_mode", ""),
                    "profile_ref": profile_ref,
                    "capabilities": source_defaults.get("capabilities", {}),
                },
            },
        }
        if auth_payload:
            metadata["source"]["auth"] = auth_payload
        if connection:
            metadata["connection"] = connection
        return metadata
