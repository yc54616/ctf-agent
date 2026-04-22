"""Saved remote automation profile helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

AUTOMATION_DIRNAME = ".remote"
AUTOMATION_PROFILE_FILENAME = "automation-profile.json"


def competition_remote_dir(competition_dir: str | Path) -> Path:
    return Path(competition_dir).resolve() / AUTOMATION_DIRNAME


def automation_profile_path(competition_dir: str | Path) -> Path:
    return competition_remote_dir(competition_dir) / AUTOMATION_PROFILE_FILENAME


def write_automation_profile(competition_dir: str | Path, profile: dict[str, Any]) -> str:
    remote_dir = competition_remote_dir(competition_dir)
    remote_dir.mkdir(parents=True, exist_ok=True)
    path = automation_profile_path(competition_dir)
    path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def load_automation_profile(profile_ref: str | Path) -> dict[str, Any]:
    path = Path(profile_ref).expanduser().resolve()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
