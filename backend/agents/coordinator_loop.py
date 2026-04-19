"""Shared coordinator event loop — used by both Claude SDK and Codex coordinators."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from backend.config import Settings
from backend.cost_tracker import CostTracker
from backend.ctfd import CTFdClient
from backend.deps import CoordinatorDeps
from backend.message_bus import CandidateRef, CoordinatorNoteRef
from backend.models import DEFAULT_MODELS
from backend.operator_ui import (
    collect_advisory_history,
    list_trace_files,
    load_ui_asset,
    read_trace_window,
)
from backend.poller import CTFdPoller
from backend.prompts import ChallengeMeta

logger = logging.getLogger(__name__)

# Callable type for a coordinator turn: (message) -> None
TurnFn = Callable[[str], Coroutine[Any, Any, None]]


def _render_solver_message(event: object) -> str:
    if isinstance(event, CandidateRef):
        return event.rendered_text()

    if isinstance(event, CoordinatorNoteRef):
        return event.rendered_text()

    if isinstance(event, str):
        if " [Advisor] " in event:
            return f"ADVISOR MESSAGE: {event}"
        return f"SOLVER MESSAGE: {event}"

    candidate_event = CandidateRef.from_snapshot(event)
    if candidate_event is not None:
        return candidate_event.rendered_text()

    coordinator_note_event = CoordinatorNoteRef.from_snapshot(event)
    if coordinator_note_event is not None:
        return coordinator_note_event.rendered_text()

    if not isinstance(event, dict):
        return f"SOLVER MESSAGE: {event}"
    payload = {str(key): value for key, value in event.items()}

    kind = str(payload.get("kind") or "solver_message")
    challenge_name = str(payload.get("challenge_name") or "").strip()
    source_model = str(payload.get("source_model") or "").strip()
    prefix = f"[{challenge_name}/{source_model}] " if challenge_name and source_model else ""

    if kind == "candidate_ref":
        source_models = payload.get("source_models") or []
        if not isinstance(source_models, list):
            source_models = []
        evidence_digest_paths = payload.get("evidence_digest_paths") or {}
        if not isinstance(evidence_digest_paths, dict):
            evidence_digest_paths = {}
        evidence_pointer_paths = payload.get("evidence_pointer_paths") or {}
        if not isinstance(evidence_pointer_paths, dict):
            evidence_pointer_paths = {}
        trace_paths = payload.get("trace_paths") or {}
        if not isinstance(trace_paths, dict):
            trace_paths = {}
        lines = [
            f"{prefix}FLAG CANDIDATE: {str(payload.get('flag') or '').strip()}",
            f"Advisor verdict: {str(payload.get('advisor_decision') or 'insufficient')}",
        ]
        if source_models:
            lines.append(f"Source models: {', '.join(str(item) for item in source_models if str(item).strip())}")
        advisor_note = str(payload.get("advisor_note") or "").strip()
        if advisor_note:
            lines.extend(["Advisor note:", advisor_note])
        summary = str(payload.get("summary") or "").strip()
        if summary:
            lines.extend(["Evidence summary:", summary])
        for digest in evidence_digest_paths.values():
            digest_text = str(digest).strip()
            if digest_text:
                lines.append(f"Evidence digest: {digest_text}")
        for pointer in evidence_pointer_paths.values():
            pointer_text = str(pointer).strip()
            if pointer_text:
                lines.append(f"Evidence pointer: {pointer_text}")
        for trace in trace_paths.values():
            trace_text = str(trace).strip()
            if trace_text:
                lines.append(f"Trace: {trace_text}")
        lines.append("Review this candidate. Submit it only if the evidence is strong; otherwise keep lanes exploring.")
        return "\n".join(lines)

    if kind == "coordinator_note":
        lines = [f"ADVISOR MESSAGE: {prefix}{str(payload.get('summary') or '').strip()}".rstrip()]
        pointer_text = str(payload.get("pointer_path") or "").strip()
        if pointer_text:
            lines.append(f"Pointer: {pointer_text}")
        return "\n".join(line for line in lines if line.strip())

    return f"SOLVER MESSAGE: {prefix}{str(payload.get('summary') or event).strip()}"


def _restored_solved_names(deps: CoordinatorDeps) -> set[str]:
    solved: set[str] = set()
    for name, result in deps.results.items():
        if not isinstance(result, dict):
            continue
        if result.get("status") == "flag_found":
            solved.add(name)
    return solved


def _local_known_challenge_names(deps: CoordinatorDeps) -> set[str]:
    return set(deps.challenge_dirs) | set(deps.challenge_metas)


def _known_challenge_names(deps: CoordinatorDeps, poller: CTFdPoller) -> set[str]:
    return set(poller.known_challenges) | _local_known_challenge_names(deps) | set(deps.results)


def _known_solved_names(deps: CoordinatorDeps, poller: CTFdPoller) -> set[str]:
    return set(poller.known_solved) | _restored_solved_names(deps)


def build_deps(
    settings: Settings,
    model_specs: list[str] | None = None,
    challenges_root: str = "challenges",
    no_submit: bool = False,
    challenge_dirs: dict[str, str] | None = None,
    challenge_metas: dict[str, ChallengeMeta] | None = None,
) -> tuple[CTFdClient, CostTracker, CoordinatorDeps]:
    """Create CTFd client, cost tracker, and coordinator deps."""
    ctfd = CTFdClient(
        base_url=settings.ctfd_url,
        token=settings.ctfd_token,
        username=settings.ctfd_user,
        password=settings.ctfd_pass,
    )
    cost_tracker = CostTracker()
    specs = model_specs or list(DEFAULT_MODELS)
    Path(challenges_root).mkdir(parents=True, exist_ok=True)

    deps = CoordinatorDeps(
        ctfd=ctfd,
        cost_tracker=cost_tracker,
        settings=settings,
        model_specs=specs,
        challenges_root=challenges_root,
        no_submit=no_submit,
        max_concurrent_challenges=getattr(settings, "max_concurrent_challenges", 10),
        challenge_dirs=challenge_dirs or {},
        challenge_metas=challenge_metas or {},
    )

    # Pre-load already-pulled challenges
    for d in Path(challenges_root).iterdir():
        meta_path = d / "metadata.yml"
        if meta_path.exists():
            meta = ChallengeMeta.from_yaml(meta_path)
            if meta.name not in deps.challenge_dirs:
                deps.challenge_dirs[meta.name] = str(d)
                deps.challenge_metas[meta.name] = meta
            result_path = d / "solve" / "result.json"
            if result_path.exists():
                try:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                except Exception:
                    logger.warning("Could not restore solved result from %s", result_path)
                else:
                    if isinstance(result, dict):
                        deps.results.setdefault(meta.name, result)

    return ctfd, cost_tracker, deps


async def cleanup_coordinator_runtime(
    deps: CoordinatorDeps,
    ctfd: CTFdClient,
    cost_tracker: CostTracker,
) -> None:
    for swarm in deps.swarms.values():
        swarm.kill()
    for task in deps.swarm_tasks.values():
        task.cancel()
    if deps.swarm_tasks:
        await asyncio.gather(*deps.swarm_tasks.values(), return_exceptions=True)
    cost_tracker.log_summary()
    try:
        await ctfd.close()
    except Exception:
        pass


async def run_event_loop(
    deps: CoordinatorDeps,
    ctfd: CTFdClient,
    cost_tracker: CostTracker,
    turn_fn: TurnFn,
    status_interval: int = 60,
    propagate_fatal: bool = False,
    cleanup_runtime_on_exit: bool = True,
) -> dict[str, Any]:
    """Run the shared coordinator event loop.

    Args:
        deps: Coordinator dependencies (shared state).
        ctfd: CTFd client (for poller).
        cost_tracker: Cost tracker.
        turn_fn: Async function that sends a message to the coordinator LLM.
        status_interval: Seconds between status updates.
    """
    poller = CTFdPoller(ctfd=ctfd, interval_s=5.0)
    await poller.start()

    # Start operator message HTTP endpoint
    msg_server = await _start_msg_server(deps.operator_inbox, deps, deps.msg_port)

    known_challenges = _known_challenge_names(deps, poller)
    known_solved = _known_solved_names(deps, poller)
    logger.info(
        "Coordinator starting: %d models, %d challenges, %d solved",
        len(deps.model_specs),
        len(known_challenges),
        len(known_solved),
    )
    deps.known_challenge_count = len(known_challenges)
    deps.known_solved_count = len(known_solved)

    unsolved = known_challenges - known_solved
    initial_msg = (
        f"CTF is LIVE. {len(known_challenges)} challenges, "
        f"{len(known_solved)} solved.\n"
        f"Unsolved: {sorted(unsolved) if unsolved else 'NONE'}\n"
        "Fetch challenges and spawn swarms for all unsolved."
    )

    try:
        await turn_fn(initial_msg)

        # Auto-spawn swarms for unsolved challenges if coordinator LLM didn't
        await _auto_spawn_unsolved(deps, poller)

        last_status = asyncio.get_event_loop().time()

        while True:
            events = []
            evt = await poller.get_event(timeout=5.0)
            if evt:
                events.append(evt)
            events.extend(poller.drain_events())
            deps.known_challenge_count = len(_known_challenge_names(deps, poller))
            deps.known_solved_count = len(_known_solved_names(deps, poller))

            # Auto-kill swarms for solved challenges
            for evt in events:
                if evt.kind == "challenge_solved" and evt.challenge_name in deps.swarms:
                    swarm = deps.swarms[evt.challenge_name]
                    if not swarm.cancel_event.is_set():
                        swarm.kill()
                        logger.info("Auto-killed swarm for: %s", evt.challenge_name)
                if evt.kind == "challenge_solved":
                    from backend.agents.coordinator_core import _drop_pending_swarm

                    _drop_pending_swarm(deps, evt.challenge_name)

            parts: list[str] = []
            for evt in events:
                if evt.kind == "new_challenge":
                    parts.append(f"NEW CHALLENGE: '{evt.challenge_name}' appeared. Spawn a swarm.")
                    # Auto-spawn for new challenges
                    await _auto_spawn_one(deps, evt.challenge_name)
                elif evt.kind == "challenge_solved":
                    parts.append(f"SOLVED: '{evt.challenge_name}' — swarm auto-killed.")

            # Detect finished swarms
            finished_names = [
                name for name, task in list(deps.swarm_tasks.items()) if task.done()
            ]
            for name in finished_names:
                task = deps.swarm_tasks[name]
                exc = task.exception() if not task.cancelled() else None
                if exc is not None:
                    parts.append(f"SOLVER FAILED: Swarm for '{name}' crashed ({type(exc).__name__}: {exc}). Consider retrying.")
                else:
                    parts.append(f"SOLVER FINISHED: Swarm for '{name}' completed. Check results or retry.")

            if finished_names:
                from backend.agents.coordinator_core import (
                    _fill_swarm_capacity,
                    _retire_finished_swarms,
                )

                _retire_finished_swarms(deps)
                spawned = await _fill_swarm_capacity(deps)
                for name in spawned:
                    parts.append(f"QUEUED SWARM STARTED: '{name}' moved from queue to active run.")

            # Drain solver-to-coordinator messages
            while True:
                try:
                    solver_msg = deps.coordinator_inbox.get_nowait()
                    parts.append(_render_solver_message(solver_msg))
                except asyncio.QueueEmpty:
                    break

            # Drain operator messages
            while True:
                try:
                    op_msg = deps.operator_inbox.get_nowait()
                    parts.append(f"OPERATOR MESSAGE: {op_msg}")
                    logger.info("Operator message: %s", op_msg[:200])
                except asyncio.QueueEmpty:
                    break

            # Periodic status update — only when there are active swarms or other events
            now = asyncio.get_event_loop().time()
            if now - last_status >= status_interval:
                last_status = now
                active = [n for n, t in deps.swarm_tasks.items() if not t.done()]
                solved_set = _known_solved_names(deps, poller)
                unsolved_set = _known_challenge_names(deps, poller) - solved_set
                status_line = (
                    f"STATUS: {len(solved_set)} solved, {len(unsolved_set)} unsolved, "
                    f"{len(active)} active swarms. Cost: ${cost_tracker.total_cost_usd:.2f}"
                )
                # Only send to coordinator if there's something happening
                if active or parts:
                    parts.append(status_line)
                else:
                    logger.info(f"Event -> coordinator: {status_line}")

            if parts:
                msg = "\n\n".join(parts)
                logger.info("Event -> coordinator: %s", msg[:200])
                await turn_fn(msg)

    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Coordinator shutting down...")
    except Exception as e:
        logger.error("Coordinator fatal: %s", e, exc_info=True)
        if propagate_fatal:
            raise
    finally:
        if msg_server:
            msg_server.close()
            await msg_server.wait_closed()
        await poller.stop()
        if cleanup_runtime_on_exit:
            await cleanup_coordinator_runtime(deps, ctfd, cost_tracker)

    return {
        "results": deps.results,
        "total_cost_usd": cost_tracker.total_cost_usd,
        "total_tokens": cost_tracker.total_tokens,
    }


async def _auto_spawn_one(deps: CoordinatorDeps, challenge_name: str) -> None:
    """Auto-spawn a swarm for a single challenge if not already running."""
    if challenge_name in deps.swarms:
        return
    try:
        from backend.agents.coordinator_core import do_spawn_swarm
        result = await do_spawn_swarm(deps, challenge_name)
        logger.info(f"Auto-spawn {challenge_name}: {result[:100]}")
    except Exception as e:
        logger.warning(f"Auto-spawn failed for {challenge_name}: {e}")


async def _auto_spawn_unsolved(deps: CoordinatorDeps, poller) -> None:
    """Auto-spawn swarms for all unsolved challenges that don't have active swarms."""
    solved_names = _known_solved_names(deps, poller)
    known_names = _known_challenge_names(deps, poller)
    unsolved = known_names - solved_names
    if not unsolved:
        return
    try:
        challenge_stubs = await deps.ctfd.fetch_challenge_stubs()
    except Exception as e:
        logger.warning("Could not fetch challenge solves for ordering: %s", e)
        ordered = sorted(unsolved)
    else:
        ranked = sorted(
            (stub for stub in challenge_stubs if stub.get("name") in unsolved),
            key=lambda stub: (-int(stub.get("solves", 0) or 0), str(stub.get("name", ""))),
        )
        ordered = [str(stub.get("name")) for stub in ranked]
        missing = sorted(name for name in unsolved if name not in set(ordered))
        ordered.extend(missing)
    local_only = [
        name
        for name in ordered
        if name in _local_known_challenge_names(deps) and name not in set(poller.known_challenges)
    ]
    if local_only:
        ranked_local = sorted(
            local_only,
            key=lambda name: (
                -int(getattr(deps.challenge_metas.get(name), "solves", 0) or 0),
                name,
            ),
        )
        ordered = [name for name in ordered if name not in set(local_only)] + ranked_local
    for name in ordered:
        await _auto_spawn_one(deps, name)


