"""Shared coordinator tool logic — called by both Claude SDK and Codex coordinators."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

from backend.challenge_config import refresh_effective_metadata
from backend.deps import CoordinatorDeps
from backend.platforms import platform_label
from backend.prompts import ChallengeMeta
from backend.sandbox import resolve_shared_artifacts_dir
from backend.solver_base import FLAG_FOUND

logger = logging.getLogger(__name__)

CTFD_REFRESH_BACKOFF_BASE_SECONDS = 15.0
CTFD_REFRESH_BACKOFF_MAX_SECONDS = 120.0
PENDING_REASON_QUEUED = "queued"
PENDING_REASON_PRIORITY_WAITING = "priority_waiting"
PENDING_REASON_CTFD_RETRY = "ctfd_retry"
PENDING_REASON_CANDIDATE_RETRY = "candidate_retry"
PENDING_REASON_RESTART_REQUESTED = "restart_requested"
LEGACY_PENDING_REASON_RESUME_REQUESTED = "resume_requested"
PENDING_REASON_QUOTA_BLOCKED = "quota_blocked"
RESTORABLE_PENDING_REASONS = {
    PENDING_REASON_QUEUED,
    PENDING_REASON_PRIORITY_WAITING,
    PENDING_REASON_CTFD_RETRY,
    PENDING_REASON_CANDIDATE_RETRY,
    PENDING_REASON_RESTART_REQUESTED,
    LEGACY_PENDING_REASON_RESUME_REQUESTED,
}

COORDINATOR_PROMPT = """\
You are a CTF competition coordinator running for the entire live event.
Your job is to maximize solved challenges while keeping productive swarms moving.

Priorities:
- Spawn swarms for unsolved challenges, prioritizing by solve count (easy first).
- Use read_solver_trace before bumping a stuck lane.
- Use broadcast only for genuinely shared insights across lanes.
- When you receive `ADVISOR MESSAGE:` or `Artifact path: /challenge/shared-artifacts/...`, treat it as evidence to inspect before deciding what to do.
- Read `/challenge/shared-artifacts/manifest.md` or the referenced digest/artifact first, then decide whether to broadcast, bump a specific lane, or ignore it.

Critical rules:
- NEVER kill a swarm unless the flag is confirmed correct.
- Solvers may auto-submit guarded flag candidates to the active remote platform. Use `submit_flag` yourself only for explicit coordinator-driven retries.
- Do not rebroadcast advisor or artifact messages blindly. Inspect the evidence first and only rebroadcast what is broadly useful.
- When a solver seems stuck, send a specific next-step bump: exact files, routes, tools, checks, or validation criteria.
- Cost is not the bottleneck. Keep swarms running.

