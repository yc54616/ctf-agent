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

    assert "fs_query" not in prompt
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
    assert solver._tool_call_watchdog_seconds("notify_coordinator", {"message": "hi"}) == 60.0


def test_gemini_turn_activity_timeout_allows_longer_post_tool_reasoning() -> None:
    solver = _make_solver()

    assert solver._turn_activity_watchdog_seconds() == 300.0


def test_gemini_touch_watchdog_refreshes_turn_activity_deadline() -> None:
    solver = _make_solver()
    solver._arm_watchdog(
        phase="turn_active",
        step=4,
        deadline_seconds=300.0,
        tool="bash",
    )
    started = solver._watchdog_started_monotonic

    solver._touch_watchdog()

    assert solver._watchdog_started_monotonic >= started


def test_gemini_runtime_status_does_not_expose_read_only_tracking() -> None:
    solver = _make_solver()

    status = solver.get_runtime_status()
    assert "read_only_streak" not in status
    assert "last_progress_kind" not in status


def test_gemini_prepare_dirs_prefers_runtime_ipc_env(monkeypatch, tmp_path) -> None:
    solver = _make_solver()
    project_dir = tmp_path / "workspace"
    project_dir.mkdir()
    legacy_ipc = project_dir / ".gemini-ipc"
    legacy_ipc.mkdir()
    configured_ipc = tmp_path / "control" / "gemini-ipc"
    solver.sandbox = SimpleNamespace(workspace_dir=str(project_dir))
    solver._system_prompt = "Solve carefully."

    auth_path = tmp_path / "oauth.json"
    auth_path.write_text(
        '{"access_token":"a","refresh_token":"r","token_type":"Bearer"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("CTF_AGENT_GEMINI_IPC_DIR", str(configured_ipc))
    monkeypatch.setenv("CTF_AGENT_PROVIDER_HOME", str(tmp_path / "provider-home"))
    monkeypatch.setattr("backend.agents.gemini_solver.resolve_home_auth_paths", lambda settings: SimpleNamespace(gemini=auth_path))
    monkeypatch.setattr(
        "backend.agents.gemini_solver.refresh_gemini_oauth",
        lambda settings: SimpleNamespace(
            access_token="new-a",
            refresh_token="new-r",
            token_type="Bearer",
            expiry_date_ms=None,
        ),
    )

    solver._prepare_gemini_dirs()

    assert solver._ipc_dir == str(configured_ipc)
    assert not legacy_ipc.exists()
    assert (configured_ipc / "requests").is_dir()
    assert (configured_ipc / "responses").is_dir()
    assert configured_ipc.stat().st_mode & 0o777 == 0o777
