"""Shared lane-runtime control-plane helpers."""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.sandbox import (
    AUTH_SEED_CONTAINER_ROOT,
    CHALLENGE_SRC_CONTAINER_ROOT,
    CONTROL_CONTAINER_ROOT,
    PROVIDER_HOME_CONTAINER_ROOT,
    REPO_CONTAINER_ROOT,
    TRACE_CONTAINER_ROOT,
    resolve_shared_artifacts_dir,
)

WORKSPACE_CONTAINER_ROOT = "/challenge/workspace"
SHARED_ARTIFACTS_CONTAINER_ROOT = "/challenge/shared-artifacts"

COMMANDS_FILENAME = "commands.jsonl"
EVENTS_FILENAME = "events.jsonl"
CONFIG_FILENAME = "config.json"
STATE_DIRNAME = "state"
HEARTBEAT_FILENAME = "heartbeat.json"
RESULT_FILENAME = "result.json"
CODEX_THREAD_FILENAME = "codex-thread.json"
RUNTIME_PID_FILENAME = "runtime.pid"

HEARTBEAT_INTERVAL_SECONDS = 5.0
HEARTBEAT_STALE_AFTER_SECONDS = 20.0


def safe_lane_token(model_spec: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in model_spec)


@dataclass(frozen=True)
class LaneHostState:
    root_dir: Path
    workspace_dir: Path
    control_dir: Path
    provider_home_dir: Path
    shared_artifacts_dir: Path
    trace_dir: Path


def ensure_lane_host_state(
    challenge_dir: str | Path,
    model_spec: str,
    *,
    repo_root: str | Path,
) -> LaneHostState:
    challenge_root = Path(challenge_dir).resolve()
    lane_root = challenge_root / ".lane-state" / safe_lane_token(model_spec)
    workspace_dir = lane_root / "workspace"
    control_dir = lane_root / "control"
    provider_home_dir = lane_root / "provider-home"
    shared_artifacts_dir = resolve_shared_artifacts_dir(challenge_root)
    trace_dir = Path(repo_root).resolve() / "logs"

    for path in (workspace_dir, control_dir, provider_home_dir, trace_dir):
        path.mkdir(parents=True, exist_ok=True)
    (control_dir / STATE_DIRNAME).mkdir(parents=True, exist_ok=True)

    return LaneHostState(
        root_dir=lane_root,
        workspace_dir=workspace_dir,
        control_dir=control_dir,
        provider_home_dir=provider_home_dir,
        shared_artifacts_dir=shared_artifacts_dir,
        trace_dir=trace_dir,
    )


@dataclass(frozen=True)
class LaneControlPaths:
    root_dir: Path
    commands_path: Path
    events_path: Path
    config_path: Path
    state_dir: Path
    heartbeat_path: Path
    result_path: Path
    codex_thread_path: Path
    runtime_pid_path: Path


def lane_control_paths(control_dir: str | Path) -> LaneControlPaths:
    root = Path(control_dir)
    state_dir = root / STATE_DIRNAME
    state_dir.mkdir(parents=True, exist_ok=True)
    return LaneControlPaths(
        root_dir=root,
        commands_path=root / COMMANDS_FILENAME,
        events_path=root / EVENTS_FILENAME,
        config_path=root / CONFIG_FILENAME,
        state_dir=state_dir,
        heartbeat_path=state_dir / HEARTBEAT_FILENAME,
        result_path=state_dir / RESULT_FILENAME,
        codex_thread_path=state_dir / CODEX_THREAD_FILENAME,
        runtime_pid_path=state_dir / RUNTIME_PID_FILENAME,
    )


def write_json_atomic(path: str | Path, payload: object, *, mode: int | None = None) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        delete=False,
        dir=target.parent,
    ) as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
        temp_path = Path(fh.name)
    if mode is not None:
        temp_path.chmod(mode)
    temp_path.replace(target)


def read_json(path: str | Path, *, default: object | None = None) -> object:
    target = Path(path)
    if not target.exists():
        return {} if default is None else default
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {} if default is None else default


def append_jsonl(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=True) + "\n")


def read_new_jsonl(path: str | Path, *, offset: int = 0) -> tuple[int, list[dict[str, Any]]]:
    target = Path(path)
    if not target.exists():
        return offset, []

    events: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append({str(key): value for key, value in payload.items()})
        offset = fh.tell()
    return offset, events


def runtime_session_payload(provider: str, *, thread_id: str = "", session_id: str = "") -> dict[str, str]:
    if provider == "codex":
        return {"kind": "codex-thread", "id": thread_id}
    if provider in {"gemini", "google"}:
        return {"kind": "gemini-session", "id": session_id}
    return {"kind": provider or "unknown", "id": ""}


def heartbeat_payload(
    *,
    provider: str,
    model_spec: str,
    runtime_status: dict[str, object],
    last_event: str = "",
    reset_counts: dict[str, int] | None = None,
    last_reset_reason: str = "",
    thread_id: str = "",
    session_id: str = "",
) -> dict[str, object]:
    now = time.time()
    lifecycle = str(runtime_status.get("lifecycle") or "starting")
    current_command = str(runtime_status.get("current_command") or "").strip()
    last_command = str(runtime_status.get("last_command") or "").strip()
    activity = current_command or last_command or str(runtime_status.get("last_exit_hint") or "").strip()
    return {
        "ts": now,
        "provider": provider,
        "model_spec": model_spec,
        "lifecycle": lifecycle,
        "runtime_health": "healthy",
        "step_count": int(runtime_status.get("step_count", 0) or 0),
        "activity": activity,
        "last_event": last_event,
        "last_exit_hint": str(runtime_status.get("last_exit_hint") or "").strip(),
        "session": runtime_session_payload(
            provider,
            thread_id=thread_id,
            session_id=session_id,
        ),
        "reset_counts": dict(reset_counts or {}),
        "last_reset_reason": last_reset_reason,
        "raw_runtime": runtime_status,
    }


def runtime_env(repo_root: str) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", repo_root)
    env["CTF_AGENT_LOG_DIR"] = TRACE_CONTAINER_ROOT
    return env


def build_runtime_config(
    *,
    model_spec: str,
    provider: str,
    challenge_dir_host: str,
    meta: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any]:
    return {
        "model_spec": model_spec,
        "provider": provider,
        "challenge_dir_host": challenge_dir_host,
        "challenge_dir": CHALLENGE_SRC_CONTAINER_ROOT,
        "workspace_dir": WORKSPACE_CONTAINER_ROOT,
        "shared_artifacts_dir": SHARED_ARTIFACTS_CONTAINER_ROOT,
        "control_dir": CONTROL_CONTAINER_ROOT,
        "provider_home_dir": PROVIDER_HOME_CONTAINER_ROOT,
        "repo_root_dir": REPO_CONTAINER_ROOT,
        "trace_dir": TRACE_CONTAINER_ROOT,
        "auth_seed_dir": AUTH_SEED_CONTAINER_ROOT,
        "meta": meta,
        "settings": settings,
    }
