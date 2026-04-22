"""Remote platform client selection for coordinator runs."""

from __future__ import annotations

import logging
from typing import Any

import click

from backend.automation_profile import load_automation_profile
from backend.config import Settings
from backend.cookie_file import load_cookie_header
from backend.platforms.base import CompositePlatformClient, NullPlatformClient, PlatformClient
from backend.platforms.browser import BrowserPlatformClient
from backend.platforms.catalog import build_registered_platform_client

logger = logging.getLogger(__name__)


def _dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _ctfd_settings_explicit(settings: Settings) -> bool:
    defaults = Settings()
    return any(
        [
            str(settings.ctfd_token or "").strip(),
            str(settings.ctfd_url or "").strip() != str(defaults.ctfd_url or "").strip(),
            str(settings.ctfd_user or "").strip() != str(defaults.ctfd_user or "").strip(),
            str(settings.ctfd_pass or "").strip() != str(defaults.ctfd_pass or "").strip(),
        ]
    )


def _resolve_source_cookie_header(
    source: dict[str, Any],
    *,
    default_cookie_header: str,
    cookie_cache: dict[str, str],
) -> str:
    normalized_default = str(default_cookie_header or "").strip()
    if normalized_default:
        return normalized_default
    auth = _dict(source.get("auth"))
    cookie_file = str(auth.get("cookie_file") or "").strip()
    if not cookie_file:
        return ""
    if cookie_file in cookie_cache:
        return cookie_cache[cookie_file]
    try:
        cookie_header, _cookie_path = load_cookie_header(cookie_file)
    except click.ClickException as exc:
        logger.warning(
            "Could not reload stored cookie file for imported platform %r: %s",
            source.get("platform"),
            exc,
        )
        cookie_cache[cookie_file] = ""
        return ""
    cookie_cache[cookie_file] = cookie_header
    return cookie_header


def _build_browser_profile_client(
    challenge_name: str,
    raw_meta: object,
    source: dict[str, Any],
) -> PlatformClient | None:
    auth = _dict(source.get("auth"))
    remote = _dict(source.get("remote"))
    session_ref = str(auth.get("session_ref") or "").strip()
    profile_ref = str(remote.get("profile_ref") or "").strip()
    if not session_ref or not profile_ref:
        return None
    profile = load_automation_profile(profile_ref)
    runtime_mode = str(remote.get("runtime_mode") or profile.get("runtime_mode") or "").strip().lower()
    if runtime_mode not in {"full_remote", "operator_only"}:
        return None
    initial_hint = {
        "name": str(challenge_name or "").strip(),
        "category": str(getattr(raw_meta, "category", "") or "").strip(),
        "value": int(getattr(raw_meta, "value", 0) or 0),
        "solves": int(getattr(raw_meta, "solves", 0) or 0),
        "challenge_id": source.get("challenge_id"),
    }
    return BrowserPlatformClient(
        platform=str(source.get("platform") or profile.get("platform") or "browser").strip(),
        label=str(source.get("platform_label") or profile.get("platform_label") or "browser platform").strip(),
        competition_url=str(
            _dict(source.get("competition")).get("url")
            or profile.get("competition_url")
            or source.get("challenge_url")
            or ""
        ).strip(),
        session_ref=session_ref,
        profile_ref=profile_ref,
        challenge_hints=[initial_hint],
    )


def build_platform_client(
    settings: Settings,
    challenge_metas: dict[str, object],
    *,
    local_mode: bool = False,
    cookie_header: str = "",
) -> PlatformClient:
    if local_mode:
        return NullPlatformClient()
    cookie_header = str(cookie_header or settings.remote_cookie_header or "").strip()
    cookie_cache: dict[str, str] = {}

    clients: dict[str, PlatformClient] = {}
    routes: dict[str, str] = {}
    imported_platform_present = False

    for challenge_name, raw_meta in challenge_metas.items():
        source = _dict(getattr(raw_meta, "source", {}))
        platform = str(source.get("platform") or "").strip().lower()
        if not platform or platform == "ctfd":
            continue
        imported_platform_present = True
        browser_client = _build_browser_profile_client(challenge_name, raw_meta, source)
        if browser_client is not None:
            client_key = f"{platform}:profile:{str(_dict(source.get('remote')).get('profile_ref') or '').strip()}"
            existing_client = clients.get(client_key)
            if isinstance(existing_client, BrowserPlatformClient):
                existing_client.challenge_hints.extend(
                    list(getattr(browser_client, "challenge_hints", []))
                )
            elif client_key not in clients:
                clients[client_key] = browser_client
            routes[str(challenge_name)] = client_key
            continue
        source_cookie_header = _resolve_source_cookie_header(
            source,
            default_cookie_header=cookie_header,
            cookie_cache=cookie_cache,
        )
        client = build_registered_platform_client(
            source,
            settings,
            cookie_header=source_cookie_header,
        )
        if client is None:
            continue

        competition = _dict(source.get("competition"))
        client_key_parts = [platform]
        competition_slug = str(competition.get("slug") or "").strip()
        applicant_id = str(source.get("applicant_id") or "").strip()
        if competition_slug:
            client_key_parts.append(competition_slug)
        if applicant_id:
            client_key_parts.append(applicant_id)
        client_key = ":".join(client_key_parts)
        if client_key not in clients:
            clients[client_key] = client
        routes[str(challenge_name)] = client_key

    if _ctfd_settings_explicit(settings) or not imported_platform_present:
        ctfd_client = build_registered_platform_client(
            {"platform": "ctfd"},
            settings,
            cookie_header=cookie_header,
        )
        if ctfd_client is not None:
            clients["ctfd"] = ctfd_client
        for challenge_name, raw_meta in challenge_metas.items():
            source = _dict(getattr(raw_meta, "source", {}))
            platform = str(source.get("platform") or "").strip().lower()
            if not platform or platform == "ctfd":
                routes.setdefault(str(challenge_name), "ctfd")

    if not clients:
        return NullPlatformClient()
    if len(clients) == 1 and not routes:
        return next(iter(clients.values()))
    if len(clients) == 1:
        client = next(iter(clients.values()))
        composite = CompositePlatformClient(clients={"default": client}, challenge_routes={})
        for challenge_name in routes:
            composite.register_challenge_route(challenge_name, "default")
        return composite
    return CompositePlatformClient(clients=clients, challenge_routes=routes)