You will receive event messages. Respond with tool calls to manage the competition.
"""


def _int_from_object(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _float_from_object(value: object) -> float:
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _challenge_sort_key(challenge: dict[str, object]) -> tuple[int, str]:
    solves = _int_from_object(challenge.get("solves", 0))
    name = str(challenge.get("name", ""))
    return (-solves, name)


def _normalize_pending_reason(reason: str) -> str:
    cleaned = str(reason or "").strip()
    if cleaned == LEGACY_PENDING_REASON_RESUME_REQUESTED:
        return PENDING_REASON_RESTART_REQUESTED
    return cleaned


def _restored_solved_names(deps: CoordinatorDeps) -> set[str]:
    solved: set[str] = set()
    for name, result in deps.results.items():
        if not isinstance(result, dict):
            continue
        if result.get("status") == FLAG_FOUND:
            solved.add(name)
    return solved


def _local_challenge_records(
    deps: CoordinatorDeps,
    solved: set[str],
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for name, meta in deps.challenge_metas.items():
        records.append(
            {
                "name": name,
                "category": getattr(meta, "category", "?"),
                "value": getattr(meta, "value", 0),
                "solves": getattr(meta, "solves", 0),
                "status": "SOLVED" if name in solved else "unsolved",
                "description": str(getattr(meta, "description", "") or "")[:200],
                "source": "local",
            }
        )
    return records


def _retire_finished_swarms(deps: CoordinatorDeps) -> list[str]:
    retired: list[str] = []
    for name, _swarm in list(deps.swarms.items()):
        task = deps.swarm_tasks.get(name)
        if task and task.done():
            retired.append(name)
    for name in retired:
        deps.swarms.pop(name, None)
        deps.swarm_tasks.pop(name, None)
    return retired


def _pending_swarm_meta(deps: CoordinatorDeps, challenge_name: str) -> dict[str, object]:
    meta = deps.pending_swarm_meta.get(challenge_name)
    if isinstance(meta, dict):
        return meta
    meta: dict[str, object] = {
        "priority": False,
        "reason": PENDING_REASON_QUEUED,
        "enqueued_at": time.time(),
    }
    deps.pending_swarm_meta[challenge_name] = meta
    return meta


def _challenge_spawnable_from_local(deps: CoordinatorDeps, challenge_name: str) -> bool:
    return challenge_name in deps.challenge_dirs or challenge_name in deps.challenge_metas


def _pending_swarm_sort_key(
    deps: CoordinatorDeps,
    challenge_name: str,
) -> tuple[int, int, int, str]:
    meta = _pending_swarm_meta(deps, challenge_name)
    priority_rank = 0 if bool(meta.get("priority")) else 1
    local_rank = 1
    if _ctfd_refresh_backoff_remaining(deps) > 0 and _challenge_spawnable_from_local(deps, challenge_name):
        local_rank = 0
    challenge_meta = deps.challenge_metas.get(challenge_name)
    solves = _int_from_object(getattr(challenge_meta, "solves", 0) if challenge_meta is not None else 0)
    return (priority_rank, local_rank, -solves, challenge_name)


def _pending_swarm_order(
    deps: CoordinatorDeps,
    *,
    include_priority_waiting: bool = True,
    include_quota_blocked: bool = True,
) -> list[str]:
    names = [name for name in deps.pending_swarm_queue if name in deps.pending_swarm_set]
    if not include_priority_waiting:
        names = [
            name
            for name in names
            if str(_pending_swarm_meta(deps, name).get("reason") or "") != PENDING_REASON_PRIORITY_WAITING
        ]
    if not include_quota_blocked:
        names = [
            name
            for name in names
            if str(_pending_swarm_meta(deps, name).get("reason") or "") != PENDING_REASON_QUOTA_BLOCKED
        ]
    return sorted(dict.fromkeys(names), key=lambda name: _pending_swarm_sort_key(deps, name))


def _pending_swarm_entries(deps: CoordinatorDeps) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for challenge_name in _pending_swarm_order(deps):
        meta = _pending_swarm_meta(deps, challenge_name)
        entries.append(
            {
                "challenge_name": challenge_name,
                "priority": bool(meta.get("priority")),
                "reason": str(meta.get("reason") or PENDING_REASON_QUEUED),
                "local_preloaded": _challenge_spawnable_from_local(deps, challenge_name),
                "enqueued_at": _float_from_object(meta.get("enqueued_at")),
            }
        )
    return entries


def _set_pending_swarm_meta(
    deps: CoordinatorDeps,
    challenge_name: str,
    *,
    priority: bool | None = None,
    reason: str | None = None,
) -> None:
    meta = _pending_swarm_meta(deps, challenge_name)
    if priority is not None:
        meta["priority"] = bool(priority)
    if reason is not None:
        meta["reason"] = reason


def _pop_next_pending_swarm(
    deps: CoordinatorDeps,
    *,
    include_priority_waiting: bool = True,
    include_quota_blocked: bool = True,
) -> str | None:
    ordered = _pending_swarm_order(
        deps,
        include_priority_waiting=include_priority_waiting,
        include_quota_blocked=include_quota_blocked,
    )
    if not ordered:
        return None
    challenge_name = ordered[0]
    deps.pending_swarm_queue = deque(
        name for name in deps.pending_swarm_queue if name != challenge_name
    )
    deps.pending_swarm_set.discard(challenge_name)
    deps.pending_swarm_meta.pop(challenge_name, None)
    return challenge_name


def _drop_pending_swarm(deps: CoordinatorDeps, challenge_name: str) -> bool:
    if challenge_name not in deps.pending_swarm_set:
        return False
    deps.pending_swarm_set.discard(challenge_name)
    deps.pending_swarm_meta.pop(challenge_name, None)
    deps.pending_swarm_queue = deque(
        item for item in deps.pending_swarm_queue if item != challenge_name
    )
    return True


def _enqueue_swarm(
    deps: CoordinatorDeps,
    challenge_name: str,
    *,
    priority: bool = False,
    reason: str = PENDING_REASON_QUEUED,
) -> bool:
    challenge_meta = deps.challenge_metas.get(challenge_name)
    effective_priority = priority or bool(getattr(challenge_meta, "priority", False))
    if challenge_name in deps.swarms:
        return False
    if challenge_name in deps.pending_swarm_set:
        _set_pending_swarm_meta(
            deps,
            challenge_name,
            priority=(
                effective_priority
                or bool(_pending_swarm_meta(deps, challenge_name).get("priority"))
            ),
            reason=reason,
        )
        return False
    result = deps.results.get(challenge_name, {})
    if result.get("status") == FLAG_FOUND:
        return False
    deps.pending_swarm_queue.append(challenge_name)
    deps.pending_swarm_set.add(challenge_name)
    deps.pending_swarm_meta[challenge_name] = {
        "priority": bool(effective_priority),
        "reason": reason,
        "enqueued_at": time.time(),
    }
    return True


def _enqueue_finished_swarm(
    deps: CoordinatorDeps,
    challenge_name: str,
    *,
    priority: bool = False,
    reason: str = PENDING_REASON_QUEUED,
) -> None:
    if challenge_name in deps.pending_swarm_set:
        _set_pending_swarm_meta(deps, challenge_name, priority=priority, reason=reason)
        return
    deps.pending_swarm_queue.append(challenge_name)
    deps.pending_swarm_set.add(challenge_name)
    deps.pending_swarm_meta[challenge_name] = {
        "priority": bool(priority),
        "reason": reason,
        "enqueued_at": time.time(),
    }


def restore_pending_swarms_from_results(deps: CoordinatorDeps) -> list[str]:
    restored: list[str] = []
    for challenge_name in sorted(deps.results):
        if challenge_name in deps.swarms or challenge_name in deps.pending_swarm_set:
            continue
        record = deps.results.get(challenge_name)
        if not isinstance(record, dict):
            continue
        status = str(record.get("status") or "").strip()
        if status in {FLAG_FOUND, "candidate_pending"}:
            continue

        restore_reason = ""
        restore_priority = False
        if bool(record.get("requeue_requested")):
            reason = _normalize_pending_reason(str(record.get("requeue_reason") or ""))
            if reason in RESTORABLE_PENDING_REASONS:
                restore_reason = reason
                restore_priority = bool(record.get("requeue_priority"))
        elif status == "pending":
            restore_reason = PENDING_REASON_RESTART_REQUESTED

        if not restore_reason:
            continue
        if _enqueue_swarm(
            deps,
            challenge_name,
            priority=restore_priority,
            reason=restore_reason,
        ):
            restored.append(challenge_name)
    return restored


def _ctfd_refresh_backoff_remaining(deps: CoordinatorDeps) -> float:
    return max(0.0, deps.ctfd_refresh_backoff_until - time.time())


def _clear_ctfd_refresh_backoff(deps: CoordinatorDeps) -> None:
    deps.ctfd_refresh_backoff_until = 0.0
    deps.ctfd_refresh_backoff_failures = 0
    deps.ctfd_refresh_backoff_reason = ""


def _note_ctfd_refresh_failure(deps: CoordinatorDeps, exc: Exception) -> float:
    failures = min(deps.ctfd_refresh_backoff_failures + 1, 4)
    delay = min(
        CTFD_REFRESH_BACKOFF_MAX_SECONDS,
        CTFD_REFRESH_BACKOFF_BASE_SECONDS * (2 ** (failures - 1)),
    )
    deps.ctfd_refresh_backoff_failures = failures
    deps.ctfd_refresh_backoff_until = time.time() + delay
    deps.ctfd_refresh_backoff_reason = str(exc).strip()
    return delay


def _is_retryable_spawn_result(result: str) -> bool:
    lowered = str(result or "").strip().lower()
    return lowered.endswith("refresh backoff active") or " refresh backoff active " in lowered or lowered.startswith(
        "could not refresh challenge"
    ) or lowered.startswith("could not pull challenge")


def _is_quota_blocked_spawn_result(result: str) -> bool:
    return str(result or "").strip().lower().startswith("no runnable models left")


async def do_fetch_challenges(deps: CoordinatorDeps) -> str:
    restored_solved = _restored_solved_names(deps)
    local_records = _local_challenge_records(deps, restored_solved)
    if deps.local_mode:
        result = local_records
        result.sort(key=_challenge_sort_key)
        return json.dumps(result, indent=2)
    try:
        challenges = await deps.ctfd.fetch_all_challenges()
        solved = await deps.ctfd.fetch_solved_names()
    except Exception as exc:
        logger.warning(
            "Could not fetch challenges from %s, using local preload only: %s",
            platform_label(deps.ctfd),
            exc,
        )
        result = local_records
        result.sort(key=_challenge_sort_key)
        return json.dumps(result, indent=2)

    solved |= restored_solved
    # Cache the RAW remote challenge dicts (with files/hints/connection_info)
    # so the Fetch button's auto-import step can feed them to pull_challenge.
    deps.remote_challenge_cache = {
        str(ch.get("name", "")): ch
        for ch in challenges
        if ch.get("name")
    }
    result = [
        {
            "name": ch.get("name", "?"),
            "category": ch.get("category", "?"),
            "value": ch.get("value", 0),
            "solves": ch.get("solves", 0),
            "status": "SOLVED" if ch.get("name") in solved else "unsolved",
            "description": (ch.get("description") or "")[:200],
            "source": ch.get("source") or getattr(deps.ctfd, "platform", "remote"),
        }
        for ch in challenges
    ]
    seen = {str(record.get("name", "")) for record in result}
    result.extend(record for record in local_records if str(record.get("name", "")) not in seen)
    result.sort(key=_challenge_sort_key)
    return json.dumps(result, indent=2)


async def do_get_solve_status(deps: CoordinatorDeps) -> str:
    solved = _restored_solved_names(deps)
    if not deps.local_mode:
        try:
            solved |= await deps.ctfd.fetch_solved_names()
        except Exception as exc:
            logger.warning(
                "Could not refresh solved status from %s: %s",
                platform_label(deps.ctfd),
                exc,
            )
    swarm_status = {name: swarm.get_status() for name, swarm in deps.swarms.items()}
    return json.dumps(
        {
            "solved": sorted(solved),
            "active_swarms": swarm_status,
            "queued_swarms": [entry["challenge_name"] for entry in _pending_swarm_entries(deps)],
        },
        indent=2,
    )


async def _spawn_swarm_now(deps: CoordinatorDeps, challenge_name: str) -> str:
    if challenge_name in deps.swarms:
        return f"Swarm still running for {challenge_name}"

    # Auto-pull challenge if needed
    if challenge_name not in deps.challenge_dirs:
        if deps.local_mode:
            return f"Challenge '{challenge_name}' not found under local challenges dir"
        remaining = _ctfd_refresh_backoff_remaining(deps)
        if remaining > 0:
            platform_name = platform_label(deps.ctfd)
            reason = deps.ctfd_refresh_backoff_reason or f"recent {platform_name} refresh failure"
            return f"{platform_name} refresh backoff active for {remaining:.0f}s after recent failure: {reason}"
        try:
            challenges = await deps.ctfd.fetch_all_challenges()
        except Exception as exc:
            delay = _note_ctfd_refresh_failure(deps, exc)
            logger.warning(
                "Could not refresh challenge %r from %s before spawn: %s (backoff %.0fs)",
                challenge_name,
                platform_label(deps.ctfd),
                exc,
                delay,
            )
            return f"Could not refresh challenge '{challenge_name}' from {platform_label(deps.ctfd)}: {exc}"
        _clear_ctfd_refresh_backoff(deps)
        ch_data = next((c for c in challenges if c.get("name") == challenge_name), None)
        if not ch_data:
            return f"Challenge '{challenge_name}' not found on {platform_label(deps.ctfd)}"
        output_dir = str(Path(deps.challenges_root))
        try:
            ch_dir = await deps.ctfd.pull_challenge(ch_data, output_dir)
        except Exception as exc:
            delay = _note_ctfd_refresh_failure(deps, exc)
            logger.warning(
                "Could not pull challenge %r from %s: %s (backoff %.0fs)",
                challenge_name,
                platform_label(deps.ctfd),
                exc,
                delay,
            )
            return f"Could not pull challenge '{challenge_name}' from {platform_label(deps.ctfd)}: {exc}"
        _clear_ctfd_refresh_backoff(deps)
        deps.challenge_dirs[challenge_name] = ch_dir
        deps.challenge_metas[challenge_name] = ChallengeMeta.from_yaml(Path(ch_dir) / "metadata.yml")

    refresh_effective_metadata(deps.challenge_dirs[challenge_name])
    meta = deps.challenge_metas[challenge_name]

    from backend.agents.swarm import ChallengeSwarm

    active_model_specs = [
        spec for spec in deps.model_specs if spec not in deps.quota_exhausted_model_specs
    ]
    if not active_model_specs:
        disabled = ", ".join(sorted(deps.quota_exhausted_model_specs)) or "all configured models"
        logger.warning(
            "Could not spawn %r: all configured models are session-disabled after quota exhaustion (%s)",
            challenge_name,
            disabled,
        )
        return (
            f"No runnable models left for '{challenge_name}' "
            f"(session quota exhausted: {disabled})"
        )

    swarm = ChallengeSwarm(
        challenge_dir=deps.challenge_dirs[challenge_name],
        meta=meta,
        ctfd=deps.ctfd,
        cost_tracker=deps.cost_tracker,
        settings=deps.settings,
        result_store=deps.results,
        model_specs=active_model_specs,
        disabled_model_specs=deps.quota_exhausted_model_specs,
        no_submit=(deps.no_submit or bool(getattr(meta, "no_submit", False))),
        local_mode=deps.local_mode,
        coordinator_inbox=deps.coordinator_inbox,
        solve_reports_log=deps.solve_reports,
    )
    deps.swarms[challenge_name] = swarm

    async def _run_and_cleanup() -> None:
        result = await swarm.run()
        existing = deps.results.get(challenge_name, {})
        record = dict(existing) if isinstance(existing, dict) else {}
        payload_fn = getattr(swarm, "_runtime_result_payload", None)
        if callable(payload_fn):
            record.update(payload_fn())
        if result:
            record.update(
                {
                    "step_count": max(int(record.get("step_count", 0) or 0), result.step_count),
                    "cost_usd": result.cost_usd,
                    "log_path": result.log_path,
                    "winner_model": swarm.winner_model_spec,
                    "advisor_note": swarm.last_advisor_note,
                    "coordinator_advisor_note": swarm.last_coordinator_advisor_note,
                    "shared_finding": swarm.last_shared_finding,
                    "shared_findings": {
                        model_spec: finding.snapshot()
                        for model_spec, finding in sorted(swarm.shared_finding_events.items())
                    },
                    "flag_candidates": {
                        flag: candidate.snapshot()
                        for flag, candidate in sorted(swarm.flag_candidates.items())
                    },
                }
            )
            if result.status == FLAG_FOUND:
                record.update(
                    {
                        "status": result.status,
                        "flag": result.flag,
                        "findings_summary": result.findings_summary,
                    }
                )
                if swarm.winner_confirmation_source == "operator_local":
                    record["submit"] = "approved in local mode"
                elif swarm.winner_confirmation_source == "operator_manual":
                    record["submit"] = "approved manually by operator"
                elif swarm.winner_confirmation_source == "operator_external":
                    record["submit"] = "reported solved by operator"
                else:
                    record["submit"] = "confirmed by solver"
            elif not getattr(swarm, "requeue_requested", False) and not bool(record.get("flag_candidates")):
                record.update(
                    {
                        "status": result.status,
                        "flag": result.flag,
                        "findings_summary": result.findings_summary,
                    }
                )
            if swarm.saved_solve_artifacts:
                record.update(swarm.saved_solve_artifacts)
        if record:
            deps.results[challenge_name] = record
        if getattr(swarm, "requeue_requested", False) and record.get("status") != FLAG_FOUND:
            restart_packets_fn = getattr(swarm, "snapshot_requeue_restart_packets", None)
            if callable(restart_packets_fn):
                restart_packets = restart_packets_fn()
                if restart_packets:
                    record["restart_packets"] = restart_packets
            _enqueue_finished_swarm(
                deps,
                challenge_name,
                priority=bool(getattr(swarm, "requeue_priority", False)),
                reason=_normalize_pending_reason(
                    str(getattr(swarm, "requeue_reason", "") or PENDING_REASON_QUEUED)
                ),
            )

    task = asyncio.create_task(_run_and_cleanup(), name=f"swarm-{challenge_name}")
    deps.swarm_tasks[challenge_name] = task
    return f"Swarm spawned for {challenge_name} with {len(active_model_specs)} models"


async def _fill_swarm_capacity(deps: CoordinatorDeps) -> list[str]:
    spawned: list[str] = []
    attempts_remaining = len(deps.pending_swarm_queue)
    while (
        deps.pending_swarm_queue
        and len(deps.swarms) < deps.max_concurrent_challenges
        and attempts_remaining > 0
    ):
        challenge_name = _pop_next_pending_swarm(
            deps,
            include_priority_waiting=False,
            include_quota_blocked=False,
        )
        if challenge_name is None:
            break
        attempts_remaining -= 1
        if challenge_name in deps.swarms:
            continue
        result = deps.results.get(challenge_name, {})
        if result.get("status") == FLAG_FOUND:
            continue
        spawn_result = await _spawn_swarm_now(deps, challenge_name)
        if challenge_name in deps.swarms:
            deps.pending_swarm_meta.pop(challenge_name, None)
            spawned.append(challenge_name)
            continue
        if _is_retryable_spawn_result(spawn_result):
            _enqueue_swarm(
                deps,
                challenge_name,
                priority=False,
                reason=PENDING_REASON_CTFD_RETRY,
            )
            continue
        if _is_quota_blocked_spawn_result(spawn_result):
            _enqueue_swarm(
                deps,
                challenge_name,
                priority=False,
                reason=PENDING_REASON_QUOTA_BLOCKED,
            )
            break
    return spawned


async def do_spawn_swarm(deps: CoordinatorDeps, challenge_name: str) -> str:
    _retire_finished_swarms(deps)
    result = deps.results.get(challenge_name, {})
    if result.get("status") == FLAG_FOUND:
        return f"Challenge {challenge_name} is already solved"

    if challenge_name in deps.swarms:
        return f"Swarm still running for {challenge_name}"
    if challenge_name in deps.pending_swarm_set:
        position = _pending_swarm_order(deps).index(challenge_name) + 1
        return f"Swarm already queued for {challenge_name} (position {position})"

    active_count = len(deps.swarms)
    if active_count >= deps.max_concurrent_challenges:
        if not _enqueue_swarm(deps, challenge_name, reason=PENDING_REASON_QUEUED):
            return f"Challenge {challenge_name} is already queued or solved"
        return (
            f"Queued swarm for {challenge_name} "
            f"({active_count}/{deps.max_concurrent_challenges} challenges running, "
            f"{len(deps.pending_swarm_queue)} queued)"
        )

    spawn_result = await _spawn_swarm_now(deps, challenge_name)
    if challenge_name in deps.swarms:
        return spawn_result
    if _is_retryable_spawn_result(spawn_result) and _enqueue_swarm(
        deps,
        challenge_name,
        reason=PENDING_REASON_CTFD_RETRY,
    ):
        position = _pending_swarm_order(deps).index(challenge_name) + 1
        return (
            f"Queued swarm for {challenge_name} awaiting remote refresh retry "
            f"(position {position}): {spawn_result}"
        )
    if _is_quota_blocked_spawn_result(spawn_result) and _enqueue_swarm(
        deps,
        challenge_name,
        reason=PENDING_REASON_QUOTA_BLOCKED,
    ):
        position = _pending_swarm_order(deps).index(challenge_name) + 1
        return (
            f"Queued swarm for {challenge_name} waiting on remaining non-quota models "
            f"(position {position}): {spawn_result}"
        )
    return spawn_result


async def do_check_swarm_status(deps: CoordinatorDeps, challenge_name: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    return json.dumps(swarm.get_status(), indent=2)


async def do_submit_flag(
    deps: CoordinatorDeps,
    challenge_name: str,
    flag: str,
    *,
    force: bool = False,
) -> str:
    """Submit a flag to the remote platform.

    Args:
        force: When True, bypass the ``no_submit`` guard.  Use this for
               human-initiated submissions (operator UI / CLI) where the human
               is explicitly requesting a submit even in human-coordinator mode.
               Still blocked in local mode (no remote platform available).
    """
    if deps.local_mode:
        return (
            f'LOCAL MODE — cannot submit "{flag.strip()}" for {challenge_name} remotely. '
            "Use approve-candidate or mark-solved instead."
        )
    if deps.no_submit and not force:
        return (
            f'SUBMISSION DISABLED — not submitting "{flag.strip()}" for {challenge_name} '
            "because --no-submit is set. Use approve-candidate or pass force=True for a "
            "human-initiated submit."
        )
    normalized = flag.strip()
    swarm = deps.swarms.get(challenge_name)
    if swarm:
        block_reason = swarm.candidate_resubmission_block_reason(normalized)
        if block_reason:
            return (
                f'SUBMIT BLOCKED — "{normalized}" was {block_reason}. '
                "Do not re-submit the same exact flag for this challenge."
            )
    else:
        existing = deps.results.get(challenge_name, {})
        raw_candidates = existing.get("flag_candidates", {}) if isinstance(existing, dict) else {}
        if isinstance(raw_candidates, dict):
            payload = raw_candidates.get(normalized)
            if isinstance(payload, dict):
                from backend.agents.swarm import ChallengeSwarm

                block_reason = ChallengeSwarm._candidate_resubmission_block_reason_from_status(
                    str(payload.get("status") or "")
                )
                if block_reason:
                    return (
                        f'SUBMIT BLOCKED — "{normalized}" was {block_reason}. '
                        "Do not re-submit the same exact flag for this challenge."
                    )
    try:
        result = await deps.ctfd.submit_flag(challenge_name, flag)
        if swarm:
            await swarm.note_coordinator_submission(flag, result.display, result.status)
        if result.status in {"correct", "already_solved"}:
            existing = deps.results.get(challenge_name, {})
            merged = dict(existing) if isinstance(existing, dict) else {}
            merged.update(
                {
                    "status": FLAG_FOUND,
                    "flag": flag.strip(),
                    "submit": "confirmed by coordinator",
                }
            )
            deps.results[challenge_name] = merged
            challenge_dir = deps.challenge_dirs.get(challenge_name)
            if challenge_dir:
                solve_dir = Path(challenge_dir) / "solve"
                solve_dir.mkdir(parents=True, exist_ok=True)
                (solve_dir / "flag.txt").write_text(flag.strip() + "\n", encoding="utf-8")
                (solve_dir / "result.json").write_text(
                    json.dumps(merged, indent=2),
                    encoding="utf-8",
                )
            # Auto-kill the swarm immediately — no need to wait for the platform
            # poller to detect the solve.
            if swarm and not swarm.cancel_event.is_set():
                swarm.kill(reason=f"flag confirmed by coordinator: {challenge_name}")
                logger.info("Auto-killed swarm for %s after flag accepted", challenge_name)
        return result.display
    except Exception as e:
        return f"submit_flag error: {e}"


def _known_challenge(deps: CoordinatorDeps, challenge_name: str) -> bool:
    return challenge_name in (
        set(deps.challenge_dirs)
        | set(deps.challenge_metas)
        | set(deps.results)
        | set(deps.swarms)
        | set(deps.pending_swarm_set)
    )


def _manual_confirmation_source(deps: CoordinatorDeps) -> str:
    return "operator_local" if deps.local_mode else "operator_manual"


def _manual_confirmation_display(deps: CoordinatorDeps, flag: str) -> str:
    if deps.local_mode:
        return f'USER CONFIRMED LOCALLY — "{flag}" marked solved in local mode.'
    return f'USER CONFIRMED MANUALLY — "{flag}" marked solved without automatic remote confirmation.'


def _manual_rejection_display(deps: CoordinatorDeps, flag: str) -> str:
    if deps.local_mode:
        return f'USER REJECTED — "{flag}" dismissed in local mode.'
    return f'USER REJECTED — "{flag}" dismissed by operator review.'


def _persist_result_snapshot(
    deps: CoordinatorDeps,
    challenge_name: str,
    payload: dict[str, object],
    *,
    write_flag: bool,
) -> None:
    challenge_dir = deps.challenge_dirs.get(challenge_name)
    if not challenge_dir:
        return
    solve_dir = Path(challenge_dir) / "solve"
    solve_dir.mkdir(parents=True, exist_ok=True)
    payload["shared_artifacts_path"] = str(resolve_shared_artifacts_dir(challenge_dir).resolve())
    if write_flag and payload.get("flag"):
        flag_path = solve_dir / "flag.txt"
        flag_path.write_text(str(payload.get("flag") or "") + "\n", encoding="utf-8")
        payload["flag_path"] = str(flag_path)
    result_path = solve_dir / "result.json"
    payload["result_path"] = str(result_path)
    result_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


async def do_mark_challenge_solved(
    deps: CoordinatorDeps,
    challenge_name: str,
    flag: str,
    *,
    note: str = "",
) -> str:
    normalized_flag = flag.strip()
    if not normalized_flag:
        return "External solve rejected: empty flag."

    note_text = " ".join(str(note or "").split()).strip()[:500]
    existing_before = deps.results.get(challenge_name, {})
    if isinstance(existing_before, dict) and existing_before.get("status") == FLAG_FOUND:
        existing_flag = str(existing_before.get("flag") or "").strip()
        if existing_flag == normalized_flag:
            return f'Already solved with "{normalized_flag}".'
        if existing_flag:
            return (
                f'Cannot mark "{normalized_flag}" solved because '
                f'"{existing_flag}" is already confirmed.'
            )

    swarm = deps.swarms.get(challenge_name)
    if swarm is not None:
        result = await swarm.mark_solved_externally(
            normalized_flag,
            note=note_text,
            approved_by="operator_external",
        )
        if result.startswith("USER REPORTED EXTERNAL SOLVE"):
            existing = deps.results.get(challenge_name, {})
            merged = dict(existing) if isinstance(existing, dict) else {}
            payload_fn = getattr(swarm, "_runtime_result_payload", None)
            if callable(payload_fn):
                merged.update(payload_fn())
            else:
                merged.update(
                    {
                        "challenge_name": challenge_name,
                        "status": FLAG_FOUND,
                        "flag": normalized_flag,
                        "confirmation_source": "operator_external",
                        "findings_summary": result,
                    }
                )
            if note_text:
                merged["external_note"] = note_text
            merged["submit"] = "reported solved by operator"
            _persist_result_snapshot(deps, challenge_name, merged, write_flag=True)
            deps.results[challenge_name] = merged
        return result

    if not _known_challenge(deps, challenge_name):
        return f'Unknown challenge "{challenge_name}".'

    _drop_pending_swarm(deps, challenge_name)
    saved_at = datetime.now(UTC).isoformat()
    display = (
        f'USER REPORTED EXTERNAL SOLVE — "{normalized_flag}" marked solved from operator input.'
    )
    if note_text:
        display = f"{display} Note: {note_text[:200]}"

    merged = dict(existing_before) if isinstance(existing_before, dict) else {}
    merged.update(
        {
            "challenge_name": challenge_name,
            "status": FLAG_FOUND,
            "flag": normalized_flag,
            "confirmation_source": "operator_external",
            "submit": "reported solved by operator",
            "findings_summary": display,
            "saved_at": saved_at,
        }
    )
    if note_text:
        merged["external_note"] = note_text

    _persist_result_snapshot(deps, challenge_name, merged, write_flag=True)

    deps.results[challenge_name] = merged
    return display


async def do_approve_flag_candidate(deps: CoordinatorDeps, challenge_name: str, flag: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    approved_by = _manual_confirmation_source(deps)
    normalized_flag = flag.strip()
    if not swarm:
        existing = deps.results.get(challenge_name, {})
        if not isinstance(existing, dict):
            return f"No swarm running for {challenge_name}"
        if existing.get("status") == FLAG_FOUND:
            existing_flag = str(existing.get("flag") or "").strip()
            if existing_flag == normalized_flag:
                return f'Already solved with "{normalized_flag}".'
            if existing_flag:
                return (
                    f'Cannot approve "{normalized_flag}" because '
                    f'"{existing_flag}" is already confirmed.'
                )
        raw_candidates = existing.get("flag_candidates", {})
        if not isinstance(raw_candidates, dict):
            return f'No candidate "{normalized_flag}" is queued for {challenge_name}.'
        candidate = raw_candidates.get(normalized_flag)
        if not isinstance(candidate, dict):
            return f'No candidate "{normalized_flag}" is queued for {challenge_name}.'
        display = _manual_confirmation_display(deps, normalized_flag)
        candidate_payload = dict(candidate)
        candidate_payload.update(
            {
                "status": "confirmed",
                "confirmation_source": approved_by,
                "submit_display": display,
                "last_seen_at": time.time(),
            }
        )
        flag_candidates = dict(raw_candidates)
        flag_candidates[normalized_flag] = candidate_payload
        merged = dict(existing)
        merged.update(
            {
                "challenge_name": challenge_name,
                "status": FLAG_FOUND,
                "flag": normalized_flag,
                "confirmation_source": approved_by,
                "findings_summary": display,
                "flag_candidates": flag_candidates,
                "submit": "approved in local mode" if approved_by == "operator_local" else "approved manually by operator",
            }
        )
        _drop_pending_swarm(deps, challenge_name)
        _persist_result_snapshot(deps, challenge_name, merged, write_flag=True)
        deps.results[challenge_name] = merged
        return display

    result = await swarm.approve_flag_candidate(flag, approved_by=approved_by)
    if result.startswith("USER CONFIRMED "):
        existing = deps.results.get(challenge_name, {})
        merged = dict(existing) if isinstance(existing, dict) else {}
        payload_fn = getattr(swarm, "_runtime_result_payload", None)
        if callable(payload_fn):
            merged.update(payload_fn())
        else:
            merged.update(
                {
                    "challenge_name": challenge_name,
                    "status": FLAG_FOUND,
                    "flag": flag.strip(),
                    "confirmation_source": approved_by,
                }
            )
        deps.results[challenge_name] = merged
    return result


async def do_reject_flag_candidate(deps: CoordinatorDeps, challenge_name: str, flag: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    rejected_by = _manual_confirmation_source(deps)
    normalized_flag = flag.strip()
    if not swarm:
        existing = deps.results.get(challenge_name, {})
        if not isinstance(existing, dict):
            return f"No swarm running for {challenge_name}"
        if existing.get("status") == FLAG_FOUND:
            existing_flag = str(existing.get("flag") or "").strip()
            if existing_flag == normalized_flag:
                return f'Cannot reject "{normalized_flag}" because it is already confirmed.'
            if existing_flag:
                return (
                    f'Cannot reject "{normalized_flag}" because '
                    f'"{existing_flag}" is already confirmed.'
                )
        raw_candidates = existing.get("flag_candidates", {})
        if not isinstance(raw_candidates, dict):
            return f'No candidate "{normalized_flag}" is queued for {challenge_name}.'
        candidate = raw_candidates.get(normalized_flag)
        if not isinstance(candidate, dict):
            return f'No candidate "{normalized_flag}" is queued for {challenge_name}.'
        display = _manual_rejection_display(deps, normalized_flag)
        candidate_payload = dict(candidate)
        candidate_payload.update(
            {
                "status": "rejected",
                "confirmation_source": rejected_by,
                "submit_display": display,
                "last_seen_at": time.time(),
            }
        )
        flag_candidates = dict(raw_candidates)
        flag_candidates[normalized_flag] = candidate_payload
        merged = dict(existing)
        merged["flag_candidates"] = flag_candidates
        merged["status"] = (
            "candidate_pending"
            if any(
                str(payload.get("status") or "").strip().lower() not in {"confirmed", "rejected"}
                for payload in flag_candidates.values()
                if isinstance(payload, dict)
            )
            else "pending"
        )
        _enqueue_swarm(
            deps,
            challenge_name,
            priority=False,
            reason=PENDING_REASON_CANDIDATE_RETRY,
        )
        _persist_result_snapshot(deps, challenge_name, merged, write_flag=False)
        deps.results[challenge_name] = merged
        return display

    result = await swarm.reject_flag_candidate(flag, rejected_by=rejected_by)
    if result.startswith("USER REJECTED"):
        existing = deps.results.get(challenge_name, {})
        merged = dict(existing) if isinstance(existing, dict) else {}
        payload_fn = getattr(swarm, "_runtime_result_payload", None)
        if callable(payload_fn):
            merged.update(payload_fn())
            deps.results[challenge_name] = merged
    return result


async def do_set_max_concurrent_challenges(deps: CoordinatorDeps, max_active: int) -> str:
    if max_active < 0:
        return "max_active must be >= 0"

    previous = deps.max_concurrent_challenges
    deps.max_concurrent_challenges = max_active
    try:
        deps.settings.max_concurrent_challenges = max_active
    except Exception:
        pass

    if max_active > previous and deps.pending_swarm_queue and len(deps.swarms) < max_active:
        await _fill_swarm_capacity(deps)

    active_count = len(deps.swarms)
    if active_count > max_active:
        return (
            f"Active challenge limit updated: {previous} -> {max_active}. "
            f"Soft cap active; {active_count} swarms will drain naturally."
        )
    return f"Active challenge limit updated: {previous} -> {max_active}."


async def do_set_challenge_priority_waiting(
    deps: CoordinatorDeps,
    challenge_name: str,
    *,
    priority: bool,
) -> str:
    if not _known_challenge(deps, challenge_name):
        return f'Unknown challenge "{challenge_name}".'

    existing = deps.results.get(challenge_name, {})
    if isinstance(existing, dict) and existing.get("status") == FLAG_FOUND:
        return f'Challenge "{challenge_name}" is already solved.'

    swarm = deps.swarms.get(challenge_name)
    if swarm is not None:
        if not priority:
            return f'Challenge "{challenge_name}" is currently active; restore it by letting the current run finish.'
        requester = getattr(swarm, "request_requeue", None)
        if callable(requester):
            requester(priority=True, reason=PENDING_REASON_PRIORITY_WAITING)
        swarm.kill(reason=f"operator moved {challenge_name} to priority waiting")
        return f'Pausing "{challenge_name}" and returning it to priority waiting.'

    if challenge_name in deps.pending_swarm_set:
        _set_pending_swarm_meta(
            deps,
            challenge_name,
            priority=priority,
            reason=PENDING_REASON_PRIORITY_WAITING if priority else PENDING_REASON_QUEUED,
        )
        if not priority and deps.pending_swarm_queue and len(deps.swarms) < deps.max_concurrent_challenges:
            await _fill_swarm_capacity(deps)
        return (
            f'Challenge "{challenge_name}" moved to priority waiting.'
            if priority
            else f'Challenge "{challenge_name}" restored to standard waiting.'
        )

    if not _enqueue_swarm(
        deps,
        challenge_name,
        priority=priority,
        reason=PENDING_REASON_PRIORITY_WAITING if priority else PENDING_REASON_QUEUED,
    ):
        return f'Could not queue "{challenge_name}".'
    if not priority and deps.pending_swarm_queue and len(deps.swarms) < deps.max_concurrent_challenges:
        await _fill_swarm_capacity(deps)
    return (
        f'Challenge "{challenge_name}" queued as priority waiting.'
        if priority
        else f'Challenge "{challenge_name}" queued.'
    )


async def do_restart_challenge(deps: CoordinatorDeps, challenge_name: str) -> str:
    if not _known_challenge(deps, challenge_name):
        return f'Unknown challenge "{challenge_name}".'

    existing = deps.results.get(challenge_name, {})
    if isinstance(existing, dict) and existing.get("status") == FLAG_FOUND:
        return f'Challenge "{challenge_name}" is already solved.'

    swarm = deps.swarms.get(challenge_name)
    if swarm is not None:
        requester = getattr(swarm, "request_requeue", None)
        if callable(requester):
            requester(priority=True, reason=PENDING_REASON_RESTART_REQUESTED)
        swarm.kill(reason=f"operator restarting {challenge_name}")
        return f'Restarting "{challenge_name}" after the current run stops.'

    if challenge_name in deps.pending_swarm_set:
        _set_pending_swarm_meta(
            deps,
            challenge_name,
            priority=True,
            reason=PENDING_REASON_RESTART_REQUESTED,
        )
        if deps.pending_swarm_queue and len(deps.swarms) < deps.max_concurrent_challenges:
            await _fill_swarm_capacity(deps)
        if challenge_name in deps.swarms:
            return f'Restarted "{challenge_name}" from saved notes.'
        return f'Restart queued for "{challenge_name}".'

    active_count = len(deps.swarms)
    if active_count >= deps.max_concurrent_challenges:
        if not _enqueue_swarm(
            deps,
            challenge_name,
            priority=True,
            reason=PENDING_REASON_RESTART_REQUESTED,
        ):
            return f'Could not queue "{challenge_name}" for restart.'
        return (
            f'Restart queued for "{challenge_name}" '
            f"({active_count}/{deps.max_concurrent_challenges} challenges running)."
        )

    spawn_result = await _spawn_swarm_now(deps, challenge_name)
    if challenge_name in deps.swarms:
        return f'Restarted "{challenge_name}" from saved notes.'
    if _is_retryable_spawn_result(spawn_result) and _enqueue_swarm(
        deps,
        challenge_name,
        priority=True,
        reason=PENDING_REASON_RESTART_REQUESTED,
    ):
        return f'Restart queued for "{challenge_name}": {spawn_result}'
    return spawn_result


async def do_kill_swarm(deps: CoordinatorDeps, challenge_name: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    swarm.kill(reason=f"operator kill: {challenge_name}")
    return f"Swarm for {challenge_name} cancelled"


async def do_bump_agent(deps: CoordinatorDeps, challenge_name: str, model_spec: str, insights: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    solver = swarm.solvers.get(model_spec)
    if not solver:
        return f"No solver for {model_spec} in {challenge_name}"
    # Append an acknowledge-please clause so the human can see their hint
    # was actually received.  Explicit about notify_coordinator because
    # agentMessage / reasoning text doesn't surface to the human GUI —
    # only notify_coordinator tool calls do.
    tagged = (
        f"[HUMAN TACTICAL HINT] {str(insights or '').strip()}\n\n"
        "Acknowledge by emitting a `notify_coordinator` tool call on "
        "your very next step ('received — applying …' is fine).  "
        "IMPORTANT: the human only sees notify_coordinator tool calls "
        "in their GUI — your agentMessage / reasoning text is invisible "
        "to them.  If you reply only in reasoning, the human will think "
        "you ignored them."
    )
    operator_bump = getattr(solver, "bump_operator", None)
    if callable(operator_bump):
        operator_bump(tagged)
    else:
        solver.bump(tagged)
    # Publish the human's bump as a visible report so the operator sees
    # their own intervention land in the feed.  Without this the message
    # silently went into the solver's commands queue — if the lane chose
    # not to respond via notify_coordinator (likely for soft hints), the
    # operator had zero signal that it was even delivered.
    publish_report = getattr(swarm, "publish_report", None)
    if callable(publish_report):
        publish_report(
            kind="hint",
            title=f"[HUMAN → {model_spec}] {str(insights or '').strip()[:160]}",
            body=str(insights or ""),
            lane_id=model_spec,
        )
    return f"Bumped {model_spec} on {challenge_name}"


async def do_read_solver_trace(deps: CoordinatorDeps, challenge_name: str, model_spec: str, last_n: int = 20) -> str:
    """Read the last N trace events from a solver's JSONL log."""
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm for {challenge_name}"
    solver = swarm.solvers.get(model_spec)
    if not solver:
        return f"No solver for {model_spec}"
    trace_path = getattr(solver, "tracer", None)
    if not trace_path:
        return "No tracer on solver"
    path = trace_path.path if hasattr(trace_path, "path") else str(trace_path)
    try:
        lines = Path(path).read_text().strip().split("\n")
        recent = lines[-last_n:]
        summary = []
        for line in recent:
            try:
                d = json.loads(line)
                t = d.get("type", "?")
                if t == "tool_call":
                    args_str = str(d.get("args", ""))[:100]
                    summary.append(f"step {d.get('step','?')} CALL {d.get('tool','?')}: {args_str}")
                elif t == "tool_result":
                    result_str = str(d.get("result", ""))[:100]
                    summary.append(f"step {d.get('step','?')} RESULT {d.get('tool','?')}: {result_str}")
                elif t in ("finish", "error", "bump", "turn_failed"):
                    summary.append(f"** {t}: {json.dumps({k:v for k,v in d.items() if k != 'ts'})}")
                elif t == "usage":
                    summary.append(f"usage: in={d.get('input_tokens',0)} out={d.get('output_tokens',0)} cost=${d.get('cost_usd',0):.4f}")
                else:
                    summary.append(f"{t}: {str(d)[:80]}")
            except Exception:
                summary.append(line[:100])
        return "\n".join(summary)
    except FileNotFoundError:
        return f"Trace file not found: {path}"
    except Exception as e:
        return f"Error reading trace: {e}"


