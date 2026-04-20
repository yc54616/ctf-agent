"""Browser operator console assets and trace helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.models import model_id_from_spec
from backend.tracing import _sanitize

STATIC_DIR = Path(__file__).resolve().parent / "static"
TRACE_LIMIT_DEFAULT = 200
TRACE_LIMIT_MAX = 1000
ADVISORY_HISTORY_LIMIT_DEFAULT = 12
_UI_ASSETS = {
    "operator_ui.html": "text/html; charset=utf-8",
    "operator_ui.css": "text/css; charset=utf-8",
    "operator_ui.js": "application/javascript; charset=utf-8",
}


def load_ui_asset(name: str) -> tuple[str, str]:
    """Return the content type and text for a built-in UI asset."""
    if name not in _UI_ASSETS:
        raise FileNotFoundError(name)
    asset_path = STATIC_DIR / name
    return _UI_ASSETS[name], asset_path.read_text(encoding="utf-8")


def _trace_glob(challenge_name: str, model_spec: str) -> str:
    model_id = model_id_from_spec(model_spec)
    return f"trace-{_sanitize(challenge_name)}-{_sanitize(model_id)}-*.jsonl"


def _trace_model_id(trace_path: Path, challenge_name: str) -> str | None:
    prefix = f"trace-{_sanitize(challenge_name)}-"
    if not trace_path.name.startswith(prefix):
        return None
    tail = trace_path.name[len(prefix):]
    parts = tail.rsplit("-", 2)
    if len(parts) != 3:
        return None
    return parts[0]


def list_challenge_trace_files(
    challenge_name: str,
    *,
    log_dir: str | Path = "logs",
) -> list[Path]:
    """List all trace files for a challenge newest-first."""
    root = Path(log_dir)
    if not root.exists():
        return []
    return sorted(
        root.glob(f"trace-{_sanitize(challenge_name)}-*.jsonl"),
        key=lambda path: path.name,
        reverse=True,
    )


def list_trace_files(
    challenge_name: str,
    model_spec: str,
    *,
    log_dir: str | Path = "logs",
) -> list[Path]:
    """List matching trace files newest-first for a challenge lane."""
    root = Path(log_dir)
    if not root.exists():
        return []
    expected_model_id = _sanitize(model_id_from_spec(model_spec))
    return sorted(
        (
            path
            for path in list_challenge_trace_files(challenge_name, log_dir=log_dir)
            if _trace_model_id(path, challenge_name) == expected_model_id
        ),
        key=lambda path: path.name,
        reverse=True,
    )


def read_trace_window(
    challenge_name: str,
    model_spec: str,
    trace_name: str,
    *,
    cursor: int | None = None,
    limit: int = TRACE_LIMIT_DEFAULT,
    log_dir: str | Path = "logs",
) -> dict[str, Any]:
    """Read a validated JSONL trace window."""
    clamped_limit = max(1, min(int(limit), TRACE_LIMIT_MAX))
    candidates = {path.name: path for path in list_trace_files(challenge_name, model_spec, log_dir=log_dir)}
    trace_path = candidates.get(trace_name)
    if trace_path is None:
        raise FileNotFoundError(trace_name)

    events: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(trace_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        event: dict[str, Any]
        try:
            parsed_event = json.loads(line)
        except json.JSONDecodeError:
            event = {"type": "invalid_json", "raw": line}
        else:
            if isinstance(parsed_event, dict):
                event = {str(key): value for key, value in parsed_event.items()}
            else:
                event = {"type": "invalid_event", "raw": parsed_event}
        event.setdefault("type", "event")
        event["line_no"] = line_no
        events.append(event)

    total = len(events)
    if total == 0:
        start = 0
        end = 0
    elif cursor is None:
        start = max(0, total - clamped_limit)
        end = total
    else:
        requested = max(0, int(cursor))
        start = requested if requested < total else max(0, total - clamped_limit)
        end = min(total, start + clamped_limit)

    older_cursor = max(0, start - clamped_limit) if start > 0 else None
    newer_cursor = end if end < total else None
    return {
        "trace_name": trace_name,
        "cursor": start,
        "limit": clamped_limit,
        "total_events": total,
        "has_older": start > 0,
        "older_cursor": older_cursor,
        "next_cursor": newer_cursor,
        "eof": end >= total,
        "events": events[start:end],
    }


def collect_advisory_history(
    challenge_name: str,
    *,
    limit: int = ADVISORY_HISTORY_LIMIT_DEFAULT,
    log_dir: str | Path = "logs",
) -> dict[str, Any]:
    """Collect recent lane advisories from structured runtime events plus legacy bump traces."""
    entries: list[dict[str, Any]] = []

    for events_path in _list_runtime_event_files(challenge_name):
        control_dir = events_path.parent
        model_spec, model_id = _load_lane_identity(control_dir)
        for raw_line in events_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "advisory_applied":
                continue
            source = str(event.get("source") or "structured")
            if source not in {"advisor", "auto", "advisory"}:
                continue
            text = str(event.get("insights", "")).strip()
            if not text:
                continue
            entries.append(
                {
                    "ts": float(event.get("ts", 0) or 0),
                    "model_id": model_id,
                    "model_spec": model_spec,
                    "trace_name": events_path.name,
                    "source": source,
                    "preview": " ".join(text.split())[:220],
                    "text": text,
                    "_priority": 0,
                }
            )

    traces = list_challenge_trace_files(challenge_name, log_dir=log_dir)
    entries.extend(_collect_legacy_bump_entries(challenge_name, traces))

    entries.sort(key=lambda item: (-float(item.get("ts", 0) or 0), int(item.get("_priority", 99)), str(item.get("model_id", ""))))
    deduped: list[dict[str, Any]] = []
    dedupe_index: list[tuple[str, str, float]] = []
    for entry in entries:
        model_id = str(entry.get("model_id", ""))
        text = " ".join(str(entry.get("text", "")).split())
        ts = float(entry.get("ts", 0) or 0)
        if not model_id or not text:
            continue
        duplicate = False
        for seen_model, seen_text, seen_ts in dedupe_index:
            if seen_model != model_id or seen_text != text:
                continue
            if abs(seen_ts - ts) <= 60:
                duplicate = True
                break
        if duplicate:
            continue
        dedupe_index.append((model_id, text, ts))
        deduped.append({key: value for key, value in entry.items() if not key.startswith("_")})
    return {
        "challenge_name": challenge_name,
        "entries": deduped[: max(1, int(limit))],
    }


def _collect_legacy_bump_entries(challenge_name: str, traces: list[Path]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for trace_path in traces:
        model_id = _trace_model_id(trace_path, challenge_name)
        if not model_id:
            continue
        for raw_line in trace_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "bump":
                continue
            source = str(event.get("source") or "legacy")
            if source not in {"auto", "advisory"}:
                continue
            insights = str(event.get("insights", "")).strip()
            if not insights:
                continue
            marker = "Private advisor note for this lane:\n"
            text = insights[len(marker):].strip() if insights.startswith(marker) else insights
            if not text:
                continue
            entries.append(
                {
                    "ts": float(event.get("ts", 0) or 0),
                    "model_id": model_id,
                    "model_spec": "",
                    "trace_name": trace_path.name,
                    "source": source,
                    "preview": " ".join(text.split())[:220],
                    "text": text,
                    "_priority": 1,
                }
            )
    return entries


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _lane_event_glob_patterns(challenge_name: str) -> tuple[str, ...]:
    return (
        f"logs/**/{challenge_name}/.lane-state/*/control/events.jsonl",
        f"challenges/**/{challenge_name}/.lane-state/*/control/events.jsonl",
        f"{challenge_name}/.lane-state/*/control/events.jsonl",
    )


def _list_runtime_event_files(challenge_name: str) -> list[Path]:
    root = _repo_root()
    found: dict[Path, None] = {}
    for pattern in _lane_event_glob_patterns(challenge_name):
        for path in root.glob(pattern):
            if path.is_file():
                found[path.resolve()] = None
    return sorted(found.keys())


def _load_lane_identity(control_dir: Path) -> tuple[str, str]:
    config_path = control_dir / "config.json"
    model_spec = ""
    if config_path.exists():
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            model_spec = str(payload.get("model_spec") or "").strip()
    if model_spec:
        return model_spec, _sanitize(model_id_from_spec(model_spec))
    return "", control_dir.parent.name
