"""Declarative regex-driven platform importer."""

from __future__ import annotations

import json
import re
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
from backend.platforms.base import normalize_platform_capabilities, runtime_mode_from_capabilities
from backend.platforms.specs import PlatformSpec, compile_platform_regex
from backend.prompts import infer_flag_guard_from_texts

USER_AGENT = "Mozilla/5.0 (compatible; ctf-agent import)"
_TITLE_RE = re.compile(r"<title[^>]*>(?P<title>.*?)</title>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _slugify(value: str) -> str:
    text = re.sub(r"[^0-9A-Za-z._-]+", "_", str(value or "").strip())
    text = re.sub(r"_+", "_", text).strip("._")
    return text or "competition"


def _clean_text(value: object) -> str:
    text = unescape(str(value or ""))
    text = re.sub(r"(?i)<br\\s*/?>", "\n", text)
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", text.replace("\xa0", " ")).strip()


def _extract_title(html: str, regex_pattern: str) -> str:
    matcher = compile_platform_regex(regex_pattern)
    if matcher is not None:
        match = matcher.search(html)
        if match is not None:
            title = match.groupdict().get("title", match.group(0))
            cleaned = _clean_text(title)
            if cleaned:
                return cleaned
    title_match = _TITLE_RE.search(html)
    if title_match:
        return _clean_text(title_match.group("title"))
    return ""


def _extract_slug(url: str, regex_pattern: str, *, fallback: str) -> str:
    matcher = compile_platform_regex(regex_pattern)
    if matcher is not None:
        match = matcher.search(url)
        if match is not None:
            slug = match.groupdict().get("slug", match.group(0))
            normalized = _slugify(slug)
            if normalized:
                return normalized
    parsed = urlsplit(url)
    tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if tail:
        return _slugify(tail)
    return _slugify(fallback)


def _int_value(value: object) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    match = re.search(r"-?\d+", text)
    if match is None:
        return 0
    return int(match.group(0))


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


def _source_defaults(spec: PlatformSpec) -> dict[str, Any]:
    capabilities = normalize_platform_capabilities(spec.capabilities)
    return {
        "platform": spec.platform,
        "platform_label": spec.label,
        "capabilities": capabilities,
        "runtime_mode": runtime_mode_from_capabilities(capabilities),
    }


class SpecPlatformImporter(PlatformImporter):
    def __init__(self, spec: PlatformSpec) -> None:
        self.spec = spec
        self.platform = spec.platform

    def supports_url(self, url: str) -> bool:
        return self.spec.matches_url(url)

    async def import_competition(
        self,
        url: str,
        auth: ImportAuth,
        root: str | Path,
        *,
        refresh: bool = False,
    ) -> CompetitionImportResult:
        normalized_url = str(url or "").strip()
        if not self.spec.matches_url(normalized_url):
            raise RuntimeError(f"Unsupported {self.spec.label} competition URL: {url}")

        public_headers = {"User-Agent": USER_AGENT}
        auth_headers = dict(public_headers)
        if auth.enabled:
            auth_headers["Cookie"] = auth.cookie_header

        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            public_html = await self._fetch_text(client, normalized_url, headers=public_headers)
            authed_html = ""
            if auth.enabled:
                authed_html = await self._fetch_text(client, normalized_url, headers=auth_headers)

        parse_html = authed_html or public_html
        title = _extract_title(parse_html, self.spec.import_regex.competition_title_regex)
        slug = _extract_slug(
            normalized_url,
            self.spec.import_regex.competition_slug_regex,
            fallback=title or self.spec.label,
        )
        competition_dir = Path(root).resolve() / slug
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

        warnings: list[str] = []
        challenge_entries: list[dict[str, Any]] = []
        source_defaults = _source_defaults(self.spec)
        profile_ref = str(automation_profile_path(competition_dir))
        remote_profile = {
            "version": 1,
            "platform": self.spec.platform,
            "platform_label": self.spec.label,
            "competition_url": normalized_url,
            "runtime_mode": source_defaults.get("runtime_mode", ""),
            "capabilities": source_defaults.get("capabilities", {}),
            "mode": "import_only",
            "challenge_hints": [],
            "spec_path": str(self.spec.path) if self.spec.path else "",
        }
        if self.spec.import_regex.challenge_regex:
            self._import_challenges(
                competition_dir=competition_dir,
                cache_dir=cache_dir,
                competition_url=normalized_url,
                competition_slug=slug,
                competition_title=title or slug,
                html=parse_html,
                auth=auth,
                challenge_entries=challenge_entries,
                profile_ref=profile_ref,
                remote_profile=remote_profile,
            )
            if not challenge_entries:
                warnings.append(
                    f"{self.spec.label} spec matched the page but did not extract any challenges."
                )
        else:
            warnings.append(
                f"{self.spec.label} spec does not define import.challenge_regex; imported competition metadata only."
            )

        imported_at = datetime.now(UTC).isoformat()
        auth_payload = _auth_payload(auth)
        write_automation_profile(competition_dir, remote_profile)
        manifest = {
            "platform": self.spec.platform,
            "platform_label": source_defaults.get("platform_label", self.spec.label),
            "capabilities": source_defaults.get("capabilities", {}),
            "runtime_mode": source_defaults.get("runtime_mode", ""),
            "source_url": normalized_url,
            "title": title or slug,
            "competition_slug": slug,
            "imported_at": imported_at,
            "auth_mode": auth.mode,
            "challenge_entries": challenge_entries,
            "spec_path": str(self.spec.path) if self.spec.path else "",
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
            platform=self.spec.platform,
            competition_dir=competition_dir,
            title=title or slug,
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

    def _import_challenges(
        self,
        *,
        competition_dir: Path,
        cache_dir: Path,
        competition_url: str,
        competition_slug: str,
        competition_title: str,
        html: str,
        auth: ImportAuth,
        challenge_entries: list[dict[str, Any]],
        profile_ref: str,
        remote_profile: dict[str, Any],
    ) -> None:
        matcher = compile_platform_regex(self.spec.import_regex.challenge_regex)
        if matcher is None:
            return
        source_defaults = _source_defaults(self.spec)
        auth_payload = _auth_payload(auth)
        for index, match in enumerate(matcher.finditer(html), start=1):
            groups = {key: _clean_text(value) for key, value in match.groupdict().items()}
            raw_name = groups.get("name") or groups.get("title") or f"challenge-{index}"
            name = raw_name.strip() or f"challenge-{index}"
            challenge_slug = _slugify(name)
            challenge_dir = competition_dir / challenge_slug
            challenge_dir.mkdir(parents=True, exist_ok=True)

            description = groups.get("description", "")
            category = groups.get("category", "")
            challenge_url = urljoin(competition_url, groups.get("challenge_url", "") or "")
            connection = infer_connection(description, groups.get("connection_info", ""))
            connection_info = render_connection_info(connection, fallback=groups.get("connection_info", ""))
            flag_format, flag_regex = infer_flag_guard_from_texts(description, groups.get("flag_format", ""))
            source_payload = {
                **source_defaults,
                "competition": {
                    "slug": competition_slug,
                    "title": competition_title,
                    "url": competition_url,
                },
                "challenge_url": challenge_url,
                "status": {
                    "solved": groups.get("solved", "").lower() in {"true", "yes", "1", "solved"},
                    "writeup_submitted": groups.get("writeup_submitted", "").lower()
                    in {"true", "yes", "1", "submitted"},
                },
                "spec_path": str(self.spec.path) if self.spec.path else "",
                "remote": {
                    "runtime_mode": source_defaults.get("runtime_mode", ""),
                    "profile_ref": profile_ref,
                    "capabilities": source_defaults.get("capabilities", {}),
                },
            }
            if auth_payload:
                source_payload["auth"] = auth_payload
            metadata: dict[str, Any] = {
                "name": name,
                "category": category,
                "description": description,
                "value": _int_value(groups.get("value", "0")),
                "tags": [category] if category else [],
                "connection_info": connection_info,
                "solves": _int_value(groups.get("solves", "0")),
                "flag_format": flag_format,
                "flag_regex": flag_regex,
                "source": source_payload,
            }
            if connection:
                metadata["connection"] = connection
            (challenge_dir / "metadata.yml").write_text(
                yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            cache_name = f"challenge-{challenge_slug}.json"
            (cache_dir / cache_name).write_text(
                json.dumps(groups, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            challenge_entries.append(
                {
                    "name": name,
                    "slug": challenge_slug,
                    "path": challenge_slug,
                    "challenge_url": challenge_url,
                    "solved": source_payload["status"]["solved"],
                    "writeup_submitted": source_payload["status"]["writeup_submitted"],
                    "remote_attached": source_defaults.get("runtime_mode", "") == "full_remote",
                }
            )
            remote_profile["challenge_hints"].append(
                {
                    "name": name,
                    "category": category,
                    "challenge_url": challenge_url,
                }
            )