async def do_broadcast(deps: CoordinatorDeps, challenge_name: str, message: str) -> str:
    """Broadcast a message to all solvers working on a challenge."""
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    await swarm.message_bus.broadcast(message)
    # Visible receipt in the Reports tab so the operator sees the broadcast
    # actually went out, even if no lane chooses to echo it back.
    publish_report = getattr(swarm, "publish_report", None)
    if callable(publish_report):
        publish_report(
            kind="hint",
            title=f"[HUMAN BROADCAST → all lanes] {str(message or '').strip()[:160]}",
            body=str(message or ""),
            lane_id="all lanes",
        )
    return f"Broadcast to all solvers on {challenge_name}"


async def do_advisor_intervene(deps: CoordinatorDeps, challenge_name: str, critique: str) -> str:
    """Send a human strategic override to the advisor and all active solver lanes.

    The advisor cannot be preempted mid-LLM-call (API calls complete atomically),
    but its monitoring loop polls every ~2 seconds.  The critique is posted to the
    shared message bus — the advisor sees it on its next cycle and adjusts its
    directions accordingly.  All active solver lanes also receive the critique
    immediately via operator bump so they can factor it into their next step.
    """
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"

    # Tag the message so the advisor can identify it as a human override.
    tagged = f"[HUMAN STRATEGIC OVERRIDE] {critique.strip()}"
    await swarm.message_bus.broadcast(tagged)

    # Also bump every non-finished lane directly so they pick it up without
    # waiting for the message bus delivery on the next solver turn.
    bumped: list[str] = []
    stronger_prompt = (
        "[HUMAN STRATEGIC OVERRIDE] "
        + critique.strip()
        + "\n\nAcknowledge this by emitting a `notify_coordinator` tool "
        "call on your very next step ('received — pivoting to …' is fine).  "
        "IMPORTANT: the human only sees notify_coordinator tool calls in "
        "their GUI — your agentMessage / reasoning text is invisible to "
        "them.  If you reply only in reasoning, the human will think you "
        "ignored them."
    )
    for model_spec, solver in swarm.solvers.items():
        bump_fn = getattr(solver, "bump_operator", None) or getattr(solver, "bump", None)
        if callable(bump_fn):
            bump_fn(stronger_prompt)
            bumped.append(model_spec)

    # Visible receipt — without this the strategic override was silent
    # from the operator's perspective.  Now the Reports feed shows the
    # override immediately; if any lane responds via notify_coordinator the
    # response lands right underneath as a classified report.
    publish_report = getattr(swarm, "publish_report", None)
    if callable(publish_report):
        publish_report(
            kind="hint",
            title=f"[HUMAN STRATEGIC OVERRIDE → {len(bumped)} lane(s)] "
                  + critique.strip()[:140],
            body=critique.strip(),
            lane_id="all lanes",
        )

    logger.info(
        "Human intervention on %s: bumped %d lane(s). Critique: %s",
        challenge_name, len(bumped), critique[:120],
    )
    return f"Intervention sent to {len(bumped)} lane(s) on '{challenge_name}'"