def _status_snapshot(deps: CoordinatorDeps) -> dict[str, Any]:
    active = {
        name: swarm.get_status()
        for name, swarm in deps.swarms.items()
        if name in deps.swarm_tasks and not deps.swarm_tasks[name].done()
    }
    finished = {
        name: deps.swarms[name].get_status()
        for name, task in deps.swarm_tasks.items()
        if task.done() and name in deps.swarms
    }
    swarm_names = set(active) | set(finished)
    live_steps = 0
    for swarm in [*active.values(), *finished.values()]:
        agents = swarm.get("agents", {})
        if not isinstance(agents, dict):
            continue
        for agent in agents.values():
            if isinstance(agent, dict):
                live_steps += int(agent.get("step_count", 0) or 0)
    restored_steps = sum(
        int(result.get("step_count", 0) or 0)
        for name, result in deps.results.items()
        if name not in swarm_names and isinstance(result, dict)
    )
    return {
        "models": list(deps.model_specs),
        "session_started_at": deps.session_started_at,
        "max_concurrent_challenges": deps.max_concurrent_challenges,
        "known_challenge_count": deps.known_challenge_count,
        "known_solved_count": deps.known_solved_count,
        "active_swarm_count": len(active),
        "finished_swarm_count": len(finished),
        "pending_challenge_count": len(deps.pending_swarm_queue),
        "pending_challenges": list(deps.pending_swarm_queue),
        "active_swarms": active,
        "finished_swarms": finished,
        "results": deps.results,
        "cost_usd": round(deps.cost_tracker.total_cost_usd, 4),
        "total_tokens": deps.cost_tracker.total_tokens,
        "total_step_count": live_steps + restored_steps,
        "coordinator_queue_depth": deps.coordinator_inbox.qsize(),
        "operator_queue_depth": deps.operator_inbox.qsize(),
    }


