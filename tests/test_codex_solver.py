from __future__ import annotations

import pytest

from backend.agents.codex_solver import CodexSolver
from backend.prompts import ChallengeMeta


def _make_solver(model_spec: str) -> CodexSolver:
    return CodexSolver(
        model_spec=model_spec,
        challenge_dir=".",
        meta=ChallengeMeta(name="test"),
        ctfd=object(),  # type: ignore[arg-type]
        cost_tracker=object(),  # type: ignore[arg-type]
        settings=object(),
        no_submit=True,
    )


def test_build_thread_params_omits_service_tier() -> None:
    solver = _make_solver("codex/gpt-5.4")

    params = solver._build_thread_params("system prompt")

    assert params["model"] == "gpt-5.4"
    assert params["approvalPolicy"] == "on-request"
    assert "serviceTier" not in params
    assert "system prompt" in params["baseInstructions"]


def test_build_thread_params_keeps_reasoning_for_codex_53() -> None:
    solver = _make_solver("codex/gpt-5.3-codex")

    params = solver._build_thread_params("system prompt")

    assert params["model"] == "gpt-5.3-codex"
    assert params["reasoningEffort"] == "xhigh"


def test_codex_operator_bump_overrides_soft_bump_prompt() -> None:
    solver = _make_solver("codex/gpt-5.4")

    solver.bump("Keep checking the session flow.")
    solver.bump_operator("Use Authorization: Token first.")

    prompt = solver._consume_turn_prompt()

    assert "Highest priority guidance from the operator" in prompt
    assert "Use Authorization: Token first." in prompt
    assert solver._operator_bump_insights is None
    assert solver._bump_insights is None


def test_codex_regular_bump_prompt_still_works() -> None:
    solver = _make_solver("codex/gpt-5.4")

    solver.bump("Try a different route.")

    prompt = solver._consume_turn_prompt()

    assert "Additional guidance" in prompt
    assert "Try a different route." in prompt
    assert solver._bump_insights is None


def test_codex_advisory_bump_overrides_soft_bump_prompt() -> None:
    solver = _make_solver("codex/gpt-5.4")

    solver.bump("Read the new digest.")
    solver.bump_advisory("Inspect the spectrogram before more generic carving.")

    prompt = solver._consume_turn_prompt()

    assert "Prioritize this lane advisory" in prompt
    assert "Inspect the spectrogram before more generic carving." in prompt
    assert solver._advisory_bump_insights is None
    assert solver._bump_insights == "Read the new digest."


def test_codex_operator_bump_overrides_advisory_prompt() -> None:
    solver = _make_solver("codex/gpt-5.4")

    solver.bump_advisory("Validate the login artifact first.")
    solver.bump_operator("Use Authorization: Token first.")

    prompt = solver._consume_turn_prompt()

    assert "Highest priority guidance from the operator" in prompt
    assert "Use Authorization: Token first." in prompt
    assert solver._operator_bump_insights is None
    assert solver._advisory_bump_insights is None
    assert solver._bump_insights is None


def test_codex_compaction_triggers_on_absolute_token_threshold() -> None:
    assert CodexSolver._should_request_compaction(None, 250_000) is True
    assert CodexSolver._should_request_compaction(1_000_000, 249_999) is False


def test_codex_compaction_triggers_on_context_fraction() -> None:
    assert CodexSolver._should_request_compaction(100_000, 70_001) is True
    assert CodexSolver._should_request_compaction(100_000, 69_999) is False


@pytest.mark.asyncio
async def test_codex_watchdog_marks_turn_stalled(monkeypatch) -> None:
    solver = _make_solver("codex/gpt-5.4")
    solver._turn_done.clear()
    solver._runtime.mark_ready()

    monkeypatch.setattr("backend.agents.codex_solver.WATCHDOG_SAMPLE_SECONDS", 0.001)
    monkeypatch.setattr("backend.agents.codex_solver.WATCHDOG_STALL_SAMPLES", 3)
    monkeypatch.setattr("backend.agents.codex_solver.WATCHDOG_IDLE_GRACE_SECONDS", 0.0)

    await solver._watch_turn_progress()

    assert solver._turn_done.is_set()
    assert solver._turn_error == "stalled: no progress across 3 samples"
    assert solver._findings == "stalled: no progress across 3 samples"
    assert solver.get_runtime_status()["lifecycle"] == "error"
