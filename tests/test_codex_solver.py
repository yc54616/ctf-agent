from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from backend.agents.codex_solver import SANDBOX_TOOLS, CodexSolver
from backend.cost_tracker import CostTracker
from backend.prompts import ChallengeMeta
from backend.solver_base import parse_candidate_rejection_alert


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

    assert {"bash", "report_flag_candidate", "notify_coordinator"} <= names
    assert "fs_query" not in names
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
    assert CodexSolver._tool_call_watchdog_seconds("notify_coordinator", {"message": "hi"}) == 60.0


def test_codex_turn_activity_timeout_allows_longer_post_tool_reasoning() -> None:
    assert CodexSolver._turn_activity_watchdog_seconds() == 300.0


def test_codex_runtime_status_does_not_expose_read_only_tracking() -> None:
    solver = _make_solver("codex/gpt-5.4")

    status = solver.get_runtime_status()
    assert "read_only_streak" not in status
    assert "last_progress_kind" not in status


@pytest.mark.asyncio
async def test_codex_rejected_candidate_does_not_set_candidate_state() -> None:
    solver = _make_solver("codex/gpt-5.4")

    async def fake_report(
        flag: str,
        evidence: str,
        confidence: str,
        step_count: int,
        trace_path: str,
    ) -> str:
        assert flag == "NOT_SOLVE"
        assert evidence == "analysis marker"
        assert confidence == "low"
        assert step_count == 0
        assert trace_path
        return "Flag candidate rejected: placeholder sentinel."

    solver.report_flag_candidate_fn = fake_report

    ack = await solver._report_flag_candidate(
        "NOT_SOLVE",
        evidence="analysis marker",
        confidence="low",
    )

    assert ack == "Flag candidate rejected: placeholder sentinel."
    assert solver._candidate_flag is None
    assert solver._candidate_evidence == ""
    assert solver._candidate_confidence == ""


@pytest.mark.asyncio
async def test_codex_rejected_candidate_cools_down_and_notifies(monkeypatch) -> None:
    solver = _make_solver("codex/gpt-5.4")
    notifications: list[str] = []
    wait_timeouts: list[float] = []

    async def fake_notify(message: str) -> None:
        notifications.append(message)

    async def fake_wait_for(awaitable, timeout):  # type: ignore[no-untyped-def]
        if hasattr(awaitable, "close"):
            awaitable.close()
        wait_timeouts.append(float(timeout))
        raise asyncio.TimeoutError

    solver.notify_coordinator = fake_notify
    monkeypatch.setattr("backend.agents.codex_solver.asyncio.wait_for", fake_wait_for)

    await solver._handle_rejected_candidate(
        "BLOCKED_NO_FLAG",
        "Flag candidate rejected: placeholder sentinel.",
    )

    assert wait_timeouts == [15.0]
    assert notifications
    payload = parse_candidate_rejection_alert(notifications[0])
    assert payload is not None
    assert payload["flag"] == "BLOCKED_NO_FLAG"
    assert payload["reason"] == "placeholder sentinel."
    assert payload["cooldown_seconds"] == 15
    assert "Cooling down 15s before continuing." in solver._findings


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


@pytest.mark.asyncio
async def test_codex_agent_message_delta_updates_commentary_preview() -> None:
    solver = _make_solver("codex/gpt-5.4")

    await solver._handle_notification(
        "item/agentMessage/delta",
        {
            "itemId": "agent-1",
            "delta": "Checking whether the extracted rootfs contains ",
            "threadId": "thread-1",
            "turnId": "turn-1",
        },
    )
    await solver._handle_notification(
        "item/agentMessage/delta",
        {
            "itemId": "agent-1",
            "delta": "the verifier patch.",
            "threadId": "thread-1",
            "turnId": "turn-1",
        },
    )
    await solver._handle_notification(
        "item/completed",
        {
            "item": {
                "id": "agent-1",
                "type": "agentMessage",
                "phase": "commentary",
                "text": "",
            }
        },
    )

    status = solver.get_runtime_status()

    assert status["commentary_preview"] == "Checking whether the extracted rootfs contains the verifier patch."
    assert solver._findings == "Checking whether the extracted rootfs contains the verifier patch."


@pytest.mark.asyncio
async def test_codex_item_started_is_traced_with_item_type() -> None:
    solver = _make_solver("codex/gpt-5.4")

    await solver._handle_notification(
        "item/started",
        {
            "item": {
                "id": "reasoning-1",
                "type": "reasoning",
            }
        },
    )

    payloads = [
        line
        for line in Path(solver.tracer.path).read_text(encoding="utf-8").splitlines()
        if "rpc_item_started" in line
    ]
    assert payloads
    assert '"item_type": "reasoning"' in payloads[-1]


