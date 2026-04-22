from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from backend.agents.coordinator_core import (
    PENDING_REASON_PRIORITY_WAITING,
    PENDING_REASON_QUEUED,
    PENDING_REASON_QUOTA_BLOCKED,
    PENDING_REASON_RESTART_REQUESTED,
    _fill_swarm_capacity,
    _retire_finished_swarms,
    _spawn_swarm_now,
    do_fetch_challenges,
    do_get_solve_status,
    do_reject_flag_candidate,
    do_restart_challenge,
    do_set_challenge_priority_waiting,
    do_spawn_swarm,
    do_submit_flag,
    restore_pending_swarms_from_results,
)
from backend.agents.coordinator_loop import _auto_spawn_unsolved
from backend.agents.swarm import ChallengeSwarm, FlagCandidateRecord
from backend.cost_tracker import CostTracker
from backend.ctfd import SubmitResult
from backend.deps import CoordinatorDeps
from backend.prompts import ChallengeMeta


class _FakeSwarm:
    def __init__(self, *, cancelled: bool = False) -> None:
        self.cancel_event = asyncio.Event()
        if cancelled:
            self.cancel_event.set()


class _FakeTask:
    def __init__(self, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


class _FakeCTFd:
    platform = "ctfd"
    label = "CTFd"

    def __init__(self, challenges: list[dict[str, object]], solved: set[str] | None = None) -> None:
        self._challenges = challenges
        self._solved = solved or set()

    async def fetch_all_challenges(self) -> list[dict[str, object]]:
        return list(self._challenges)

    async def fetch_challenge_stubs(self) -> list[dict[str, object]]:
        return [
            {
                "name": challenge.get("name"),
                "solves": challenge.get("solves", 0),
            }
            for challenge in self._challenges
        ]

    async def fetch_solved_names(self) -> set[str]:
        return set(self._solved)


class _FailingCTFd(_FakeCTFd):
    async def fetch_all_challenges(self) -> list[dict[str, object]]:
        raise RuntimeError("All connection attempts failed")


class _FailingSolvedCTFd(_FakeCTFd):
    async def fetch_solved_names(self) -> set[str]:
        raise RuntimeError("CTFd GET timed out: /api/v1/users/me")


class _AutoPull404CTFd(_FakeCTFd):
    async def fetch_all_challenges(self) -> list[dict[str, object]]:
        request = httpx.Request("GET", "https://ctfd.example/api/v1/challenges/45")
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("404 NOT FOUND", request=request, response=response)


class _ImmediateStopSolver:
    def __init__(self) -> None:
        self.sandbox = SimpleNamespace(workspace_dir="")
        self.process_stopped = 0

    async def stop_process(self) -> None:
        self.process_stopped += 1


class _AdvisoryCaptureSolver(_ImmediateStopSolver):
    def __init__(self) -> None:
        super().__init__()
        self.advisories: list[str] = []

    def bump_advisory(self, message: str) -> None:
        self.advisories.append(message)


class _CapturedSwarm:
    captured: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        type(self).captured = kwargs

    async def run(self) -> None:
        return None


@pytest.mark.asyncio
async def test_do_spawn_swarm_queues_when_capacity_is_full() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
        max_concurrent_challenges=1,
    )
    deps.swarms["challenge-a"] = _FakeSwarm()
    deps.swarm_tasks["challenge-a"] = cast(Any, _FakeTask(False))

    result = await do_spawn_swarm(deps, "challenge-b")

    assert "Queued swarm for challenge-b" in result
    assert list(deps.pending_swarm_queue) == ["challenge-b"]
    assert deps.pending_swarm_set == {"challenge-b"}


@pytest.mark.asyncio
async def test_fill_swarm_capacity_starts_queued_challenges_in_fifo_order(monkeypatch) -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
        max_concurrent_challenges=1,
    )
    deps.pending_swarm_queue.extend(["challenge-b", "challenge-c"])
    deps.pending_swarm_set.update({"challenge-b", "challenge-c"})

    spawned: list[str] = []

    async def fake_spawn(deps_obj: CoordinatorDeps, challenge_name: str) -> str:
        spawned.append(challenge_name)
        deps_obj.swarms[challenge_name] = _FakeSwarm()
        deps_obj.swarm_tasks[challenge_name] = cast(Any, _FakeTask(False))
        return f"spawned {challenge_name}"

    monkeypatch.setattr("backend.agents.coordinator_core._spawn_swarm_now", fake_spawn)

    first = await _fill_swarm_capacity(deps)

    assert first == ["challenge-b"]
    assert spawned == ["challenge-b"]
    assert list(deps.pending_swarm_queue) == ["challenge-c"]

    deps.swarm_tasks["challenge-b"] = cast(Any, _FakeTask(True))
    retired = _retire_finished_swarms(deps)
    second = await _fill_swarm_capacity(deps)

    assert retired == ["challenge-b"]
    assert second == ["challenge-c"]
    assert spawned == ["challenge-b", "challenge-c"]
    assert list(deps.pending_swarm_queue) == []


