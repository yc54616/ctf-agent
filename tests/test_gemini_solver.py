from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from backend.agents.gemini_solver import GeminiSolver
from backend.cost_tracker import CostTracker
from backend.prompts import ChallengeMeta, build_shell_solver_preamble


def _make_solver() -> GeminiSolver:
    return GeminiSolver(
        model_spec="gemini/gemini-2.5-flash",
        challenge_dir="challenge-dir",
        meta=ChallengeMeta(name="chal"),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=SimpleNamespace(sandbox_image="ctf-sandbox"),
    )


def test_gemini_shell_preamble_stays_short_and_specific() -> None:
    prompt = build_shell_solver_preamble()

    assert "fs_query" in prompt
    assert "report_flag_candidate 'FLAG'" in prompt
    assert "notify_coordinator 'MSG'" in prompt
    assert len(prompt) < 700


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
async def test_gemini_fs_query_ipc_calls_core(monkeypatch) -> None:
    solver = _make_solver()

    async def fake_fs_query(*args, **kwargs) -> str:
        return "found file-a"

    monkeypatch.setattr("backend.agents.gemini_solver.do_fs_query", fake_fs_query)

    response = await solver._handle_ipc_request(
        {
            "action": "fs_query",
            "query_action": "find",
            "path": "/challenge/distfiles",
            "maxdepth": 4,
        }
    )

    assert response == {"output": "found file-a"}


@pytest.mark.asyncio
async def test_gemini_report_flag_candidate_ipc_queues_candidate(monkeypatch) -> None:
    solver = _make_solver()
    recorded: list[tuple[str, str, str, int, str]] = []

    async def fake_report(flag: str, evidence: str, confidence: str, step_count: int, trace_path: str) -> str:
        recorded.append((flag, evidence, confidence, step_count, trace_path))
        return "queued"

    solver.report_flag_candidate_fn = fake_report

    response = await solver._handle_ipc_request(
        {
            "action": "report_flag_candidate",
            "flag": "flag{test}",
            "evidence": "candidate from route",
            "confidence": "high",
        }
    )

    assert response == {"message": "queued"}
    assert recorded
    assert recorded[0][0] == "flag{test}"
    assert recorded[0][1] == "candidate from route"
    assert recorded[0][2] == "high"


@pytest.mark.asyncio
async def test_gemini_watchdog_terminates_stalled_turn(monkeypatch) -> None:
    solver = GeminiSolver(
        model_spec="gemini/gemini-2.5-flash",
        challenge_dir="challenge-dir",
        meta=ChallengeMeta(name="chal"),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=SimpleNamespace(sandbox_image="ctf-sandbox"),
    )
    solver._runtime.mark_ready()
    solver._proc = cast(Any, SimpleNamespace(returncode=None))
    state = {"stalled_reason": ""}
    terminated: list[bool] = []
    solver._arm_watchdog(
        phase="turn_start",
        step=0,
        deadline_seconds=0.0,
    )

    async def fake_terminate() -> None:
        terminated.append(True)
        proc = cast(Any, solver._proc)
        proc.returncode = 1

    monkeypatch.setattr(solver, "_terminate_proc", fake_terminate)
    monkeypatch.setattr("backend.agents.gemini_solver.WATCHDOG_SAMPLE_SECONDS", 0.001)

    await solver._watch_turn_progress(state)

    assert terminated == [True]
    assert state["stalled_reason"] == "stalled: turn_start_inactivity after 0s"
    assert solver._findings == "stalled: turn_start_inactivity after 0s"
    assert solver.get_runtime_status()["lifecycle"] == "error"


def test_gemini_tool_call_timeout_scales_with_bash_timeout() -> None:
    solver = _make_solver()

    assert solver._tool_call_watchdog_seconds("bash", {"timeout_seconds": 180}) == 210.0
    assert solver._tool_call_watchdog_seconds("fs_query", {"action": "find", "path": "/tmp"}) == 60.0


def test_gemini_post_tool_timeout_restarts_quickly_after_tool_result() -> None:
    solver = _make_solver()

    assert solver._post_tool_watchdog_seconds("fs_query", {"action": "find", "path": "/tmp"}) == 30.0
    assert solver._post_tool_watchdog_seconds("bash", {"command": "python3 solve.py"}) == 30.0
    assert solver._post_tool_watchdog_seconds("bash", {"command": "sed -n '1,20p' out.txt"}) == 30.0


def test_gemini_touch_watchdog_refreshes_post_tool_deadline() -> None:
    solver = _make_solver()
    solver._arm_watchdog(
        phase="post_tool",
        step=4,
        deadline_seconds=30.0,
        tool="bash",
    )
    started = solver._watchdog_started_monotonic

    solver._touch_watchdog()

    assert solver._watchdog_started_monotonic >= started


def test_gemini_read_only_progress_tracking_updates_runtime_status() -> None:
    solver = _make_solver()

    solver._record_tool_progress("fs_query")
    status = solver.get_runtime_status()
    assert status["read_only_streak"] == 1
    assert status["last_progress_kind"] == "read_only_tool"

    solver._record_tool_progress("bash")
    status = solver.get_runtime_status()
    assert status["read_only_streak"] == 0
    assert status["last_progress_kind"] == "exec_tool"