@pytest.mark.asyncio
async def test_codex_reasoning_summary_deltas_show_without_raw_reasoning_text() -> None:
    solver = _make_solver("codex/gpt-5.4")

    await solver._handle_notification(
        "item/reasoning/summaryPartAdded",
        {
            "itemId": "reasoning-1",
            "summaryIndex": 0,
            "threadId": "thread-1",
            "turnId": "turn-1",
        },
    )
    await solver._handle_notification(
        "item/reasoning/summaryTextDelta",
        {
            "itemId": "reasoning-1",
            "summaryIndex": 0,
            "delta": "Inspecting the ELF header ",
            "threadId": "thread-1",
            "turnId": "turn-1",
        },
    )
    await solver._handle_notification(
        "item/reasoning/summaryTextDelta",
        {
            "itemId": "reasoning-1",
            "summaryIndex": 0,
            "delta": "and checking whether the footer is fake.",
            "threadId": "thread-1",
            "turnId": "turn-1",
        },
    )
    before = solver.get_runtime_status()["commentary_preview"]

    await solver._handle_notification(
        "item/reasoning/textDelta",
        {
            "itemId": "reasoning-1",
            "contentIndex": 0,
            "delta": "private raw reasoning text",
            "threadId": "thread-1",
            "turnId": "turn-1",
        },
    )
    after = solver.get_runtime_status()["commentary_preview"]

    assert before == "Inspecting the ELF header and checking whether the footer is fake."
    assert after == before

    payloads = [
        line
        for line in Path(solver.tracer.path).read_text(encoding="utf-8").splitlines()
        if "reasoning_text_delta_seen" in line
    ]
    assert payloads


@pytest.mark.asyncio
async def test_codex_compaction_is_requested_out_of_band() -> None:
    solver = _make_solver("codex/gpt-5.4")
    solver._thread_id = "thread-1"
    called: list[tuple[int, int | None]] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_request_compaction(*, total_tokens: int, context_window: int | None) -> None:
        called.append((total_tokens, context_window))
        started.set()
        await release.wait()
        solver._compact_task = None

    solver._request_compaction = fake_request_compaction  # type: ignore[method-assign]

    await solver._handle_notification(
        "thread/tokenUsage/updated",
        {
            "tokenUsage": {
                "modelContextWindow": 100_000,
                "last": {"inputTokens": 10, "outputTokens": 2, "cachedInputTokens": 0},
                "total": {"totalTokens": 80_000, "inputTokens": 80_000, "outputTokens": 2, "cachedInputTokens": 0},
            }
        },
    )

    assert solver._compact_requested is True
    assert solver._compact_task is not None
    await asyncio.wait_for(started.wait(), timeout=1)
    assert called == [(80_000, 100_000)]

    release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_codex_declines_command_execution_approval_requests() -> None:
    solver = _make_solver("codex/gpt-5.4")
    responses: list[tuple[int, dict[str, object]]] = []

    async def fake_respond(request_id: int, result: Any) -> None:
        responses.append((request_id, result))

    solver._respond_to_request = fake_respond  # type: ignore[method-assign]

    await solver._handle_approval_request(
        41,
        "item/commandExecution/requestApproval",
        {
            "itemId": "cmd-1",
            "reason": "Need approval to run shell command",
            "command": "strings Image.gz | rg commit_creds",
        },
    )

    assert responses == [(41, {"decision": "decline"})]
    payloads = [
        line
        for line in Path(solver.tracer.path).read_text(encoding="utf-8").splitlines()
        if "approval_request_declined" in line
    ]
    assert payloads
    assert '"method": "item/commandExecution/requestApproval"' in payloads[-1]


@pytest.mark.asyncio
async def test_codex_read_loop_routes_command_execution_approval_requests() -> None:
    solver = _make_solver("codex/gpt-5.4")
    called: list[tuple[int, str, dict[str, object]]] = []

    async def fake_handle(request_id: int, method: str, params: dict[str, Any]) -> None:
        called.append((request_id, method, params))

    solver._handle_approval_request = fake_handle  # type: ignore[method-assign]

    class _FakeStdout:
        def __init__(self) -> None:
            self._lines = [
                json.dumps(
                    {
                        "id": 77,
                        "method": "item/commandExecution/requestApproval",
                        "params": {
                            "itemId": "cmd-1",
                            "reason": "Need approval to run shell command",
                            "command": "id",
                        },
                    }
                ),
                "",
            ]

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout = _FakeStdout()

    solver._proc = cast(Any, _FakeProc())

    async def fake_read_jsonrpc_line(_stdout: Any) -> str:
        return solver._proc.stdout._lines.pop(0)

    import backend.agents.codex_solver as codex_solver_module

    original = codex_solver_module.read_jsonrpc_line
    codex_solver_module.read_jsonrpc_line = fake_read_jsonrpc_line
    try:
        await solver._read_loop()
    finally:
        codex_solver_module.read_jsonrpc_line = original

    assert called == [
        (
            77,
            "item/commandExecution/requestApproval",
            {
                "itemId": "cmd-1",
                "reason": "Need approval to run shell command",
                "command": "id",
            },
        )
    ]
