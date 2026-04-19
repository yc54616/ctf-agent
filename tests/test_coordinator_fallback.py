from __future__ import annotations

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
from backend.agents.codex_coordinator import run_codex_coordinator
from backend.agents.coordinator_loop import _render_solver_message
from backend.cli import _run_coordinator
from backend.cost_tracker import CostTracker
from backend.deps import CoordinatorDeps
from backend.message_bus import CandidateRef


@pytest.mark.asyncio
async def test_run_coordinator_falls_back_to_codex_when_claude_fails(monkeypatch) -> None:
    async def fake_cleanup() -> None:
        return None

    def fake_configure(_max_containers: int) -> None:
        return None

    async def fail_claude(**kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("claude down")

    async def run_codex(**kwargs):  # type: ignore[no-untyped-def]
        return {"results": {"chal": {"flag": "flag{ok}"}}, "total_cost_usd": 0.0}

    printed: list[str] = []

    monkeypatch.setattr("backend.sandbox.cleanup_orphan_containers", fake_cleanup)
    monkeypatch.setattr("backend.sandbox.configure_semaphore", fake_configure)
    monkeypatch.setattr(
        "backend.cli.build_deps",
        lambda settings, model_specs, challenges_root, no_submit: (
            object(),
            object(),
            type("Deps", (), {"msg_port": 0, "results": {}, "swarms": {}, "swarm_tasks": {}})(),
        ),
    )
    monkeypatch.setattr("backend.cli.cleanup_coordinator_runtime", lambda deps, ctfd, cost_tracker: fake_cleanup())
    monkeypatch.setattr("backend.agents.claude_coordinator.run_claude_coordinator", fail_claude)
    monkeypatch.setattr("backend.cli.run_codex_coordinator", run_codex)
    monkeypatch.setattr("backend.cli.console.print", lambda *args, **kwargs: printed.append(" ".join(map(str, args))))

    await _run_coordinator(
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
        challenges_dir="challenges",
        no_submit=True,
        coordinator_model=None,
        coordinator_backend="claude",
        max_challenges=2,
        msg_port=9400,
    )

    assert any("Falling back to Codex coordinator" in line for line in printed)
    assert any("flag{ok}" in line for line in printed)


@pytest.mark.asyncio
async def test_run_coordinator_reports_inactive_claude_before_fallback(monkeypatch) -> None:
    async def fake_cleanup() -> None:
        return None

    def fake_configure(_max_containers: int) -> None:
        return None

    async def fail_claude(**kwargs):  # type: ignore[no-untyped-def]
        raise ClaudeCoordinatorInactiveError("Claude coordinator produced no tool actions")

    async def run_codex(**kwargs):  # type: ignore[no-untyped-def]
        return {"results": {}, "total_cost_usd": 0.0}

    printed: list[str] = []

    monkeypatch.setattr("backend.sandbox.cleanup_orphan_containers", fake_cleanup)
    monkeypatch.setattr("backend.sandbox.configure_semaphore", fake_configure)
    monkeypatch.setattr(
        "backend.cli.build_deps",
        lambda settings, model_specs, challenges_root, no_submit: (
            object(),
            object(),
            type("Deps", (), {"msg_port": 0, "results": {}, "swarms": {}, "swarm_tasks": {}})(),
        ),
    )
    monkeypatch.setattr("backend.cli.cleanup_coordinator_runtime", lambda deps, ctfd, cost_tracker: fake_cleanup())
    monkeypatch.setattr("backend.agents.claude_coordinator.run_claude_coordinator", fail_claude)
    monkeypatch.setattr("backend.cli.run_codex_coordinator", run_codex)
    monkeypatch.setattr("backend.cli.console.print", lambda *args, **kwargs: printed.append(" ".join(map(str, args))))

    await _run_coordinator(
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
        challenges_dir="challenges",
        no_submit=True,
        coordinator_model=None,
        coordinator_backend="claude",
        max_challenges=2,
        msg_port=9400,
    )

    assert any("Claude coordinator inactive" in line for line in printed)
    assert any("Falling back to Codex coordinator" in line for line in printed)


@pytest.mark.asyncio
async def test_run_coordinator_fallback_reuses_active_runtime(monkeypatch) -> None:
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

    def fake_build_deps(settings, model_specs, challenges_root, no_submit):  # type: ignore[no-untyped-def]
        return shared_ctfd, shared_cost_tracker, deps

    async def fail_claude(**kwargs):  # type: ignore[no-untyped-def]
        shared_state["claude_deps"] = kwargs["deps"]
        kwargs["deps"].swarms["chal"] = "alive"
        kwargs["deps"].results["chal"] = {"flag": "flag{kept}"}
        raise ClaudeCoordinatorInactiveError("Claude coordinator produced no tool actions")

    async def run_codex(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["deps"] is deps
        assert kwargs["ctfd"] is shared_ctfd
        assert kwargs["cost_tracker"] is shared_cost_tracker
        assert kwargs["deps"].swarms["chal"] == "alive"
        assert kwargs["deps"].results["chal"]["flag"] == "flag{kept}"
        shared_state["codex_deps"] = kwargs["deps"]
        return {"results": {"chal": {"flag": "flag{kept}"}}, "total_cost_usd": 0.0}

    async def fake_cleanup_runtime(deps_obj, ctfd_obj, cost_tracker_obj):  # type: ignore[no-untyped-def]
        assert deps_obj is deps
        assert ctfd_obj is shared_ctfd
        assert cost_tracker_obj is shared_cost_tracker

    monkeypatch.setattr("backend.sandbox.cleanup_orphan_containers", fake_cleanup)
    monkeypatch.setattr("backend.sandbox.configure_semaphore", fake_configure)
    monkeypatch.setattr("backend.cli.build_deps", fake_build_deps)
    monkeypatch.setattr("backend.cli.cleanup_coordinator_runtime", fake_cleanup_runtime)
    monkeypatch.setattr("backend.agents.claude_coordinator.run_claude_coordinator", fail_claude)
    monkeypatch.setattr("backend.cli.run_codex_coordinator", run_codex)

    await _run_coordinator(
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
        challenges_dir="challenges",
        no_submit=True,
        coordinator_model=None,
        coordinator_backend="claude",
        max_challenges=2,
        msg_port=9400,
    )

    assert shared_state["claude_deps"] is deps
    assert shared_state["codex_deps"] is deps


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

    def fake_build_deps(settings, model_specs, challenges_root, no_submit):  # type: ignore[no-untyped-def]
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
        return {"results": {}, "total_cost_usd": 0.0}

    monkeypatch.setattr("backend.agents.codex_coordinator.build_deps", fake_build_deps)
    monkeypatch.setattr("backend.agents.codex_coordinator.CodexCoordinator", FakeCoordinator)
    monkeypatch.setattr("backend.agents.codex_coordinator.run_event_loop", fake_run_event_loop)

    result = await run_codex_coordinator(
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
        challenges_root="challenges",
        no_submit=True,
        coordinator_model=None,
        msg_port=9400,
    )

    assert captured["model"] == "gpt-5.4"
    assert result == {"results": {}, "total_cost_usd": 0.0}
