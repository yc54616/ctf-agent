"""Pydantic Settings — credentials from .env file + environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # CTFd
    ctfd_url: str = "http://localhost:8000"
    ctfd_user: str = "admin"
    ctfd_pass: str = "admin"
    ctfd_token: str = ""

    # Home auth discovery
    use_home_auth: bool = True
    codex_auth_path: str = ""
    claude_auth_path: str = ""
    gemini_auth_path: str = ""

    # Infra
    sandbox_image: str = "ctf-sandbox"
    max_concurrent_challenges: int = 10
    max_attempts_per_challenge: int = 3
    container_memory_limit: str = "4g"
    exec_output_spill_threshold_bytes: int = 65_536
    read_file_spill_threshold_bytes: int = 262_144
    artifact_preview_bytes: int = 8_192

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