@pytest.mark.asyncio
async def test_spawn_swarm_now_skips_session_quota_exhausted_models(monkeypatch, tmp_path: Path) -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
        model_specs=["codex/gpt-5.4", "gemini/gemini-2.5-flash"],
    )
    deps.challenge_dirs["quota-test"] = str(tmp_path)
    deps.challenge_metas["quota-test"] = ChallengeMeta(name="quota-test")
    deps.quota_exhausted_model_specs.add("gemini/gemini-2.5-flash")

    monkeypatch.setattr("backend.agents.swarm.ChallengeSwarm", _CapturedSwarm)

    result = await _spawn_swarm_now(deps, "quota-test")

    assert result == "Swarm spawned for quota-test with 1 models"
    assert _CapturedSwarm.captured["model_specs"] == ["codex/gpt-5.4"]
    assert _CapturedSwarm.captured["disabled_model_specs"] is deps.quota_exhausted_model_specs


@pytest.mark.asyncio
async def test_fill_swarm_capacity_requeues_when_all_models_are_quota_blocked(tmp_path: Path) -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
        model_specs=["codex/gpt-5.4"],
    )
    deps.pending_swarm_queue.append("blocked-challenge")
    deps.pending_swarm_set.add("blocked-challenge")
    deps.pending_swarm_meta["blocked-challenge"] = {
        "priority": False,
        "reason": PENDING_REASON_QUEUED,
        "enqueued_at": 1.0,
    }
    deps.challenge_dirs["blocked-challenge"] = str(tmp_path)
    deps.challenge_metas["blocked-challenge"] = ChallengeMeta(name="blocked-challenge")
    deps.quota_exhausted_model_specs.add("codex/gpt-5.4")

    spawned = await _fill_swarm_capacity(deps)

    assert spawned == []
    assert list(deps.pending_swarm_queue) == ["blocked-challenge"]
    assert deps.pending_swarm_meta["blocked-challenge"]["reason"] == PENDING_REASON_QUOTA_BLOCKED


def test_swarm_records_quota_exhausted_models_in_shared_session_set(tmp_path: Path) -> None:
    disabled_models: set[str] = set()
    swarm = ChallengeSwarm(
        challenge_dir=str(tmp_path),
        meta=ChallengeMeta(name="quota-test"),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
        disabled_model_specs=disabled_models,
    )

    swarm._note_quota_exhausted_model("codex/gpt-5.4")

    assert disabled_models == {"codex/gpt-5.4"}


@pytest.mark.asyncio
async def test_fill_swarm_capacity_prefers_local_preloaded_challenges_during_backoff(monkeypatch) -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
        max_concurrent_challenges=1,
    )
    deps.pending_swarm_queue.extend(["remote-first", "local-second"])
    deps.pending_swarm_set.update({"remote-first", "local-second"})
    deps.challenge_dirs["local-second"] = "/tmp/local-second"
    deps.challenge_metas["local-second"] = SimpleNamespace(solves=17)
    deps.ctfd_refresh_backoff_until = 10**12

    spawned: list[str] = []

    async def fake_spawn(deps_obj: CoordinatorDeps, challenge_name: str) -> str:
        spawned.append(challenge_name)
        deps_obj.swarms[challenge_name] = _FakeSwarm()
        deps_obj.swarm_tasks[challenge_name] = cast(Any, _FakeTask(False))
        return f"spawned {challenge_name}"

    monkeypatch.setattr("backend.agents.coordinator_core._spawn_swarm_now", fake_spawn)

    first = await _fill_swarm_capacity(deps)

    assert first == ["local-second"]
    assert spawned == ["local-second"]
    assert list(deps.pending_swarm_queue) == ["remote-first"]


@pytest.mark.asyncio
async def test_fill_swarm_capacity_skips_priority_waiting_entries(monkeypatch) -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
        max_concurrent_challenges=1,
    )
    deps.pending_swarm_queue.extend(["priority-held", "normal-next"])
    deps.pending_swarm_set.update({"priority-held", "normal-next"})
    deps.pending_swarm_meta["priority-held"] = {
        "priority": True,
        "reason": PENDING_REASON_PRIORITY_WAITING,
        "enqueued_at": 1.0,
    }
    deps.pending_swarm_meta["normal-next"] = {
        "priority": False,
        "reason": PENDING_REASON_QUEUED,
        "enqueued_at": 2.0,
    }

    spawned: list[str] = []

    async def fake_spawn(deps_obj: CoordinatorDeps, challenge_name: str) -> str:
        spawned.append(challenge_name)
        deps_obj.swarms[challenge_name] = _FakeSwarm()
        deps_obj.swarm_tasks[challenge_name] = cast(Any, _FakeTask(False))
        return f"spawned {challenge_name}"

    monkeypatch.setattr("backend.agents.coordinator_core._spawn_swarm_now", fake_spawn)

    started = await _fill_swarm_capacity(deps)

    assert started == ["normal-next"]
    assert spawned == ["normal-next"]
    assert list(deps.pending_swarm_queue) == ["priority-held"]
    assert deps.pending_swarm_meta["priority-held"]["reason"] == PENDING_REASON_PRIORITY_WAITING


