from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.agents.coordinator_core import (
    _fill_swarm_capacity,
    _retire_finished_swarms,
    do_fetch_challenges,
    do_spawn_swarm,
)
from backend.agents.coordinator_loop import _auto_spawn_unsolved
from backend.cost_tracker import CostTracker
from backend.deps import CoordinatorDeps


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


@pytest.mark.asyncio
async def test_do_spawn_swarm_queues_when_capacity_is_full() -> None:
    deps = CoordinatorDeps(
        ctfd=object(),  # type: ignore[arg-type]
        cost_tracker=CostTracker(),
        settings=object(),
        max_concurrent_challenges=1,
    )
    deps.swarms["challenge-a"] = _FakeSwarm()
    deps.swarm_tasks["challenge-a"] = _FakeTask(False)  # type: ignore[assignment]

    result = await do_spawn_swarm(deps, "challenge-b")

    assert "Queued swarm for challenge-b" in result
    assert list(deps.pending_swarm_queue) == ["challenge-b"]
    assert deps.pending_swarm_set == {"challenge-b"}


@pytest.mark.asyncio
async def test_fill_swarm_capacity_starts_queued_challenges_in_fifo_order(monkeypatch) -> None:
    deps = CoordinatorDeps(
        ctfd=object(),  # type: ignore[arg-type]
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
        deps_obj.swarm_tasks[challenge_name] = _FakeTask(False)  # type: ignore[assignment]
        return f"spawned {challenge_name}"

    monkeypatch.setattr("backend.agents.coordinator_core._spawn_swarm_now", fake_spawn)

    first = await _fill_swarm_capacity(deps)

    assert first == ["challenge-b"]
    assert spawned == ["challenge-b"]
    assert list(deps.pending_swarm_queue) == ["challenge-c"]

    deps.swarm_tasks["challenge-b"] = _FakeTask(True)  # type: ignore[assignment]
    retired = _retire_finished_swarms(deps)
    second = await _fill_swarm_capacity(deps)

    assert retired == ["challenge-b"]
    assert second == ["challenge-c"]
    assert spawned == ["challenge-b", "challenge-c"]
    assert list(deps.pending_swarm_queue) == []


@pytest.mark.asyncio
async def test_do_fetch_challenges_sorts_by_solves_descending() -> None:
    deps = CoordinatorDeps(
        ctfd=_FakeCTFd(
            [
                {"name": "charlie", "category": "misc", "value": 100, "solves": 3, "description": ""},
                {"name": "alpha", "category": "misc", "value": 100, "solves": 11, "description": ""},
                {"name": "bravo", "category": "misc", "value": 100, "solves": 11, "description": ""},
            ],
            solved={"bravo"},
        ),
        cost_tracker=CostTracker(),
        settings=object(),
    )

    payload = json.loads(await do_fetch_challenges(deps))

    assert [item["name"] for item in payload] == ["alpha", "bravo", "charlie"]
    assert payload[1]["status"] == "SOLVED"


@pytest.mark.asyncio
async def test_do_fetch_challenges_includes_local_only_entries() -> None:
    deps = CoordinatorDeps(
        ctfd=_FakeCTFd(
            [
                {"name": "ctfd-only", "category": "misc", "value": 100, "solves": 5, "description": ""},
            ],
            solved=set(),
        ),
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
        ctfd=_FailingCTFd([]),
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
async def test_auto_spawn_unsolved_prefers_most_solved_challenges(monkeypatch) -> None:
    deps = CoordinatorDeps(
        ctfd=_FakeCTFd(
            [
                {"name": "easy-but-popular", "solves": 120},
                {"name": "mid", "solves": 45},
                {"name": "hard", "solves": 3},
            ]
        ),
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
async def test_auto_spawn_unsolved_includes_local_preloaded_challenges(monkeypatch) -> None:
    deps = CoordinatorDeps(
        ctfd=_FakeCTFd(
            [
                {"name": "ctfd-visible", "solves": 12},
                {"name": "another-ctfd", "solves": 2},
            ]
        ),
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
