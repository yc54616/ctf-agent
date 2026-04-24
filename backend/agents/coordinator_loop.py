"""Shared coordinator event loop — used by both Claude SDK and Codex coordinators."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from backend.challenge_config import (
    apply_override_patch,
    challenge_config_snapshot,
    delete_override,
    discover_challenge_dirs,
    load_effective_metadata,
    load_override,
    refresh_effective_metadata,
    sanitize_override,
    write_override,
)
from backend.config import Settings
from backend.cost_tracker import CostTracker, _fmt_tokens
from backend.deps import CoordinatorDeps
from backend.instance_probe import probe_instance_connection
from backend.message_bus import CandidateRef, CoordinatorNoteRef
from backend.models import DEFAULT_MODELS
from backend.operator_ui import (
    collect_advisory_history,
    list_ui_trace_files,
    load_ui_asset,
    read_trace_window,
)
from backend.platforms import NullPlatformClient, PlatformClient, build_platform_client
from backend.poller import CTFdPoller, PlatformPoller
from backend.prompts import ChallengeMeta
from backend.solver_base import format_candidate_rejection_alert, parse_candidate_rejection_alert

logger = logging.getLogger(__name__)

# Callable type for a coordinator turn: (message) -> None
TurnFn = Callable[[str], Coroutine[Any, Any, None]]
SHUTDOWN_SWARM_GRACE_SECONDS = 8.0
UI_ALERT_MIN_TTL_SECONDS = 20.0


def _is_loop_closed_error(exc: BaseException) -> bool:
    return isinstance(exc, RuntimeError) and "Event loop is closed" in str(exc)


def _render_solver_message(event: object) -> str:
    if isinstance(event, CandidateRef):
        return event.rendered_text()

    if isinstance(event, CoordinatorNoteRef):
        parsed_rejection = parse_candidate_rejection_alert(event.summary)
        if parsed_rejection is not None:
            prefix = (
                f"[{event.challenge_name}/{event.source_model}] "
                if event.challenge_name and event.source_model
                else ""
            )
            return f"UI ALERT: {prefix}{format_candidate_rejection_alert(event.summary)}".strip()
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
        parsed_rejection = parse_candidate_rejection_alert(payload.get("summary"))
        if parsed_rejection is not None:
            return f"UI ALERT: {prefix}{format_candidate_rejection_alert(payload.get('summary'))}".strip()
        lines = [f"ADVISOR MESSAGE: {prefix}{str(payload.get('summary') or '').strip()}".rstrip()]
        pointer_text = str(payload.get("pointer_path") or "").strip()
        if pointer_text:
            lines.append(f"Pointer: {pointer_text}")
        return "\n".join(line for line in lines if line.strip())

    return f"SOLVER MESSAGE: {prefix}{str(payload.get('summary') or event).strip()}"


def _dedupe_preserve_order(messages: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for message in messages:
        if message in seen:
            continue
        seen.add(message)
        deduped.append(message)
    return deduped


def _snapshot_ui_alerts(deps: CoordinatorDeps) -> list[dict[str, Any]]:
    now = time.time()
    active_alerts = [
        dict(alert)
        for alert in deps.ui_alerts
        if float(alert.get("expires_at") or 0.0) > now
    ]
    if len(active_alerts) != len(deps.ui_alerts):
        deps.ui_alerts.clear()
        deps.ui_alerts.extend(active_alerts)
    return active_alerts


def _capture_advisor_report(deps: CoordinatorDeps, event: object) -> None:
    """Extract advisor-relevant portions of an inbox event into ``deps.advisor_reports``.

    Runs on every drained event (in addition to existing processing).  Entries
    are used by the human UI's "Advisor Reports" panel so the operator sees
    real-time strategic notes alongside the intervene controls.  Silent no-op
    when the event carries no advisor signal.
    """
    log = getattr(deps, "advisor_reports", None)
    if log is None:
        return

    def _push(record: dict[str, Any]) -> None:
        record.setdefault("ts", time.time())
        log.append(record)

    if isinstance(event, CoordinatorNoteRef):
        summary = str(event.summary or "")
        advisor_idx = summary.find("[Advisor]")
        if advisor_idx < 0:
            return
        advisor_text = summary[advisor_idx + len("[Advisor]"):].strip()
        solver_text = summary[:advisor_idx].strip()
        if not advisor_text:
            return
        _push(
            {
                "ts": float(getattr(event, "timestamp", 0.0)) or time.time(),
                "challenge_name": str(event.challenge_name or ""),
                "lane_id": str(event.source_model or ""),
                "kind": "coordinator_annotation",
                "text": advisor_text[:1500],
                "context": solver_text[:500],
            }
        )
        return

    if isinstance(event, CandidateRef):
        decision = str(getattr(event, "advisor_decision", "") or "").strip()
        note = str(getattr(event, "advisor_note", "") or "").strip()
        if not (decision or note):
            return
        _push(
            {
                "challenge_name": str(event.challenge_name or ""),
                "lane_id": ", ".join(getattr(event, "source_models", []) or []),
                "kind": "candidate_review",
                "advisor_decision": decision or "insufficient",
                "flag": str(getattr(event, "flag", "") or ""),
                "text": (note or f"Flag {event.flag!r}: {decision or 'insufficient'}")[:1500],
            }
        )


def _capture_solver_ui_alert(deps: CoordinatorDeps, event: object) -> bool:
    note = event if isinstance(event, CoordinatorNoteRef) else CoordinatorNoteRef.from_snapshot(event)
    if note is None:
        return False
    parsed = parse_candidate_rejection_alert(note.summary)
    if parsed is None:
        return False
    cooldown_seconds = max(
        int(parsed.get("cooldown_seconds") or 0),
        int(UI_ALERT_MIN_TTL_SECONDS),
    )
    deps.ui_alerts.append(
        {
            "id": f"candidate-rejected:{note.challenge_name}:{note.source_model}:{int(note.timestamp * 1000)}",
            "kind": "candidate_rejected",
            "tone": "warn",
            "ts": note.timestamp,
            "expires_at": note.timestamp + cooldown_seconds,
            "challenge_name": note.challenge_name,
            "lane_id": note.source_model,
            "message": format_candidate_rejection_alert(note.summary),
        }
    )
    return True


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


def _known_challenge_names(deps: CoordinatorDeps, poller: PlatformPoller | None) -> set[str]:
    poller_names = set(poller.known_challenges) if poller is not None else set()
    return poller_names | _local_known_challenge_names(deps) | set(deps.results)


def _known_solved_names(deps: CoordinatorDeps, poller: PlatformPoller | None) -> set[str]:
    poller_names = set(poller.known_solved) if poller is not None else set()
    return poller_names | _restored_solved_names(deps)


def build_deps(
    settings: Settings,
    model_specs: list[str] | None = None,
    challenges_root: str = "challenges",
    no_submit: bool = False,
    local_mode: bool = False,
    cookie_header: str = "",
    challenge_dirs: dict[str, str] | None = None,
    challenge_metas: dict[str, ChallengeMeta] | None = None,
) -> tuple[PlatformClient, CostTracker, CoordinatorDeps]:
    """Create the remote platform client, cost tracker, and coordinator deps."""
    cost_tracker = CostTracker()
    specs = model_specs or list(DEFAULT_MODELS)
    Path(challenges_root).mkdir(parents=True, exist_ok=True)

    deps = CoordinatorDeps(
        ctfd=NullPlatformClient(),
        cost_tracker=cost_tracker,
        settings=settings,
        model_specs=specs,
        challenges_root=challenges_root,
        no_submit=(no_submit or local_mode),
        local_mode=local_mode,
        max_concurrent_challenges=getattr(settings, "max_concurrent_challenges", 10),
        challenge_dirs=challenge_dirs or {},
        challenge_metas=challenge_metas or {},
        msg_host=getattr(settings, "ui_host", "127.0.0.1"),
        msg_port=getattr(settings, "ui_port", 0),
    )

    # Pre-load already-pulled challenges, including nested competition folders.
    for challenge_dir in discover_challenge_dirs(challenges_root):
        refresh_effective_metadata(challenge_dir)
        meta = ChallengeMeta.from_dict(load_effective_metadata(challenge_dir))
        if meta.name not in deps.challenge_dirs:
            deps.challenge_dirs[meta.name] = str(challenge_dir)
            deps.challenge_metas[meta.name] = meta
        result_path = challenge_dir / "solve" / "result.json"
        if result_path.exists():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except Exception:
                logger.warning("Could not restore solved result from %s", result_path)
            else:
                if isinstance(result, dict):
                    deps.results.setdefault(meta.name, result)

    deps.ctfd = build_platform_client(
        settings,
        deps.challenge_metas,
        local_mode=local_mode,
        cookie_header=cookie_header,
    )
    if getattr(deps.ctfd, "platform", "") == "local":
        deps.no_submit = True

    return deps.ctfd, cost_tracker, deps


async def cleanup_coordinator_runtime(
    deps: CoordinatorDeps,
    ctfd: PlatformClient,
    cost_tracker: CostTracker,
    *,
    reason: str | None = None,
) -> None:
    shutdown_reason = " ".join(str(reason or deps.shutdown_reason or "coordinator cleanup").split()).strip()
    for swarm in deps.swarms.values():
        prepare_for_shutdown = getattr(swarm, "prepare_for_shutdown", None)
        if callable(prepare_for_shutdown):
            try:
                prepare_for_shutdown(preserve_solver_state=False)
            except Exception:
                logger.debug("Failed to switch swarm shutdown cleanup mode", exc_info=True)
        swarm.kill(reason=shutdown_reason)
    active_swarm_tasks = [task for task in deps.swarm_tasks.values() if not task.done()]
    if active_swarm_tasks:
        done, pending = await asyncio.wait(active_swarm_tasks, timeout=SHUTDOWN_SWARM_GRACE_SECONDS)
        if pending:
            logger.warning(
                "Coordinator shutdown grace period expired after %.1fs; force-cancelling %d swarm task(s)",
                SHUTDOWN_SWARM_GRACE_SECONDS,
                len(pending),
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        if done:
            await asyncio.gather(*done, return_exceptions=True)
    cost_tracker.log_summary()
    _log_shutdown_cost_details(deps, cost_tracker)
    try:
        await ctfd.close()
    except Exception:
        pass


def _lane_last_command(deps: CoordinatorDeps, agent_name: str) -> str:
    challenge_name, _, _model_name = agent_name.rpartition("/")
    if not challenge_name:
        return ""
    swarm = deps.swarms.get(challenge_name)
    if swarm is None:
        return ""
    for solver in getattr(swarm, "solvers", {}).values():
        if getattr(solver, "agent_name", "") != agent_name:
            continue
        runtime_getter = getattr(solver, "get_runtime_status", None)
        if not callable(runtime_getter):
            return ""
        runtime = runtime_getter()
        return str(runtime.get("current_command") or runtime.get("last_command") or "").strip()
    return ""


def _challenge_usage_snapshot(cost_tracker: CostTracker) -> dict[str, dict[str, float | int]]:
    by_challenge: dict[str, dict[str, float | int]] = {}
    for agent_name, agent in cost_tracker.by_agent.items():
        challenge_name, _, _model_name = agent_name.rpartition("/")
        if not challenge_name:
            continue
        bucket = by_challenge.setdefault(
            challenge_name,
            {
                "cost_usd": 0.0,
                "duration_seconds": 0.0,
                "total_tokens": 0,
                "input_tokens": 0,
                "cached_tokens": 0,
                "output_tokens": 0,
            },
        )
        bucket["cost_usd"] = float(bucket["cost_usd"]) + float(agent.cost_usd)
        bucket["duration_seconds"] = float(bucket["duration_seconds"]) + float(agent.duration_seconds)
        bucket["total_tokens"] = int(bucket["total_tokens"]) + int(agent.usage.total_tokens)
        bucket["input_tokens"] = int(bucket["input_tokens"]) + int(agent.usage.input_tokens)
        bucket["cached_tokens"] = int(bucket["cached_tokens"]) + int(agent.usage.cache_read_tokens)
        bucket["output_tokens"] = int(bucket["output_tokens"]) + int(agent.usage.output_tokens)

    for usage in by_challenge.values():
        usage["cost_usd"] = round(float(usage["cost_usd"]), 4)
        usage["duration_seconds"] = round(float(usage["duration_seconds"]), 3)

    return by_challenge


def _challenge_usage_or_default(
    usage_by_challenge: dict[str, dict[str, float | int]],
    challenge_name: str,
) -> dict[str, float | int]:
    usage = usage_by_challenge.get(challenge_name)
    if usage:
        return dict(usage)
    return {
        "cost_usd": 0.0,
        "duration_seconds": 0.0,
        "total_tokens": 0,
        "input_tokens": 0,
        "cached_tokens": 0,
        "output_tokens": 0,
    }


def _log_shutdown_cost_details(deps: CoordinatorDeps, cost_tracker: CostTracker) -> None:
    usage_by_agent = cost_tracker.get_usage_by_agent()
    lane_usage = {agent_name: stats for agent_name, stats in usage_by_agent.items() if "/" in agent_name}
    if not lane_usage:
        return

    by_challenge = _challenge_usage_snapshot(cost_tracker)

    if by_challenge:
        top_challenge_name, top_challenge_stats = max(
            by_challenge.items(),
            key=lambda item: (float(item[1]["cost_usd"]), int(item[1]["input_tokens"]), item[0]),
        )
        logger.info(
            "  Top challenge: %s | $%.2f | %s in / %s cached / %s out",
            top_challenge_name,
            float(top_challenge_stats["cost_usd"]),
            _fmt_tokens(int(top_challenge_stats["input_tokens"])),
            _fmt_tokens(int(top_challenge_stats["cached_tokens"])),
            _fmt_tokens(int(top_challenge_stats["output_tokens"])),
        )

    hot_agent_name, hot_agent_stats = max(
        lane_usage.items(),
        key=lambda item: (
            int(item[1].get("input", 0) or 0),
            float(item[1].get("cost", 0.0) or 0.0),
            item[0],
        ),
    )
    last_command = _lane_last_command(deps, hot_agent_name) or "-"
    logger.info(
        "  Peak lane: %s | %s in / %s cached / %s out | last=%s",
        hot_agent_name,
        _fmt_tokens(int(hot_agent_stats.get("input", 0) or 0)),
        _fmt_tokens(int(hot_agent_stats.get("cached", 0) or 0)),
        _fmt_tokens(int(hot_agent_stats.get("output", 0) or 0)),
        last_command,
    )


async def run_event_loop(
    deps: CoordinatorDeps,
    ctfd: PlatformClient,
    cost_tracker: CostTracker,
    turn_fn: TurnFn,
    status_interval: int = 60,
    propagate_fatal: bool = False,
    cleanup_runtime_on_exit: bool = True,
    auto_spawn: bool = True,
) -> dict[str, Any]:
    """Run the shared coordinator event loop.

    Args:
        deps: Coordinator dependencies (shared state).
        ctfd: Remote platform client (for poller).
        cost_tracker: Cost tracker.
        turn_fn: Async function that sends a message to the coordinator LLM.
            In human mode this is a no-op logging function.
        status_interval: Seconds between status updates.
        auto_spawn: When True (default / LLM mode) unsolved challenges are
            spawned automatically.  Set to False in human mode so the human
            decides which challenges to tackle via the operator UI.
    """
    poller: PlatformPoller | None = None
    if not deps.local_mode:
        poller = CTFdPoller(ctfd=ctfd, interval_s=5.0)
        await poller.start()
    # Expose the poller on deps so HTTP handlers can re-wire it after a
    # ctfd-config change and surface its status in the snapshot.
    deps.poller = poller

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
    if deps.local_mode:
        initial_msg = (
            f"LOCAL MODE. {len(known_challenges)} local challenges loaded, "
            f"{len(known_solved)} already marked solved.\n"
            f"Unsolved: {sorted(unsolved) if unsolved else 'NONE'}\n"
            "Spawn swarms for all unsolved local challenges. Do not fetch or submit remotely."
        )
    else:
        initial_msg = (
            f"CTF is LIVE. {len(known_challenges)} challenges, "
            f"{len(known_solved)} solved.\n"
            f"Unsolved: {sorted(unsolved) if unsolved else 'NONE'}\n"
            "Fetch challenges and spawn swarms for all unsolved."
        )

    shutdown_reason = ""
    try:
        await turn_fn(initial_msg)

        # Auto-spawn swarms for unsolved challenges if coordinator LLM didn't.
        # Skipped in human mode — the human picks challenges via the operator UI.
        if auto_spawn:
            await _auto_spawn_unsolved(deps, poller)

        last_status = asyncio.get_running_loop().time()

        while True:
            if deps.shutdown_event.is_set():
                shutdown_reason = deps.shutdown_reason or "coordinator shutdown requested"
                logger.info("Coordinator shutdown requested: %s", shutdown_reason)
                break
            events = []
            evt = None
            if poller is not None:
                try:
                    evt = await poller.get_event(timeout=5.0)
                except RuntimeError as exc:
                    if _is_loop_closed_error(exc):
                        shutdown_reason = "coordinator loop closed during shutdown"
                        logger.info("Coordinator loop is closing; shutting down cleanly")
                        break
                    raise
            else:
                await asyncio.sleep(5.0)
            if evt:
                events.append(evt)
            if poller is not None:
                events.extend(poller.drain_events())
            deps.known_challenge_count = len(_known_challenge_names(deps, poller))
            deps.known_solved_count = len(_known_solved_names(deps, poller))

            # Auto-kill swarms for solved challenges
            for evt in events:
                if evt.kind == "challenge_solved" and evt.challenge_name in deps.swarms:
                    swarm = deps.swarms[evt.challenge_name]
                    if not swarm.cancel_event.is_set():
                        swarm.kill(reason=f"challenge solved elsewhere: {evt.challenge_name}")
                        logger.info("Auto-killed swarm for: %s", evt.challenge_name)
                if evt.kind == "challenge_solved":
                    from backend.agents.coordinator_core import _drop_pending_swarm

                    _drop_pending_swarm(deps, evt.challenge_name)

            parts: list[str] = []
            for evt in events:
                if evt.kind == "new_challenge":
                    parts.append(f"NEW CHALLENGE: '{evt.challenge_name}' appeared. Spawn a swarm.")
                    # Auto-spawn for new challenges (skipped in human mode).
                    if auto_spawn:
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
                from backend.agents.coordinator_core import _retire_finished_swarms

                _retire_finished_swarms(deps)

            # Drain solver-to-coordinator messages
            while True:
                try:
                    solver_msg = deps.coordinator_inbox.get_nowait()
                    # Mirror any advisor signal into deps.advisor_reports for the
                    # human UI's reports panel (non-destructive — runs before
                    # other processing).
                    _capture_advisor_report(deps, solver_msg)
                    if _capture_solver_ui_alert(deps, solver_msg):
                        continue
                    parts.append(_render_solver_message(solver_msg))
                except asyncio.QueueEmpty:
                    break

            if deps.pending_swarm_queue and len(deps.swarms) < deps.max_concurrent_challenges:
                from backend.agents.coordinator_core import _fill_swarm_capacity

                spawned = await _fill_swarm_capacity(deps)
                for name in spawned:
                    parts.append(f"QUEUED SWARM STARTED: '{name}' moved from queue to active run.")

            # Drain operator messages
            while True:
                try:
                    op_msg = deps.operator_inbox.get_nowait()
                    parts.append(f"OPERATOR MESSAGE: {op_msg}")
                    logger.info("Operator message: %s", op_msg[:200])
                except asyncio.QueueEmpty:
                    break

            # Periodic status update — only when there are active swarms or other events
            now = asyncio.get_running_loop().time()
            is_human = bool(getattr(deps, "human_mode", False))
            if now - last_status >= status_interval:
                last_status = now
                active = [n for n, t in deps.swarm_tasks.items() if not t.done()]
                solved_set = _known_solved_names(deps, poller)
                unsolved_set = _known_challenge_names(deps, poller) - solved_set
                status_line = (
                    f"STATUS: {len(solved_set)} solved, {len(unsolved_set)} unsolved, "
                    f"{len(active)} active swarms. Cost: ${cost_tracker.total_cost_usd:.2f}"
                )
                # In human mode the operator already sees these metrics live in the
                # top bar via SSE snapshots — don't spam the event feed / log with them.
                # In LLM mode, only forward when there's something happening.
                if not is_human:
                    if active or parts:
                        parts.append(status_line)
                    else:
                        logger.debug("Status (idle, not forwarded): %s", status_line)

            if parts:
                parts = _dedupe_preserve_order(parts)
                msg = "\n\n".join(parts)
                if is_human:
                    # Human mode — there is no AI coordinator.  `turn_fn` just
                    # forwards the message onto the operator's SSE stream.
                    logger.debug("Event -> human UI: %s", msg[:200])
                else:
                    logger.info("Event -> coordinator: %s", msg[:200])
                await turn_fn(msg)
                if deps.shutdown_event.is_set():
                    shutdown_reason = deps.shutdown_reason or "coordinator shutdown requested"
                    logger.info("Coordinator shutdown requested after turn: %s", shutdown_reason)
                    break

    except KeyboardInterrupt:
        shutdown_reason = "KeyboardInterrupt"
        logger.info("Coordinator shutting down...")
    except asyncio.CancelledError:
        shutdown_reason = "coordinator task cancelled"
        logger.info("Coordinator shutting down...")
    except Exception as e:
        shutdown_reason = f"coordinator fatal: {type(e).__name__}: {e}"
        logger.error("Coordinator fatal: %s", e, exc_info=True)
        if propagate_fatal:
            raise
    finally:
        deps.shutdown_reason = shutdown_reason or deps.shutdown_reason or "coordinator event loop exited"
        if msg_server:
            try:
                msg_server.close()
                await msg_server.wait_closed()
            except RuntimeError as exc:
                if not _is_loop_closed_error(exc):
                    raise
        if poller is not None:
            try:
                await poller.stop()
            except RuntimeError as exc:
                if not _is_loop_closed_error(exc):
                    raise
            deps.poller = None
        if cleanup_runtime_on_exit:
            await cleanup_coordinator_runtime(
                deps,
                ctfd,
                cost_tracker,
                reason=deps.shutdown_reason,
            )

    return {
        "results": deps.results,
        "total_cost_usd": cost_tracker.total_cost_usd,
        "total_tokens": cost_tracker.total_tokens,
        "shutdown_reason": deps.shutdown_reason,
    }


async def _auto_spawn_one(deps: CoordinatorDeps, challenge_name: str) -> None:
    """Auto-spawn a swarm for a single challenge if not already running."""
    if challenge_name in deps.swarms:
        return
    try:
        from backend.agents.coordinator_core import do_spawn_swarm
        result = await do_spawn_swarm(deps, challenge_name)
        if result.startswith("Swarm already queued for ") or result.startswith("Queued swarm for "):
            logger.debug("Auto-spawn %s: %s", challenge_name, result[:140])
        else:
            logger.info("Auto-spawn %s: %s", challenge_name, result[:140])
    except Exception as e:
        logger.warning(f"Auto-spawn failed for {challenge_name}: {e}")


async def _auto_spawn_unsolved(deps: CoordinatorDeps, poller: PlatformPoller | None) -> None:
    """Auto-spawn swarms for all unsolved challenges that don't have active swarms."""
    solved_names = _known_solved_names(deps, poller)
    known_names = _known_challenge_names(deps, poller)
    unsolved = known_names - solved_names
    if not unsolved:
        return
    if deps.local_mode:
        ordered = sorted(
            unsolved,
            key=lambda name: (
                -int(getattr(deps.challenge_metas.get(name), "solves", 0) or 0),
                name,
            ),
        )
    else:
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
        if name in _local_known_challenge_names(deps)
        and name not in (set(poller.known_challenges) if poller is not None else set())
    ]
    if local_only and (deps.local_mode or deps.ctfd_refresh_backoff_until > 0):
        ranked_local = sorted(
            local_only,
            key=lambda name: (
                -int(getattr(deps.challenge_metas.get(name), "solves", 0) or 0),
                name,
            ),
        )
        ordered = ranked_local + [name for name in ordered if name not in set(local_only)]
    for name in ordered:
        await _auto_spawn_one(deps, name)