@pytest.mark.asyncio
async def test_restore_normal_waiting_makes_priority_held_challenge_spawnable(monkeypatch) -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
        max_concurrent_challenges=1,
    )
    deps.pending_swarm_queue.append("priority-held")
    deps.pending_swarm_set.add("priority-held")
    deps.pending_swarm_meta["priority-held"] = {
        "priority": True,
        "reason": PENDING_REASON_PRIORITY_WAITING,
        "enqueued_at": 1.0,
    }
    deps.results["priority-held"] = {"status": "pending"}

    spawned: list[str] = []

    async def fake_spawn(deps_obj: CoordinatorDeps, challenge_name: str) -> str:
        spawned.append(challenge_name)
        deps_obj.swarms[challenge_name] = _FakeSwarm()
        deps_obj.swarm_tasks[challenge_name] = cast(Any, _FakeTask(False))
        return f"spawned {challenge_name}"

    monkeypatch.setattr("backend.agents.coordinator_core._spawn_swarm_now", fake_spawn)

    result = await do_set_challenge_priority_waiting(
        deps,
        "priority-held",
        priority=False,
    )

    assert result == 'Challenge "priority-held" restored to standard waiting.'
    assert spawned == ["priority-held"]
    assert "priority-held" in deps.swarms
    assert "priority-held" not in deps.pending_swarm_set


@pytest.mark.asyncio
async def test_restart_challenge_promotes_pending_item_and_spawns_it(monkeypatch) -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
        max_concurrent_challenges=1,
    )
    deps.pending_swarm_queue.append("restart-me")
    deps.pending_swarm_set.add("restart-me")
    deps.pending_swarm_meta["restart-me"] = {
        "priority": True,
        "reason": PENDING_REASON_PRIORITY_WAITING,
        "enqueued_at": 1.0,
    }
    deps.results["restart-me"] = {"status": "pending"}

    spawned: list[str] = []

    async def fake_spawn(deps_obj: CoordinatorDeps, challenge_name: str) -> str:
        spawned.append(challenge_name)
        deps_obj.swarms[challenge_name] = _FakeSwarm()
        deps_obj.swarm_tasks[challenge_name] = cast(Any, _FakeTask(False))
        return f"spawned {challenge_name}"

    monkeypatch.setattr("backend.agents.coordinator_core._spawn_swarm_now", fake_spawn)

    result = await do_restart_challenge(deps, "restart-me")

    assert result == 'Restarted "restart-me" from saved notes.'
    assert spawned == ["restart-me"]
    assert "restart-me" in deps.swarms
    assert "restart-me" not in deps.pending_swarm_set


@pytest.mark.asyncio
async def test_restart_challenge_requeues_active_swarm_with_restart_reason() -> None:
    class _RestartableSwarm(_FakeSwarm):
        def __init__(self) -> None:
            super().__init__()
            self.requeue_requested: tuple[bool, str] | None = None
            self.killed: list[str] = []

        def request_requeue(self, *, priority: bool = False, reason: str = "queued") -> None:
            self.requeue_requested = (priority, reason)

        def kill(self, reason: str = "swarm cancelled") -> None:
            self.killed.append(reason)

    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
        max_concurrent_challenges=1,
    )
    swarm = _RestartableSwarm()
    deps.swarms["restart-live"] = swarm

    result = await do_restart_challenge(deps, "restart-live")

    assert result == 'Restarting "restart-live" after the current run stops.'
    assert swarm.requeue_requested == (True, PENDING_REASON_RESTART_REQUESTED)
    assert swarm.killed == ["operator restarting restart-live"]


@pytest.mark.asyncio
async def test_retire_finished_swarms_keeps_cancelled_but_running_swarm() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
        max_concurrent_challenges=1,
    )
    deps.swarms["challenge-a"] = _FakeSwarm(cancelled=True)
    deps.swarm_tasks["challenge-a"] = cast(Any, _FakeTask(False))

    retired = _retire_finished_swarms(deps)

    assert retired == []
    assert "challenge-a" in deps.swarms
    assert "challenge-a" in deps.swarm_tasks


