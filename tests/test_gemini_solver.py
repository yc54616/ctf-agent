from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.agents.gemini_solver import GeminiSolver
from backend.cost_tracker import CostTracker
from backend.prompts import ChallengeMeta


def _make_solver() -> GeminiSolver:
    return GeminiSolver(
        model_spec="gemini/gemini-2.5-flash",
        challenge_dir="challenge-dir",
        meta=ChallengeMeta(name="chal"),
        ctfd=object(),  # type: ignore[arg-type]
        cost_tracker=CostTracker(),
        settings=SimpleNamespace(sandbox_image="ctf-sandbox"),
    )


def test_gemini_operator_bump_overrides_soft_bump_prompt() -> None:
    solver = _make_solver()

    solver.bump("Keep checking the session flow.")
    solver.bump_operator("Use Authorization: Token first.")

    prompt = solver._consume_turn_prompt()

    assert "Highest priority guidance from the operator" in prompt
    assert "Use Authorization: Token first." in prompt
    assert solver._operator_bump_insights is None
    assert solver._bump_insights is None


def test_gemini_regular_bump_prompt_still_works() -> None:
    solver = _make_solver()

    solver.bump("Try a different route.")

    prompt = solver._consume_turn_prompt()

    assert "Additional guidance" in prompt
    assert "Try a different route." in prompt
    assert solver._bump_insights is None


def test_gemini_advisory_bump_overrides_soft_bump_prompt() -> None:
    solver = _make_solver()

    solver.bump("Read the updated digest.")
    solver.bump_advisory("Validate the spectrogram path before broader audio stego.")

    prompt = solver._consume_turn_prompt()

    assert "Prioritize this lane advisory" in prompt
    assert "Validate the spectrogram path before broader audio stego." in prompt
    assert solver._advisory_bump_insights is None
    assert solver._bump_insights == "Read the updated digest."


def test_gemini_operator_bump_overrides_advisory_prompt() -> None:
    solver = _make_solver()

    solver.bump_advisory("Check the rendered invite artifact first.")
    solver.bump_operator("Use Authorization: Token first.")

    prompt = solver._consume_turn_prompt()

    assert "Highest priority guidance from the operator" in prompt
    assert "Use Authorization: Token first." in prompt
    assert solver._operator_bump_insights is None
    assert solver._advisory_bump_insights is None
    assert solver._bump_insights is None


@pytest.mark.asyncio
async def test_gemini_check_findings_ipc_returns_findings_every_fifth_poll(monkeypatch) -> None:
    solver = GeminiSolver(
        model_spec="gemini/gemini-2.5-flash",
        challenge_dir="challenge-dir",
        meta=ChallengeMeta(name="chal"),
        ctfd=object(),  # type: ignore[arg-type]
        cost_tracker=CostTracker(),
        settings=SimpleNamespace(sandbox_image="ctf-sandbox"),
        message_bus=object(),
    )

    calls: list[str] = []

    async def fake_check_findings(_message_bus, model_spec: str) -> str:
        calls.append(model_spec)
        return "**Findings from other agents:**\n\n[codex/gpt-5.4] check this path"

    monkeypatch.setattr("backend.tools.core.do_check_findings", fake_check_findings)

    for _ in range(4):
        response = await solver._handle_ipc_request({"action": "check_findings"})
        assert response == {"findings": ""}

    response = await solver._handle_ipc_request({"action": "check_findings"})

    assert response["findings"].startswith("**Findings from other agents:**")
    assert calls == ["gemini/gemini-2.5-flash"]


@pytest.mark.asyncio
async def test_gemini_prompt_findings_pull_filters_empty_bus(monkeypatch) -> None:
    solver = GeminiSolver(
        model_spec="gemini/gemini-2.5-flash",
        challenge_dir="challenge-dir",
        meta=ChallengeMeta(name="chal"),
        ctfd=object(),  # type: ignore[arg-type]
        cost_tracker=CostTracker(),
        settings=SimpleNamespace(sandbox_image="ctf-sandbox"),
        message_bus=object(),
    )

    async def fake_check_findings(_message_bus, _model_spec: str) -> str:
        return "No new findings from other agents."

    monkeypatch.setattr("backend.tools.core.do_check_findings", fake_check_findings)

    assert await solver._pull_shared_findings_for_prompt() == ""


@pytest.mark.asyncio
async def test_gemini_watchdog_terminates_stalled_turn(monkeypatch) -> None:
    solver = GeminiSolver(
        model_spec="gemini/gemini-2.5-flash",
        challenge_dir="challenge-dir",
        meta=ChallengeMeta(name="chal"),
        ctfd=object(),  # type: ignore[arg-type]
        cost_tracker=CostTracker(),
        settings=SimpleNamespace(sandbox_image="ctf-sandbox"),
    )
    solver._runtime.mark_ready()
    solver._proc = SimpleNamespace(returncode=None)
    state = {"stalled_reason": ""}
    terminated: list[bool] = []

    async def fake_terminate() -> None:
        terminated.append(True)
        solver._proc.returncode = 1

    monkeypatch.setattr(solver, "_terminate_proc", fake_terminate)
    monkeypatch.setattr("backend.agents.gemini_solver.WATCHDOG_SAMPLE_SECONDS", 0.001)
    monkeypatch.setattr("backend.agents.gemini_solver.WATCHDOG_STALL_SAMPLES", 3)
    monkeypatch.setattr("backend.agents.gemini_solver.WATCHDOG_IDLE_GRACE_SECONDS", 0.0)

    await solver._watch_turn_progress(state)

    assert terminated == [True]
    assert state["stalled_reason"] == "stalled: no progress across 3 samples"
    assert solver._findings == "stalled: no progress across 3 samples"
    assert solver.get_runtime_status()["lifecycle"] == "error"
