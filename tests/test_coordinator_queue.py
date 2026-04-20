from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from backend.agents.coordinator_core import (
    _fill_swarm_capacity,
    _retire_finished_swarms,
    do_fetch_challenges,
    do_get_solve_status,
    do_spawn_swarm,
    do_submit_flag,
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
async def test_do_spawn_swarm_returns_nonfatal_error_when_ctfd_refresh_fails() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, _AutoPull404CTFd([])),
        cost_tracker=CostTracker(),
        settings=object(),
    )

    result = await do_spawn_swarm(deps, "ghost-challenge")

    assert result.startswith("Could not refresh challenge 'ghost-challenge' from CTFd:")
    assert deps.swarms == {}


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
