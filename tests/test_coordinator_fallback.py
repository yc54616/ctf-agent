from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from backend.agents.claude_coordinator import (
    COORDINATOR_PREFLIGHT_PROMPT,
    COORDINATOR_PROMPT,
    ClaudeCoordinatorInactiveError,
    _next_inactive_turn_count,
    _validate_turn_activity,
)
from backend.agents.codex_coordinator import (
    COORDINATOR_PROMPT as CODEX_COORDINATOR_PROMPT,
)
from backend.agents.codex_coordinator import CodexCoordinator, run_codex_coordinator
from backend.agents.coordinator_loop import (
    _render_solver_message,
    cleanup_coordinator_runtime,
    run_event_loop,
)
from backend.cli import _run_coordinator
from backend.cost_tracker import CostTracker
from backend.deps import CoordinatorDeps
from backend.message_bus import CandidateRef


@pytest.mark.asyncio
async def test_run_coordinator_uses_codex_only(monkeypatch) -> None:
    async def fake_cleanup() -> None:
        return None

    def fake_configure(_max_containers: int) -> None:
        return None

    async def run_codex(**kwargs):  # type: ignore[no-untyped-def]
        return {"results": {"chal": {"flag": "flag{ok}"}}, "total_cost_usd": 0.0}

    printed: list[str] = []

    monkeypatch.setattr("backend.sandbox.cleanup_orphan_containers", fake_cleanup)
    monkeypatch.setattr("backend.sandbox.configure_semaphore", fake_configure)
    monkeypatch.setattr(
        "backend.cli.build_deps",
        lambda settings, model_specs, challenges_root, no_submit, local_mode: (
            object(),
            object(),
            type("Deps", (), {"msg_port": 0, "results": {}, "swarms": {}, "swarm_tasks": {}})(),
        ),
    )
    monkeypatch.setattr(
        "backend.cli.cleanup_coordinator_runtime",
        lambda deps, ctfd, cost_tracker, **kwargs: fake_cleanup(),
    )
    monkeypatch.setattr("backend.cli.run_codex_coordinator", run_codex)
    monkeypatch.setattr("backend.cli.console.print", lambda *args, **kwargs: printed.append(" ".join(map(str, args))))

    await _run_coordinator(
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
        challenges_dir="challenges",
        no_submit=True,
        local_mode=False,
        coordinator_model=None,
        coordinator_backend="claude",
        max_challenges=2,
        resume_mode=False,
        msg_port=9400,
    )

    assert any("Starting coordinator (codex" in line for line in printed)
    assert any("flag{ok}" in line for line in printed)


@pytest.mark.asyncio
async def test_run_coordinator_ignores_non_codex_backend_argument(monkeypatch) -> None:
    async def fake_cleanup() -> None:
        return None

    def fake_configure(_max_containers: int) -> None:
        return None

    seen: dict[str, object] = {}
    async def run_codex(**kwargs):  # type: ignore[no-untyped-def]
        seen["backend"] = "codex"
        return {"results": {}, "total_cost_usd": 0.0}

    monkeypatch.setattr("backend.sandbox.cleanup_orphan_containers", fake_cleanup)
    monkeypatch.setattr("backend.sandbox.configure_semaphore", fake_configure)
    monkeypatch.setattr(
        "backend.cli.build_deps",
        lambda settings, model_specs, challenges_root, no_submit, local_mode: (
            object(),
            object(),
            type("Deps", (), {"msg_port": 0, "results": {}, "swarms": {}, "swarm_tasks": {}})(),
        ),
    )
    monkeypatch.setattr(
        "backend.cli.cleanup_coordinator_runtime",
        lambda deps, ctfd, cost_tracker, **kwargs: fake_cleanup(),
    )
    monkeypatch.setattr("backend.cli.run_codex_coordinator", run_codex)

    await _run_coordinator(
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
        challenges_dir="challenges",
        no_submit=True,
        local_mode=False,
        coordinator_model=None,
        coordinator_backend="claude",
        max_challenges=2,
        resume_mode=False,
        msg_port=9400,
    )

    assert seen["backend"] == "codex"