def test_restore_pending_swarms_from_results_rehydrates_resume_queue() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
    )
    deps.results["resume-me"] = {
        "status": "pending",
        "shared_artifacts_path": "/challenge/shared-artifacts",
    }
    deps.results["hold-me"] = {
        "status": "pending",
        "requeue_requested": True,
        "requeue_priority": True,
        "requeue_reason": PENDING_REASON_PRIORITY_WAITING,
    }
    deps.results["candidate-review"] = {
        "status": "candidate_pending",
        "paused_candidate_flag": "flag{candidate}",
    }
    deps.results["solved"] = {
        "status": "flag_found",
        "flag": "flag{done}",
    }

    restored = restore_pending_swarms_from_results(deps)

    assert restored == ["hold-me", "resume-me"]
    assert list(deps.pending_swarm_queue) == ["hold-me", "resume-me"]
    assert deps.pending_swarm_meta["hold-me"]["reason"] == PENDING_REASON_PRIORITY_WAITING
    assert deps.pending_swarm_meta["hold-me"]["priority"] is True
    assert deps.pending_swarm_meta["resume-me"]["reason"] == PENDING_REASON_RESTART_REQUESTED
    assert deps.pending_swarm_meta["resume-me"]["priority"] is False
    assert "candidate-review" not in deps.pending_swarm_set
    assert "solved" not in deps.pending_swarm_set


@pytest.mark.asyncio
async def test_do_fetch_challenges_sorts_by_solves_descending() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(
            Any,
            _FakeCTFd(
            [
                {"name": "charlie", "category": "misc", "value": 100, "solves": 3, "description": ""},
                {"name": "alpha", "category": "misc", "value": 100, "solves": 11, "description": ""},
                {"name": "bravo", "category": "misc", "value": 100, "solves": 11, "description": ""},
            ],
            solved={"bravo"},
        )),
        cost_tracker=CostTracker(),
        settings=object(),
    )

    payload = json.loads(await do_fetch_challenges(deps))

    assert [item["name"] for item in payload] == ["alpha", "bravo", "charlie"]
    assert payload[1]["status"] == "SOLVED"


@pytest.mark.asyncio
async def test_do_fetch_challenges_includes_local_only_entries() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(
            Any,
            _FakeCTFd(
            [
                {"name": "ctfd-only", "category": "misc", "value": 100, "solves": 5, "description": ""},
            ],
            solved=set(),
        )),
        cost_tracker=CostTracker(),
        settings=object(),
    )
    deps.challenge_metas["local-only"] = SimpleNamespace(
        category="pwn",
        value=300,
        solves=17,
        description="local preload",
    )

    payload = json.loads(await do_fetch_challenges(deps))

    assert [item["name"] for item in payload] == ["local-only", "ctfd-only"]
    assert payload[0]["source"] == "local"
    assert payload[1]["source"] == "ctfd"


@pytest.mark.asyncio
async def test_do_fetch_challenges_falls_back_to_local_when_ctfd_is_unreachable() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, _FailingCTFd([])),
        cost_tracker=CostTracker(),
        settings=object(),
    )
    deps.challenge_metas["aeBPF"] = SimpleNamespace(
        category="pwn",
        value=0,
        solves=0,
        description="local preload only",
    )

    payload = json.loads(await do_fetch_challenges(deps))

    assert payload == [
        {
            "name": "aeBPF",
            "category": "pwn",
            "value": 0,
            "solves": 0,
            "status": "unsolved",
            "description": "local preload only",
            "source": "local",
        }
    ]


@pytest.mark.asyncio
async def test_local_mode_fetch_challenges_never_calls_ctfd() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, _FailingCTFd([])),
        cost_tracker=CostTracker(),
        settings=object(),
        local_mode=True,
        no_submit=True,
    )
    deps.challenge_metas["aeBPF"] = SimpleNamespace(
        category="pwn",
        value=300,
        solves=17,
        description="local-only challenge",
    )

    payload = json.loads(await do_fetch_challenges(deps))

    assert payload == [
        {
            "name": "aeBPF",
            "category": "pwn",
            "value": 300,
            "solves": 17,
            "status": "unsolved",
            "description": "local-only challenge",
            "source": "local",
        }
    ]


@pytest.mark.asyncio
async def test_local_mode_get_solve_status_never_calls_ctfd() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, _FailingCTFd([])),
        cost_tracker=CostTracker(),
        settings=object(),
        local_mode=True,
        no_submit=True,
    )
    deps.results["aeBPF"] = {"status": "flag_found", "flag": "flag{local}"}

    payload = json.loads(await do_get_solve_status(deps))

    assert payload["solved"] == ["aeBPF"]
    assert payload["active_swarms"] == {}
    assert payload["queued_swarms"] == []


