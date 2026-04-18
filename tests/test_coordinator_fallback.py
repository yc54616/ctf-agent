from __future__ import annotations

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
from backend.agents.codex_coordinator import (
    run_codex_coordinator,
)
from backend.cli import _run_coordinator


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
    monkeypatch.setattr("backend.agents.claude_coordinator.run_claude_coordinator", fail_claude)
    monkeypatch.setattr("backend.agents.codex_coordinator.run_codex_coordinator", run_codex)
    monkeypatch.setattr("backend.cli.console.print", lambda *args, **kwargs: printed.append(" ".join(map(str, args))))

    await _run_coordinator(
        settings=object(),  # type: ignore[arg-type]
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
    monkeypatch.setattr("backend.agents.claude_coordinator.run_claude_coordinator", fail_claude)
    monkeypatch.setattr("backend.agents.codex_coordinator.run_codex_coordinator", run_codex)
    monkeypatch.setattr("backend.cli.console.print", lambda *args, **kwargs: printed.append(" ".join(map(str, args))))

    await _run_coordinator(
        settings=object(),  # type: ignore[arg-type]
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


@pytest.mark.asyncio
async def test_run_codex_coordinator_defaults_to_gpt_54_mini(monkeypatch) -> None:
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

    async def fake_run_event_loop(deps, ctfd, cost_tracker, turn_fn):  # type: ignore[no-untyped-def]
        await turn_fn("ping")
        return {"results": {}, "total_cost_usd": 0.0}

    monkeypatch.setattr("backend.agents.codex_coordinator.build_deps", fake_build_deps)
    monkeypatch.setattr("backend.agents.codex_coordinator.CodexCoordinator", FakeCoordinator)
    monkeypatch.setattr("backend.agents.codex_coordinator.run_event_loop", fake_run_event_loop)

    result = await run_codex_coordinator(
        settings=object(),  # type: ignore[arg-type]
        model_specs=["codex/gpt-5.4-mini"],
        challenges_root="challenges",
        no_submit=True,
        coordinator_model=None,
        msg_port=9400,
    )

    assert captured["model"] == "gpt-5.4-mini"
    assert result == {"results": {}, "total_cost_usd": 0.0}