@pytest.mark.asyncio
async def test_run_coordinator_reuses_active_runtime(monkeypatch) -> None:
    async def fake_cleanup() -> None:
        return None

    def fake_configure(_max_containers: int) -> None:
        return None

    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
    )
    shared_ctfd = cast(Any, object())
    shared_cost_tracker = CostTracker()
    shared_state: dict[str, object] = {}

    def fake_build_deps(settings, model_specs, challenges_root, no_submit, local_mode):  # type: ignore[no-untyped-def]
        return shared_ctfd, shared_cost_tracker, deps

    async def run_codex(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["deps"] is deps
        assert kwargs["ctfd"] is shared_ctfd
        assert kwargs["cost_tracker"] is shared_cost_tracker
        assert kwargs["deps"].swarms["chal"] == "alive"
        assert kwargs["deps"].results["chal"]["flag"] == "flag{kept}"
        shared_state["codex_deps"] = kwargs["deps"]
        return {"results": {"chal": {"flag": "flag{kept}"}}, "total_cost_usd": 0.0}

    async def fake_cleanup_runtime(deps_obj, ctfd_obj, cost_tracker_obj, **kwargs):  # type: ignore[no-untyped-def]
        assert deps_obj is deps
        assert ctfd_obj is shared_ctfd
        assert cost_tracker_obj is shared_cost_tracker
        shared_state["cleanup_reason"] = kwargs.get("reason")

    monkeypatch.setattr("backend.sandbox.cleanup_orphan_containers", fake_cleanup)
    monkeypatch.setattr("backend.sandbox.configure_semaphore", fake_configure)
    monkeypatch.setattr("backend.cli.build_deps", fake_build_deps)
    monkeypatch.setattr("backend.cli.cleanup_coordinator_runtime", fake_cleanup_runtime)
    monkeypatch.setattr("backend.cli.run_codex_coordinator", run_codex)

    deps.swarms["chal"] = "alive"
    deps.results["chal"] = {"flag": "flag{kept}"}

    await _run_coordinator(
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
        challenges_dir="challenges",
        no_submit=True,
        local_mode=False,
        coordinator_model=None,
        coordinator_backend="claude",
        max_challenges=2,
        resume_mode=False,
        msg_port=9400,
    )

    assert shared_state["codex_deps"] is deps
    assert shared_state["cleanup_reason"] in {"", None}


def test_next_inactive_turn_count_tracks_missing_tool_actions() -> None:
    assert _next_inactive_turn_count(msg_count=1, tool_calls_delta=1, previous_inactive_turns=0) == 0
    assert _next_inactive_turn_count(msg_count=1, tool_calls_delta=0, previous_inactive_turns=0) == 1
    assert _next_inactive_turn_count(msg_count=1, tool_calls_delta=0, previous_inactive_turns=1) == 2
    assert _next_inactive_turn_count(msg_count=0, tool_calls_delta=0, previous_inactive_turns=0) == 1


def test_validate_turn_activity_requires_tool_action_for_preflight() -> None:
    with pytest.raises(ClaudeCoordinatorInactiveError, match="preflight produced no tool actions"):
        _validate_turn_activity(
            msg_count=1,
            tool_calls_delta=0,
            previous_inactive_turns=0,
            require_tool_action=True,
        )

    assert _validate_turn_activity(
        msg_count=1,
        tool_calls_delta=1,
        previous_inactive_turns=0,
        require_tool_action=True,
    ) == 0


def test_coordinator_preflight_prompt_is_read_only() -> None:
    assert "fetch_challenges" in COORDINATOR_PREFLIGHT_PROMPT
    assert "get_solve_status" in COORDINATOR_PREFLIGHT_PROMPT
    assert "Do not spawn" in COORDINATOR_PREFLIGHT_PROMPT


def test_coordinator_prompts_require_artifact_inspection_before_rebroadcast() -> None:
    for prompt in (COORDINATOR_PROMPT, CODEX_COORDINATOR_PROMPT):
        assert "ADVISOR MESSAGE:" in prompt
        assert "Artifact path: /challenge/shared-artifacts/..." in prompt
        assert "/challenge/shared-artifacts/manifest.md" in prompt
        assert "Do not rebroadcast advisor or artifact messages blindly" in prompt
        assert len(prompt) < 2200


def test_render_solver_message_formats_pointer_first_candidate_events() -> None:
    rendered = _render_solver_message(
        CandidateRef(
            challenge_name="aeBPF",
            source_models=["codex/gpt-5.4"],
            flag="flag{candidate}",
            advisor_decision="likely",
            advisor_note="route and evidence look plausible",
            summary="matched hidden admin route",
            evidence_digest_paths={
                "codex/gpt-5.4": "/challenge/shared-artifacts/.advisor/candidate.digest.md",
            },
            evidence_pointer_paths={
                "codex/gpt-5.4": "/challenge/shared-artifacts/candidate.txt",
            },
            trace_paths={
                "codex/gpt-5.4": "/tmp/trace.jsonl",
            },
        )
    )

    assert "FLAG CANDIDATE: flag{candidate}" in rendered
    assert "Advisor verdict: likely" in rendered
    assert "Evidence digest: /challenge/shared-artifacts/.advisor/candidate.digest.md" in rendered
    assert "Evidence pointer: /challenge/shared-artifacts/candidate.txt" in rendered


@pytest.mark.asyncio
async def test_run_codex_coordinator_defaults_to_gpt_54(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_build_deps(settings, model_specs, challenges_root, no_submit, local_mode):  # type: ignore[no-untyped-def]
        return object(), object(), type("Deps", (), {"msg_port": 0})()

    class FakeCoordinator:
        def __init__(self, deps, model):  # type: ignore[no-untyped-def]
            captured["model"] = model

        async def start(self) -> None:
            return None

        async def turn(self, _message: str) -> None:
            return None

        async def stop(self) -> None:
            return None

    async def fake_run_event_loop(deps, ctfd, cost_tracker, turn_fn, **kwargs):  # type: ignore[no-untyped-def]
        await turn_fn("ping")
        return {"results": {}, "total_cost_usd": 0.0, "shutdown_reason": "test shutdown"}

    monkeypatch.setattr("backend.agents.codex_coordinator.build_deps", fake_build_deps)
    monkeypatch.setattr("backend.agents.codex_coordinator.CodexCoordinator", FakeCoordinator)
    monkeypatch.setattr("backend.agents.codex_coordinator.run_event_loop", fake_run_event_loop)

    result = await run_codex_coordinator(
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
        challenges_root="challenges",
        no_submit=True,
        local_mode=False,
        coordinator_model=None,
        msg_port=9400,
    )

    assert captured["model"] == "gpt-5.4"
    assert result["results"] == {}
    assert result["total_cost_usd"] == 0.0
    assert result["shutdown_reason"] == "test shutdown"


@pytest.mark.asyncio
async def test_run_coordinator_skips_runtime_reset_in_resume_mode(monkeypatch) -> None:
    startup_cleanup_called = {"value": False}

    async def fake_cleanup() -> None:
        startup_cleanup_called["value"] = True

    def fake_configure(_max_containers: int) -> None:
        return None

    reset_called = {"value": False}

    def fake_reset_runtime_state_dirs(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        reset_called["value"] = True
        raise AssertionError("runtime reset should not run in resume mode")

    monkeypatch.setattr("backend.sandbox.cleanup_orphan_containers", fake_cleanup)
    monkeypatch.setattr("backend.sandbox.configure_semaphore", fake_configure)
    monkeypatch.setattr("backend.cli._reset_runtime_state_dirs", fake_reset_runtime_state_dirs)
    monkeypatch.setattr(
        "backend.cli.build_deps",
        lambda settings, model_specs, challenges_root, no_submit, local_mode: (
            object(),
            object(),
            type(
                "Deps",
                (),
                {
                    "msg_port": 0,
                    "results": {},
                    "swarms": {},
                    "swarm_tasks": {},
                    "pending_swarm_queue": [],
                    "pending_swarm_set": set(),
                    "pending_swarm_meta": {},
                },
            )(),
        ),
    )
    monkeypatch.setattr(
        "backend.cli.cleanup_coordinator_runtime",
        lambda deps, ctfd, cost_tracker, **kwargs: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        "backend.cli.run_codex_coordinator",
        lambda **kwargs: asyncio.sleep(0, result={"results": {}, "total_cost_usd": 0.0}),
    )

    await _run_coordinator(
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
        challenges_dir="challenges",
        no_submit=True,
        local_mode=False,
        coordinator_model=None,
        coordinator_backend="claude",
        max_challenges=2,
        resume_mode=True,
        msg_port=9400,
    )

    assert reset_called["value"] is False
    assert startup_cleanup_called["value"] is False


@pytest.mark.asyncio
async def test_run_coordinator_restores_saved_pending_queue_in_resume_mode(monkeypatch) -> None:
    async def fake_cleanup() -> None:
        return None

    def fake_configure(_max_containers: int) -> None:
        return None

    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
    )
    deps.results["resume-me"] = {"status": "pending"}
    deps.results["hold-me"] = {
        "status": "pending",
        "requeue_requested": True,
        "requeue_priority": True,
        "requeue_reason": "priority_waiting",
    }

    captured: dict[str, object] = {}

    monkeypatch.setattr("backend.sandbox.cleanup_orphan_containers", fake_cleanup)
    monkeypatch.setattr("backend.sandbox.configure_semaphore", fake_configure)
    monkeypatch.setattr(
        "backend.cli.build_deps",
        lambda settings, model_specs, challenges_root, no_submit, local_mode: (
            object(),
            CostTracker(),
            deps,
        ),
    )
    monkeypatch.setattr(
        "backend.cli.cleanup_coordinator_runtime",
        lambda deps, ctfd, cost_tracker, **kwargs: fake_cleanup(),
    )

    async def run_codex(**kwargs):  # type: ignore[no-untyped-def]
        captured["pending"] = list(kwargs["deps"].pending_swarm_queue)
        captured["hold_reason"] = kwargs["deps"].pending_swarm_meta["hold-me"]["reason"]
        captured["resume_reason"] = kwargs["deps"].pending_swarm_meta["resume-me"]["reason"]
        return {"results": {}, "total_cost_usd": 0.0}

    monkeypatch.setattr("backend.cli.run_codex_coordinator", run_codex)

    await _run_coordinator(
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
        challenges_dir="challenges",
        no_submit=True,
        local_mode=False,
        coordinator_model=None,
        coordinator_backend="claude",
        max_challenges=2,
        resume_mode=True,
        msg_port=9400,
    )

    assert captured["pending"] == ["hold-me", "resume-me"]
    assert captured["hold_reason"] == "priority_waiting"
    assert captured["resume_reason"] == "resume_requested"


@pytest.mark.asyncio
async def test_codex_coordinator_read_loop_handles_oversized_jsonrpc_line() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
    )
    coordinator = CodexCoordinator(deps, model="gpt-5.4")
    reader = asyncio.StreamReader(limit=32)
    coordinator._proc = cast(Any, SimpleNamespace(stdout=reader))

    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    coordinator._pending_responses[7] = future

    payload = {
        "id": 7,
        "result": {
            "thread": {"id": "thread-1"},
            "blob": "x" * 4096,
        },
    }
    reader.feed_data((json.dumps(payload) + "\n").encode())
    reader.feed_eof()

    await coordinator._read_loop()

    assert future.done()
    assert future.result()["result"]["thread"]["id"] == "thread-1"


@pytest.mark.asyncio
async def test_cleanup_coordinator_runtime_propagates_shutdown_reason() -> None:
    recorded: list[str] = []

    class _FakeSwarm:
        def kill(self, reason: str = "swarm cancelled") -> None:
            recorded.append(reason)

    class _FakeCTFd:
        async def close(self) -> None:
            return None

    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
    )
    deps.swarms["chal"] = cast(Any, _FakeSwarm())
    deps.swarm_tasks["chal"] = asyncio.create_task(asyncio.sleep(0))

    await cleanup_coordinator_runtime(
        deps,
        cast(Any, _FakeCTFd()),
        deps.cost_tracker,
        reason="KeyboardInterrupt",
    )

    assert recorded == ["KeyboardInterrupt"]