def _status_snapshot(deps: CoordinatorDeps) -> dict[str, Any]:
    from backend.agents.coordinator_core import _pending_swarm_entries

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
    challenge_usage = _challenge_usage_snapshot(deps.cost_tracker)
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
    pending_entries = _pending_swarm_entries(deps)
    pending = _pending_swarms_snapshot(deps, pending_entries)
    for challenge_name, swarm in [*active.items(), *pending.items(), *finished.items()]:
        if isinstance(swarm, dict):
            swarm["usage"] = _challenge_usage_or_default(challenge_usage, challenge_name)
    return {
        "models": list(deps.model_specs),
        "session_started_at": deps.session_started_at,
        "max_concurrent_challenges": deps.max_concurrent_challenges,
        "known_challenge_count": deps.known_challenge_count,
        "known_solved_count": deps.known_solved_count,
        "active_swarm_count": len(active),
        "finished_swarm_count": len(finished),
        "pending_challenge_count": len(pending_entries),
        "pending_challenges": [entry["challenge_name"] for entry in pending_entries],
        "pending_challenge_entries": pending_entries,
        "active_swarms": active,
        "pending_swarms": pending,
        "finished_swarms": finished,
        "results": deps.results,
        "cost_usd": round(deps.cost_tracker.total_cost_usd, 4),
        "total_tokens": deps.cost_tracker.total_tokens,
        "total_step_count": live_steps + restored_steps,
        "coordinator_queue_depth": deps.coordinator_inbox.qsize(),
        "operator_queue_depth": deps.operator_inbox.qsize(),
    }


