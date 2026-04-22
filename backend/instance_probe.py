"""Challenge instance readiness probes for operator workflows."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from backend.challenge_config import infer_connection, render_connection_info, sanitize_connection

_HTTP_SCHEMES = {"http", "https"}


def _resolved_connection(meta: dict[str, Any]) -> dict[str, Any]:
    effective = meta if isinstance(meta, dict) else {}
    connection = sanitize_connection(effective.get("connection"))
    if connection:
        return connection
    inferred = infer_connection(
        effective.get("connection_info", ""),
        effective.get("description", ""),
    )
    return sanitize_connection(inferred)


def _probe_target(connection: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    resolved = sanitize_connection(connection)
    inferred_from_command = infer_connection(resolved.get("raw_command", ""))
    candidate = {**inferred_from_command, **resolved}
    scheme = str(candidate.get("scheme") or "").strip().lower()
    url = str(candidate.get("url") or "").strip()
    host = str(candidate.get("host") or "").strip()
    port = candidate.get("port")
    raw_command = str(candidate.get("raw_command") or "").strip()

    if url:
        return "http", {"url": url}
    if scheme in _HTTP_SCHEMES and host:
        if isinstance(port, int):
            return "http", {"url": f"{scheme}://{host}:{port}"}
        return "http", {"url": f"{scheme}://{host}"}
    if host and isinstance(port, int):
        return "tcp", {"host": host, "port": port, "scheme": scheme or "tcp"}
    if raw_command:
        return "unsupported", {
            "detail": "Current raw command cannot be probed safely. Provide host/port/url for checks.",
        }
    return "missing", {"detail": "No connection details are configured yet."}


async def _probe_http(url: str, *, timeout: float) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            verify=False,
        ) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        return {
            "ready": False,
            "kind": "http",
            "target": url,
            "detail": f"HTTP probe failed: {exc}",
            "error": str(exc),
        }
    return {
        "ready": True,
        "kind": "http",
        "target": url,
        "detail": f"HTTP {response.status_code} from {url}",
        "status_code": response.status_code,
    }


async def _probe_tcp(host: str, port: int, *, timeout: float, scheme: str = "tcp") -> dict[str, Any]:
    writer = None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        try:
            reader.feed_eof()
        except (AttributeError, RuntimeError):
            pass
    except (asyncio.TimeoutError, OSError) as exc:
        return {
            "ready": False,
            "kind": "tcp",
            "target": f"{host}:{port}",
            "detail": f"{scheme or 'tcp'} probe failed: {exc}",
            "error": str(exc),
        }
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
    return {
        "ready": True,
        "kind": "tcp",
        "target": f"{host}:{port}",
        "detail": f"Connected to {host}:{port}",
        "scheme": scheme or "tcp",
    }


async def probe_instance_connection(meta: dict[str, Any], *, timeout: float = 5.0) -> dict[str, Any]:
    """Probe the effective challenge connection without executing arbitrary commands."""
    effective = meta if isinstance(meta, dict) else {}
    connection = _resolved_connection(effective)
    target_label = render_connection_info(connection, fallback=str(effective.get("connection_info") or ""))
    needs_instance = bool(effective.get("needs_instance"))
    kind, target = _probe_target(connection)
    if kind == "http":
        result = await _probe_http(str(target["url"]), timeout=timeout)
    elif kind == "tcp":
        result = await _probe_tcp(
            str(target["host"]),
            int(target["port"]),
            timeout=timeout,
            scheme=str(target.get("scheme") or "tcp"),
        )
    else:
        detail = str(target.get("detail") or "")
        if not detail and needs_instance:
            detail = "Deploy or refresh the challenge instance, then save the new connection details."
        elif not detail:
            detail = "No probeable connection is configured."
        result = {
            "ready": False,
            "kind": kind,
            "target": target_label or "-",
            "detail": detail,
        }
    result.setdefault("target", target_label or "-")
    result["needs_instance"] = needs_instance
    return result