@pytest.mark.asyncio
async def test_cleanup_coordinator_runtime_logs_top_challenge_and_peak_lane(caplog) -> None:
    class _FakeSolver:
        agent_name = "hot-chal/gpt-5.4"

        def get_runtime_status(self) -> dict[str, object]:
            return {
                "lifecycle": "idle",
                "last_command": "rg -n token /challenge/shared-artifacts/login.html",
            }

    class _FakeSwarm:
        def __init__(self) -> None:
            self.solvers = {"codex/gpt-5.4": _FakeSolver()}

        def kill(self, reason: str = "swarm cancelled") -> None:
            return None

    class _FakeCTFd:
        async def close(self) -> None:
            return None

    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
    )
    deps.swarms["hot-chal"] = cast(Any, _FakeSwarm())
    deps.cost_tracker.record_tokens(
        "hot-chal/gpt-5.4",
        "gpt-5.4",
        input_tokens=12_000,
        output_tokens=900,
        cache_read_tokens=6_000,
        provider_spec="codex",
    )
    deps.cost_tracker.record_tokens(
        "cold-chal/gpt-5.4-mini",
        "gpt-5.4-mini",
        input_tokens=2_000,
        output_tokens=120,
        cache_read_tokens=500,
        provider_spec="codex",
    )

    with caplog.at_level("INFO"):
        await cleanup_coordinator_runtime(
            deps,
            cast(Any, _FakeCTFd()),
            deps.cost_tracker,
            reason="test shutdown",
        )

    assert "Top challenge: hot-chal" in caplog.text
    assert "Peak lane: hot-chal/gpt-5.4" in caplog.text
    assert "last=rg -n token /challenge/shared-artifacts/login.html" in caplog.text


