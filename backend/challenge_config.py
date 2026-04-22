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
_OVERRIDE_FIELDS = {
    "connection",
    "instance_stages",
    "priority",
    "no_submit",
    "notes",
    "needs_instance",
    "current_stage",
    "stages",
}
_CONNECTION_FIELDS = {"scheme", "host", "port", "url", "raw_command"}
_INSTANCE_ENDPOINT_FIELDS = {"id", "title", "description", "connection"}
_INSTANCE_STAGE_FIELDS = {
    "id",
    "title",
    "description",
    "manual_action",
    "notes",
    "connection",
    "endpoints",
}
_INSTANCE_STAGE_STATE_FIELDS = {"status", "connection", "current_endpoint", "endpoints"}
_INSTANCE_ENDPOINT_STATE_FIELDS = {"connection"}
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


def sanitize_instance_stage(value: object) -> dict[str, Any]:
    raw = _dict(value)
    stage_id = str(raw.get("id") or "").strip()
    if not stage_id:
        return {}
    sanitized: dict[str, Any] = {"id": stage_id}
    for key in ("title", "description", "manual_action", "notes"):
        text = str(raw.get(key) or "").strip()
        if text:
            sanitized[key] = text
    connection = sanitize_connection(raw.get("connection"))
    if connection:
        sanitized["connection"] = connection
    endpoints = sanitize_instance_endpoints(raw.get("endpoints"))
    if endpoints:
        sanitized["endpoints"] = endpoints
    return sanitized


def sanitize_instance_endpoint(value: object) -> dict[str, Any]:
    raw = _dict(value)
    endpoint_id = str(raw.get("id") or "").strip()
    if not endpoint_id:
        return {}
    sanitized: dict[str, Any] = {"id": endpoint_id}
    for key in ("title", "description"):
        text = str(raw.get(key) or "").strip()
        if text:
            sanitized[key] = text
    connection = sanitize_connection(raw.get("connection"))
    if connection:
        sanitized["connection"] = connection
    return sanitized


