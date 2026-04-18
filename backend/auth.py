"""Home-directory auth discovery and validation helpers."""

from __future__ import annotations

import base64
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.config import Settings


class AuthValidationError(RuntimeError):
    """Raised when a required auth source is missing or invalid."""


@dataclass(frozen=True)
class HomeAuthPaths:
    codex: Path
    claude: Path
    gemini: Path


@dataclass(frozen=True)
class GeminiOAuth:
    access_token: str
    refresh_token: str | None
    token_type: str
    expiry_date_ms: int | None

    @property
    def expires_at_epoch_seconds(self) -> float | None:
        if self.expiry_date_ms is None:
            return None
        return self.expiry_date_ms / 1000.0

    @property
    def is_expired(self) -> bool:
        expires_at = self.expires_at_epoch_seconds
        if expires_at is None:
            return False
        return expires_at <= time.time()


def resolve_home_auth_paths(settings: Settings) -> HomeAuthPaths:
    """Resolve all home-auth files, honoring path overrides."""
    home = Path.home()
    codex_path = Path(settings.codex_auth_path).expanduser() if settings.codex_auth_path else home / ".codex" / "auth.json"
    claude_path = Path(settings.claude_auth_path).expanduser() if settings.claude_auth_path else home / ".claude" / ".credentials.json"
    gemini_path = Path(settings.gemini_auth_path).expanduser() if settings.gemini_auth_path else home / ".gemini" / "oauth_creds.json"
    return HomeAuthPaths(codex=codex_path, claude=claude_path, gemini=gemini_path)


def validate_codex_auth(settings: Settings) -> None:
    """Validate Codex CLI availability plus auth file structure."""
    if shutil.which("codex") is None:
        raise AuthValidationError("`codex` CLI is required but was not found on PATH.")

    path = resolve_home_auth_paths(settings).codex
    data = _load_json_file(path, "Codex")
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        raise AuthValidationError(f"Codex auth file is missing `tokens`: {path}")
    if not isinstance(tokens.get("access_token"), str) and not isinstance(tokens.get("refresh_token"), str):
        raise AuthValidationError(
            f"Codex auth file does not contain an access or refresh token: {path}"
        )


def validate_claude_auth(settings: Settings) -> None:
    """Validate Claude CLI availability plus credentials file structure."""
    if shutil.which("claude") is None:
        raise AuthValidationError("`claude` CLI is required but was not found on PATH.")

    path = resolve_home_auth_paths(settings).claude
    data = _load_json_file(path, "Claude")
    claude_oauth = data.get("claudeAiOauth")
    if not isinstance(claude_oauth, dict):
        raise AuthValidationError(f"Claude credentials file is missing `claudeAiOauth`: {path}")
    if not isinstance(claude_oauth.get("accessToken"), str):
        raise AuthValidationError(
            f"Claude credentials file does not contain an access token: {path}"
        )


def load_gemini_oauth(settings: Settings) -> GeminiOAuth:
    """Load Gemini CLI OAuth credentials from disk."""
    path = resolve_home_auth_paths(settings).gemini
    data = _load_json_file(path, "Gemini")

    access_token = data.get("access_token")
    if not isinstance(access_token, str):
        raise AuthValidationError(f"Gemini credentials file is missing `access_token`: {path}")

    refresh_token = data.get("refresh_token")
    if refresh_token is not None and not isinstance(refresh_token, str):
        raise AuthValidationError(f"Gemini credentials file has an invalid `refresh_token`: {path}")

    token_type = data.get("token_type", "Bearer")
    if not isinstance(token_type, str):
        token_type = "Bearer"

    expiry_date = data.get("expiry_date")
    expiry_date_ms = expiry_date if isinstance(expiry_date, int) else None

    return GeminiOAuth(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type=token_type,
        expiry_date_ms=expiry_date_ms,
    )


def refresh_gemini_oauth(settings: Settings, oauth: GeminiOAuth | None = None) -> GeminiOAuth:
    """Return Gemini CLI OAuth data without embedding refresh credentials in the repo."""
    oauth = oauth or load_gemini_oauth(settings)
    if not oauth.is_expired or oauth.refresh_token:
        return oauth
    raise AuthValidationError(
        "Gemini OAuth token is expired and no refresh token is available. Re-run `gemini` login."
    )


def validate_gemini_auth(settings: Settings) -> GeminiOAuth:
    """Validate Gemini CLI availability plus OAuth file structure."""
    if shutil.which("gemini") is None:
        raise AuthValidationError("`gemini` CLI is required but was not found on PATH.")
    return refresh_gemini_oauth(settings)


def validate_required_auth(
    settings: Settings,
    *,
    needs_codex: bool = False,
    needs_claude: bool = False,
    needs_gemini: bool = False,
) -> dict[str, Any]:
    """Validate all auth sources needed for the current run."""
    validated: dict[str, Any] = {}
    if needs_codex:
        validate_codex_auth(settings)
        validated["codex"] = True
    if needs_claude:
        validate_claude_auth(settings)
        validated["claude"] = True
    if needs_gemini:
        validated["gemini"] = validate_gemini_auth(settings)
    return validated


def _load_json_file(path: Path, label: str) -> dict[str, Any]:
    if not path.exists():
        raise AuthValidationError(f"{label} auth file not found: {path}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise AuthValidationError(f"{label} auth file is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise AuthValidationError(f"{label} auth file must contain a JSON object: {path}")
    return data


def jwt_expiry_timestamp(token: str) -> int | None:
    """Decode a JWT expiry timestamp without verifying the signature."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(decoded)
    except Exception:
        return None
    exp = claims.get("exp")
    return exp if isinstance(exp, int) else None
