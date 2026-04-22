"""Shared raw Cookie header file loader."""

from __future__ import annotations

from pathlib import Path

import click


def load_cookie_header(cookie_file: str | None) -> tuple[str, str]:
    if not cookie_file:
        return "", ""
    path = Path(cookie_file).expanduser().resolve()
    if not path.exists():
        raise click.ClickException(f"Cookie file does not exist: {path}")
    raw_lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not raw_lines:
        raise click.ClickException(f"Cookie file is empty: {path}")
    if len(raw_lines) == 1 and ":" in raw_lines[0] and raw_lines[0].lower().startswith("cookie:"):
        cookie_header = raw_lines[0].split(":", 1)[1].strip()
    elif len(raw_lines) == 1 and ";" in raw_lines[0]:
        cookie_header = raw_lines[0]
    else:
        cookie_header = "; ".join(raw_lines)
    if not cookie_header:
        raise click.ClickException(f"Cookie file did not contain a valid Cookie header: {path}")
    return cookie_header, str(path)
