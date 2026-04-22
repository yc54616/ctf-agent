"""Competition import CLI."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from backend.browser_sessions import ensure_playwright_import_auth
from backend.cookie_file import load_cookie_header
from backend.importers.base import ImportAuth, PlatformImporter
from backend.importers.registry import pick_importer_for_url


def _load_cookie_header(cookie_file: str | None) -> ImportAuth:
    cookie_header, cookie_path = load_cookie_header(cookie_file)
    if cookie_header:
        return ImportAuth(mode="cookie_file", cookie_header=cookie_header, cookie_file=cookie_path)
    return ImportAuth()


async def _load_import_auth(url: str, cookie_file: str | None) -> ImportAuth:
    file_auth = _load_cookie_header(cookie_file)
    if file_auth.enabled:
        return file_auth
    return await ensure_playwright_import_auth(url)


def _pick_importer(url: str, platform_specs: tuple[str, ...] = ()) -> PlatformImporter:
    importer = pick_importer_for_url(url, spec_paths=tuple(Path(path) for path in platform_specs))
    if importer is None:
        raise click.ClickException(f"No platform importer is available for URL: {url}")
    return importer


@click.command(name="ctf-import")
@click.option("--url", "source_url", required=True, help="Competition URL to import")
@click.option("--root", default="challenges", show_default=True, help="Directory under which competition folders will be created")
@click.option(
    "--cookie-file",
    default=None,
    help="Advanced/compatibility only: file containing a raw HTTP Cookie header value",
)
@click.option(
    "--platform-spec",
    "platform_specs",
    multiple=True,
    help="Advanced/compatibility only: additional JSON/YAML platform spec file or directory",
)
@click.option("--refresh", is_flag=True, help="Refresh source metadata in an existing competition folder")
def main(
    source_url: str,
    root: str,
    cookie_file: str | None,
    platform_specs: tuple[str, ...],
    refresh: bool,
) -> None:
    importer = _pick_importer(source_url, platform_specs)
    try:
        auth = asyncio.run(_load_import_auth(source_url, cookie_file))
        result = asyncio.run(importer.import_competition(source_url, auth, root, refresh=refresh))
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"platform: {result.platform}")
    click.echo(f"title: {result.title}")
    click.echo(f"competition_dir: {result.competition_dir}")
    click.echo(f"auth_mode: {result.auth_mode}")
    click.echo(f"challenges: {len(result.challenge_entries)}")
    for warning in result.warnings:
        click.echo(f"warning: {warning}")