async def do_request_status_report(
    deps: CoordinatorDeps,
    challenge_name: str,
    *,
    report_window: int = 40,
    **_legacy_kwargs: Any,
) -> str:
    """Ask the swarm + advisor for a status report — multiple paths so we
    ALWAYS give the human something useful.

    The operator kept hitting "advisor returned no synthesis" messages.
    Single-path approach was fragile — if Claude was rate-limited or the
    prompt format confused the advisor's NO_ADVICE heuristic, the human
    got nothing.  Now we fire three parallel signals:

    1. **Lane status bumps** — every active lane gets a soft bump asking
       it to post one notify_coordinator call summarising what it's
       doing.  Each lane's response flows into solve_reports as a
       classified entry within 5–30 s.  Visible guaranteed output.

    2. **Advisor synthesis** (fire-and-forget) — if advisor is available
       and not rate-limited, it publishes a narrative synthesis report.

    3. **Fallback mechanical digest** — if #2 returns empty OR errors,
       we publish a mechanical per-lane digest so the Reports tab isn't
       empty.  Clearly labelled "[mechanical — advisor unavailable]"
       so the operator knows the difference.
    """
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"

    import time as _time

    # Collect recent reports for THIS challenge from the shared log.
    recent_all = list(getattr(deps, "solve_reports", []))
    recent = [
        r for r in recent_all
        if isinstance(r, dict) and str(r.get("challenge_name") or "") == challenge_name
    ]
    recent = recent[-max(4, int(report_window or 40)):]

    # Current lane state.
    try:
        status = swarm.get_status()
    except Exception:  # noqa: BLE001
        status = {}
    agents = status.get("agents", {}) if isinstance(status, dict) else {}
    active_lanes = [spec for spec in (agents or {}) if isinstance(agents.get(spec), dict)]

    publish_report = getattr(swarm, "publish_report", None)

    # ── Path 1: immediate mechanical per-lane digest (always works) ──────
    # The user needs something to read the moment they click — not a
    # 5-30s wait + possibly empty advisor.  Mechanical is clearly
    # labelled so it can't be mistaken for advisor reasoning.
    mechanical_lines: list[str] = []
    for spec, agent in (agents or {}).items():
        if not isinstance(agent, dict):
            continue
        lifecycle = str(agent.get("lifecycle") or agent.get("status") or "?")
        steps = int(agent.get("step_count", 0) or 0)
        cur = str(agent.get("current_command") or agent.get("activity") or "").strip()[:180]
        finding = str(agent.get("findings") or "").strip()[:300]
        lane_block = [f"• {spec} — {lifecycle}, {steps} steps"]
        if cur:
            lane_block.append(f"  now: {cur}")
        if finding:
            lane_block.append(f"  last finding: {finding}")
        mechanical_lines.append("\n".join(lane_block))
    recent_kinds_counter: dict[str, int] = {}
    for r in recent:
        k = str(r.get("kind") or "lane_note")
        recent_kinds_counter[k] = recent_kinds_counter.get(k, 0) + 1
    kinds_summary = ", ".join(f"{k}×{v}" for k, v in sorted(recent_kinds_counter.items()))
    mechanical_body = (
        "[mechanical digest — raw in-memory state, NOT advisor reasoning]\n\n"
        + (f"Recent activity ({len(recent)} reports): {kinds_summary or 'none'}\n\n"
           if recent else "")
        + "Active lanes:\n"
        + ("\n\n".join(mechanical_lines) if mechanical_lines else "  (none)")
    )
    if callable(publish_report):
        publish_report(
            kind="hint",
            title=f"[STATUS — mechanical digest] {len(active_lanes)} lane(s), {len(recent)} recent report(s)",
            body=mechanical_body,
            lane_id="swarm",
        )

    # ── Path 2: lane self-report bumps (soft, no pause) ──────────────────
    # Each lane gets a bump asking it to emit a notify_coordinator call
    # summarising its state on the next step boundary.  No mid-step
    # interrupt — lane finishes its current tool call first.
    bumped: list[str] = []
    prompt = (
        "[HUMAN STATUS REQUEST] The human pressed 'Report now' and wants "
        "to know what you're doing.  On your very next step, emit a "
        "`notify_coordinator` tool call (NOT just agentMessage / reasoning "
        "— the human only sees notify_coordinator tool calls in their GUI) "
        "with a short status:\n"
        "  • Currently: <what you're working on>\n"
        "  • Tried: <1–2 things you've tried>\n"
        "  • Learned: <what you know so far>\n"
        "  • Blocked: <if stuck>\n"
        "  • Next: <your next step>\n"
        "Keep it under ~8 lines.  After that, resume your current task "
        "with the tool call you were about to make."
    )
    for spec in active_lanes:
        solver = swarm.solvers.get(spec)
        bump_fn = getattr(solver, "bump_operator", None) or getattr(solver, "bump", None)
        if callable(bump_fn):
            try:
                bump_fn(prompt)
                bumped.append(spec)
            except Exception as exc:  # noqa: BLE001
                logger.debug("status bump failed for %s: %s", spec, exc)

    # ── Path 3: advisor synthesis (fire-and-forget, best-effort) ─────────
    schedule_fn = getattr(swarm, "_schedule_background", None)
    build_synth = getattr(swarm, "_build_advised_coordinator_message", None)
    synth_scheduled = False
    if callable(schedule_fn) and callable(build_synth) and (recent or active_lanes):
        async def _advisor_synth() -> None:
            try:
                # Short prompt to avoid confusing the advisor's NO_ADVICE heuristic.
                report_lines: list[str] = []
                for r in recent[-20:]:  # keep prompt small
                    ts = _time.strftime("%H:%M:%S", _time.localtime(float(r.get("ts", 0) or 0)))
                    lane = str(r.get("lane_id") or "")[:60]
                    kind = str(r.get("kind") or "")[:20]
                    title = str(r.get("title") or "")[:150]
                    report_lines.append(f"[{ts}] {lane} ({kind}): {title}")

                state_lines: list[str] = []
                for spec, agent in (agents or {}).items():
                    if not isinstance(agent, dict):
                        continue
                    lifecycle = str(agent.get("lifecycle") or "?")
                    steps = int(agent.get("step_count", 0) or 0)
                    finding = str(agent.get("findings") or "").strip()[:200]
                    state_lines.append(
                        f"{spec}: {lifecycle}, {steps} steps"
                        + (f" — {finding}" if finding else "")
                    )

                synth_prompt = (
                    "A human operator pressed 'status report now' on a CTF "
                    "swarm they're supervising.  Give them a 5–10 line "
                    "human-readable status of what the swarm is doing and "
                    "what to watch for.  Never say NO_ADVICE — they pressed "
                    "the button, they want a human-readable status.  At "
                    "minimum describe: what each lane is doing, any promising "
                    "leads, any dead ends.\n\n"
                    "RECENT REPORTS:\n"
                    + ("\n".join(report_lines) if report_lines else "(none yet)")
                    + "\n\nLANE STATE:\n"
                    + ("\n".join(state_lines) if state_lines else "(no active lanes)")
                )
                first_lane = active_lanes[0] if active_lanes else "swarm"
                result = await build_synth(first_lane, synth_prompt)
                # If advisor returns the prompt unchanged (its "no advice" path),
                # we already published the mechanical digest above — no fallback
                # needed here.  Just log quietly.
                if not str(result or "").strip() or str(result).strip() == synth_prompt.strip():
                    logger.debug("advisor returned no synthesis for report-now; mechanical already published")
            except Exception as exc:  # noqa: BLE001
                logger.warning("report-now advisor synthesis failed: %s", exc)

        try:
            schedule_fn(_advisor_synth())
            synth_scheduled = True
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not schedule advisor synth: %s", exc)

    logger.info(
        "Report-now for %s: mechanical digest published, bumped %d lane(s), advisor synthesis %s",
        challenge_name, len(bumped),
        "scheduled" if synth_scheduled else "skipped",
    )
    pieces = [f"mechanical digest published in Reports (immediate)"]
    if bumped:
        pieces.append(f"asked {len(bumped)} lane(s) for self-report")
    if synth_scheduled:
        pieces.append("advisor synthesis queued (best-effort)")
    return " · ".join(pieces)


