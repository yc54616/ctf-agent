from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from backend.auth import (
    GeminiOAuth,
    load_gemini_oauth,
    refresh_gemini_oauth,
    resolve_home_auth_paths,
    validate_claude_auth,
    validate_codex_auth,
)
from backend.config import Settings


def test_resolve_home_auth_paths_uses_overrides() -> None:
    settings = Settings(
        codex_auth_path="~/custom/codex.json",
        claude_auth_path="~/custom/claude.json",
        gemini_auth_path="~/custom/gemini.json",
    )

    paths = resolve_home_auth_paths(settings)

    assert paths.codex == Path("~/custom/codex.json").expanduser()
    assert paths.claude == Path("~/custom/claude.json").expanduser()
    assert paths.gemini == Path("~/custom/gemini.json").expanduser()


def test_validate_codex_auth_accepts_access_or_refresh_token(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "codex-auth.json"
    path.write_text(json.dumps({"tokens": {"access_token": "tok"}}))
    monkeypatch.setattr("backend.auth.shutil.which", lambda _: "/usr/bin/codex")

    settings = Settings(codex_auth_path=str(path))

    validate_codex_auth(settings)


def test_validate_claude_auth_accepts_oauth_token(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "claude.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok"}}))
    monkeypatch.setattr("backend.auth.shutil.which", lambda _: "/usr/bin/claude")

    settings = Settings(claude_auth_path=str(path))

    validate_claude_auth(settings)


def test_load_gemini_oauth_reads_file(tmp_path: Path) -> None:
    path = tmp_path / "gemini.json"
    path.write_text(
        json.dumps(
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "token_type": "Bearer",
                "expiry_date": 123,
            }
        )
    )

    settings = Settings(gemini_auth_path=str(path))
    oauth = load_gemini_oauth(settings)

    assert oauth == GeminiOAuth(
        access_token="access",
        refresh_token="refresh",
        token_type="Bearer",
        expiry_date_ms=123,
    )


def test_refresh_gemini_oauth_keeps_local_refresh_token(tmp_path: Path) -> None:
    path = tmp_path / "gemini.json"
    path.write_text(
        json.dumps(
            {
                "access_token": "stale",
                "refresh_token": "refresh-me",
                "token_type": "Bearer",
                "expiry_date": int((time.time() - 30) * 1000),
            }
        )
    )

    settings = Settings(gemini_auth_path=str(path))
    oauth = refresh_gemini_oauth(settings)

    assert oauth.access_token == "stale"
    assert oauth.refresh_token == "refresh-me"
    assert oauth.token_type == "Bearer"
    assert oauth.expiry_date_ms is not None


def test_refresh_gemini_oauth_rejects_expired_token_without_refresh_token(tmp_path: Path) -> None:
    path = tmp_path / "gemini.json"
    path.write_text(
        json.dumps(
            {
                "access_token": "stale",
                "token_type": "Bearer",
                "expiry_date": int((time.time() - 30) * 1000),
            }
        )
    )

    settings = Settings(gemini_auth_path=str(path))

    with pytest.raises(RuntimeError, match="Re-run `gemini` login"):
        refresh_gemini_oauth(settings)
