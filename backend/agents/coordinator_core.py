"""Shared coordinator tool logic — called by both Claude SDK and Codex coordinators."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from pathlib import Path

from backend.deps import CoordinatorDeps
from backend.prompts import ChallengeMeta
from backend.solver_base import FLAG_FOUND

logger = logging.getLogger(__name__)


def _challenge_sort_key(challenge: dict[str, object]) -> tuple[int, str]:
    solves = int(challenge.get("solves", 0) or 0)
    name = str(challenge.get("name", ""))
    return (-solves, name)


def _restored_solved_names(deps: CoordinatorDeps) -> set[str]:
    solved: set[str] = set()
    for name, result in deps.results.items():
        if not isinstance(result, dict):
            continue
        if result.get("status") == FLAG_FOUND or result.get("flag"):
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
    for name, swarm in list(deps.swarms.items()):
        task = deps.swarm_tasks.get(name)
        if (getattr(swarm, "cancel_event", None) and swarm.cancel_event.is_set()) or (
            task and task.done()
        ):
            retired.append(name)
    for name in retired:
        deps.swarms.pop(name, None)
        deps.swarm_tasks.pop(name, None)
    return retired


def _drop_pending_swarm(deps: CoordinatorDeps, challenge_name: str) -> bool:
    if challenge_name not in deps.pending_swarm_set:
        return False
    deps.pending_swarm_set.discard(challenge_name)
    deps.pending_swarm_queue = deque(
        item for item in deps.pending_swarm_queue if item != challenge_name
    )
    return True


def _enqueue_swarm(deps: CoordinatorDeps, challenge_name: str) -> bool:
    if challenge_name in deps.swarms or challenge_name in deps.pending_swarm_set:
        return False
    result = deps.results.get(challenge_name, {})
    if result.get("status") == FLAG_FOUND or result.get("flag"):
        return False
    deps.pending_swarm_queue.append(challenge_name)
    deps.pending_swarm_set.add(challenge_name)
    return True


async def do_fetch_challenges(deps: CoordinatorDeps) -> str:
    restored_solved = _restored_solved_names(deps)
    local_records = _local_challenge_records(deps, restored_solved)
    try:
        challenges = await deps.ctfd.fetch_all_challenges()
        solved = await deps.ctfd.fetch_solved_names()
    except Exception as exc:
        logger.warning("Could not fetch challenges from CTFd, using local preload only: %s", exc)
        result = local_records
        result.sort(key=_challenge_sort_key)
        return json.dumps(result, indent=2)

    solved |= restored_solved
    result = [
        {
            "name": ch.get("name", "?"),
            "category": ch.get("category", "?"),
            "value": ch.get("value", 0),
            "solves": ch.get("solves", 0),
            "status": "SOLVED" if ch.get("name") in solved else "unsolved",
            "description": (ch.get("description") or "")[:200],
            "source": "ctfd",
        }
        for ch in challenges
    ]
    seen = {str(record.get("name", "")) for record in result}
    result.extend(record for record in local_records if str(record.get("name", "")) not in seen)
    result.sort(key=_challenge_sort_key)
    return json.dumps(result, indent=2)


async def do_get_solve_status(deps: CoordinatorDeps) -> str:
    solved = await deps.ctfd.fetch_solved_names()
    solved |= _restored_solved_names(deps)
    swarm_status = {name: swarm.get_status() for name, swarm in deps.swarms.items()}
    return json.dumps(
        {
            "solved": sorted(solved),
            "active_swarms": swarm_status,
            "queued_swarms": list(deps.pending_swarm_queue),
        },
        indent=2,
    )


async def _spawn_swarm_now(deps: CoordinatorDeps, challenge_name: str) -> str:
    if challenge_name in deps.swarms:
        return f"Swarm still running for {challenge_name}"

    # Auto-pull challenge if needed
    if challenge_name not in deps.challenge_dirs:
        challenges = await deps.ctfd.fetch_all_challenges()
        ch_data = next((c for c in challenges if c.get("name") == challenge_name), None)
        if not ch_data:
            return f"Challenge '{challenge_name}' not found on CTFd"
        output_dir = str(Path(deps.challenges_root))
        ch_dir = await deps.ctfd.pull_challenge(ch_data, output_dir)
        deps.challenge_dirs[challenge_name] = ch_dir
        deps.challenge_metas[challenge_name] = ChallengeMeta.from_yaml(Path(ch_dir) / "metadata.yml")

    from backend.agents.swarm import ChallengeSwarm

    swarm = ChallengeSwarm(
        challenge_dir=deps.challenge_dirs[challenge_name],
        meta=deps.challenge_metas[challenge_name],
        ctfd=deps.ctfd,
        cost_tracker=deps.cost_tracker,
        settings=deps.settings,
        model_specs=deps.model_specs,
        no_submit=deps.no_submit,
        coordinator_inbox=deps.coordinator_inbox,
    )
    deps.swarms[challenge_name] = swarm

    async def _run_and_cleanup() -> None:
        result = await swarm.run()
        if result:
            record = {
                "status": result.status,
                "flag": result.flag,
                "findings_summary": result.findings_summary,
                "step_count": result.step_count,
                "cost_usd": result.cost_usd,
                "log_path": result.log_path,
                "winner_model": swarm.winner_model_spec,
            }
            if result.status == FLAG_FOUND:
                record["submit"] = "DRY RUN" if deps.no_submit else "confirmed by solver"
            if swarm.saved_solve_artifacts:
                record.update(swarm.saved_solve_artifacts)
            deps.results[challenge_name] = record

    task = asyncio.create_task(_run_and_cleanup(), name=f"swarm-{challenge_name}")
    deps.swarm_tasks[challenge_name] = task
    return f"Swarm spawned for {challenge_name} with {len(deps.model_specs)} models"


async def _fill_swarm_capacity(deps: CoordinatorDeps) -> list[str]:
    spawned: list[str] = []
    while deps.pending_swarm_queue and len(deps.swarms) < deps.max_concurrent_challenges:
        challenge_name = deps.pending_swarm_queue.popleft()
        deps.pending_swarm_set.discard(challenge_name)
        if challenge_name in deps.swarms:
            continue
        result = deps.results.get(challenge_name, {})
        if result.get("status") == FLAG_FOUND or result.get("flag"):
            continue
        await _spawn_swarm_now(deps, challenge_name)
        spawned.append(challenge_name)
    return spawned


async def do_spawn_swarm(deps: CoordinatorDeps, challenge_name: str) -> str:
    _retire_finished_swarms(deps)
    result = deps.results.get(challenge_name, {})
    if result.get("status") == FLAG_FOUND or result.get("flag"):
        return f"Challenge {challenge_name} is already solved"

    if challenge_name in deps.swarms:
        return f"Swarm still running for {challenge_name}"
    if challenge_name in deps.pending_swarm_set:
        position = list(deps.pending_swarm_queue).index(challenge_name) + 1
        return f"Swarm already queued for {challenge_name} (position {position})"

    active_count = len(deps.swarms)
    if active_count >= deps.max_concurrent_challenges:
        if not _enqueue_swarm(deps, challenge_name):
            return f"Challenge {challenge_name} is already queued or solved"
        return (
            f"Queued swarm for {challenge_name} "
            f"({active_count}/{deps.max_concurrent_challenges} challenges running, "
            f"{len(deps.pending_swarm_queue)} queued)"
        )

    return await _spawn_swarm_now(deps, challenge_name)


async def do_check_swarm_status(deps: CoordinatorDeps, challenge_name: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    return json.dumps(swarm.get_status(), indent=2)


async def do_submit_flag(deps: CoordinatorDeps, challenge_name: str, flag: str) -> str:
    if deps.no_submit:
        return f'DRY RUN — would submit "{flag.strip()}" for {challenge_name}'
    try:
        result = await deps.ctfd.submit_flag(challenge_name, flag)
        return result.display
    except Exception as e:
        return f"submit_flag error: {e}"


async def do_kill_swarm(deps: CoordinatorDeps, challenge_name: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    swarm.kill()
    return f"Swarm for {challenge_name} cancelled"


async def do_bump_agent(deps: CoordinatorDeps, challenge_name: str, model_spec: str, insights: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    solver = swarm.solvers.get(model_spec)
    if not solver:
        return f"No solver for {model_spec} in {challenge_name}"
    operator_bump = getattr(solver, "bump_operator", None)
    if callable(operator_bump):
        operator_bump(insights)
    else:
        solver.bump(insights)
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
    return f"Broadcast to all solvers on {challenge_name}"