async def do_add_persistent_directive(
    deps: CoordinatorDeps, challenge_name: str, text: str,
) -> dict[str, Any]:
    """Register a standing directive on an active swarm.

    Unlike Strategic Override / Tactical Hint (one-shot), a standing
    directive is re-bumped every ~30 s so it stays in the solvers'
    recent context until the operator revokes it.
    """
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return {"ok": False, "error": f"No swarm running for {challenge_name}"}
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "error": "text is required"}
    add_fn = getattr(swarm, "add_persistent_directive", None)
    if not callable(add_fn):
        return {"ok": False, "error": "Swarm doesn't support standing directives"}
    directive_id = add_fn(text)
    return {
        "ok": True,
        "id": directive_id,
        "directives": list(getattr(swarm, "persistent_directives", [])),
    }


async def do_remove_persistent_directive(
    deps: CoordinatorDeps, challenge_name: str, directive_id: str,
) -> dict[str, Any]:
    """Remove a specific standing directive by ID."""
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return {"ok": False, "error": f"No swarm running for {challenge_name}"}
    remove_fn = getattr(swarm, "remove_persistent_directive", None)
    if not callable(remove_fn):
        return {"ok": False, "error": "Swarm doesn't support standing directives"}
    found = remove_fn(directive_id)
    return {
        "ok": True,
        "removed": found,
        "directives": list(getattr(swarm, "persistent_directives", [])),
    }