@pytest.mark.asyncio
async def test_get_solve_status_tolerates_ctfd_solved_timeout() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, _FailingSolvedCTFd([])),
        cost_tracker=CostTracker(),
        settings=object(),
        local_mode=False,
        no_submit=False,
    )
    deps.results["aeBPF"] = {"status": "flag_found", "flag": "flag{local}"}

    payload = json.loads(await do_get_solve_status(deps))

    assert payload["solved"] == ["aeBPF"]
    assert payload["active_swarms"] == {}
    assert payload["queued_swarms"] == []


@pytest.mark.asyncio
async def test_approve_stored_candidate_drops_pending_queue() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
        local_mode=False,
        no_submit=False,
    )
    deps.pending_swarm_queue.append("queued-challenge")
    deps.pending_swarm_set.add("queued-challenge")
    deps.results["queued-challenge"] = {
        "status": "candidate_pending",
        "flag_candidates": {
            "flag{queued}": {
                "status": "pending",
                "flag": "flag{queued}",
            }
        },
    }

    from backend.agents.coordinator_core import do_approve_flag_candidate

    result = await do_approve_flag_candidate(deps, "queued-challenge", "flag{queued}")

    assert result.startswith('USER CONFIRMED MANUALLY — "flag{queued}"')
    assert list(deps.pending_swarm_queue) == []
    assert deps.pending_swarm_set == set()
    assert deps.results["queued-challenge"]["status"] == "flag_found"


@pytest.mark.asyncio
async def test_reject_stored_candidate_requeues_challenge() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
        local_mode=False,
        no_submit=False,
    )
    deps.results["queued-challenge"] = {
        "status": "candidate_pending",
        "flag_candidates": {
            "flag{queued}": {
                "status": "pending",
                "flag": "flag{queued}",
            }
        },
    }

    result = await do_reject_flag_candidate(deps, "queued-challenge", "flag{queued}")

    assert result.startswith('USER REJECTED — "flag{queued}"')
    assert list(deps.pending_swarm_queue) == ["queued-challenge"]
    assert deps.pending_swarm_meta["queued-challenge"]["reason"] == "candidate_retry"


@pytest.mark.asyncio
async def test_do_spawn_swarm_returns_nonfatal_error_when_ctfd_refresh_fails() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, _AutoPull404CTFd([])),
        cost_tracker=CostTracker(),
        settings=object(),
    )

    result = await do_spawn_swarm(deps, "ghost-challenge")

    assert result.startswith("Queued swarm for ghost-challenge awaiting remote refresh retry")
    assert deps.swarms == {}
    assert list(deps.pending_swarm_queue) == ["ghost-challenge"]
    assert deps.pending_swarm_set == {"ghost-challenge"}


@pytest.mark.asyncio
async def test_fill_swarm_capacity_requeues_retryable_refresh_failures() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, _FailingCTFd([])),
        cost_tracker=CostTracker(),
        settings=object(),
        max_concurrent_challenges=1,
    )
    deps.pending_swarm_queue.append("ghost-challenge")
    deps.pending_swarm_set.add("ghost-challenge")

    spawned = await _fill_swarm_capacity(deps)

    assert spawned == []
    assert list(deps.pending_swarm_queue) == ["ghost-challenge"]
    assert deps.pending_swarm_set == {"ghost-challenge"}
    assert deps.ctfd_refresh_backoff_until > 0


@pytest.mark.asyncio
async def test_auto_spawn_unsolved_prefers_most_solved_challenges(monkeypatch) -> None:
    deps = CoordinatorDeps(
        ctfd=cast(
            Any,
            _FakeCTFd(
            [
                {"name": "easy-but-popular", "solves": 120},
                {"name": "mid", "solves": 45},
                {"name": "hard", "solves": 3},
            ]
        )),
        cost_tracker=CostTracker(),
        settings=object(),
    )

    class _FakePoller:
        known_challenges = {"mid", "hard", "easy-but-popular"}
        known_solved = set()

    spawned: list[str] = []

    async def fake_auto_spawn_one(deps_obj: CoordinatorDeps, challenge_name: str) -> None:
        spawned.append(challenge_name)

    monkeypatch.setattr("backend.agents.coordinator_loop._auto_spawn_one", fake_auto_spawn_one)

    await _auto_spawn_unsolved(deps, _FakePoller())

    assert spawned == ["easy-but-popular", "mid", "hard"]


