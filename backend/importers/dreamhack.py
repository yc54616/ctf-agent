"""Dreamhack competition importer."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml
from markdownify import markdownify as markdownify

from backend.automation_profile import automation_profile_path, write_automation_profile
from backend.challenge_config import infer_connection, render_connection_info
from backend.importers.base import CompetitionImportResult, ImportAuth, PlatformImporter
from backend.platforms.catalog import platform_source_defaults
from backend.platforms.dreamhack import DREAMHACK_API_BASE_URL
from backend.prompts import infer_flag_guard_from_texts

USER_AGENT = "Mozilla/5.0 (compatible; ctf-agent import)"

_COMPETITION_URL_RE = re.compile(
    r"^https?://(?:www\.)?dreamhack\.io/career/competitions/(?P<slug>[A-Za-z0-9_-]+)"
)
_TITLE_RE = re.compile(
    r'<div[^>]*class="[^"]*rms-title[^"]*"[^>]*>\s*(?P<title>.*?)\s*</div>',
    re.S,
)
_STATUS_RE = re.compile(
    r'<div[^>]*class="[^"]*rms-info-actions[^"]*"[^>]*>.*?<button[^>]*>\s*(?P<status>.*?)\s*</button>',
    re.S,
)
_MARKDOWN_BLOCK_RE = re.compile(
    r'<div[^>]*class="[^"]*rms-markdown[^"]*"[^>]*>(?P<html>.*?)</div>',
    re.S,
)
_SPAN_SECTION_RE = re.compile(
    r'<span[^>]*class="[^"]*label[^"]*"[^>]*>\s*(?P<label>[^<]+?)\s*</span>\s*'
    r'<div[^>]*class="[^"]*content[^"]*"[^>]*>(?P<content>.*?)</div>',
    re.S,
)
_DL_SECTION_RE = re.compile(
    r'<dt[^>]*>\s*(?P<label>[^<]+?)\s*</dt>\s*<dd[^>]*>(?P<content>.*?)</dd>',
    re.S,
)
_NUGGET_RE = re.compile(r'(?P<key>starts_at|ends_at|contact_email|invitation_type):"(?P<value>[^"]*)"')
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


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


def _automation_profile(
    *,
    competition: dict[str, Any],
    applicant_id: str,
    challenge_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "version": 1,
        "platform": "dreamhack",
        "platform_label": "Dreamhack",
        "competition_url": competition.get("source_url", ""),
        "api_base_url": DREAMHACK_API_BASE_URL,
        "runtime_mode": "full_remote",
        "mode": "http_json_api",
        "challenge_hints": [
            {
                "name": entry.get("name", ""),
                "challenge_id": entry.get("challenge_id"),
                "challenge_url": entry.get("challenge_url", ""),
            }
            for entry in challenge_entries
        ],
        "poll": {
            "method": "GET",
            "path": f"/v1/career/recruitment-applicants/{applicant_id}/",
            "items_path": "ctf_challenges",
            "name_field": "title",
            "id_field": "id",
            "solved_field": "is_solved",
        },
        "submit": {
            "method": "POST",
            "path_template": "/v1/career/recruitment-ctf-challenges/{challenge_id}/submit/",
            "body": {"flag": "{flag}"},
            "success_path": "is_correct",
        },
    }


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


def _html_fragment_to_markdown(fragment: str) -> str:
    markdown = markdownify(fragment or "", heading_style="ATX")
    cleaned = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    return cleaned


def _label_sections(html: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    for pattern in (_SPAN_SECTION_RE, _DL_SECTION_RE):
        for match in pattern.finditer(html):
            label = _clean_text(match.group("label"))
            content = str(match.group("content") or "").strip()
            if label and label not in sections:
                sections[label] = content
    return sections


def _nugt_data(html: str) -> dict[str, str]:
    nuggets: dict[str, str] = {}
    for match in _NUGGET_RE.finditer(html):
        nuggets[match.group("key")] = match.group("value")
    return nuggets


def _competition_metadata(html: str, url: str) -> dict[str, Any]:
    title_match = _TITLE_RE.search(html)
    title = _clean_text(title_match.group("title")) if title_match else ""
    status_match = _STATUS_RE.search(html)
    sections = _label_sections(html)
    markdown_block = _MARKDOWN_BLOCK_RE.search(html)
    nuggets = _nugt_data(html)
    competition_match = _COMPETITION_URL_RE.match(url)
    competition_slug = competition_match.group("slug") if competition_match else _slugify(title)
    return {
        "slug": competition_slug,
        "title": title or competition_slug,
        "status": _clean_text(status_match.group("status")) if status_match else "",
        "host": _clean_text(sections.get("CTF 개최자", "")),
        "description_markdown": _html_fragment_to_markdown(markdown_block.group("html")) if markdown_block else "",
        "rules_markdown": _html_fragment_to_markdown(sections.get("규칙", "")),
        "flag_format_text": _clean_text(sections.get("플래그 형식", "")),
        "flag_submission_limit_text": _clean_text(sections.get("플래그 제출 제한", "")),
        "starts_at": nuggets.get("starts_at", ""),
        "ends_at": nuggets.get("ends_at", ""),
        "contact_email": nuggets.get("contact_email", ""),
        "source_url": url,
    }


def _competition_status_text(payload: dict[str, Any], *, fallback: str = "") -> str:
    if payload.get("is_ended"):
        return "종료됨"
    if payload.get("is_started") is False:
        return "진행 예정"
    my_applying = _dict(payload.get("my_applying"))
    if my_applying.get("is_ended"):
        return "종료됨"
    return fallback or ("진행중" if payload else "")


def _competition_payload(
    html_payload: dict[str, Any],
    api_payload: dict[str, Any],
    *,
    source_url: str,
) -> dict[str, Any]:
    company = _dict(api_payload.get("company"))
    return {
        "id": api_payload.get("id"),
        "slug": str(api_payload.get("slug") or html_payload.get("slug") or "").strip(),
        "title": str(api_payload.get("title") or html_payload.get("title") or "").strip(),
        "status": _competition_status_text(api_payload, fallback=str(html_payload.get("status") or "").strip()),
        "host": str(company.get("name") or html_payload.get("host") or "").strip(),
        "description_markdown": str(html_payload.get("description_markdown") or "").strip(),
        "rules_markdown": str(html_payload.get("rules_markdown") or "").strip(),
        "flag_format_text": str(html_payload.get("flag_format_text") or "").strip(),
        "flag_submission_limit_text": str(html_payload.get("flag_submission_limit_text") or "").strip(),
        "starts_at": str(api_payload.get("starts_at") or html_payload.get("starts_at") or "").strip(),
        "ends_at": str(api_payload.get("ends_at") or html_payload.get("ends_at") or "").strip(),
        "contact_email": str(api_payload.get("contact_email") or html_payload.get("contact_email") or "").strip(),
        "source_url": source_url,
    }


def _writeup_ids_by_challenge(applicant_payload: dict[str, Any]) -> dict[int, str]:
    writeup_ids: dict[int, str] = {}
    for writeup in applicant_payload.get("writeups", []) or []:
        if not isinstance(writeup, dict):
            continue
        challenge = _dict(writeup.get("challenge"))
        challenge_id = challenge.get("id")
        if isinstance(challenge_id, int):
            writeup_ids[challenge_id] = str(writeup.get("id") or "").strip()
    return writeup_ids


def _challenge_payload(
    challenge: dict[str, Any],
    *,
    competition: dict[str, Any],
    applicant_id: str,
    writeup_ids: dict[int, str],
    auth: ImportAuth,
    profile_ref: str,
) -> dict[str, Any]:
    platform_defaults = platform_source_defaults("dreamhack")
    name = str(challenge.get("title") or "").strip() or f"challenge-{challenge.get('id', '?')}"
    description = str(challenge.get("description") or "").strip()
    tags = [str(tag).strip() for tag in (challenge.get("tags") or []) if str(tag).strip()]
    category = tags[0] if tags else ""
    connection = infer_connection(description)
    flag_format, flag_regex = infer_flag_guard_from_texts(
        description,
        competition.get("flag_format_text", ""),
    )
    challenge_id = challenge.get("id")
    challenge_id_value = challenge_id if isinstance(challenge_id, int) else None
    challenge_anchor = (
        f"{competition['source_url']}#challenge-{challenge_id_value}"
        if challenge_id_value is not None
        else competition["source_url"]
    )
    source_payload: dict[str, Any] = {
        **platform_defaults,
        "competition": {
            "id": competition.get("id"),
            "slug": competition.get("slug", ""),
            "title": competition.get("title", ""),
            "url": competition.get("source_url", ""),
        },
        "applicant_id": applicant_id,
        "challenge_id": challenge_id_value,
        "challenge_url": challenge_anchor,
        "order": str(challenge.get("order") or "").strip(),
        "public_url": str(challenge.get("public") or "").strip(),
        "needs_vm": bool(challenge.get("needs_vm")),
        "status": {
            "solved": bool(challenge.get("is_solved")),
            "writeup_submitted": challenge_id_value in writeup_ids if challenge_id_value is not None else False,
        },
        "remote": {
            "runtime_mode": platform_defaults.get("runtime_mode", ""),
            "profile_ref": profile_ref,
            "capabilities": platform_defaults.get("capabilities", {}),
        },
    }
    if challenge_id_value is not None and challenge_id_value in writeup_ids:
        source_payload["writeup_id"] = writeup_ids[challenge_id_value]
    auth_payload = _auth_payload(auth)
    if auth_payload:
        source_payload["auth"] = auth_payload

    metadata: dict[str, Any] = {
        "name": name,
        "category": category,
        "description": description,
        "value": 0,
        "tags": tags,
        "connection_info": render_connection_info(connection),
        "solves": 0,
        "flag_format": flag_format,
        "flag_regex": flag_regex,
        "source": source_payload,
    }
    if connection:
        metadata["connection"] = connection
    return metadata


def _competition_manifest_payload(
    *,
    competition: dict[str, Any],
    source_url: str,
    auth_mode: str,
    auth: ImportAuth,
    imported_at: str,
    challenge_entries: list[dict[str, Any]],
    warnings: list[str],
    profile_ref: str,
) -> dict[str, Any]:
    platform_defaults = platform_source_defaults("dreamhack")
    payload = {
        "platform": "dreamhack",
        "platform_label": platform_defaults.get("platform_label", "Dreamhack"),
        "capabilities": platform_defaults.get("capabilities", {}),
        "runtime_mode": platform_defaults.get("runtime_mode", ""),
        "source_url": source_url,
        "title": competition.get("title", ""),
        "competition_slug": competition.get("slug", ""),
        "status": competition.get("status", ""),
        "host": competition.get("host", ""),
        "starts_at": competition.get("starts_at", ""),
        "ends_at": competition.get("ends_at", ""),
        "contact_email": competition.get("contact_email", ""),
        "imported_at": imported_at,
        "auth_mode": auth_mode,
        "challenge_entries": challenge_entries,
        "remote": {
            "runtime_mode": platform_defaults.get("runtime_mode", ""),
            "profile_ref": profile_ref,
        },
    }
    auth_payload = _auth_payload(auth)
    if auth_payload:
        payload["auth"] = auth_payload
    if warnings:
        payload["warnings"] = warnings
    return payload


class DreamhackImporter(PlatformImporter):
    platform = "dreamhack"

    def supports_url(self, url: str) -> bool:
        return _COMPETITION_URL_RE.match(str(url or "").strip()) is not None

    async def import_competition(
        self,
        url: str,
        auth: ImportAuth,
        root: str | Path,
        *,
        refresh: bool = False,
    ) -> CompetitionImportResult:
        match = _COMPETITION_URL_RE.match(url.strip())
        if match is None:
            raise RuntimeError(f"Unsupported Dreamhack competition URL: {url}")

        competition_slug = match.group("slug")
        public_headers = {"User-Agent": USER_AGENT}
        auth_headers = dict(public_headers)
        if auth.enabled:
            auth_headers["Cookie"] = auth.cookie_header

        competition_api_url = f"{DREAMHACK_API_BASE_URL}/v1/career/rms/{competition_slug}/"

        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            public_html = await self._fetch_text(client, url, headers=public_headers)
            public_competition_payload = await self._fetch_json(client, competition_api_url, headers=public_headers)
            competition = _competition_payload(
                _competition_metadata(public_html, url),
                public_competition_payload,
                source_url=url,
            )

            competition_dir = Path(root).resolve() / _slugify(competition["slug"])
            cache_dir = competition_dir / ".source-cache"
            if competition_dir.exists() and not refresh:
                raise RuntimeError(
                    f"{competition_dir} already exists. Re-run with --refresh to update source metadata."
                )

            competition_dir.mkdir(parents=True, exist_ok=True)
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / "competition-public.html").write_text(public_html, encoding="utf-8")
            (cache_dir / "competition-public.json").write_text(
                json.dumps(public_competition_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            warnings: list[str] = []
            challenge_entries: list[dict[str, Any]] = []
            profile_ref = str(automation_profile_path(competition_dir))
            resolved_applicant_id = ""
            if auth.enabled:
                authed_html = await self._fetch_text(client, url, headers=auth_headers)
                (cache_dir / "competition-authenticated.html").write_text(authed_html, encoding="utf-8")
                authed_competition_payload = await self._fetch_json(
                    client,
                    competition_api_url,
                    headers=auth_headers,
                )
                (cache_dir / "competition-authenticated.json").write_text(
                    json.dumps(authed_competition_payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                resolved_applicant_id = str(_dict(authed_competition_payload.get("my_applying")).get("id") or "").strip()
                if resolved_applicant_id:
                    applicant_api_url = (
                        f"{DREAMHACK_API_BASE_URL}/v1/career/recruitment-applicants/{resolved_applicant_id}/"
                    )
                    applicant_payload = await self._fetch_json(client, applicant_api_url, headers=auth_headers)
                    (cache_dir / "applicant-authenticated.json").write_text(
                        json.dumps(applicant_payload, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    await self._import_challenges(
                        client,
                        applicant_payload,
                        competition=competition,
                        auth=auth,
                        profile_ref=profile_ref,
                        competition_dir=competition_dir,
                        cache_dir=cache_dir,
                        headers=auth_headers,
                        challenge_entries=challenge_entries,
                    )
                else:
                    warnings.append("Authenticated competition API response did not include an applicant id.")
            else:
                warnings.append("Cookie auth was not provided; imported competition metadata only.")

            imported_at = datetime.now(UTC).isoformat()
            write_automation_profile(
                competition_dir,
                _automation_profile(
                    competition=competition,
                    applicant_id=resolved_applicant_id,
                    challenge_entries=challenge_entries,
                )
                if resolved_applicant_id
                else {
                    "version": 1,
                    "platform": "dreamhack",
                    "platform_label": "Dreamhack",
                    "competition_url": competition.get("source_url", ""),
                    "runtime_mode": "import_only",
                    "mode": "import_only",
                    "challenge_hints": [],
                },
            )
            manifest = _competition_manifest_payload(
                competition=competition,
                source_url=url,
                auth_mode=auth.mode,
                auth=auth,
                imported_at=imported_at,
                challenge_entries=challenge_entries,
                warnings=warnings,
                profile_ref=profile_ref,
            )
            (competition_dir / "competition.yml").write_text(
                yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            return CompetitionImportResult(
                platform=self.platform,
                competition_dir=competition_dir,
                title=competition["title"],
                source_url=url,
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

    async def _fetch_json(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        payload = response.json()
        return _dict(payload)

    async def _import_challenges(
        self,
        client: httpx.AsyncClient,
        applicant_payload: dict[str, Any],
        *,
        competition: dict[str, Any],
        auth: ImportAuth,
        profile_ref: str,
        competition_dir: Path,
        cache_dir: Path,
        headers: dict[str, str],
        challenge_entries: list[dict[str, Any]],
    ) -> None:
        applicant_id = str(applicant_payload.get("id") or "").strip()
        writeup_ids = _writeup_ids_by_challenge(applicant_payload)
        for challenge in applicant_payload.get("ctf_challenges", []) or []:
            if not isinstance(challenge, dict):
                continue
            metadata = _challenge_payload(
                challenge,
                competition=competition,
                applicant_id=applicant_id,
                writeup_ids=writeup_ids,
                auth=auth,
                profile_ref=profile_ref,
            )
            challenge_slug = _slugify(metadata["name"])
            challenge_dir = competition_dir / challenge_slug
            challenge_dir.mkdir(parents=True, exist_ok=True)
            challenge_id = metadata.get("source", {}).get("challenge_id")
            cache_name = (
                f"challenge-{challenge_slug}.json"
                if challenge_id is None
                else f"challenge-{challenge_slug}-{challenge_id}.json"
            )
            (cache_dir / cache_name).write_text(
                json.dumps(challenge, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            public_url = str(metadata.get("source", {}).get("public_url") or "").strip()
            if public_url:
                await self._download_files(
                    client,
                    [public_url],
                    challenge_dir / "distfiles",
                    headers=headers,
                )
            (challenge_dir / "metadata.yml").write_text(
                yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            challenge_entries.append(
                {
                    "name": metadata["name"],
                    "slug": challenge_slug,
                    "path": challenge_slug,
                    "challenge_url": metadata.get("source", {}).get("challenge_url", ""),
                    "challenge_id": metadata.get("source", {}).get("challenge_id"),
                    "solved": metadata.get("source", {}).get("status", {}).get("solved", False),
                    "writeup_submitted": metadata.get("source", {}).get("status", {}).get("writeup_submitted", False),
                    "remote_attached": True,
                }
            )

    async def _download_files(
        self,
        client: httpx.AsyncClient,
        file_urls: object,
        dist_dir: Path,
        *,
        headers: dict[str, str],
    ) -> None:
        urls = [str(item).strip() for item in file_urls if str(item).strip()] if isinstance(file_urls, list) else []
        if not urls:
            return
        dist_dir.mkdir(parents=True, exist_ok=True)

        async def _download_one(file_url: str) -> None:
            response = await client.get(file_url, headers=headers)
            response.raise_for_status()
            filename = Path(urlsplit(file_url).path).name or _slugify(file_url)
            (dist_dir / filename).write_bytes(response.content)

        await asyncio.gather(*(_download_one(file_url) for file_url in urls))
