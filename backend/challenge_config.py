"""Challenge source/override config helpers."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from backend.platforms.base import RUNTIME_MODE_FULL_REMOTE
from backend.platforms.catalog import normalize_platform_source

METADATA_FILENAME = "metadata.yml"
RUNTIME_DIRNAME = ".runtime"
OVERRIDE_FILENAME = "override.json"
EFFECTIVE_METADATA_FILENAME = "effective-metadata.yml"

_SKIP_DISCOVERY_PARTS = {
    ".lane-state",
    ".runtime",
    ".shared-artifacts",
    "distfiles",
    "solve",
    "provider-home",
}
_OVERRIDE_FIELDS = {"connection", "priority", "no_submit", "notes", "needs_instance"}
_CONNECTION_FIELDS = {"scheme", "host", "port", "url", "raw_command"}
_URL_RE = re.compile(r"(?P<url>https?://[^\s<>'\"`]+)")
_NC_RE = re.compile(
    r"(?P<command>\b(?:nc|ncat)\s+(?:-[^\s]+\s+)*(?P<host>[A-Za-z0-9._:-]+)\s+(?P<port>\d{1,5}))"
)
_HOST_LINE_RE = re.compile(r"(?im)^\s*host\s*:\s*(?P<host>[^\s]+)\s*$")
_PORT_LINE_RE = re.compile(r"(?im)^\s*port\s*:\s*(?P<port>\d{1,5})\b")


def _dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(value)


def challenge_metadata_path(challenge_dir: str | Path) -> Path:
    return Path(challenge_dir).resolve() / METADATA_FILENAME


def challenge_runtime_dir(challenge_dir: str | Path) -> Path:
    return Path(challenge_dir).resolve() / RUNTIME_DIRNAME


def challenge_override_path(challenge_dir: str | Path) -> Path:
    return challenge_runtime_dir(challenge_dir) / OVERRIDE_FILENAME


def challenge_effective_metadata_path(challenge_dir: str | Path) -> Path:
    return challenge_runtime_dir(challenge_dir) / EFFECTIVE_METADATA_FILENAME


def discover_challenge_dirs(root: str | Path) -> list[Path]:
    base = Path(root).resolve()
    if (base / METADATA_FILENAME).exists():
        return [base]
    if not base.exists():
        return []

    discovered: list[Path] = []
    seen: set[Path] = set()
    for metadata_path in sorted(base.rglob(METADATA_FILENAME)):
        if any(part in _SKIP_DISCOVERY_PARTS for part in metadata_path.parts):
            continue
        challenge_dir = metadata_path.parent.resolve()
        if challenge_dir in seen:
            continue
        seen.add(challenge_dir)
        discovered.append(challenge_dir)
    return discovered


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return _dict(data)


def load_source_metadata(challenge_dir: str | Path) -> dict[str, Any]:
    return _read_yaml(challenge_metadata_path(challenge_dir))


def load_override(challenge_dir: str | Path) -> dict[str, Any]:
    path = challenge_override_path(challenge_dir)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return sanitize_override(raw)


def _normalize_port(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 < value <= 65535 else None
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"\d{1,5}", text)
    if not match:
        return None
    port = int(match.group(0))
    return port if 0 < port <= 65535 else None


def sanitize_connection(value: object) -> dict[str, Any]:
    connection = _dict(value)
    sanitized: dict[str, Any] = {}
    for key in _CONNECTION_FIELDS:
        raw = connection.get(key)
        if raw is None:
            continue
        if key == "port":
            port = _normalize_port(raw)
            if port is not None:
                sanitized[key] = port
            continue
        text = str(raw).strip()
        if text:
            sanitized[key] = text
    return sanitized


def infer_connection(*texts: object) -> dict[str, Any]:
    connection: dict[str, Any] = {}
    for raw_text in texts:
        text = str(raw_text or "")
        if not text.strip():
            continue

        if not connection.get("url"):
            match = _URL_RE.search(text)
            if match:
                url = match.group("url")
                connection["url"] = url
                parsed = urlsplit(url)
                if parsed.scheme:
                    connection.setdefault("scheme", parsed.scheme)
                if parsed.hostname:
                    connection.setdefault("host", parsed.hostname)
                if parsed.port:
                    connection.setdefault("port", parsed.port)

        if not connection.get("raw_command"):
            match = _NC_RE.search(text)
            if match:
                connection["raw_command"] = match.group("command").strip()
                connection.setdefault("scheme", "tcp")
                connection.setdefault("host", match.group("host").strip())
                connection.setdefault("port", int(match.group("port")))

        if not connection.get("host"):
            match = _HOST_LINE_RE.search(text)
            if match:
                connection["host"] = match.group("host").strip()

        if "port" not in connection:
            match = _PORT_LINE_RE.search(text)
            if match:
                connection["port"] = int(match.group("port"))

    return sanitize_connection(connection)


def render_connection_info(connection: object, *, fallback: str = "") -> str:
    sanitized = sanitize_connection(connection)
    raw_command = str(sanitized.get("raw_command") or "").strip()
    if raw_command:
        return raw_command

    url = str(sanitized.get("url") or "").strip()
    if url:
        return url

    host = str(sanitized.get("host") or "").strip()
    port = sanitized.get("port")
    scheme = str(sanitized.get("scheme") or "").strip().lower()
    if host and isinstance(port, int):
        if scheme in {"http", "https"}:
            return f"{scheme}://{host}:{port}"
        if scheme == "ssh":
            return f"ssh {host} -p {port}"
        return f"nc {host} {port}"
    if host and scheme in {"http", "https"}:
        return f"{scheme}://{host}"
    if host:
        return host
    return str(fallback or "").strip()


def build_source_view(source_meta: dict[str, Any]) -> dict[str, Any]:
    source_view = deepcopy(_dict(source_meta))
    normalized_source = normalize_platform_source(source_view.get("source"))
    if normalized_source:
        source_view["source"] = normalized_source
    else:
        source_view.pop("source", None)
    inferred = infer_connection(
        source_view.get("connection_info", ""),
        source_view.get("description", ""),
    )
    existing_connection = sanitize_connection(source_view.get("connection"))
    merged_connection = {**inferred, **existing_connection}
    if merged_connection:
        source_view["connection"] = merged_connection
    elif "connection" in source_view:
        source_view.pop("connection", None)

    connection_info = str(source_view.get("connection_info") or "").strip()
    if not connection_info:
        rendered = render_connection_info(source_view.get("connection"))
        if rendered:
            source_view["connection_info"] = rendered
    needs_instance = False
    if "needs_instance" in source_view:
        needs_instance = _coerce_bool(source_view.get("needs_instance"))
    elif "needs_instance" in normalized_source:
        needs_instance = _coerce_bool(normalized_source.get("needs_instance"))
    elif "needs_vm" in normalized_source:
        needs_instance = _coerce_bool(normalized_source.get("needs_vm"))
    source_view["needs_instance"] = needs_instance
    return source_view


def sanitize_override(value: object) -> dict[str, Any]:
    raw = _dict(value)
    sanitized: dict[str, Any] = {}
    for key in _OVERRIDE_FIELDS:
        if key not in raw:
            continue
        item = raw.get(key)
        if key == "connection":
            connection = sanitize_connection(item)
            if connection:
                sanitized[key] = connection
            continue
        if key in {"priority", "no_submit", "needs_instance"}:
            sanitized[key] = _coerce_bool(item)
            continue
        text = str(item or "").strip()
        if text:
            sanitized[key] = text
    return sanitized


def _normalize_override_patch(patch: object) -> dict[str, Any]:
    raw = _dict(patch)
    normalized: dict[str, Any] = {}
    for key in _OVERRIDE_FIELDS:
        if key not in raw:
            continue
        item = raw.get(key)
        if item is None:
            normalized[key] = None
            continue
        if key == "connection":
            connection_patch = _dict(item)
            normalized_connection: dict[str, Any] = {}
            for field in _CONNECTION_FIELDS:
                if field not in connection_patch:
                    continue
                field_value = connection_patch.get(field)
                if field_value is None:
                    normalized_connection[field] = None
                    continue
                if field == "port":
                    port = _normalize_port(field_value)
                    if port is not None:
                        normalized_connection[field] = port
                    continue
                text = str(field_value).strip()
                if text:
                    normalized_connection[field] = text
            normalized[key] = normalized_connection
            continue
        if key in {"priority", "no_submit", "needs_instance"}:
            normalized[key] = _coerce_bool(item)
            continue
        normalized[key] = str(item).strip()
    return normalized


def apply_override_patch(existing: object, patch: object) -> dict[str, Any]:
    current = sanitize_override(existing)
    updates = _normalize_override_patch(patch)
    merged = deepcopy(current)
    for key, value in updates.items():
        if key == "connection":
            if value is None:
                merged.pop("connection", None)
                continue
            next_connection = sanitize_connection(merged.get("connection"))
            for field, field_value in _dict(value).items():
                if field_value is None:
                    next_connection.pop(field, None)
                else:
                    next_connection[field] = field_value
            if next_connection:
                merged["connection"] = sanitize_connection(next_connection)
            else:
                merged.pop("connection", None)
            continue
        if value is None or value == "":
            merged.pop(key, None)
        else:
            merged[key] = value
    return sanitize_override(merged)


def build_effective_metadata(source_meta: dict[str, Any], override: object | None = None) -> dict[str, Any]:
    effective = build_source_view(source_meta)
    sanitized_override = sanitize_override(override or {})
    source_info = normalize_platform_source(effective.get("source"))
    if source_info:
        effective["source"] = source_info
    else:
        effective.pop("source", None)

    source_connection = sanitize_connection(effective.get("connection"))
    override_connection = sanitize_connection(sanitized_override.get("connection"))
    merged_connection = {**source_connection, **override_connection}
    if merged_connection:
        effective["connection"] = merged_connection

    for key in ("priority", "notes", "needs_instance"):
        if key in sanitized_override:
            effective[key] = sanitized_override[key]
    if "no_submit" in sanitized_override:
        effective["no_submit"] = sanitized_override["no_submit"]
    effective["needs_instance"] = _coerce_bool(effective.get("needs_instance"))

    source_runtime_mode = str(source_info.get("runtime_mode") or "").strip()
    if source_runtime_mode and source_runtime_mode != RUNTIME_MODE_FULL_REMOTE:
        effective["no_submit"] = True

    effective["connection_info"] = render_connection_info(
        effective.get("connection"),
        fallback=str(effective.get("connection_info") or ""),
    )
    return effective


def load_effective_metadata(challenge_dir: str | Path) -> dict[str, Any]:
    source_meta = load_source_metadata(challenge_dir)
    return build_effective_metadata(source_meta, load_override(challenge_dir))


def write_override(challenge_dir: str | Path, override: object) -> Path | None:
    path = challenge_override_path(challenge_dir)
    sanitized = sanitize_override(override)
    if not sanitized:
        if path.exists():
            path.unlink()
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sanitized, indent=2, sort_keys=True), encoding="utf-8")
    return path


def refresh_effective_metadata(challenge_dir: str | Path) -> Path:
    path = challenge_effective_metadata_path(challenge_dir)
    effective = load_effective_metadata(challenge_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(effective, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def delete_override(challenge_dir: str | Path) -> None:
    path = challenge_override_path(challenge_dir)
    if path.exists():
        path.unlink()
    refresh_effective_metadata(challenge_dir)


def challenge_config_snapshot(challenge_dir: str | Path) -> dict[str, Any]:
    source = build_source_view(load_source_metadata(challenge_dir))
    override = load_override(challenge_dir)
    effective = build_effective_metadata(source, override)
    effective_source = normalize_platform_source(effective.get("source"))
    source_runtime_mode = str(effective_source.get("runtime_mode") or "").strip()
    runtime_mode = source_runtime_mode
    if bool(effective.get("no_submit")) and runtime_mode == RUNTIME_MODE_FULL_REMOTE:
        runtime_mode = "operator_only"
    if not runtime_mode and bool(effective.get("no_submit")):
        runtime_mode = "operator_only"
    return {
        "source": source,
        "override": override,
        "effective": effective,
        "runtime_mode": runtime_mode,
        "automatic_submit": bool(runtime_mode == RUNTIME_MODE_FULL_REMOTE and not effective.get("no_submit")),
        "paths": {
            "challenge_dir": str(Path(challenge_dir).resolve()),
            "metadata": str(challenge_metadata_path(challenge_dir)),
            "override": str(challenge_override_path(challenge_dir)),
            "effective_metadata": str(challenge_effective_metadata_path(challenge_dir)),
        },
    }