@pytest.mark.asyncio
async def test_run_event_loop_treats_loop_closed_as_shutdown(monkeypatch) -> None:
    class _FakePoller:
        def __init__(self, ctfd, interval_s=5.0) -> None:
            self.known_challenges = set()
            self.known_solved = set()

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

        async def get_event(self, timeout: float = 1.0):
            raise RuntimeError("Event loop is closed")

        def drain_events(self) -> list[object]:
            return []

    class _FakeServer:
        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    async def _fake_start_msg_server(inbox, deps, port):
        return _FakeServer()

    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
    )

    messages: list[str] = []

    async def turn_fn(message: str) -> None:
        messages.append(message)

    monkeypatch.setattr("backend.agents.coordinator_loop.CTFdPoller", _FakePoller)
    monkeypatch.setattr("backend.agents.coordinator_loop._start_msg_server", _fake_start_msg_server)
    monkeypatch.setattr("backend.agents.coordinator_loop._auto_spawn_unsolved", lambda deps, poller: asyncio.sleep(0))

    result = await run_event_loop(
        deps,
        cast(Any, object()),
        deps.cost_tracker,
        turn_fn,
        cleanup_runtime_on_exit=False,
    )

    assert messages
    assert result["shutdown_reason"] == "coordinator loop closed during shutdown"