def _pending_candidate_count(challenges: list[dict[str, Any]], results: dict[str, Any]) -> int:
    count = 0
    seen: set[tuple[str, str]] = set()
    for challenge in challenges:
        challenge_name = str(challenge.get("challenge") or "").strip()
        raw_candidates = challenge.get("flag_candidates", {})
        if not isinstance(raw_candidates, dict):
            continue
        for flag, candidate in raw_candidates.items():
            if not isinstance(candidate, dict):
                continue
            if str(candidate.get("status") or "").strip().lower() not in {"confirmed", "rejected"}:
                key = (challenge_name, str(flag).strip())
                if key in seen:
                    continue
                seen.add(key)
                count += 1
    for challenge_name, payload in results.items():
        if not isinstance(payload, dict):
            continue
        raw_candidates = payload.get("flag_candidates", {})
        if not isinstance(raw_candidates, dict):
            continue
        normalized_name = str(challenge_name).strip()
        for flag, candidate in raw_candidates.items():
            if not isinstance(candidate, dict):
                continue
            if str(candidate.get("status") or "").strip().lower() not in {"confirmed", "rejected"}:
                key = (normalized_name, str(flag).strip())
                if key in seen:
                    continue
                seen.add(key)
                count += 1
    return count


def _runtime_snapshot(deps: CoordinatorDeps) -> dict[str, Any]:
    legacy = _status_snapshot(deps)
    ui_alerts = _snapshot_ui_alerts(deps)
    active = legacy.get("active_swarms", {})
    pending = legacy.get("pending_swarms", {})
    finished = legacy.get("finished_swarms", {})
    results = legacy.get("results", {})
    active_values = [value for value in active.values() if isinstance(value, dict)]
    pending_values = [value for value in pending.values() if isinstance(value, dict)]
    finished_values = [value for value in finished.values() if isinstance(value, dict)]

    healthy_lanes = 0
    stale_lanes = 0
    resetting_lanes = 0
    error_lanes = 0
    busy_lanes = 0
    for swarm in [*active_values, *finished_values]:
        agents = swarm.get("agents", {})
        if not isinstance(agents, dict):
            continue
        for agent in agents.values():
            if not isinstance(agent, dict):
                continue
            runtime_health = str(agent.get("runtime_health") or "")
            lifecycle = str(agent.get("lifecycle") or agent.get("status") or "")
            if runtime_health == "stale":
                stale_lanes += 1
            elif runtime_health == "resetting":
                resetting_lanes += 1
            else:
                healthy_lanes += 1
            if lifecycle in {"error", "quota_error"}:
                error_lanes += 1
            if lifecycle == "busy":
                busy_lanes += 1

    usage_by_model = deps.cost_tracker.get_usage_by_model()
    total_input = sum(int(entry.get("input", 0) or 0) for entry in usage_by_model.values())
    total_cached = sum(int(entry.get("cached", 0) or 0) for entry in usage_by_model.values())
    total_output = sum(int(entry.get("output", 0) or 0) for entry in usage_by_model.values())
    cache_hit_rate = (total_cached / total_input) if total_input > 0 else 0.0

    return {
        "session_started_at": legacy.get("session_started_at"),
        "health_summary": {
            "healthy_lanes": healthy_lanes,
            "stale_lanes": stale_lanes,
            "resetting_lanes": resetting_lanes,
            "error_lanes": error_lanes,
            "busy_lanes": busy_lanes,
        },
        "cost_summary": {
            "cost_usd": legacy.get("cost_usd", 0.0),
            "input_tokens": total_input,
            "cached_tokens": total_cached,
            "output_tokens": total_output,
            "cache_hit_rate": cache_hit_rate,
        },
        "challenge_summary": {
            "known_challenge_count": legacy.get("known_challenge_count", 0),
            "known_solved_count": legacy.get("known_solved_count", 0),
            "active_challenge_count": len(active_values),
            "pending_challenge_count": len(legacy.get("pending_challenges", [])),
            "pending_candidate_count": _pending_candidate_count(
                [*active_values, *pending_values, *finished_values],
                results if isinstance(results, dict) else {},
            ),
            "local_approval_enabled": bool(deps.local_mode),
            "manual_approval_enabled": True,
            "external_solve_enabled": True,
        },
        "models": legacy.get("models", []),
        "active_swarms": active,
        "pending_swarms": pending,
        "finished_swarms": finished,
        "pending_challenges": legacy.get("pending_challenges", []),
        "pending_challenge_entries": legacy.get("pending_challenge_entries", []),
        "known_challenges": _known_challenges_snapshot(deps),
        "advisor_reports": list(getattr(deps, "advisor_reports", []))[-30:],
        "solve_reports": list(getattr(deps, "solve_reports", []))[-80:],
        "persistent_directives": {
            name: list(getattr(swarm, "persistent_directives", []) or [])
            for name, swarm in deps.swarms.items()
            if getattr(swarm, "persistent_directives", None)
        },
        "results": results,
        "known_challenge_count": legacy.get("known_challenge_count", 0),
        "known_solved_count": legacy.get("known_solved_count", 0),
        "active_swarm_count": legacy.get("active_swarm_count", 0),
        "finished_swarm_count": legacy.get("finished_swarm_count", 0),
        "pending_challenge_count": legacy.get("pending_challenge_count", 0),
        "cost_usd": legacy.get("cost_usd", 0.0),
        "total_step_count": legacy.get("total_step_count", 0),
        "signals": {
            "coordinator_queue_depth": legacy.get("coordinator_queue_depth", 0),
            "operator_queue_depth": legacy.get("operator_queue_depth", 0),
        },
        "ui_alerts": ui_alerts,
        "no_submit": bool(deps.no_submit),
        "local_mode": bool(deps.local_mode),
        "human_mode": bool(getattr(deps, "human_mode", False)),
        "mode": "human" if getattr(deps, "human_mode", False) else "llm",
        "poller_status": (
            deps.poller.status()
            if getattr(deps, "poller", None) is not None
            and callable(getattr(deps.poller, "status", None))
            else {
                "healthy": True,
                "failure_count": 0,
                "last_error": "",
                "interval_s": 0,
                "known_challenges": 0,
                "known_solved": 0,
                "platform": "local" if deps.local_mode else "remote",
            }
        ),
    }