async def _start_msg_server(
    inbox: asyncio.Queue,
    deps: CoordinatorDeps,
    port: int = 0,
) -> asyncio.Server | None:
    """Start a tiny HTTP server that accepts operator messages and exposes status."""

    from backend.agents.coordinator_core import do_bump_agent

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        def _write_response(status: str, body: bytes, content_type: str) -> None:
            writer.write(
                (
                    f"HTTP/1.1 {status}\r\n"
                    f"Content-Type: {content_type}\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    "Connection: close\r\n"
                    "\r\n"
                ).encode()
                + body
            )

        def _json_response(status: str, payload: dict[str, Any]) -> None:
            _write_response(status, json.dumps(payload).encode(), "application/json")

        async def _status_stream_response() -> None:
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\n"
                b"Connection: keep-alive\r\n"
                b"\r\n"
            )
            await writer.drain()
            previous_payload: str | None = None
            while not writer.is_closing() and not reader.at_eof():
                payload = json.dumps(_status_snapshot(deps), sort_keys=True)
                if payload != previous_payload:
                    writer.write(f"event: status\ndata: {payload}\n\n".encode())
                    previous_payload = payload
                else:
                    writer.write(b": keepalive\n\n")
                await writer.drain()
                await asyncio.sleep(1)

        try:
            # Read HTTP request
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            headers: dict[str, str] = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                if line in (b"\r\n", b"\n", b""):
                    break
                if b":" in line:
                    k, v = line.decode().split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            request_parts = request_line.decode().split() if request_line else []
            method = request_parts[0] if request_parts else ""
            raw_path = request_parts[1] if len(request_parts) > 1 else "/"
            parsed = urlsplit(raw_path)
            path = parsed.path
            query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
            content_length = int(headers.get("content-length", 0))

            if method == "GET" and path == "/status":
                _json_response("200 OK", _status_snapshot(deps))
            elif method == "GET" and path == "/status/stream":
                await _status_stream_response()
            elif method == "GET" and path in {"/ui", "/ui.css", "/ui.js"}:
                asset_name = {
                    "/ui": "operator_ui.html",
                    "/ui.css": "operator_ui.css",
                    "/ui.js": "operator_ui.js",
                }[path]
                content_type, text = load_ui_asset(asset_name)
                _write_response("200 OK", text.encode("utf-8"), content_type)
            elif method == "GET" and path == "/trace-files":
                challenge_name = str(query.get("challenge_name", "")).strip()
                model_spec = str(query.get("model_spec", "")).strip()
                if not challenge_name or not model_spec:
                    _json_response(
                        "400 Bad Request",
                        {"error": "challenge_name and model_spec are required"},
                    )
                else:
                    trace_files = [
                        trace_path.name
                        for trace_path in list_trace_files(challenge_name, model_spec)
                    ]
                    _json_response("200 OK", {"trace_files": trace_files})
            elif method == "GET" and path == "/trace":
                challenge_name = str(query.get("challenge_name", "")).strip()
                model_spec = str(query.get("model_spec", "")).strip()
                trace_name = str(query.get("trace_name", "")).strip()
                if not challenge_name or not model_spec or not trace_name:
                    _json_response(
                        "400 Bad Request",
                        {
                            "error": (
                                "challenge_name, model_spec, and trace_name are required"
                            )
                        },
                    )
                else:
                    cursor_raw = query.get("cursor")
                    limit_raw = query.get("limit")
                    try:
                        cursor = int(cursor_raw) if cursor_raw is not None else None
                        limit = int(limit_raw) if limit_raw is not None else 200
                        payload = read_trace_window(
                            challenge_name,
                            model_spec,
                            trace_name,
                            cursor=cursor,
                            limit=limit,
                        )
                    except ValueError:
                        _json_response(
                            "400 Bad Request",
                            {"error": "cursor and limit must be integers"},
                        )
                    except FileNotFoundError:
                        _json_response(
                            "404 Not Found",
                            {"error": "trace file not found for that challenge lane"},
                        )
                    else:
                        _json_response("200 OK", payload)
            elif method == "GET" and path == "/advisories":
                challenge_name = str(query.get("challenge_name", "")).strip()
                if not challenge_name:
                    _json_response(
                        "400 Bad Request",
                        {"error": "challenge_name is required"},
                    )
                else:
                    limit_raw = query.get("limit")
                    try:
                        limit = int(limit_raw) if limit_raw is not None else 12
                    except ValueError:
                        _json_response(
                            "400 Bad Request",
                            {"error": "limit must be an integer"},
                        )
                    else:
                        _json_response(
                            "200 OK",
                            collect_advisory_history(challenge_name, limit=limit),
                        )
            elif method == "POST" and path == "/msg" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5)
                try:
                    data = json.loads(body)
                    message = data.get("message", body.decode())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    message = body.decode("utf-8", errors="replace")

                inbox.put_nowait(message)
                _json_response("200 OK", {"ok": True, "queued": message[:200]})
            elif method == "POST" and path == "/bump" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}

                challenge_name = str(data.get("challenge_name", "")).strip()
                model_spec = str(data.get("model_spec", "")).strip()
                insights = str(data.get("insights", "")).strip()

                if not challenge_name or not model_spec or not insights:
                    _json_response(
                        "400 Bad Request",
                        {
                            "error": "challenge_name, model_spec, and insights are required",
                            "usage": {
                                "bump_lane": (
                                    'POST /bump {"challenge_name": "...", '
                                    '"model_spec": "...", "insights": "..."}'
                                ),
                            },
                        },
                    )
                else:
                    result = await do_bump_agent(deps, challenge_name, model_spec, insights)
                    if result.startswith("No "):
                        _json_response("404 Not Found", {"ok": False, "error": result})
                    else:
                        logger.info(
                            "Operator lane bump: challenge=%s model=%s",
                            challenge_name,
                            model_spec,
                        )
                        _json_response("200 OK", {"ok": True, "result": result})
            else:
                _json_response(
                    "400 Bad Request",
                    {
                        "error": "Unsupported request",
                        "usage": {
                            "send_message": "POST /msg {\"message\": \"...\"}",
                            "bump_lane": (
                                'POST /bump {"challenge_name": "...", '
                                '"model_spec": "...", "insights": "..."}'
                            ),
                            "status": "GET /status",
                            "status_stream": "GET /status/stream",
                            "ui": "GET /ui",
                            "trace_files": (
                                "GET /trace-files?challenge_name=...&model_spec=..."
                            ),
                            "trace": (
                                "GET /trace?challenge_name=...&model_spec=..."
                                "&trace_name=...&cursor=...&limit=..."
                            ),
                            "advisories": "GET /advisories?challenge_name=...&limit=...",
                        },
                    },
                )

            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    try:
        server = await asyncio.start_server(_handle, "127.0.0.1", port)
        actual_port = server.sockets[0].getsockname()[1]
        logger.info(f"Operator message endpoint listening on http://127.0.0.1:{actual_port}")
        return server
    except OSError as e:
        logger.warning(f"Could not start operator message endpoint: {e}")
        return None
