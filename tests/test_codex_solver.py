from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from backend.agents.codex_solver import SANDBOX_TOOLS, CodexSolver
from backend.cost_tracker import CostTracker
from backend.prompts import ChallengeMeta


def _make_solver(model_spec: str) -> CodexSolver:
    return CodexSolver(
        model_spec=model_spec,
        challenge_dir=".",
        meta=ChallengeMeta(name="test"),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
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


def test_codex_sandbox_tools_include_minimal_solver_surface() -> None:
    names = {tool["name"] for tool in SANDBOX_TOOLS}

    assert {"bash", "fs_query", "report_flag_candidate", "notify_coordinator"} <= names
    assert "submit_flag" not in names
    assert "find_files" not in names


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
async def test_codex_watchdog_marks_turn_start_stall(monkeypatch) -> None:
    solver = _make_solver("codex/gpt-5.4")
    solver._turn_done.clear()
    solver._runtime.mark_ready()
    solver._arm_watchdog(
        phase="turn_start",
        step=0,
        deadline_seconds=0.0,
    )

    monkeypatch.setattr("backend.agents.codex_solver.WATCHDOG_SAMPLE_SECONDS", 0.001)

    await solver._watch_turn_progress()

    assert solver._turn_done.is_set()
    assert solver._turn_error == "stalled: turn_start_inactivity after 0s"
    assert solver._findings == "stalled: turn_start_inactivity after 0s"
    assert solver.get_runtime_status()["lifecycle"] == "error"

    payloads = [
        line
        for line in Path(solver.tracer.path).read_text(encoding="utf-8").splitlines()
        if "turn_stalled" in line
    ]
    assert payloads
    assert '"watchdog_phase": "turn_start"' in payloads[-1]
    assert '"watchdog_kind": "turn_start_inactivity"' in payloads[-1]


def test_codex_tool_call_timeout_scales_with_bash_timeout() -> None:
    assert CodexSolver._tool_call_watchdog_seconds("bash", {"timeout_seconds": 180}) == 210.0
    assert CodexSolver._tool_call_watchdog_seconds("fs_query", {"action": "find", "path": "/tmp"}) == 60.0


def test_codex_post_tool_timeout_restarts_quickly_after_tool_result() -> None:
    assert CodexSolver._post_tool_watchdog_seconds("fs_query", {"action": "find", "path": "/tmp"}) == 30.0
    assert CodexSolver._post_tool_watchdog_seconds("bash", {"command": "python3 solve.py"}) == 30.0
    assert CodexSolver._post_tool_watchdog_seconds("bash", {"command": "sed -n '1,20p' out.txt"}) == 30.0


def test_codex_read_only_progress_tracking_updates_runtime_status() -> None:
    solver = _make_solver("codex/gpt-5.4")

    solver._record_tool_progress("fs_query")
    status = solver.get_runtime_status()
    assert status["read_only_streak"] == 1
    assert status["last_progress_kind"] == "read_only_tool"

    solver._record_tool_progress("bash")
    status = solver.get_runtime_status()
    assert status["read_only_streak"] == 0
    assert status["last_progress_kind"] == "exec_tool"


def test_codex_tool_call_phase_expires_when_local_tool_runs_past_deadline() -> None:
    solver = _make_solver("codex/gpt-5.4")
    solver._runtime.mark_busy("bash", "python3 solve.py", step_count=3)
    solver._arm_watchdog(
        phase="tool_call",
        step=3,
        deadline_seconds=0.0,
        tool="bash",
    )

    assert solver._watchdog_expired() is True
