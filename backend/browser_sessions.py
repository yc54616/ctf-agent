"""Browser-backed session persistence and cookie extraction helpers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import click

if TYPE_CHECKING:
    from backend.importers.base import ImportAuth

_SESSION_DIRNAME = "browser-sessions"


def _repo_cache_root() -> Path:
    return (Path(__file__).resolve().parents[1] / ".cache").resolve()


def browser_session_root() -> Path:
    path = _repo_cache_root() / _SESSION_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _session_slug(url: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    hostname = re.sub(r"[^0-9A-Za-z._-]+", "_", str(parsed.hostname or "").strip().lower())
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    scheme = re.sub(r"[^0-9A-Za-z]+", "_", str(parsed.scheme or "https").lower())
    stem = "_".join(part for part in (scheme, hostname, str(port)) if part)
    return stem or "session"


def default_session_path(url: str) -> Path:
    return browser_session_root() / f"{_session_slug(url)}.json"


def resolve_session_ref(session_ref: str | None, *, url: str = "") -> Path:
    normalized = str(session_ref or "").strip()
    if normalized:
        candidate = Path(normalized).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        return (browser_session_root() / candidate).resolve()
    return default_session_path(url)


def _load_storage_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _hostname_matches(cookie_domain: str, hostname: str) -> bool:
    normalized_domain = str(cookie_domain or "").strip().lstrip(".").lower()
    normalized_host = str(hostname or "").strip().lower()
    if not normalized_domain or not normalized_host:
        return False
    return normalized_host == normalized_domain or normalized_host.endswith(f".{normalized_domain}")


def load_cookie_header_from_session_ref(session_ref: str | None, *, url: str) -> tuple[str, str]:
    path = resolve_session_ref(session_ref, url=url)
    payload = _load_storage_state(path)
    cookies = payload.get("cookies")
    if not isinstance(cookies, list):
        return "", str(path)
    hostname = str(urlsplit(url).hostname or "").strip().lower()
    pairs: list[str] = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        if hostname and not _hostname_matches(str(cookie.get("domain") or ""), hostname):
            continue
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "").strip()
        if name:
            pairs.append(f"{name}={value}")
    return "; ".join(pairs), str(path)


async def ensure_playwright_import_auth(
    url: str,
    *,
    session_ref: str | None = None,
) -> ImportAuth:
    from backend.importers.base import ImportAuth

    session_path = resolve_session_ref(session_ref, url=url)
    cookie_header, resolved_path = load_cookie_header_from_session_ref(str(session_path), url=url)
    if cookie_header:
        return ImportAuth(
            mode="playwright_storage_state",
            session_ref=resolved_path,
            cookie_header=cookie_header,
        )

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise click.ClickException(
            "Playwright is required for auto browser-backed imports. "
            "Install dependencies with `uv sync` and browsers with `uv run playwright install chromium`."
        ) from exc

    click.echo(f"Opening Chromium for login/session capture: {url}")
    click.echo("Complete any required login in the opened browser, then return here and press Enter.")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded")
        click.prompt("Press Enter after the competition page is ready", default="", show_default=False)
        await page.goto(url, wait_until="domcontentloaded")
        await context.storage_state(path=str(session_path))
        await browser.close()

    cookie_header, resolved_path = load_cookie_header_from_session_ref(str(session_path), url=url)
    return ImportAuth(
        mode="playwright_storage_state",
        session_ref=resolved_path,
        cookie_header=cookie_header,
    )