def sanitize_instance_endpoints(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    sanitized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in value:
        endpoint = sanitize_instance_endpoint(item)
        endpoint_id = str(endpoint.get("id") or "").strip()
        if not endpoint_id or endpoint_id in seen_ids:
            continue
        seen_ids.add(endpoint_id)
        sanitized.append(endpoint)
    return sanitized


def sanitize_instance_stages(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    sanitized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in value:
        stage = sanitize_instance_stage(item)
        stage_id = str(stage.get("id") or "").strip()
        if not stage_id or stage_id in seen_ids:
            continue
        seen_ids.add(stage_id)
        sanitized.append(stage)
    return sanitized


def sanitize_stage_state(value: object) -> dict[str, Any]:
    raw = _dict(value)
    sanitized: dict[str, Any] = {}
    status = str(raw.get("status") or "").strip().lower()
    if status:
        sanitized["status"] = status
    current_endpoint = str(raw.get("current_endpoint") or "").strip()
    if current_endpoint:
        sanitized["current_endpoint"] = current_endpoint
    connection = sanitize_connection(raw.get("connection"))
    if connection:
        sanitized["connection"] = connection
    endpoints = sanitize_stage_endpoint_states(raw.get("endpoints"))
    if endpoints:
        sanitized["endpoints"] = endpoints
    return sanitized


def sanitize_stage_endpoint_state(value: object) -> dict[str, Any]:
    raw = _dict(value)
    sanitized: dict[str, Any] = {}
    connection = sanitize_connection(raw.get("connection"))
    if connection:
        sanitized["connection"] = connection
    return sanitized


def sanitize_stage_endpoint_states(value: object) -> dict[str, dict[str, Any]]:
    raw = _dict(value)
    sanitized: dict[str, dict[str, Any]] = {}
    for key, item in raw.items():
        endpoint_id = str(key or "").strip()
        if not endpoint_id:
            continue
        endpoint_state = sanitize_stage_endpoint_state(item)
        if endpoint_state:
            sanitized[endpoint_id] = endpoint_state
    return sanitized


def sanitize_stage_states(value: object) -> dict[str, dict[str, Any]]:
    raw = _dict(value)
    sanitized: dict[str, dict[str, Any]] = {}
    for key, item in raw.items():
        stage_id = str(key or "").strip()
        if not stage_id:
            continue
        stage_state = sanitize_stage_state(item)
        if stage_state:
            sanitized[stage_id] = stage_state
    return sanitized


def _source_instance_stages(source_meta: dict[str, Any], normalized_source: dict[str, Any]) -> list[dict[str, Any]]:
    return sanitize_instance_stages(
        source_meta.get("instance_stages")
        if "instance_stages" in source_meta
        else normalized_source.get("instance_stages")
    )


def _resolve_current_stage(
    instance_stages: list[dict[str, Any]],
    stage_states: dict[str, dict[str, Any]],
    preferred_stage: object,
) -> str:
    preferred = str(preferred_stage or "").strip()
    stage_ids = [str(stage.get("id") or "").strip() for stage in instance_stages if str(stage.get("id") or "").strip()]
    known_stage_ids = list(dict.fromkeys(stage_ids + list(stage_states.keys())))
    if preferred and preferred in known_stage_ids:
        preferred_status = str(stage_states.get(preferred, {}).get("status") or "").strip().lower()
        if preferred_status != "done":
            return preferred
    for stage_id in known_stage_ids:
        status = str(stage_states.get(stage_id, {}).get("status") or "").strip().lower()
        if status != "done":
            return stage_id
    if preferred and preferred in known_stage_ids:
        return preferred
    return known_stage_ids[0] if known_stage_ids else ""


def _resolve_current_endpoint(
    endpoints: list[dict[str, Any]],
    endpoint_states: dict[str, dict[str, Any]],
    preferred_endpoint: object,
) -> str:
    preferred = str(preferred_endpoint or "").strip()
    endpoint_ids = [
        str(endpoint.get("id") or "").strip()
        for endpoint in endpoints
        if str(endpoint.get("id") or "").strip()
    ]
    known_endpoint_ids = list(dict.fromkeys(endpoint_ids + list(endpoint_states.keys())))
    if preferred and preferred in known_endpoint_ids:
        return preferred
    return known_endpoint_ids[0] if known_endpoint_ids else ""


def _merge_stage_endpoints(
    source_endpoints: list[dict[str, Any]],
    endpoint_states: dict[str, dict[str, Any]],
    *,
    current_endpoint: str = "",
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def _append_endpoint(endpoint_id: str, source_endpoint: dict[str, Any] | None = None) -> None:
        if not endpoint_id or endpoint_id in seen_ids:
            return
        seen_ids.add(endpoint_id)
        base = deepcopy(source_endpoint or {"id": endpoint_id})
        base["id"] = endpoint_id
        state = endpoint_states.get(endpoint_id, {})
        base_connection = sanitize_connection(base.get("connection"))
        state_connection = sanitize_connection(state.get("connection"))
        merged_connection = {**base_connection, **state_connection}
        if merged_connection:
            base["connection"] = merged_connection
            base["connection_info"] = render_connection_info(merged_connection)
        else:
            base.pop("connection", None)
            base["connection_info"] = ""
        if endpoint_id == current_endpoint:
            base["is_current"] = True
        else:
            base.pop("is_current", None)
        merged.append(base)

    for endpoint in source_endpoints:
        endpoint_id = str(endpoint.get("id") or "").strip()
        _append_endpoint(endpoint_id, endpoint)
    for endpoint_id in endpoint_states:
        _append_endpoint(endpoint_id)
    return merged


def _current_endpoint_entry(stage_entry: dict[str, Any], current_endpoint: str) -> dict[str, Any]:
    endpoints = stage_entry.get("endpoints")
    if not isinstance(endpoints, list):
        return {}
    for endpoint in endpoints:
        if str(_dict(endpoint).get("id") or "").strip() == current_endpoint:
            return _dict(endpoint)
    return {}


def _merge_stage_definitions(
    source_stages: list[dict[str, Any]],
    override_stages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not override_stages:
        return sanitize_instance_stages(source_stages)
    source_by_id = {
        str(stage.get("id") or "").strip(): deepcopy(stage)
        for stage in source_stages
        if str(stage.get("id") or "").strip()
    }
    merged: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for override_stage in override_stages:
        stage_id = str(override_stage.get("id") or "").strip()
        if not stage_id or stage_id in seen_ids:
            continue
        seen_ids.add(stage_id)
        base = deepcopy(source_by_id.get(stage_id, {}))
        base.update({key: value for key, value in override_stage.items() if key != "id"})
        base["id"] = stage_id
        merged_stage = sanitize_instance_stage(base)
        if merged_stage:
            merged.append(merged_stage)
    for stage in source_stages:
        stage_id = str(stage.get("id") or "").strip()
        if stage_id and stage_id not in seen_ids:
            merged.append(deepcopy(stage))
    return sanitize_instance_stages(merged)


def _merge_instance_stages(
    source_stages: list[dict[str, Any]],
    stage_states: dict[str, dict[str, Any]],
    *,
    current_stage: str = "",
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def _append_stage(stage_id: str, source_stage: dict[str, Any] | None = None) -> None:
        if not stage_id or stage_id in seen_ids:
            return
        seen_ids.add(stage_id)
        base = deepcopy(source_stage or {"id": stage_id})
        base["id"] = stage_id
        state = stage_states.get(stage_id, {})
        base_connection = sanitize_connection(base.get("connection"))
        state_connection = sanitize_connection(state.get("connection"))
        merged_connection = {**base_connection, **state_connection}
        source_endpoints = sanitize_instance_endpoints(base.get("endpoints"))
        endpoint_states = sanitize_stage_endpoint_states(state.get("endpoints"))
        current_endpoint = _resolve_current_endpoint(
            source_endpoints,
            endpoint_states,
            state.get("current_endpoint"),
        )
        merged_endpoints = _merge_stage_endpoints(
            source_endpoints,
            endpoint_states,
            current_endpoint=current_endpoint,
        )
        current_endpoint_entry = (
            _current_endpoint_entry({"endpoints": merged_endpoints}, current_endpoint)
            if merged_endpoints
            else {}
        )
        current_endpoint_connection = sanitize_connection(current_endpoint_entry.get("connection"))
        effective_connection = current_endpoint_connection or merged_connection
        if merged_connection:
            base["stage_connection"] = merged_connection
            base["stage_connection_info"] = render_connection_info(merged_connection)
        else:
            base.pop("stage_connection", None)
            base["stage_connection_info"] = ""
        if effective_connection:
            base["connection"] = effective_connection
            base["connection_info"] = render_connection_info(effective_connection)
        else:
            base.pop("connection", None)
            base["connection_info"] = ""
        if merged_endpoints:
            base["endpoints"] = merged_endpoints
        else:
            base.pop("endpoints", None)
        if current_endpoint:
            base["current_endpoint"] = current_endpoint
            base["current_endpoint_title"] = str(
                current_endpoint_entry.get("title") or current_endpoint_entry.get("id") or current_endpoint
            ).strip()
        else:
            base.pop("current_endpoint", None)
            base.pop("current_endpoint_title", None)
        status = str(state.get("status") or "").strip().lower()
        if status:
            base["status"] = status
        else:
            base.pop("status", None)
        if stage_id == current_stage:
            base["is_current"] = True
        else:
            base.pop("is_current", None)
        merged.append(base)

    for stage in source_stages:
        stage_id = str(stage.get("id") or "").strip()
        _append_stage(stage_id, stage)
    for stage_id in stage_states:
        _append_stage(stage_id)
    return merged


def _current_stage_entry(instance_stages: list[dict[str, Any]], current_stage: str) -> dict[str, Any]:
    for stage in instance_stages:
        if str(stage.get("id") or "").strip() == current_stage:
            return stage
    return {}


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

    instance_stages = _source_instance_stages(source_view, normalized_source)
    if instance_stages:
        source_view["instance_stages"] = instance_stages
    else:
        source_view.pop("instance_stages", None)

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
    elif instance_stages:
        needs_instance = True
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
        if key == "instance_stages":
            stages = sanitize_instance_stages(item)
            if stages:
                sanitized[key] = stages
            continue
        if key == "stages":
            stages = sanitize_stage_states(item)
            if stages:
                sanitized[key] = stages
            continue
        if key in {"priority", "no_submit", "needs_instance"}:
            sanitized[key] = _coerce_bool(item)
            continue
        if key == "current_stage":
            stage_id = str(item or "").strip()
            if stage_id:
                sanitized[key] = stage_id
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
        if key == "instance_stages":
            stages = sanitize_instance_stages(item)
            normalized[key] = stages if stages else None
            continue
        if key == "stages":
            raw_stage_patch = _dict(item)
            normalized_stages: dict[str, Any] = {}
            for stage_id, stage_state in raw_stage_patch.items():
                normalized_stage_id = str(stage_id or "").strip()
                if not normalized_stage_id:
                    continue
                if stage_state is None:
                    normalized_stages[normalized_stage_id] = None
                    continue
                normalized_state: dict[str, Any] = {}
                state_patch = _dict(stage_state)
                for field in _INSTANCE_STAGE_STATE_FIELDS:
                    if field not in state_patch:
                        continue
                    field_value = state_patch.get(field)
                    if field_value is None:
                        normalized_state[field] = None
                        continue
                    if field == "connection":
                        normalized_connection = {}
                        connection_patch = _dict(field_value)
                        for connection_field in _CONNECTION_FIELDS:
                            if connection_field not in connection_patch:
                                continue
                            connection_value = connection_patch.get(connection_field)
                            if connection_value is None:
                                normalized_connection[connection_field] = None
                                continue
                            if connection_field == "port":
                                port = _normalize_port(connection_value)
                                if port is not None:
                                    normalized_connection[connection_field] = port
                                continue
                            text = str(connection_value).strip()
                            if text:
                                normalized_connection[connection_field] = text
                        normalized_state[field] = normalized_connection
                        continue
                    if field == "endpoints":
                        normalized_endpoints = {}
                        endpoint_patch = _dict(field_value)
                        for endpoint_id, endpoint_value in endpoint_patch.items():
                            normalized_endpoint_id = str(endpoint_id or "").strip()
                            if not normalized_endpoint_id:
                                continue
                            if endpoint_value is None:
                                normalized_endpoints[normalized_endpoint_id] = None
                                continue
                            endpoint_state = _dict(endpoint_value)
                            normalized_endpoint_state: dict[str, Any] = {}
                            if "connection" in endpoint_state:
                                connection_patch = _dict(endpoint_state.get("connection"))
                                normalized_connection = {}
                                for connection_field in _CONNECTION_FIELDS:
                                    if connection_field not in connection_patch:
                                        continue
                                    connection_value = connection_patch.get(connection_field)
                                    if connection_value is None:
                                        normalized_connection[connection_field] = None
                                        continue
                                    if connection_field == "port":
                                        port = _normalize_port(connection_value)
                                        if port is not None:
                                            normalized_connection[connection_field] = port
                                        continue
                                    text = str(connection_value).strip()
                                    if text:
                                        normalized_connection[connection_field] = text
                                normalized_endpoint_state["connection"] = normalized_connection
                            normalized_endpoints[normalized_endpoint_id] = normalized_endpoint_state
                        normalized_state[field] = normalized_endpoints
                        continue
                    if field == "current_endpoint":
                        normalized_endpoint = str(field_value or "").strip()
                        if normalized_endpoint:
                            normalized_state[field] = normalized_endpoint
                        continue
                    normalized_status = str(field_value).strip().lower()
                    if normalized_status:
                        normalized_state[field] = normalized_status
                normalized_stages[normalized_stage_id] = normalized_state
            normalized[key] = normalized_stages
            continue
        if key in {"priority", "no_submit", "needs_instance"}:
            normalized[key] = _coerce_bool(item)
            continue
        if key == "current_stage":
            stage_id = str(item or "").strip()
            if stage_id:
                normalized[key] = stage_id
            else:
                normalized[key] = None
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
        if key == "stages":
            next_stages = sanitize_stage_states(merged.get("stages"))
            for stage_id, stage_value in _dict(value).items():
                normalized_stage_id = str(stage_id or "").strip()
                if not normalized_stage_id:
                    continue
                if stage_value is None:
                    next_stages.pop(normalized_stage_id, None)
                    continue
                existing_stage = sanitize_stage_state(next_stages.get(normalized_stage_id))
                for field, field_value in _dict(stage_value).items():
                    if field == "connection":
                        next_connection = sanitize_connection(existing_stage.get("connection"))
                        if field_value is None:
                            existing_stage.pop("connection", None)
                        else:
                            for connection_field, connection_field_value in _dict(field_value).items():
                                if connection_field_value is None:
                                    next_connection.pop(connection_field, None)
                                else:
                                    next_connection[connection_field] = connection_field_value
                            if next_connection:
                                existing_stage["connection"] = sanitize_connection(next_connection)
                            else:
                                existing_stage.pop("connection", None)
                        continue
                    if field == "endpoints":
                        next_endpoints = sanitize_stage_endpoint_states(existing_stage.get("endpoints"))
                        if field_value is None:
                            existing_stage.pop("endpoints", None)
                            continue
                        for endpoint_id, endpoint_value in _dict(field_value).items():
                            normalized_endpoint_id = str(endpoint_id or "").strip()
                            if not normalized_endpoint_id:
                                continue
                            if endpoint_value is None:
                                next_endpoints.pop(normalized_endpoint_id, None)
                                continue
                            existing_endpoint = sanitize_stage_endpoint_state(
                                next_endpoints.get(normalized_endpoint_id)
                            )
                            for endpoint_field, endpoint_field_value in _dict(endpoint_value).items():
                                if endpoint_field != "connection":
                                    continue
                                next_connection = sanitize_connection(existing_endpoint.get("connection"))
                                if endpoint_field_value is None:
                                    existing_endpoint.pop("connection", None)
                                else:
                                    for connection_field, connection_field_value in _dict(endpoint_field_value).items():
                                        if connection_field_value is None:
                                            next_connection.pop(connection_field, None)
                                        else:
                                            next_connection[connection_field] = connection_field_value
                                    if next_connection:
                                        existing_endpoint["connection"] = sanitize_connection(next_connection)
                                    else:
                                        existing_endpoint.pop("connection", None)
                            if existing_endpoint:
                                next_endpoints[normalized_endpoint_id] = sanitize_stage_endpoint_state(existing_endpoint)
                            else:
                                next_endpoints.pop(normalized_endpoint_id, None)
                        if next_endpoints:
                            existing_stage["endpoints"] = sanitize_stage_endpoint_states(next_endpoints)
                        else:
                            existing_stage.pop("endpoints", None)
                        continue
                    if field == "current_endpoint":
                        if field_value is None:
                            existing_stage.pop("current_endpoint", None)
                        else:
                            current_endpoint = str(field_value).strip()
                            if current_endpoint:
                                existing_stage["current_endpoint"] = current_endpoint
                            else:
                                existing_stage.pop("current_endpoint", None)
                        continue
                    if field_value is None:
                        existing_stage.pop(field, None)
                    else:
                        existing_stage[field] = str(field_value).strip().lower()
                if existing_stage:
                    next_stages[normalized_stage_id] = sanitize_stage_state(existing_stage)
                else:
                    next_stages.pop(normalized_stage_id, None)
            if next_stages:
                merged["stages"] = sanitize_stage_states(next_stages)
            else:
                merged.pop("stages", None)
            continue
        if key == "instance_stages":
            if value is None:
                merged.pop("instance_stages", None)
            else:
                stages = sanitize_instance_stages(value)
                if stages:
                    merged["instance_stages"] = stages
                else:
                    merged.pop("instance_stages", None)
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
    source_stages = sanitize_instance_stages(effective.get("instance_stages"))
    override_stage_definitions = sanitize_instance_stages(sanitized_override.get("instance_stages"))
    effective_stage_definitions = _merge_stage_definitions(source_stages, override_stage_definitions)
    override_stage_states = sanitize_stage_states(sanitized_override.get("stages"))
    current_stage = _resolve_current_stage(
        effective_stage_definitions,
        override_stage_states,
        sanitized_override.get("current_stage"),
    )
    merged_instance_stages = _merge_instance_stages(
        effective_stage_definitions,
        override_stage_states,
        current_stage=current_stage,
    )
    if merged_instance_stages:
        effective["instance_stages"] = merged_instance_stages
    else:
        effective.pop("instance_stages", None)

    for key in ("priority", "notes", "needs_instance"):
        if key in sanitized_override:
            effective[key] = sanitized_override[key]
    if "no_submit" in sanitized_override:
        effective["no_submit"] = sanitized_override["no_submit"]
    effective["needs_instance"] = _coerce_bool(effective.get("needs_instance"))

    current_stage_entry = _current_stage_entry(merged_instance_stages, current_stage)
    current_stage_connection = sanitize_connection(current_stage_entry.get("connection"))
    effective_connection = current_stage_connection or merged_connection
    if effective_connection:
        effective["connection"] = effective_connection
    else:
        effective.pop("connection", None)
    if current_stage:
        effective["current_stage"] = current_stage
    else:
        effective.pop("current_stage", None)
    if current_stage_entry:
        effective["current_stage_title"] = str(
            current_stage_entry.get("title") or current_stage_entry.get("id") or ""
        ).strip()
        effective["current_stage_status"] = str(current_stage_entry.get("status") or "").strip().lower()
        current_endpoint = str(current_stage_entry.get("current_endpoint") or "").strip()
        if current_endpoint:
            effective["current_stage_endpoint"] = current_endpoint
            effective["current_stage_endpoint_title"] = str(
                current_stage_entry.get("current_endpoint_title") or current_endpoint
            ).strip()
        else:
            effective.pop("current_stage_endpoint", None)
            effective.pop("current_stage_endpoint_title", None)
        for field in ("description", "manual_action", "notes"):
            value = str(current_stage_entry.get(field) or "").strip()
            key = f"current_stage_{field}"
            if value:
                effective[key] = value
            else:
                effective.pop(key, None)
    else:
        for key in (
            "current_stage_title",
            "current_stage_status",
            "current_stage_endpoint",
            "current_stage_endpoint_title",
            "current_stage_description",
            "current_stage_manual_action",
            "current_stage_notes",
        ):
            effective.pop(key, None)

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