def _pending_swarms_snapshot(
    deps: CoordinatorDeps,
    pending_entries: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    pending: dict[str, dict[str, Any]] = {}
    for entry in pending_entries:
        challenge_name = str(entry.get("challenge_name") or "").strip()
        if not challenge_name:
            continue
        result = deps.results.get(challenge_name, {})
        result_dict = result if isinstance(result, dict) else {}
        pending[challenge_name] = {
            "challenge": challenge_name,
            "started_at": result_dict.get("started_at"),
            "winner": result_dict.get("flag") or result_dict.get("status") or "",
            "winner_model": result_dict.get("winner_model") or "",
            "agents": {},
            "step_count": int(result_dict.get("step_count", 0) or 0),
            "status": str(result_dict.get("status") or "pending"),
            "candidate_review_mode": str(result_dict.get("candidate_review_mode") or ""),
            "flag_candidates": result_dict.get("flag_candidates") or {},
            "coordinator_advisor_note": str(result_dict.get("coordinator_advisor_note") or ""),
            "shared_finding": str(result_dict.get("shared_finding") or ""),
            "shared_findings": result_dict.get("shared_findings") or {},
            "pending_reason": str(entry.get("reason") or "queued"),
            "pending_priority": bool(entry.get("priority")),
            "pending_local_preloaded": bool(entry.get("local_preloaded")),
            "signals": {
                "total_posts": 0,
                "total_checks": 0,
                "total_delivered": 0,
                "coordinator_messages": 0,
                "advisor_lane_hints": 0,
                "advisor_coordinator_appends": 0,
            },
        }
    return pending


def _ctfd_summary(deps: CoordinatorDeps) -> dict[str, Any]:
    """Return a UI-safe summary of the current CTFd connection (never leaks tokens)."""
    base_url = str(getattr(deps.ctfd, "base_url", "") or getattr(deps.settings, "ctfd_url", "") or "")
    platform = str(getattr(deps.ctfd, "platform", "") or "remote")
    username = str(getattr(deps.settings, "ctfd_user", "") or "")
    token_raw = str(getattr(deps.settings, "ctfd_token", "") or "")
    return {
        "configured": bool(base_url),
        "base_url": base_url,
        "platform": platform,
        "username": username,
        "token_present": bool(token_raw),
        "token_length": len(token_raw),
        "local_mode": bool(deps.local_mode),
    }


async def _rebuild_ctfd_client(deps: CoordinatorDeps, *, base_url: str, token: str) -> dict[str, Any]:
    """Rebuild ``deps.ctfd`` with a new URL / token combo at runtime.

    Closes the previous httpx client, replaces the client on deps, updates
    settings, AND rewires the background poller to the new client so it
    stops hitting "Cannot send a request, as the client has been closed".
    Resets the poller's exponential backoff so the switch takes effect on
    the next tick instead of waiting minutes.
    """
    base_url = base_url.strip().rstrip("/")
    token = token.strip()
    if not base_url:
        return {"ok": False, "error": "base_url is required"}

    # Build the NEW client first so we can keep the old one alive until the
    # swap succeeds — prevents a window where deps.ctfd is None.
    try:
        from backend.platforms.factory import build_platform_client
        # Update settings before building so platform factory sees the new URL/token.
        deps.settings.ctfd_url = base_url
        deps.settings.ctfd_token = token
        new_client = build_platform_client(
            settings=deps.settings,
            challenge_metas=deps.challenge_metas,
            cookie_header=str(getattr(deps.settings, "remote_cookie_header", "") or ""),
            local_mode=deps.local_mode,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("CTFd client rebuild failed: %s", exc)
        return {"ok": False, "error": f"Rebuild failed: {exc}"}

    old_client = deps.ctfd
    deps.ctfd = new_client

    # Point the poller at the new client BEFORE closing the old one so an
    # in-flight poll doesn't hit a closed client mid-tick.
    poller = getattr(deps, "poller", None)
    if poller is not None:
        try:
            poller.ctfd = new_client
            reset_fn = getattr(poller, "reset_backoff", None)
            if callable(reset_fn):
                reset_fn()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Poller rewire failed: %s", exc)

    # Now it's safe to close the previous client.
    close_fn = getattr(old_client, "close", None)
    if callable(close_fn):
        try:
            await close_fn()
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.debug("Previous CTFd client close failed: %s", exc)

    logger.info("CTFd client reconfigured (base_url=%s, token=%s)", base_url, "***" if token else "none")
    return {"ok": True}


# Cookies that are NOT CTFd auth but still legitimately present — operators
# who paste their full browser Cookie header will have some of these.  We only
# warn when NONE of the known auth cookies appear.
_KNOWN_AUTH_COOKIE_NAMES = {"session", "remember_token", "remember_me"}
_KNOWN_BOT_COOKIE_NAMES = {
    "ctf_clearance", "cf_clearance", "__cf_bm", "__cf_waitingroom",
    "cf_chl_2", "cf_chl_prog", "cf_chl_rc_i", "cf_chl_rc_m",
    "_ga", "_gid", "_gat", "_gcl_au",
}


def _cookie_summary(deps: CoordinatorDeps) -> dict[str, Any]:
    """Return a safe, UI-ready summary of the currently configured CTFd cookie.

    Never returns the raw cookie value — only metadata suitable for display
    (length, number of cookie pairs, names present, first-seen timestamp).
    Also flags the common mistake of loading a CloudFlare / bot-check cookie
    without the actual CTFd ``session=`` pair.
    """
    raw = str(getattr(deps.settings, "remote_cookie_header", "") or "").strip()
    base_url = str(getattr(deps.ctfd, "base_url", "") or getattr(deps.settings, "ctfd_url", "") or "")
    platform = str(getattr(deps.ctfd, "platform", "") or "remote")
    username = str(getattr(deps.settings, "ctfd_user", "") or "")
    token_present = bool(str(getattr(deps.settings, "ctfd_token", "") or "").strip())

    names: list[str] = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        names.append(chunk.split("=", 1)[0])

    lower_names = {n.lower() for n in names}
    has_auth_cookie = bool(lower_names & _KNOWN_AUTH_COOKIE_NAMES)
    bot_only = (
        bool(lower_names)
        and not has_auth_cookie
        and all(
            (name.lower() in _KNOWN_BOT_COOKIE_NAMES)
            or name.lower().startswith(("cf_", "__cf_"))
            for name in names
        )
    )
    warning = ""
    if bot_only:
        warning = (
            "Cookie has CloudFlare / bot-check entries but no CTFd session "
            "cookie.  Log in at the CTFd site in your browser first, then copy "
            "the Cookie header — you should see a 'session=...' pair.  "
            "ctf_clearance / cf_clearance alone is NOT auth."
        )
    elif names and not has_auth_cookie:
        warning = (
            "No 'session' or 'remember_token' cookie in the pasted header.  "
            "CTFd expects a 'session=...' pair — did you copy the cookie "
            "before logging in?"
        )

    return {
        "configured": bool(raw),
        "length": len(raw),
        "cookie_count": len(names),
        "cookie_names": names[:16],
        "has_auth_cookie": has_auth_cookie,
        "warning": warning,
        "base_url": base_url,
        "platform": platform,
        "username": username,
        "token_present": token_present,
        "source": str(getattr(deps, "cookie_source", "") or ""),
    }


_COOKIE_ATTR_KEYS = {
    "path", "domain", "expires", "max-age", "secure", "httponly",
    "samesite", "priority", "partitioned",
}


def _sanitize_cookie_header(raw: str) -> str:
    """Strip Set-Cookie attributes that users often paste by accident.

    Inputs like ``session=abc; Path=/; Secure; HttpOnly; SameSite=Lax``
    come from DevTools' Response → Set-Cookie view — but those extra
    attributes are NOT valid Cookie header values to send back to the
    server.  We keep only ``name=value`` pairs whose key isn't a known
    cookie attribute.
    """
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw[len("cookie:"):].strip()

    keepers: list[str] = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        name = part.split("=", 1)[0].strip().lower()
        if name in _COOKIE_ATTR_KEYS:
            continue
        # Bare flags with no "=" sign are attributes too.
        if "=" not in part:
            continue
        keepers.append(part)
    return "; ".join(keepers)


_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


async def _run_single_probe(
    client: "httpx.AsyncClient",  # type: ignore[name-defined]  # forward-ref; import inside
    probe_url: str,
    *,
    cookie_header: str = "",
    auth_token: str = "",
) -> dict[str, Any]:
    """One HTTP GET against probe_url with the given auth bits.

    Returns a dict with ``ok``, ``status``, ``url``, ``body_preview`` and
    an ``error`` description on failure.  Doesn't try to be clever about
    what it means — the caller aggregates outcomes across several attempts.
    """
    from urllib.parse import urlparse

    headers: dict[str, str] = {
        "User-Agent": _BROWSER_UA,
        "Accept": "application/json, text/plain, */*",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    if auth_token:
        headers["Authorization"] = f"Token {auth_token}"

    try:
        resp = await client.get(probe_url, headers=headers)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"request failed: {exc}", "url": probe_url, "status": 0}

    status = resp.status_code
    final_url = str(resp.url)
    final_path = urlparse(final_url).path.lower()
    body_preview = (resp.text or "")[:200].strip()

    # Landed on a login URL after redirects → auth rejected even though HTTP says 200.
    if status == 200 and any(m in final_path for m in ("/login", "/auth/login", "/signin")):
        return {
            "ok": False, "status": status,
            "error": f"landed on {final_url} — treated as login redirect",
            "url": probe_url, "final_url": final_url, "body_preview": body_preview,
        }

    if status == 200:
        if "application/json" in (resp.headers.get("content-type") or "").lower():
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = None
            if isinstance(body, dict) and body.get("success") is False:
                return {
                    "ok": False, "status": status,
                    "error": f"CTFd said: {body.get('message') or body.get('errors') or 'auth failed'}",
                    "url": probe_url, "body_preview": body_preview,
                }
            user = ""
            if isinstance(body, dict):
                data = body.get("data") or {}
                if isinstance(data, dict):
                    user = str(data.get("name") or data.get("username") or "")
            return {
                "ok": True, "status": status, "user": user,
                "url": probe_url, "final_url": final_url,
            }
        # Non-JSON 200: OK only if the URL is an HTML page (e.g. /challenges) and
        # the body doesn't look like a login form.  Very heuristic.
        login_markers = ("<form", "login", "password", "csrf", "sign in")
        if any(m in body_preview.lower() for m in login_markers):
            return {
                "ok": False, "status": status,
                "error": "200 OK but page body looks like a login form",
                "url": probe_url, "final_url": final_url, "body_preview": body_preview,
            }
        return {
            "ok": True, "status": status, "user": "",
            "url": probe_url, "final_url": final_url,
            "note": "200 OK (non-JSON, did not parse user)",
        }

    if status in {301, 302, 303, 307, 308}:
        location = resp.headers.get("location", "")
        return {
            "ok": False, "status": status,
            "error": f"redirected to {location or '?'} — auth rejected",
            "url": probe_url, "body_preview": body_preview,
        }
    if status == 401:
        return {
            "ok": False, "status": status,
            "error": "401 Unauthorized",
            "url": probe_url, "body_preview": body_preview,
        }
    if status == 403:
        cf = "cloudflare" in body_preview.lower()
        return {
            "ok": False, "status": status,
            "error": f"403 Forbidden{' — CloudFlare block' if cf else ''}",
            "url": probe_url, "body_preview": body_preview,
        }
    return {
        "ok": False, "status": status,
        "error": f"HTTP {status}",
        "url": probe_url, "body_preview": body_preview,
    }


async def _probe_cookie(deps: CoordinatorDeps, cookie_header: str) -> dict[str, Any]:
    """Verify a CTFd session cookie by trying several auth paths in order.

    Strategy:

    1. GET ``/api/v1/users/me`` with **cookie only** (no Authorization header).
       This is the canonical "am I logged in" check and isolates the cookie
       from a possibly-bad API token.
    2. If (1) returns 404 (API path missing on old CTFd builds), fall back
       to GET ``/challenges`` with cookie only.
    3. If (1) returns 401 AND a token is configured, try (1) again with
       BOTH cookie and token — some CTFd deployments gate certain endpoints
       behind token auth.
    4. If all attempts fail, report the most informative failure
       (``attempts`` list included for the UI to display).

    Returns the same shape as before (ok / status / error / body_preview /
    probe_url / final_url) plus ``attempts`` for debugging.
    """
    base_url = str(getattr(deps.ctfd, "base_url", "") or getattr(deps.settings, "ctfd_url", "") or "")
    if not base_url:
        return {"ok": False, "error": "No CTFd URL configured"}

    cleaned = _sanitize_cookie_header(cookie_header)
    token = str(getattr(deps.settings, "ctfd_token", "") or "").strip()
    base = base_url.rstrip("/")
    api_url = base + "/api/v1/users/me"
    chal_url = base + "/challenges"

    import httpx

    attempts: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=10.0, verify=False, follow_redirects=True) as client:
        # 1) /api/v1/users/me with cookie ONLY
        r1 = await _run_single_probe(client, api_url, cookie_header=cleaned, auth_token="")
        r1["label"] = "GET /api/v1/users/me (cookie only)"
        attempts.append(r1)
        if r1.get("ok"):
            return {
                "ok": True,
                "status": r1["status"],
                "user": r1.get("user", ""),
                "probe_url": api_url,
                "final_url": r1.get("final_url", ""),
                "attempts": attempts,
            }

        # 2) fallback to /challenges on old CTFd builds (404 on /api/v1/users/me)
        if r1.get("status") in {404, 405}:
            r2 = await _run_single_probe(client, chal_url, cookie_header=cleaned, auth_token="")
            r2["label"] = "GET /challenges (cookie only, fallback)"
            attempts.append(r2)
            if r2.get("ok"):
                return {
                    "ok": True,
                    "status": r2["status"],
                    "user": "",
                    "probe_url": chal_url,
                    "final_url": r2.get("final_url", ""),
                    "attempts": attempts,
                    "note": "verified via /challenges HTML (no /api/v1/users/me on this CTFd)",
                }

        # 3) 401 on step 1 + token available → maybe endpoint wants token auth
        if r1.get("status") == 401 and token:
            r3 = await _run_single_probe(client, api_url, cookie_header=cleaned, auth_token=token)
            r3["label"] = "GET /api/v1/users/me (cookie + token)"
            attempts.append(r3)
            if r3.get("ok"):
                return {
                    "ok": True,
                    "status": r3["status"],
                    "user": r3.get("user", ""),
                    "probe_url": api_url,
                    "final_url": r3.get("final_url", ""),
                    "attempts": attempts,
                    "note": "endpoint required API token in addition to cookie",
                }

    # Pick the most informative failure for the top-level error message.
    best = r1
    for attempt in attempts[1:]:
        # Prefer the last attempt if it provided more detail.
        if attempt.get("body_preview") and not best.get("body_preview"):
            best = attempt
    primary_error = best.get("error") or "probe failed"
    # Dedicated guidance for the classic "401 with token present" pattern.
    if r1.get("status") == 401 and token:
        primary_error += " — API token may also be invalid; try clearing it and probing with cookie alone."
    elif r1.get("status") == 401 and not token:
        primary_error += " — cookie likely expired (re-login in browser, copy fresh `session=…` cookie)."

    return {
        "ok": False,
        "status": best.get("status", 0),
        "error": primary_error,
        "probe_url": best.get("url", api_url),
        "final_url": best.get("final_url", ""),
        "body_preview": best.get("body_preview", ""),
        "attempts": attempts,
    }


async def _auto_import_remote_challenges(
    deps: CoordinatorDeps,
    remote_challenges: list[dict[str, Any]],
    *,
    concurrency: int = 4,
) -> dict[str, Any]:
    """Pull each remote CTFd challenge to disk and refresh deps state.

    Skips challenges that are already present in ``deps.challenge_dirs``.
    Uses ``deps.ctfd.pull_challenge`` which writes ``metadata.yml`` +
    downloads attachments under ``distfiles/`` per challenge.

    Returns a summary dict:
      - imported: list of newly-imported challenge names
      - skipped:  list of already-on-disk names
      - failed:   list of (name, error) tuples
    """
    from urllib.parse import urlsplit

    from backend.challenge_config import (
        discover_challenge_dirs,
        load_effective_metadata,
        refresh_effective_metadata,
    )
    from backend.prompts import ChallengeMeta

    host = ""
    try:
        host = str(urlsplit(getattr(deps.ctfd, "base_url", "") or "").hostname or "")
    except Exception:
        host = ""
    host_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", host).strip("-") or "fetched"

    target_root = Path(deps.challenges_root).resolve() / host_slug
    target_root.mkdir(parents=True, exist_ok=True)

    pull_fn = getattr(deps.ctfd, "pull_challenge", None)
    if not callable(pull_fn):
        return {
            "imported": [],
            "skipped": [],
            "failed": [],
            "error": "Platform client does not support pull_challenge (auto-import unavailable)",
        }

    # Diff: only pull challenges not already known to deps.
    to_pull: list[dict[str, Any]] = []
    skipped: list[str] = []
    for ch in remote_challenges:
        name = str(ch.get("name") or "").strip()
        if not name:
            continue
        if name in deps.challenge_dirs:
            skipped.append(name)
            continue
        to_pull.append(ch)

    imported: list[str] = []
    failed: list[tuple[str, str]] = []

    if not to_pull:
        return {"imported": imported, "skipped": skipped, "failed": failed}

    sem = asyncio.Semaphore(max(1, concurrency))

    async def _pull_one(ch: dict[str, Any]) -> None:
        name = str(ch.get("name") or "").strip() or "?"
        async with sem:
            try:
                await asyncio.wait_for(pull_fn(ch, str(target_root)), timeout=120)
                imported.append(name)
            except Exception as exc:  # noqa: BLE001 — bubble up per-challenge failure
                logger.warning("auto-import: %s failed: %s", name, exc)
                failed.append((name, str(exc)))

    await asyncio.gather(*(_pull_one(ch) for ch in to_pull), return_exceptions=False)

    # Refresh deps with newly imported challenges.
    for challenge_dir in discover_challenge_dirs(deps.challenges_root):
        try:
            refresh_effective_metadata(challenge_dir)
            meta = ChallengeMeta.from_dict(load_effective_metadata(challenge_dir))
        except Exception as exc:  # noqa: BLE001
            logger.warning("auto-import: could not load meta at %s: %s", challenge_dir, exc)
            continue
        if meta.name not in deps.challenge_dirs:
            deps.challenge_dirs[meta.name] = str(challenge_dir)
            deps.challenge_metas[meta.name] = meta

    # Clear remote cache entries that are now on disk — they'll be surfaced as
    # local records by _known_challenges_snapshot instead.
    for name in imported:
        deps.remote_challenge_cache.pop(name, None)

    return {"imported": imported, "skipped": skipped, "failed": failed}


def _known_challenges_snapshot(deps: CoordinatorDeps) -> list[dict[str, Any]]:
    """Return every challenge known to deps (on-disk metas + cached remote fetches).

    This lets the human-mode UI display challenges that haven't been spawned yet,
    including ones pulled in by the Fetch button but not yet imported to disk.
    Entries carry a `source` field ("local" / "remote") so the UI can style them.
    """
    records: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 1) Challenges discovered on disk (via discover_challenge_dirs).
    for name, meta in deps.challenge_metas.items():
        if not name or name in seen:
            continue
        seen.add(name)
        records.append(
            {
                "name": name,
                "category": str(getattr(meta, "category", "") or ""),
                "value": int(getattr(meta, "value", 0) or 0),
                "description": str(getattr(meta, "description", "") or "")[:400],
                "source": "local",
                "local_preloaded": name in deps.challenge_dirs,
            }
        )

    # 2) Remote-only challenges cached by the most recent Fetch call
    #    (raw CTFd dicts — description may be HTML; category/value fields
    #    are preserved as-is from the platform).
    platform_label = str(getattr(deps.ctfd, "platform", "") or "remote")
    for name, record in getattr(deps, "remote_challenge_cache", {}).items():
        if not name or name in seen:
            continue
        seen.add(name)
        records.append(
            {
                "name": str(name),
                "category": str(record.get("category") or ""),
                "value": int(record.get("value", 0) or 0),
                "description": str(record.get("description") or "")[:400],
                "source": platform_label,
                "local_preloaded": False,
            }
        )

    records.sort(key=lambda r: (r.get("category", ""), r.get("name", "")))
    return records


