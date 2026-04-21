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


def test_runtime_solver_constructs_warm_reusable_sandbox(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    class _CapturingSandbox:
        def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            captured.update(kwargs)

    monkeypatch.setattr("backend.agents.runtime_solver.DockerSandbox", _CapturingSandbox)

    challenge_dir = tmp_path / "challenge"
    challenge_dir.mkdir()
    InSandboxRuntimeSolver(
        model_spec="codex/demo-model",
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="demo"),
        settings=SimpleNamespace(),
        cost_tracker=SimpleNamespace(record_tokens=lambda *args, **kwargs: None),
        warm_container_id="warm-xyz",
    )

    assert captured["existing_container_id"] == "warm-xyz"
    assert captured["preserve_stopped_container"] is True


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


def test_runtime_solver_hard_recovery_deletes_broken_container_before_recreate(tmp_path) -> None:
    challenge_dir = tmp_path / "challenge"
    challenge_dir.mkdir()

    class _OldSandbox:
        def __init__(self) -> None:
            self.image = "ctf-sandbox"
            self.challenge_dir = str(challenge_dir)
            self.memory_limit = "4g"
            self.exec_output_spill_threshold_bytes = 1
            self.read_file_spill_threshold_bytes = 2
            self.artifact_preview_bytes = 3
            self.workspace_dir = str(challenge_dir / "workspace")
            self.shared_artifacts_dir = str(challenge_dir / ".shared-artifacts")
            self.control_dir = str(challenge_dir / ".lane-state" / "codex")
            self.provider_home_dir = str(challenge_dir / ".lane-state" / "provider-home")
            self.trace_dir = str(challenge_dir / ".lane-state" / "trace")
            self.repo_root_dir = str(tmp_path / "repo")
            self.challenge_src_dir = str(challenge_dir)
            self.auth_seed_mounts = {"codex-auth.json": "/tmp/auth.json"}
            self.preserve_stopped_container = True
            self.stop_calls: list[bool | None] = []

        async def stop(self, *, delete: bool | None = None) -> None:
            self.stop_calls.append(delete)

    created: dict[str, object] = {}

    class _NewSandbox:
        def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            created.update(kwargs)

        async def start(self) -> None:
            return None

    old_sandbox = _OldSandbox()
    solver = InSandboxRuntimeSolver(
        model_spec="codex/demo-model",
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="demo"),
        settings=SimpleNamespace(),
        cost_tracker=SimpleNamespace(record_tokens=lambda *args, **kwargs: None),
        sandbox=old_sandbox,
    )
    solver._soft_reset_count = 1
    solver._write_runtime_config = lambda: None  # type: ignore[method-assign]
    solver._prime_runtime_state = lambda: None  # type: ignore[method-assign]

    async def _noop() -> None:
        return None

    solver._start_runtime_process = _noop  # type: ignore[method-assign]
    solver._wait_for_heartbeat = _noop  # type: ignore[method-assign]

    import backend.agents.runtime_solver as runtime_solver_module

    original_cls = runtime_solver_module.DockerSandbox
    runtime_solver_module.DockerSandbox = _NewSandbox  # type: ignore[assignment]
    try:
        asyncio.run(solver._recover_runtime("heartbeat stale"))
    finally:
        runtime_solver_module.DockerSandbox = original_cls  # type: ignore[assignment]

    assert solver.sandbox is not None
    assert created["workspace_dir"].endswith("/workspace")
    assert created["shared_artifacts_dir"].endswith("/.shared-artifacts")
    assert created["preserve_stopped_container"] is True
    assert old_sandbox.stop_calls == [True]
    assert solver._last_reset_reason == "heartbeat stale"
    assert created["challenge_dir"] == str(challenge_dir)
    assert created["challenge_src_dir"] == str(challenge_dir)


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
