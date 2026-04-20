from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.agents.runtime_solver import InSandboxRuntimeSolver
from backend.prompts import ChallengeMeta
from backend.runtime_control import append_jsonl, read_new_jsonl, write_json_atomic
from backend.sandbox import ExecResult


class _FakeSandbox:
    def __init__(self, results: dict[str, ExecResult]) -> None:
        self.results = results
        self.commands: list[tuple[str, int]] = []

    async def exec(self, command: str, timeout_s: int = 300) -> ExecResult:
        self.commands.append((command, timeout_s))
        for prefix, result in self.results.items():
            if command.startswith(prefix):
                return result
        raise AssertionError(f"unexpected command: {command}")


def _make_solver(provider: str, sandbox: _FakeSandbox, tmp_path) -> InSandboxRuntimeSolver:
    challenge_dir = tmp_path / "challenge"
    challenge_dir.mkdir()
    return InSandboxRuntimeSolver(
        model_spec=f"{provider}/demo-model",
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="demo"),
        settings=SimpleNamespace(),
        cost_tracker=SimpleNamespace(record_tokens=lambda *args, **kwargs: None),
        sandbox=sandbox,
    )


def test_runtime_preflight_uses_provider_specific_cli(tmp_path) -> None:
    sandbox = _FakeSandbox(
        {
            "python3 - <<'PY'": ExecResult(exit_code=0, stdout="ok\n", stderr=""),
            "codex --version": ExecResult(exit_code=0, stdout="codex-cli 0.121.0\n", stderr=""),
        }
    )
    solver = _make_solver("codex", sandbox, tmp_path)

    asyncio.run(solver._verify_runtime_prerequisites())

    assert len(sandbox.commands) == 2
    assert sandbox.commands[0][0].startswith("python3 - <<'PY'")
    assert sandbox.commands[1][0] == "codex --version"


def test_runtime_preflight_reports_rebuild_hint(tmp_path) -> None:
    sandbox = _FakeSandbox(
        {
            "python3 - <<'PY'": ExecResult(exit_code=1, stdout="", stderr="missing python modules: pydantic_ai"),
        }
    )
    solver = _make_solver("gemini", sandbox, tmp_path)

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(solver._verify_runtime_prerequisites())

    message = str(excinfo.value)
    assert "Sandbox runtime preflight failed for demo/gemini/demo-model" in message
    assert "Rebuild ctf-sandbox from sandbox/Dockerfile.sandbox." in message
    assert "missing python modules: pydantic_ai" in message


def test_runtime_solver_consumes_turn_results_from_events_without_run_turn(tmp_path) -> None:
    sandbox = _FakeSandbox({})
    solver = _make_solver("codex", sandbox, tmp_path)
    solver._started = True
    write_json_atomic(
        solver._control.heartbeat_path,
        {
            "ts": __import__("time").time(),
            "lifecycle": "idle",
            "step_count": 2,
            "runtime_health": "healthy",
        },
    )
    append_jsonl(
        solver._control.events_path,
        {
            "type": "turn_result",
            "ts": __import__("time").time(),
            "result": {
                "flag": None,
                "status": "gave_up",
                "findings_summary": "checked the obvious path",
                "step_count": 2,
                "cost_usd": 0.01,
                "log_path": "/tmp/trace.jsonl",
                "candidate_flag": None,
                "candidate_evidence": "",
                "candidate_confidence": "",
            },
        },
    )

    result = asyncio.run(solver.run_until_done_or_gave_up())

    assert result.status == "gave_up"
    assert result.findings_summary == "checked the obvious path"
    assert not solver._control.commands_path.exists()


def test_runtime_solver_prime_state_clears_stale_heartbeat_and_result(tmp_path) -> None:
    sandbox = _FakeSandbox({})
    solver = _make_solver("codex", sandbox, tmp_path)
    write_json_atomic(solver._control.heartbeat_path, {"ts": 1})
    write_json_atomic(solver._control.result_path, {"type": "final_result"})

    solver._prime_runtime_state()

    assert not solver._control.heartbeat_path.exists()
    assert not solver._control.result_path.exists()


def test_runtime_solver_status_marks_thinking_from_commentary(tmp_path) -> None:
    sandbox = _FakeSandbox({})
    solver = _make_solver("codex", sandbox, tmp_path)
    write_json_atomic(
        solver._control.heartbeat_path,
        {
            "ts": __import__("time").time(),
            "lifecycle": "busy",
            "step_count": 4,
            "runtime_health": "healthy",
            "raw_runtime": {
                "lifecycle": "busy",
                "step_count": 4,
                "commentary_preview": "Checking whether the BPF verifier is patched.",
                "commentary_at": __import__("time").time(),
            },
        },
    )

    status = solver.get_runtime_status()

    assert status["activity_state"] == "thinking"
    assert status["activity"] == "Checking whether the BPF verifier is patched."
    assert status["commentary_preview"] == "Checking whether the BPF verifier is patched."


def test_runtime_solver_stop_process_writes_shutdown_reason(tmp_path) -> None:
    sandbox = _FakeSandbox({"kill ": ExecResult(exit_code=0, stdout="", stderr="")})
    solver = _make_solver("codex", sandbox, tmp_path)
    write_json_atomic(solver._control.runtime_pid_path, {"pid": "1234"})

    asyncio.run(solver.stop_process(reason="coordinator cleanup: KeyboardInterrupt"))

    _, events = read_new_jsonl(solver._control.commands_path, offset=0)
    assert events[-1]["type"] == "shutdown"
    assert events[-1]["reason"] == "coordinator cleanup: KeyboardInterrupt"


def test_runtime_solver_uses_reset_detail_as_last_reset_reason(tmp_path) -> None:
    sandbox = _FakeSandbox({})
    solver = _make_solver("codex", sandbox, tmp_path)
    append_jsonl(
        solver._control.events_path,
        {
            "type": "reset",
            "ts": __import__("time").time(),
            "reason": "shutdown",
            "detail": "flag found by codex/gpt-5.4",
        },
    )

    asyncio.run(solver._process_events())

    assert solver.get_runtime_status()["last_reset_reason"] == "flag found by codex/gpt-5.4"