@pytest.mark.asyncio
async def test_run_event_loop_honors_shutdown_event(monkeypatch) -> None:
    class _FakePoller:
        def __init__(self, ctfd, interval_s=5.0) -> None:
            self.known_challenges = set()
            self.known_solved = set()

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

        async def get_event(self, timeout: float = 1.0):
            await asyncio.sleep(0)
            return None

        def drain_events(self) -> list[object]:
            return []

    class _FakeServer:
        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    async def _fake_start_msg_server(inbox, deps, port):
        return _FakeServer()

    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
    )
    deps.shutdown_reason = "signal SIGINT"
    deps.shutdown_event.set()

    messages: list[str] = []

    async def turn_fn(message: str) -> None:
        messages.append(message)

    monkeypatch.setattr("backend.agents.coordinator_loop.CTFdPoller", _FakePoller)
    monkeypatch.setattr("backend.agents.coordinator_loop._start_msg_server", _fake_start_msg_server)
    monkeypatch.setattr("backend.agents.coordinator_loop._auto_spawn_unsolved", lambda deps, poller: asyncio.sleep(0))

    result = await run_event_loop(
        deps,
        cast(Any, object()),
        deps.cost_tracker,
        turn_fn,
        cleanup_runtime_on_exit=False,
    )

    assert messages
    assert result["shutdown_reason"] == "signal SIGINT"