@pytest.mark.asyncio
async def test_swarm_report_flag_candidate_queues_operator_review_when_submission_disabled(tmp_path) -> None:
    inbox: asyncio.Queue[object] = asyncio.Queue()
    swarm = ChallengeSwarm(
        challenge_dir=str(tmp_path),
        meta=ChallengeMeta(name="candidate-chal"),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
        no_submit=True,
        coordinator_inbox=inbox,
    )

    message = await swarm.report_flag_candidate(
        "flag{candidate}",
        "codex/gpt-5.4",
        evidence="matched hidden admin route",
        confidence="high",
        step_count=12,
        trace_path="/tmp/trace.jsonl",
    )

    assert "Queued flag candidate" in message
    assert "operator review" in message

    candidate = swarm.flag_candidates["flag{candidate}"]
    assert candidate.status == "pending"
    assert candidate.advisor_decision == ""
    assert candidate.evidence_digest_paths["codex/gpt-5.4"].startswith("/challenge/shared-artifacts/.advisor/")
    assert candidate.evidence_pointer_paths["codex/gpt-5.4"].startswith("/challenge/shared-artifacts/")
    assert inbox.empty()
    assert not swarm.cancel_event.is_set()
    assert swarm.paused_candidate_flag == ""


@pytest.mark.asyncio
async def test_swarm_report_flag_candidate_auto_submits_to_ctfd(tmp_path) -> None:
    class _FakeSubmitCTFd:
        async def submit_flag(self, challenge_name: str, flag: str) -> SubmitResult:
            assert challenge_name == "candidate-chal"
            assert flag == "flag{candidate}"
            return SubmitResult("correct", "accepted", 'CORRECT — "flag{candidate}" accepted.')

    swarm = ChallengeSwarm(
        challenge_dir=str(tmp_path),
        meta=ChallengeMeta(name="candidate-chal"),
        ctfd=cast(Any, _FakeSubmitCTFd()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
    )
    solver = _ImmediateStopSolver()
    swarm.solvers["codex/gpt-5.4"] = cast(Any, solver)

    message = await swarm.report_flag_candidate(
        "flag{candidate}",
        "codex/gpt-5.4",
        evidence="matched hidden admin route",
        confidence="high",
        step_count=12,
        trace_path="/tmp/trace.jsonl",
    )

    candidate = swarm.flag_candidates["flag{candidate}"]
    assert message.startswith('CORRECT — "flag{candidate}" accepted.')
    assert candidate.status == "confirmed"
    assert candidate.confirmation_source == "ctfd"
    assert swarm.confirmed_flag == "flag{candidate}"
    assert solver.process_stopped == 1


@pytest.mark.asyncio
async def test_swarm_report_flag_candidate_ctfd_incorrect_stays_reviewable(tmp_path) -> None:
    class _FakeSubmitCTFd:
        async def submit_flag(self, challenge_name: str, flag: str) -> SubmitResult:
            assert challenge_name == "candidate-chal"
            assert flag == "flag{candidate}"
            return SubmitResult("incorrect", "rejected", 'INCORRECT — "flag{candidate}" rejected.')

    swarm = ChallengeSwarm(
        challenge_dir=str(tmp_path),
        meta=ChallengeMeta(name="candidate-chal"),
        ctfd=cast(Any, _FakeSubmitCTFd()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
    )

    message = await swarm.report_flag_candidate(
        "flag{candidate}",
        "codex/gpt-5.4",
        evidence="matched hidden admin route",
        confidence="high",
        step_count=12,
        trace_path="/tmp/trace.jsonl",
    )

    candidate = swarm.flag_candidates["flag{candidate}"]
    assert candidate.status == "incorrect"
    assert candidate.submit_display == 'INCORRECT — "flag{candidate}" rejected.'
    assert "Operator review can still confirm it manually" in message
    assert swarm.requeue_requested is True
    assert swarm.requeue_reason == "candidate_retry"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flag", "reason_fragment"),
    [
        ("PENDING", "placeholder sentinel"),
        ("NOT_FOUND", "placeholder sentinel"),
        ("NOT_SOLVE", "placeholder sentinel"),
        ("NOT_SOLVED", "placeholder sentinel"),
        ("NOT_SOLVED_YET", "placeholder sentinel"),
        ("NOT_SOLVED_REMOTE_REFUSED", "placeholder sentinel"),
        ("ALREADY SOLVED", "placeholder sentinel"),
        ("INCORRECT", "placeholder sentinel"),
        ("NO_FLAG_YET", "placeholder sentinel"),
        ("ADVISORY_RESULT: falsified_boot_setup_confirmed_attack_surface_unpriv_bpf+JIT", "placeholder sentinel"),
        ("flag{NOT_FOUND}", "placeholder sentinel"),
        ("flag{NOT_SOLVED_YET}", "placeholder sentinel"),
        ("flag{NOT_SOLVED_REMOTE_REFUSED}", "placeholder sentinel"),
        ("flag{NO_FLAG_YET}", "placeholder sentinel"),
        ("BLOCKED_NO_FLAG", "placeholder sentinel"),
        ("flag{BLOCKED_NO_FLAG}", "placeholder sentinel"),
        ("fakeflag{this_is_not_the_real_flag}", "placeholder sentinel"),
        ("flag{flag_for_testing}", "placeholder sentinel"),
        ("ping{fake_flag}", "placeholder sentinel"),
        ("DH{fake_flag}", "placeholder sentinel"),
        ("ping{Extremely_fake_flag}", "placeholder sentinel"),
        ("ping{FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE}", "placeholder sentinel"),
        ("ping{.*}", "invalid flag body"),
    ],
)
async def test_swarm_report_flag_candidate_filters_placeholder_sentinels(
    tmp_path: Path,
    flag: str,
    reason_fragment: str,
) -> None:
    swarm = ChallengeSwarm(
        challenge_dir=str(tmp_path),
        meta=ChallengeMeta(name="candidate-chal"),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
        no_submit=True,
    )

    message = await swarm.report_flag_candidate(flag, "codex/gpt-5.4")

    assert message == f"Flag candidate rejected: {reason_fragment}."
    assert swarm.flag_candidates == {}


@pytest.mark.asyncio
async def test_swarm_report_flag_candidate_rejects_challenge_flag_prefix_mismatch(tmp_path: Path) -> None:
    swarm = ChallengeSwarm(
        challenge_dir=str(tmp_path),
        meta=ChallengeMeta(name="candidate-chal", description="Flag format: DH{...}"),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
        no_submit=True,
    )

    message = await swarm.report_flag_candidate("flag{candidate}", "codex/gpt-5.4")

    assert message == 'Flag candidate rejected: format mismatch: expected prefix "DH{".'
    assert swarm.flag_candidates == {}


@pytest.mark.asyncio
async def test_swarm_report_flag_candidate_rejects_challenge_flag_regex_mismatch(tmp_path: Path) -> None:
    swarm = ChallengeSwarm(
        challenge_dir=str(tmp_path),
        meta=ChallengeMeta(name="candidate-chal", flag_regex=r"^DH\{[0-9]{4}\}$"),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
        no_submit=True,
    )

    message = await swarm.report_flag_candidate("DH{abcd}", "codex/gpt-5.4")

    assert message == 'Flag candidate rejected: format mismatch: does not match challenge regex "^DH\\{[0-9]{4}\\}$".'
    assert swarm.flag_candidates == {}


@pytest.mark.asyncio
async def test_swarm_report_flag_candidate_accepts_challenge_flag_prefix_match(tmp_path: Path) -> None:
    swarm = ChallengeSwarm(
        challenge_dir=str(tmp_path),
        meta=ChallengeMeta(name="candidate-chal", description="Flag format: DH{...}"),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
        no_submit=True,
    )

    message = await swarm.report_flag_candidate("DH{candidate}", "codex/gpt-5.4")

    assert 'Queued flag candidate "DH{candidate}"' in message
    assert "DH{candidate}" in swarm.flag_candidates


@pytest.mark.asyncio
async def test_swarm_report_flag_candidate_blocks_previously_rejected_candidate(tmp_path: Path) -> None:
    swarm = ChallengeSwarm(
        challenge_dir=str(tmp_path),
        meta=ChallengeMeta(name="candidate-chal"),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4", "claude/sonnet"],
        no_submit=True,
    )
    swarm.flag_candidates["flag{candidate}"] = FlagCandidateRecord(
        normalized_flag="flag{candidate}",
        raw_flag="flag{candidate}",
        source_models={"codex/gpt-5.4"},
        status="rejected",
    )

    message = await swarm.report_flag_candidate("flag{candidate}", "claude/sonnet")

    assert "previously rejected for this challenge" in message
    assert "Do not re-submit the same exact flag" in message


@pytest.mark.asyncio
async def test_try_submit_flag_blocks_previously_incorrect_candidate(tmp_path: Path) -> None:
    class _NeverCalledCTFd:
        async def submit_flag(self, challenge_name: str, flag: str) -> SubmitResult:
            raise AssertionError("submit_flag should not be called for blocked candidates")

    swarm = ChallengeSwarm(
        challenge_dir=str(tmp_path),
        meta=ChallengeMeta(name="candidate-chal"),
        ctfd=cast(Any, _NeverCalledCTFd()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
    )
    swarm.flag_candidates["flag{candidate}"] = FlagCandidateRecord(
        normalized_flag="flag{candidate}",
        raw_flag="flag{candidate}",
        source_models={"codex/gpt-5.4"},
        status="incorrect",
    )

    display, is_confirmed = await swarm.try_submit_flag("flag{candidate}", "codex/gpt-5.4")

    assert is_confirmed is False
    assert "previously rejected by the remote platform for this challenge" in display


@pytest.mark.asyncio
async def test_reject_flag_candidate_broadcasts_dead_end_to_all_lanes(tmp_path: Path) -> None:
    swarm = ChallengeSwarm(
        challenge_dir=str(tmp_path),
        meta=ChallengeMeta(name="candidate-chal"),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4", "claude/sonnet"],
        no_submit=True,
    )
    codex_solver = _AdvisoryCaptureSolver()
    claude_solver = _AdvisoryCaptureSolver()
    swarm.solvers["codex/gpt-5.4"] = cast(Any, codex_solver)
    swarm.solvers["claude/sonnet"] = cast(Any, claude_solver)
    swarm.flag_candidates["flag{candidate}"] = FlagCandidateRecord(
        normalized_flag="flag{candidate}",
        raw_flag="flag{candidate}",
        source_models={"codex/gpt-5.4"},
        status="pending",
    )

    message = await swarm.reject_flag_candidate("flag{candidate}")

    assert "USER REJECTED" in message
    assert codex_solver.advisories
    assert claude_solver.advisories
    assert "do not re-submit the exact same flag" in codex_solver.advisories[-1].lower()
    assert "do not re-submit the exact same flag" in claude_solver.advisories[-1].lower()


@pytest.mark.asyncio
async def test_do_submit_flag_updates_candidate_state_and_results(tmp_path: Path) -> None:
    class _FakeSubmitCTFd:
        async def submit_flag(self, challenge_name: str, flag: str) -> SubmitResult:
            assert challenge_name == "candidate-chal"
            assert flag == "flag{candidate}"
            return SubmitResult("correct", "accepted", 'CORRECT — "flag{candidate}" accepted.')

    deps = CoordinatorDeps(
        ctfd=cast(Any, _FakeSubmitCTFd()),
        cost_tracker=CostTracker(),
        settings=object(),
    )
    swarm = ChallengeSwarm(
        challenge_dir=str(tmp_path),
        meta=ChallengeMeta(name="candidate-chal"),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        result_store=deps.results,
        model_specs=["codex/gpt-5.4"],
    )
    swarm.flag_candidates["flag{candidate}"] = FlagCandidateRecord(
        normalized_flag="flag{candidate}",
        raw_flag="flag{candidate}",
        source_models={"codex/gpt-5.4"},
    )
    deps.challenge_dirs["candidate-chal"] = str(tmp_path)
    deps.swarms["candidate-chal"] = swarm

    display = await do_submit_flag(deps, "candidate-chal", "flag{candidate}")

    assert display.startswith('CORRECT — "flag{candidate}" accepted.')
    assert deps.results["candidate-chal"]["status"] == "flag_found"
    assert swarm.flag_candidates["flag{candidate}"].status == "confirmed"
    result_payload = json.loads((tmp_path / "solve" / "result.json").read_text(encoding="utf-8"))
    assert result_payload["flag"] == "flag{candidate}"
    assert result_payload["flag_candidates"]["flag{candidate}"]["status"] == "confirmed"
    assert (tmp_path / "solve" / "flag.txt").read_text(encoding="utf-8").strip() == "flag{candidate}"


@pytest.mark.asyncio
async def test_do_submit_flag_blocks_stored_incorrect_candidate(tmp_path: Path) -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
    )
    deps.results["candidate-chal"] = {
        "status": "candidate_pending",
        "flag_candidates": {
            "flag{candidate}": {
                "status": "incorrect",
            }
        },
    }

    display = await do_submit_flag(deps, "candidate-chal", "flag{candidate}")

    assert "SUBMIT BLOCKED" in display
    assert "previously rejected by the remote platform for this challenge" in display


@pytest.mark.asyncio
async def test_auto_spawn_unsolved_includes_local_preloaded_challenges(monkeypatch) -> None:
    deps = CoordinatorDeps(
        ctfd=cast(
            Any,
            _FakeCTFd(
            [
                {"name": "ctfd-visible", "solves": 12},
                {"name": "another-ctfd", "solves": 2},
            ]
        )),
        cost_tracker=CostTracker(),
        settings=object(),
    )
    deps.challenge_dirs["local-only"] = "/tmp/local-only"
    deps.challenge_metas["local-only"] = type("Meta", (), {"solves": 7})()
    deps.challenge_dirs["restored-solved"] = "/tmp/restored-solved"
    deps.challenge_metas["restored-solved"] = type("Meta", (), {"solves": 99})()
    deps.results["restored-solved"] = {"status": "flag_found", "flag": "flag{done}"}

    class _FakePoller:
        known_challenges = {"ctfd-visible", "another-ctfd"}
        known_solved = set()

    spawned: list[str] = []

    async def fake_auto_spawn_one(deps_obj: CoordinatorDeps, challenge_name: str) -> None:
        spawned.append(challenge_name)

    monkeypatch.setattr("backend.agents.coordinator_loop._auto_spawn_one", fake_auto_spawn_one)

    await _auto_spawn_unsolved(deps, _FakePoller())

    assert spawned == ["ctfd-visible", "another-ctfd", "local-only"]