async def do_list_persistent_directives(
    deps: CoordinatorDeps, challenge_name: str,
) -> dict[str, Any]:
    """Return the current standing directives for a challenge."""
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return {"ok": False, "error": f"No swarm running for {challenge_name}"}
    return {
        "ok": True,
        "directives": list(getattr(swarm, "persistent_directives", [])),
    }


async def do_clear_challenge_history(
    deps: CoordinatorDeps,
    challenge_name: str,
    *,
    delete_traces: bool = False,
) -> str:
    """Clear a challenge's solve history (result.json, flag.txt) from disk and memory.

    The swarm must not be actively running; kill it first if needed.
    Pass delete_traces=True to also remove solver JSONL trace files.
    """
    swarm = deps.swarms.get(challenge_name)
    if swarm and not swarm.cancel_event.is_set():
        return (
            f"Cannot clear history: swarm is still active for '{challenge_name}'. "
            "Kill it first with kill-swarm."
        )

    # Drop from in-memory state.
    deps.results.pop(challenge_name, None)
    _drop_pending_swarm(deps, challenge_name)

    deleted: list[str] = []
    challenge_dir = deps.challenge_dirs.get(challenge_name)
    if challenge_dir:
        solve_dir = Path(challenge_dir) / "solve"
        for fname in ("flag.txt", "result.json"):
            p = solve_dir / fname
            if p.exists():
                p.unlink()
                deleted.append(fname)

        if delete_traces:
            import glob as _glob
            pattern = str(Path(challenge_dir) / "**" / f"trace-*")
            for tf in _glob.glob(pattern, recursive=True):
                try:
                    Path(tf).unlink()
                    deleted.append(Path(tf).name)
                except OSError:
                    pass

    if not deleted:
        return f"Nothing to clear for '{challenge_name}' (already clean)."
    return f"Cleared history for '{challenge_name}': {', '.join(deleted)}"