def _refresh_challenge_meta(deps: CoordinatorDeps, challenge_name: str) -> ChallengeMeta | None:
    challenge_dir = deps.challenge_dirs.get(challenge_name)
    if not challenge_dir:
        return None
    refresh_effective_metadata(challenge_dir)
    meta = ChallengeMeta.from_dict(load_effective_metadata(challenge_dir))
    deps.challenge_metas[challenge_name] = meta
    swarm = deps.swarms.get(challenge_name)
    if swarm is not None:
        swarm.meta = meta
    return meta


def _challenge_config_payload(deps: CoordinatorDeps, challenge_name: str) -> dict[str, Any] | None:
    challenge_dir = deps.challenge_dirs.get(challenge_name)
    if not challenge_dir:
        return None
    payload = challenge_config_snapshot(challenge_dir)
    payload["challenge_name"] = challenge_name
    payload["override_present"] = bool(payload.get("override"))
    return payload


async def _start_msg_server(
    inbox: asyncio.Queue,
    deps: CoordinatorDeps,
    port: int = 0,
) -> asyncio.Server | None:
    """Start a tiny HTTP server that accepts operator messages and exposes status."""

    from backend.agents.coordinator_core import (
        do_add_persistent_directive,
        do_advisor_intervene,
        do_approve_flag_candidate,
        do_bump_agent,
        do_clear_challenge_history,
        do_list_persistent_directives,
        do_mark_challenge_solved,
        do_reject_flag_candidate,
        do_remove_persistent_directive,
        do_request_status_report,
        do_restart_challenge,
        do_set_challenge_priority_waiting,
        do_set_max_concurrent_challenges,
    )

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        def _write_response(
            status: str,
            body: bytes,
            content_type: str,
            *,
            extra_headers: str = "",
        ) -> None:
            writer.write(
                (
                    f"HTTP/1.1 {status}\r\n"
                    f"Content-Type: {content_type}\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    "Connection: close\r\n"
                    f"{extra_headers}"
                    "\r\n"
                ).encode()
                + body
            )

        def _json_response(status: str, payload: dict[str, Any]) -> None:
            _write_response(status, json.dumps(payload).encode(), "application/json")

        async def _runtime_stream_response() -> None:
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
                payload = json.dumps(_runtime_snapshot(deps), sort_keys=True)
                if payload != previous_payload:
                    writer.write(f"event: snapshot\ndata: {payload}\n\n".encode())
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

            if method == "GET" and path == "/api/runtime/snapshot":
                _json_response("200 OK", _runtime_snapshot(deps))
            elif method == "GET" and path == "/api/runtime/stream":
                await _runtime_stream_response()
            elif method == "GET" and path == "/api/runtime/challenge-config":
                challenge_name = str(query.get("challenge_name", "")).strip()
                if not challenge_name:
                    _json_response("400 Bad Request", {"error": "challenge_name is required"})
                else:
                    payload = _challenge_config_payload(deps, challenge_name)
                    if payload is None:
                        _json_response(
                            "404 Not Found",
                            {"error": f"Unknown challenge {challenge_name!r}"},
                        )
                    else:
                        _json_response("200 OK", payload)
            elif method == "GET" and path in {"/ui", "/ui.css", "/ui.js"}:
                asset_name = {
                    "/ui": "operator_ui.html",
                    "/ui.css": "operator_ui.css",
                    "/ui.js": "operator_ui.js",
                }[path]
                content_type, text = load_ui_asset(asset_name)
                _write_response("200 OK", text.encode("utf-8"), content_type)
            elif method == "GET" and path in {"/human", "/human-ui.css", "/human-ui.js"}:
                asset_name = {
                    "/human": "human_ui.html",
                    "/human-ui.css": "human-ui.css",
                    "/human-ui.js": "human-ui.js",
                }[path]
                content_type, text = load_ui_asset(asset_name)
                # Force browsers to re-fetch these on every load so UI edits
                # land immediately (no need for users to Ctrl+Shift+R).
                no_cache = (
                    "Cache-Control: no-cache, no-store, must-revalidate\r\n"
                    "Pragma: no-cache\r\n"
                    "Expires: 0\r\n"
                )
                _write_response(
                    "200 OK",
                    text.encode("utf-8"),
                    content_type,
                    extra_headers=no_cache,
                )
            elif method == "GET" and path == "/api/runtime/traces":
                challenge_name = str(query.get("challenge_name", "")).strip()
                lane_id = str(query.get("lane_id", "")).strip()
                if not challenge_name:
                    _json_response(
                        "400 Bad Request",
                        {"error": "challenge_name is required"},
                    )
                else:
                    challenge_dir = deps.challenge_dirs.get(challenge_name)
                    trace_files = [
                        trace_path.name
                        for trace_path in list_ui_trace_files(
                            challenge_name,
                            lane_id or None,
                            challenge_dir=challenge_dir,
                        )
                    ]
                    _json_response("200 OK", {"challenge_name": challenge_name, "lane_id": lane_id, "trace_files": trace_files})
            elif method == "GET" and path == "/api/runtime/trace-window":
                challenge_name = str(query.get("challenge_name", "")).strip()
                lane_id = str(query.get("lane_id", "")).strip()
                trace_name = str(query.get("trace_name", "")).strip()
                if not challenge_name or not trace_name:
                    _json_response(
                        "400 Bad Request",
                        {"error": "challenge_name and trace_name are required"},
                    )
                else:
                    cursor_raw = query.get("cursor")
                    limit_raw = query.get("limit")
                    try:
                        cursor = int(cursor_raw) if cursor_raw is not None else None
                        limit = int(limit_raw) if limit_raw is not None else 200
                        challenge_dir = deps.challenge_dirs.get(challenge_name)
                        payload = read_trace_window(
                            challenge_name,
                            lane_id or None,
                            trace_name,
                            cursor=cursor,
                            limit=limit,
                            challenge_dir=challenge_dir,
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
            elif method == "GET" and path == "/api/runtime/advisories":
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
            elif method == "POST" and path == "/api/runtime/coordinator-message" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5)
                try:
                    data = json.loads(body)
                    message = data.get("message", body.decode())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    message = body.decode("utf-8", errors="replace")

                inbox.put_nowait(message)
                _json_response("200 OK", {"ok": True, "queued": str(message)[:200]})
            elif method == "POST" and path == "/api/runtime/lane-bump" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}

                challenge_name = str(data.get("challenge_name", "")).strip()
                lane_id = str(data.get("lane_id") or data.get("model_spec") or "").strip()
                insights = str(data.get("insights", "")).strip()

                if not challenge_name or not lane_id or not insights:
                    _json_response(
                        "400 Bad Request",
                        {"error": "challenge_name, lane_id, and insights are required"},
                    )
                else:
                    result = await do_bump_agent(deps, challenge_name, lane_id, insights)
                    if result.startswith("No "):
                        _json_response("404 Not Found", {"ok": False, "error": result})
                    else:
                        _json_response("200 OK", {"ok": True, "result": result})
            elif method == "POST" and path == "/api/runtime/challenge-bump" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}
                challenge_name = str(data.get("challenge_name", "")).strip()
                insights = str(data.get("insights", "")).strip()
                if not challenge_name or not insights:
                    _json_response(
                        "400 Bad Request",
                        {"error": "challenge_name and insights are required"},
                    )
                else:
                    swarm = deps.swarms.get(challenge_name)
                    if swarm is None:
                        _json_response("404 Not Found", {"ok": False, "error": f"No swarm for challenge {challenge_name}"})
                    else:
                        statuses = swarm.get_status()
                        agents = statuses.get("agents", {}) if isinstance(statuses, dict) else {}
                        results: list[dict[str, str]] = []
                        if isinstance(agents, dict):
                            for model_spec, lane in agents.items():
                                lane_payload = lane if isinstance(lane, dict) else {}
                                lifecycle = str(lane_payload.get("lifecycle") or "")
                                if lifecycle in {"won", "finished", "cancelled", "flag_found"}:
                                    continue
                                result = await do_bump_agent(deps, challenge_name, str(model_spec), insights)
                                results.append({"lane_id": str(model_spec), "result": result})
                        _json_response("200 OK", {"ok": True, "challenge_name": challenge_name, "results": results})
            elif method == "POST" and path == "/api/runtime/approve-candidate" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}
                challenge_name = str(data.get("challenge_name", "")).strip()
                flag = str(data.get("flag", "")).strip()
                if not challenge_name or not flag:
                    _json_response(
                        "400 Bad Request",
                        {"error": "challenge_name and flag are required"},
                    )
                else:
                    result = await do_approve_flag_candidate(deps, challenge_name, flag)
                    if result.startswith("No ") or result.startswith("Cannot ") or result.startswith("Candidate approval rejected"):
                        _json_response("404 Not Found", {"ok": False, "error": result})
                    elif result.startswith("Already solved"):
                        _json_response("409 Conflict", {"ok": False, "error": result})
                    else:
                        _json_response("200 OK", {"ok": True, "result": result})
            elif method == "POST" and path == "/api/runtime/reject-candidate" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}
                challenge_name = str(data.get("challenge_name", "")).strip()
                flag = str(data.get("flag", "")).strip()
                if not challenge_name or not flag:
                    _json_response(
                        "400 Bad Request",
                        {"error": "challenge_name and flag are required"},
                    )
                else:
                    result = await do_reject_flag_candidate(deps, challenge_name, flag)
                    if result.startswith("No ") or result.startswith("Cannot ") or result.startswith("Candidate rejection rejected"):
                        _json_response("404 Not Found", {"ok": False, "error": result})
                    else:
                        _json_response("200 OK", {"ok": True, "result": result})
            elif method == "POST" and path == "/api/runtime/mark-solved" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}
                challenge_name = str(data.get("challenge_name", "")).strip()
                flag = str(data.get("flag", "")).strip()
                note = str(data.get("note", "")).strip()
                if not challenge_name or not flag:
                    _json_response(
                        "400 Bad Request",
                        {"error": "challenge_name and flag are required"},
                    )
                else:
                    result = await do_mark_challenge_solved(
                        deps,
                        challenge_name,
                        flag,
                        note=note,
                    )
                    if result.startswith("Unknown challenge") or result.startswith("External solve rejected"):
                        _json_response("404 Not Found", {"ok": False, "error": result})
                    elif result.startswith("Already solved") or result.startswith("Cannot "):
                        _json_response("409 Conflict", {"ok": False, "error": result})
                    else:
                        _json_response("200 OK", {"ok": True, "result": result})
            elif method == "POST" and path == "/api/runtime/set-max-challenges" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}
                try:
                    raw_max_active = data.get("max_active") if data.get("max_active") is not None else data.get("max")
                    if raw_max_active is None:
                        raise TypeError
                    max_active = int(raw_max_active)
                except (TypeError, ValueError):
                    _json_response("400 Bad Request", {"error": "max_active (or max) must be an integer"})
                else:
                    result = await do_set_max_concurrent_challenges(deps, max_active)
                    if result.startswith("max_active must be"):
                        _json_response("400 Bad Request", {"ok": False, "error": result})
                    else:
                        _json_response(
                            "200 OK",
                            {
                                "ok": True,
                                "result": result,
                                "max_concurrent_challenges": deps.max_concurrent_challenges,
                            },
                        )
            elif method == "POST" and path == "/api/runtime/set-challenge-priority" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}
                challenge_name = str(data.get("challenge_name", "")).strip()
                priority = bool(data.get("priority"))
                if not challenge_name:
                    _json_response("400 Bad Request", {"error": "challenge_name is required"})
                else:
                    result = await do_set_challenge_priority_waiting(
                        deps,
                        challenge_name,
                        priority=priority,
                    )
                    if result.startswith("Unknown challenge") or result.startswith("Could not queue"):
                        _json_response("404 Not Found", {"ok": False, "error": result})
                    elif result.startswith("Challenge \"") and (
                        "currently active" in result or "already solved" in result
                    ):
                        _json_response("409 Conflict", {"ok": False, "error": result})
                    else:
                        _json_response("200 OK", {"ok": True, "result": result})
            elif method == "POST" and path == "/api/runtime/restart-challenge" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}
                challenge_name = str(data.get("challenge_name", "")).strip()
                if not challenge_name:
                    _json_response("400 Bad Request", {"error": "challenge_name is required"})
                else:
                    result = await do_restart_challenge(deps, challenge_name)
                    if result.startswith("Unknown challenge") or result.startswith("Could not queue"):
                        _json_response("404 Not Found", {"ok": False, "error": result})
                    elif result.startswith("Challenge \"") and "already solved" in result:
                        _json_response("409 Conflict", {"ok": False, "error": result})
                    else:
                        _json_response("200 OK", {"ok": True, "result": result})
            elif method == "POST" and path == "/api/runtime/check-instance" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}
                challenge_name = str(data.get("challenge_name", "")).strip()
                restart_on_success = bool(data.get("restart_on_success"))
                if not challenge_name:
                    _json_response("400 Bad Request", {"error": "challenge_name is required"})
                else:
                    challenge_dir = deps.challenge_dirs.get(challenge_name)
                    if not challenge_dir:
                        _json_response(
                            "404 Not Found",
                            {"error": f"Unknown challenge {challenge_name!r}"},
                        )
                    else:
                        effective_meta = load_effective_metadata(challenge_dir)
                        probe = await probe_instance_connection(effective_meta)
                        payload = {
                            "ok": True,
                            "challenge_name": challenge_name,
                            "ready": bool(probe.get("ready")),
                            "probe": probe,
                            "restart_requested": False,
                            "challenge_config": _challenge_config_payload(deps, challenge_name),
                        }
                        if payload["ready"] and restart_on_success:
                            payload["restart_requested"] = True
                            payload["restart_result"] = await do_restart_challenge(deps, challenge_name)
                        _json_response("200 OK", payload)
            elif method == "GET" and path == "/api/runtime/ctfd-config":
                # UI-safe snapshot of the current CTFd URL + token state.
                _json_response("200 OK", {"ok": True, **_ctfd_summary(deps)})
            elif method == "PUT" and path == "/api/runtime/ctfd-config" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}
                url = str(data.get("url") or data.get("base_url") or "").strip()
                # Keep existing token if caller omits the field; empty string clears it.
                token_field_provided = "token" in data
                token = str(data.get("token") or "").strip()
                if not url:
                    _json_response("400 Bad Request", {"error": "url is required"})
                else:
                    final_token = token if token_field_provided else str(getattr(deps.settings, "ctfd_token", "") or "")
                    rebuild = await _rebuild_ctfd_client(deps, base_url=url, token=final_token)
                    if not rebuild.get("ok"):
                        _json_response("500 Internal Server Error", {"error": rebuild.get("error", "rebuild failed")})
                    else:
                        probe: dict[str, Any] = {}
                        if bool(data.get("test", True)):
                            probe = await _probe_cookie(
                                deps,
                                str(getattr(deps.settings, "remote_cookie_header", "") or ""),
                            )
                        _json_response(
                            "200 OK",
                            {"ok": True, "ctfd": _ctfd_summary(deps), "probe": probe},
                        )
            elif method == "DELETE" and path == "/api/runtime/ctfd-config":
                # Clear only the token — keep the URL (leaving local_mode flip for CLI).
                query_params = query
                field_to_clear = str(query_params.get("field", "token")).lower()
                if field_to_clear == "token":
                    deps.settings.ctfd_token = ""
                    rebuild = await _rebuild_ctfd_client(
                        deps,
                        base_url=str(getattr(deps.settings, "ctfd_url", "") or ""),
                        token="",
                    )
                    _json_response("200 OK", {"ok": True, "ctfd": _ctfd_summary(deps), "rebuild": rebuild})
                else:
                    _json_response("400 Bad Request", {"error": f"Unsupported field: {field_to_clear}"})
            elif method == "GET" and path == "/api/runtime/cookie":
                # Status snapshot (never leaks the raw cookie value).
                _json_response("200 OK", {"ok": True, **_cookie_summary(deps)})
            elif method == "PUT" and path == "/api/runtime/cookie" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}
                raw = str(data.get("cookie") or "").strip()
                # Clean up common paste artifacts — leading "Cookie:" prefix AND
                # Set-Cookie attributes (Path=, Domain=, Secure, HttpOnly, …)
                # that sneak in when operators copy from DevTools' Response view.
                cleaned = _sanitize_cookie_header(raw)
                if not cleaned:
                    # Distinguish "pasted nothing" from "pasted a bare token with no name=".
                    has_equals = "=" in raw
                    if raw and not has_equals:
                        msg = (
                            "Invalid Cookie header: looks like you pasted just the cookie "
                            "VALUE. The Cookie header needs the full 'name=value' pair, "
                            "e.g. 'session=eyJhbG...'.  Copy from DevTools → Network → "
                            "Request Headers → Cookie: line (not Application → Cookies)."
                        )
                    else:
                        msg = (
                            "cookie value is required — after stripping Set-Cookie "
                            "attributes nothing usable remained"
                        )
                    _json_response("400 Bad Request", {"error": msg})
                else:
                    deps.settings.remote_cookie_header = cleaned
                    deps.cookie_source = f"operator_api@{int(time.time())}"
                    # Push the new cookie into the live CTFd client so the
                    # poller + Fetch + submit paths use it immediately.  Without
                    # this, only NEW clients would see the cookie and the
                    # poller would keep failing with admin/admin login errors.
                    set_cookie_fn = getattr(deps.ctfd, "set_cookie_header", None)
                    if callable(set_cookie_fn):
                        try:
                            set_cookie_fn(cleaned)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("CTFd client cookie update failed: %s", exc)
                    # Reset the poller's exponential backoff so the effect is
                    # visible on the next tick instead of after the ramp.
                    poller = getattr(deps, "poller", None)
                    if poller is not None:
                        reset_fn = getattr(poller, "reset_backoff", None)
                        if callable(reset_fn):
                            reset_fn()
                    summary = _cookie_summary(deps)
                    # Optionally verify immediately.
                    probe_result: dict[str, Any] = {}
                    if bool(data.get("test")):
                        probe_result = await _probe_cookie(deps, cleaned)
                    stripped = len(raw) - len(cleaned)
                    logger.info(
                        "Cookie header updated via API (%d chars, %d cookies%s)",
                        summary["length"], summary["cookie_count"],
                        f", stripped {stripped} chars of attrs" if stripped > 0 else "",
                    )
                    _json_response(
                        "200 OK",
                        {
                            "ok": True,
                            "cookie": summary,
                            "probe": probe_result,
                            "sanitized_chars_dropped": stripped,
                        },
                    )
            elif method == "DELETE" and path == "/api/runtime/cookie":
                deps.settings.remote_cookie_header = ""
                deps.cookie_source = ""
                # Strip the cookie off the live CTFd client too so subsequent
                # requests fall back to token / credential auth (or fail
                # cleanly) instead of quietly reusing the old cookie.
                set_cookie_fn = getattr(deps.ctfd, "set_cookie_header", None)
                if callable(set_cookie_fn):
                    try:
                        set_cookie_fn("")
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("CTFd client cookie clear failed: %s", exc)
                poller = getattr(deps, "poller", None)
                if poller is not None:
                    reset_fn = getattr(poller, "reset_backoff", None)
                    if callable(reset_fn):
                        reset_fn()
                logger.info("Cookie header cleared via API")
                _json_response("200 OK", {"ok": True, "cookie": _cookie_summary(deps)})
            elif method == "POST" and path == "/api/runtime/cookie/test":
                # Probe the currently-configured cookie (doesn't accept an override).
                probe_result = await _probe_cookie(
                    deps, str(getattr(deps.settings, "remote_cookie_header", "") or "")
                )
                _json_response("200 OK", {"ok": True, "probe": probe_result, "cookie": _cookie_summary(deps)})
            elif method == "PATCH" and path == "/api/runtime/challenge-config" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}
                challenge_name = str(data.get("challenge_name", "")).strip()
                if not challenge_name:
                    _json_response("400 Bad Request", {"error": "challenge_name is required"})
                else:
                    challenge_dir = deps.challenge_dirs.get(challenge_name)
                    if not challenge_dir:
                        _json_response(
                            "404 Not Found",
                            {"error": f"Unknown challenge {challenge_name!r}"},
                        )
                    else:
                        patch = data.get("override")
                        if bool(data.get("replace")) and isinstance(patch, dict):
                            updated_override = sanitize_override(patch)
                        else:
                            if not isinstance(patch, dict):
                                patch = {
                                    key: value
                                    for key, value in data.items()
                                    if key != "challenge_name"
                                }
                            current_override = load_override(challenge_dir)
                            updated_override = apply_override_patch(current_override, patch)
                        write_override(challenge_dir, updated_override)
                        _refresh_challenge_meta(deps, challenge_name)
                        payload = _challenge_config_payload(deps, challenge_name)
                        assert payload is not None
                        _json_response("200 OK", payload)
            elif method == "DELETE" and path == "/api/runtime/challenge-config":
                challenge_name = str(query.get("challenge_name", "")).strip()
                if not challenge_name:
                    _json_response("400 Bad Request", {"error": "challenge_name is required"})
                else:
                    challenge_dir = deps.challenge_dirs.get(challenge_name)
                    if not challenge_dir:
                        _json_response(
                            "404 Not Found",
                            {"error": f"Unknown challenge {challenge_name!r}"},
                        )
                    else:
                        delete_override(challenge_dir)
                        _refresh_challenge_meta(deps, challenge_name)
                        payload = _challenge_config_payload(deps, challenge_name)
                        assert payload is not None
                        _json_response("200 OK", payload)

            # ── Human-coordinator endpoints ────────────────────────────────────
            elif method == "GET" and path == "/api/runtime/fetch-challenges":
                # Fetch all challenges from the platform (CTFd) or local dirs,
                # then auto-import remote ones to disk so they're spawnable
                # without a separate `ctf-import` run.
                from backend.agents.coordinator_core import do_fetch_challenges
                auto_import = str(query.get("import", "1")).lower() not in {"0", "false", "no"}
                try:
                    result_json = await asyncio.wait_for(do_fetch_challenges(deps), timeout=30)
                    challenges_list = json.loads(result_json)

                    summary: dict[str, Any] = {"imported": [], "skipped": [], "failed": []}
                    if auto_import and not deps.local_mode and deps.remote_challenge_cache:
                        # Feed the RAW challenge dicts (files / hints / connection_info
                        # intact) from the cache to pull_challenge — the summarised
                        # challenges_list is lossy (e.g. description truncated to 200 ch).
                        raw_remote = list(deps.remote_challenge_cache.values())
                        try:
                            summary = await asyncio.wait_for(
                                _auto_import_remote_challenges(deps, raw_remote),
                                timeout=300,
                            )
                        except asyncio.TimeoutError:
                            summary = {
                                "imported": [],
                                "skipped": [],
                                "failed": [],
                                "error": "Auto-import timed out (>5 min); partial results may be on disk",
                            }

                    _json_response(
                        "200 OK",
                        {
                            "ok": True,
                            "challenges": challenges_list,
                            "count": len(challenges_list),
                            "auto_import": auto_import,
                            "import_summary": summary,
                        },
                    )
                except asyncio.TimeoutError:
                    _json_response("504 Gateway Timeout", {"error": "Challenge fetch timed out (>30 s)"})
                except Exception as exc:
                    _json_response("500 Internal Server Error", {"error": str(exc)})

            elif method == "GET" and path == "/api/runtime/challenge-queue":
                # Return the pending swarm queue with positions and reasons.
                from backend.agents.coordinator_core import _pending_swarm_entries
                entries = _pending_swarm_entries(deps)
                _json_response(
                    "200 OK",
                    {
                        "ok": True,
                        "queue": entries,
                        "count": len(entries),
                        "max_concurrent": deps.max_concurrent_challenges,
                        "active_count": sum(
                            1 for t in deps.swarm_tasks.values() if not t.done()
                        ),
                    },
                )

            elif method == "GET" and path == "/api/runtime/solver-trace":
                challenge_name = str(query.get("challenge_name", "")).strip()
                model_spec = str(query.get("model_spec", "")).strip()
                try:
                    last_n = int(query.get("last_n", 20))
                except (ValueError, TypeError):
                    last_n = 20
                if not challenge_name or not model_spec:
                    _json_response(
                        "400 Bad Request",
                        {"error": "challenge_name and model_spec are required"},
                    )
                else:
                    from backend.agents.coordinator_core import do_read_solver_trace
                    text = await do_read_solver_trace(deps, challenge_name, model_spec, last_n)
                    if text.startswith("No swarm") or text.startswith("No solver"):
                        _json_response("404 Not Found", {"ok": False, "error": text})
                    else:
                        _json_response(
                            "200 OK",
                            {
                                "ok": True,
                                "challenge_name": challenge_name,
                                "model_spec": model_spec,
                                "last_n": last_n,
                                "trace": text,
                            },
                        )

            elif method == "POST" and path == "/api/runtime/submit-flag" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}
                challenge_name = str(data.get("challenge_name", "")).strip()
                flag = str(data.get("flag", "")).strip()
                if not challenge_name or not flag:
                    _json_response(
                        "400 Bad Request",
                        {"error": "challenge_name and flag are required"},
                    )
                else:
                    from backend.agents.coordinator_core import do_submit_flag
                    # Human operator explicitly requests submission → force=True bypasses
                    # the no_submit guard that prevents solver auto-submission.
                    result = await do_submit_flag(
                        deps, challenge_name, flag, force=True
                    )
                    if result.startswith("LOCAL MODE"):
                        _json_response("409 Conflict", {"ok": False, "error": result})
                    elif result.startswith("SUBMIT BLOCKED"):
                        _json_response("409 Conflict", {"ok": False, "error": result})
                    elif result.startswith("submit_flag error"):
                        _json_response(
                            "500 Internal Server Error", {"ok": False, "error": result}
                        )
                    else:
                        _json_response("200 OK", {"ok": True, "result": result})

            elif method == "POST" and path == "/api/runtime/spawn-swarm" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}
                challenge_name = str(data.get("challenge_name", "")).strip()
                if not challenge_name:
                    _json_response("400 Bad Request", {"error": "challenge_name is required"})
                else:
                    from backend.agents.coordinator_core import do_spawn_swarm
                    result = await do_spawn_swarm(deps, challenge_name)
                    if result.startswith("No ") or result.startswith("Could not"):
                        _json_response("404 Not Found", {"ok": False, "error": result})
                    else:
                        _json_response("200 OK", {"ok": True, "result": result})

            elif method == "POST" and path == "/api/runtime/kill-swarm" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}
                challenge_name = str(data.get("challenge_name", "")).strip()
                if not challenge_name:
                    _json_response("400 Bad Request", {"error": "challenge_name is required"})
                else:
                    from backend.agents.coordinator_core import do_kill_swarm
                    result = await do_kill_swarm(deps, challenge_name)
                    if result.startswith("No swarm"):
                        _json_response("404 Not Found", {"ok": False, "error": result})
                    else:
                        _json_response("200 OK", {"ok": True, "result": result})

            elif method == "POST" and path == "/api/runtime/broadcast" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}
                challenge_name = str(data.get("challenge_name", "")).strip()
                message = str(data.get("message", "")).strip()
                if not challenge_name or not message:
                    _json_response(
                        "400 Bad Request",
                        {"error": "challenge_name and message are required"},
                    )
                else:
                    from backend.agents.coordinator_core import do_broadcast
                    result = await do_broadcast(deps, challenge_name, message)
                    if result.startswith("No swarm"):
                        _json_response("404 Not Found", {"ok": False, "error": result})
                    else:
                        _json_response("200 OK", {"ok": True, "result": result})

            elif method == "POST" and path == "/api/runtime/advisor-intervene" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}
                challenge_name = str(data.get("challenge_name", "")).strip()
                critique = str(data.get("critique", "") or data.get("message", "")).strip()
                if not challenge_name or not critique:
                    _json_response(
                        "400 Bad Request",
                        {"error": "challenge_name and critique (or message) are required"},
                    )
                else:
                    result = await do_advisor_intervene(deps, challenge_name, critique)
                    if result.startswith("No swarm"):
                        _json_response("404 Not Found", {"ok": False, "error": result})
                    else:
                        _json_response("200 OK", {"ok": True, "result": result})

            elif method == "GET" and path == "/api/runtime/persistent-directives":
                challenge_name = str(query.get("challenge_name", "")).strip()
                if not challenge_name:
                    _json_response(
                        "400 Bad Request",
                        {"error": "challenge_name is required"},
                    )
                else:
                    result = await do_list_persistent_directives(deps, challenge_name)
                    status_code = "200 OK" if result.get("ok") else "404 Not Found"
                    _json_response(status_code, result)

            elif method == "POST" and path == "/api/runtime/persistent-directive" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}
                challenge_name = str(data.get("challenge_name", "")).strip()
                text = str(data.get("text", "") or data.get("directive", "")).strip()
                if not challenge_name or not text:
                    _json_response(
                        "400 Bad Request",
                        {"error": "challenge_name and text are required"},
                    )
                else:
                    result = await do_add_persistent_directive(deps, challenge_name, text)
                    status_code = "200 OK" if result.get("ok") else "404 Not Found"
                    _json_response(status_code, result)

            elif method == "DELETE" and path == "/api/runtime/persistent-directive" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}
                challenge_name = str(data.get("challenge_name", "")).strip()
                directive_id = str(data.get("id", "") or data.get("directive_id", "")).strip()
                if not challenge_name or not directive_id:
                    _json_response(
                        "400 Bad Request",
                        {"error": "challenge_name and id are required"},
                    )
                else:
                    result = await do_remove_persistent_directive(
                        deps, challenge_name, directive_id,
                    )
                    status_code = "200 OK" if result.get("ok") else "404 Not Found"
                    _json_response(status_code, result)

            elif method == "POST" and path == "/api/runtime/request-status-report" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}
                challenge_name = str(data.get("challenge_name", "")).strip()
                try:
                    report_window = int(data.get("report_window") or 40)
                except (TypeError, ValueError):
                    report_window = 40
                if not challenge_name:
                    _json_response(
                        "400 Bad Request",
                        {"error": "challenge_name is required"},
                    )
                else:
                    result = await do_request_status_report(
                        deps, challenge_name, report_window=report_window,
                    )
                    if result.startswith("No swarm"):
                        _json_response("404 Not Found", {"ok": False, "error": result})
                    else:
                        _json_response("200 OK", {"ok": True, "result": result})

            elif method == "POST" and path == "/api/runtime/clear-challenge-history" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}
                challenge_name = str(data.get("challenge_name", "")).strip()
                delete_traces = bool(data.get("delete_traces", False))
                if not challenge_name:
                    _json_response("400 Bad Request", {"error": "challenge_name is required"})
                else:
                    result = await do_clear_challenge_history(
                        deps, challenge_name, delete_traces=delete_traces
                    )
                    if "still active" in result:
                        _json_response("409 Conflict", {"ok": False, "error": result})
                    else:
                        _json_response("200 OK", {"ok": True, "result": result})

            elif method == "POST" and path == "/api/runtime/parse-challenge-url" and content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=30)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}
                url = str(data.get("url", "")).strip()
                if not url:
                    _json_response("400 Bad Request", {"error": "url is required"})
                else:
                    try:
                        from backend.agents.url_parser_agent import parse_challenge_url
                        # Reuse the operator's CTFd creds so private challenges
                        # pages return real content instead of a login redirect.
                        cookie_header = str(
                            data.get("cookie")
                            or getattr(deps.settings, "remote_cookie_header", "")
                            or ""
                        )
                        auth_token = str(
                            data.get("token")
                            or getattr(deps.settings, "ctfd_token", "")
                            or ""
                        )

                        # Shortcut: if the pasted URL targets the currently
                        # configured CTFd host, bypass the HTML→LLM path and
                        # call the CTFd API directly.  CTFd's /challenges page
                        # is a React SPA whose raw HTML contains no challenge
                        # data — only the JS bundle — so LLM parsing always
                        # returns 0.  Fetching via the CTFd client gives us
                        # real structured records.
                        same_host = False
                        try:
                            from urllib.parse import urlparse
                            parsed_url_host = (urlparse(url).hostname or "").lower()
                            ctfd_host = (urlparse(
                                getattr(deps.ctfd, "base_url", "")
                                or getattr(deps.settings, "ctfd_url", "")
                                or ""
                            ).hostname or "").lower()
                            same_host = bool(parsed_url_host and parsed_url_host == ctfd_host)
                        except Exception:  # noqa: BLE001
                            same_host = False

                        use_api = same_host and not deps.local_mode and not data.get("force_html")
                        api_payload: dict[str, Any] | None = None
                        if use_api:
                            try:
                                from backend.agents.coordinator_core import do_fetch_challenges
                                raw = await asyncio.wait_for(do_fetch_challenges(deps), timeout=30)
                                records = json.loads(raw)
                                api_payload = {
                                    "source_url": url,
                                    "competition_name": getattr(deps.ctfd, "label", "") or "CTFd",
                                    "challenges": records,
                                    "auth_used": {
                                        "cookie": bool(cookie_header),
                                        "token": bool(auth_token),
                                    },
                                    "note": (
                                        "URL matches configured CTFd host; used "
                                        "/api/v1/challenges instead of HTML scrape "
                                        "(Parse URL and Fetch are equivalent here)."
                                    ),
                                    "markdown_summary": "",
                                    "raw_text_preview": "",
                                }
                            except Exception as exc:  # noqa: BLE001 — fall back to HTML parse
                                logger.info(
                                    "parse-challenge-url: CTFd API shortcut failed (%s), falling back to HTML",
                                    exc,
                                )

                        if api_payload is not None:
                            _json_response("200 OK", api_payload)
                        else:
                            parsed = await asyncio.wait_for(
                                parse_challenge_url(
                                    url,
                                    cookie_header=cookie_header,
                                    auth_token=auth_token,
                                ),
                                timeout=60,
                            )
                            # If HTML parse came back empty but we're pointed at a
                            # CTFd host, nudge the user toward Fetch.
                            if (
                                same_host
                                and isinstance(parsed, dict)
                                and not parsed.get("challenges")
                            ):
                                parsed["note"] = (
                                    "HTML parse found 0 challenges — CTFd's "
                                    "/challenges page is a JS-rendered SPA with no "
                                    "challenges in the raw HTML. Use the 🔄 Fetch "
                                    "button (left panel) which hits CTFd's API directly."
                                )
                            _json_response("200 OK", parsed)
                    except asyncio.TimeoutError:
                        _json_response(
                            "504 Gateway Timeout",
                            {"error": "URL parsing timed out after 60s"},
                        )
                    except Exception as exc:
                        _json_response(
                            "500 Internal Server Error",
                            {"error": f"URL parsing failed: {exc}"},
                        )

            elif method == "GET" and path == "/api/runtime/human-events":
                # SSE stream of coordinator events for human-mode display
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/event-stream\r\n"
                    b"Cache-Control: no-cache\r\n"
                    b"Connection: keep-alive\r\n"
                    b"\r\n"
                )
                await writer.drain()
                while not writer.is_closing() and not reader.at_eof():
                    # Drain any queued events
                    sent = 0
                    while True:
                        try:
                            evt_data = deps.human_event_log.get_nowait()
                            payload = json.dumps(evt_data)
                            writer.write(f"event: coordinator\ndata: {payload}\n\n".encode())
                            sent += 1
                        except asyncio.QueueEmpty:
                            break
                    if sent == 0:
                        writer.write(b": keepalive\n\n")
                    await writer.drain()
                    await asyncio.sleep(1)

            else:
                _json_response(
                    "400 Bad Request",
                    {
                        "error": "Unsupported request",
                        "usage": {
                            "runtime_snapshot": "GET /api/runtime/snapshot",
                            "runtime_stream": "GET /api/runtime/stream",
                            "challenge_config": (
                                "GET /api/runtime/challenge-config?challenge_name=..."
                            ),
                            "patch_challenge_config": (
                                'PATCH /api/runtime/challenge-config {"challenge_name": "...", '
                                '"connection": {"host": "...", "port": 31337}}'
                            ),
                            "delete_challenge_config": (
                                "DELETE /api/runtime/challenge-config?challenge_name=..."
                            ),
                            "ui": "GET /ui",
                            "human_ui": "GET /human",
                            "runtime_traces": (
                                "GET /api/runtime/traces?challenge_name=...&lane_id=..."
                            ),
                            "runtime_trace_window": (
                                "GET /api/runtime/trace-window?challenge_name=...&lane_id=..."
                                "&trace_name=...&cursor=...&limit=..."
                            ),
                            "runtime_advisories": "GET /api/runtime/advisories?challenge_name=...&limit=...",
                            "coordinator_message": 'POST /api/runtime/coordinator-message {"message": "..."}',
                            "lane_bump": (
                                'POST /api/runtime/lane-bump {"challenge_name": "...", '
                                '"lane_id": "...", "insights": "..."}'
                            ),
                            "challenge_bump": (
                                'POST /api/runtime/challenge-bump {"challenge_name": "...", '
                                '"insights": "..."}'
                            ),
                            "approve_candidate": (
                                'POST /api/runtime/approve-candidate {"challenge_name": "...", '
                                '"flag": "..."}'
                            ),
                            "reject_candidate": (
                                'POST /api/runtime/reject-candidate {"challenge_name": "...", '
                                '"flag": "..."}'
                            ),
                            "mark_solved": (
                                'POST /api/runtime/mark-solved {"challenge_name": "...", '
                                '"flag": "...", "note": "..."}'
                            ),
                            "set_max_challenges": (
                                'POST /api/runtime/set-max-challenges {"max_active": 4}'
                            ),
                            "set_challenge_priority": (
                                'POST /api/runtime/set-challenge-priority {"challenge_name": "...", "priority": true}'
                            ),
                            "restart_challenge": (
                                'POST /api/runtime/restart-challenge {"challenge_name": "..."}'
                            ),
                            "check_instance": (
                                'POST /api/runtime/check-instance {"challenge_name": "...", "restart_on_success": true}'
                            ),
                            # Human-coordinator endpoints
                            "spawn_swarm": (
                                'POST /api/runtime/spawn-swarm {"challenge_name": "..."}'
                            ),
                            "kill_swarm": (
                                'POST /api/runtime/kill-swarm {"challenge_name": "..."}'
                            ),
                            "submit_flag": (
                                'POST /api/runtime/submit-flag {"challenge_name": "...", "flag": "FLAG{...}"}'
                            ),
                            "solver_trace": (
                                "GET /api/runtime/solver-trace"
                                "?challenge_name=...&model_spec=...&last_n=20"
                            ),
                            "broadcast": (
                                'POST /api/runtime/broadcast {"challenge_name": "...", "message": "..."}'
                            ),
                            "parse_challenge_url": (
                                'POST /api/runtime/parse-challenge-url {"url": "https://..."}'
                            ),
                            "human_events": "GET /api/runtime/human-events  (SSE stream of coordinator events)",
                            "advisor_intervene": (
                                'POST /api/runtime/advisor-intervene {"challenge_name": "...", "critique": "..."}'
                            ),
                            "clear_challenge_history": (
                                'POST /api/runtime/clear-challenge-history {"challenge_name": "...", "delete_traces": false}'
                            ),
                            "fetch_challenges": "GET /api/runtime/fetch-challenges",
                            "challenge_queue": "GET /api/runtime/challenge-queue",
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

    host = getattr(deps, "msg_host", "127.0.0.1") or "127.0.0.1"
    try:
        server = await asyncio.start_server(_handle, host, port)
        actual_port = server.sockets[0].getsockname()[1]
        display_host = "127.0.0.1" if host == "0.0.0.0" else host
        logger.info(
            "Operator UI: http://%s:%d/ui  |  Human UI: http://%s:%d/human",
            display_host, actual_port, display_host, actual_port,
        )
        if host == "0.0.0.0":
            logger.warning(
                "UI bound to 0.0.0.0 — accessible from all network interfaces. "
                "Ensure this host is behind a firewall or VPN."
            )
        return server
    except OSError as e:
        logger.warning(f"Could not start operator message endpoint: {e}")
        return None
