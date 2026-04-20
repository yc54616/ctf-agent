from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from backend.cli import (
    _default_run_log_path,
    _discover_challenge_dirs,
    _install_shutdown_signal_handlers,
    _memory_budget_summary,
    _remove_shutdown_signal_handlers,
    _reset_runtime_state_dirs,
    _setup_logging,
)


def test_discover_challenge_dirs_returns_single_challenge_root(tmp_path: Path) -> None:
    challenge_root = tmp_path / "aeBPF"
    challenge_root.mkdir()
    (challenge_root / "metadata.yml").write_text("name: aeBPF\n", encoding="utf-8")

    discovered = _discover_challenge_dirs(challenge_root)

    assert discovered == [challenge_root.resolve()]


def test_reset_runtime_state_dirs_removes_runtime_artifacts_but_preserves_results(tmp_path: Path) -> None:
    challenges_root = tmp_path / "challenges"
    challenge_root = challenges_root / "aeBPF"
    challenge_root.mkdir(parents=True)
    (challenge_root / "metadata.yml").write_text("name: aeBPF\n", encoding="utf-8")
    lane_state = challenge_root / ".lane-state"
    (lane_state / "codex-gpt-5.4" / "control").mkdir(parents=True)
    (lane_state / "codex-gpt-5.4" / "control" / "events.jsonl").write_text("{}", encoding="utf-8")
    shared_artifacts = challenge_root / ".shared-artifacts"
    (shared_artifacts / "manifest.md").parent.mkdir(parents=True)
    (shared_artifacts / "manifest.md").write_text("# manifest\n", encoding="utf-8")
    (challenge_root / "solve").mkdir()
    (challenge_root / "solve" / "lanes").mkdir()
    (challenge_root / "solve" / "lanes" / "codex-gpt-5.4.handoff.jsonl").write_text("{}", encoding="utf-8")
    (challenge_root / "solve" / "result.json").write_text("{}", encoding="utf-8")
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "trace-aeBPF-gpt-5.4-20260420-000000.jsonl").write_text("{}", encoding="utf-8")
    (logs_dir / "trace-aeBPF-gpt-5.4-20260420-000000-rpc.jsonl").write_text("{}", encoding="utf-8")

    summary = _reset_runtime_state_dirs(_discover_challenge_dirs(challenges_root), log_dir=logs_dir)

    assert summary.lane_state_dirs == 1
    assert summary.shared_artifact_dirs == 1
    assert summary.solve_lane_dirs == 1
    assert summary.trace_files == 2
    assert not lane_state.exists()
    assert not shared_artifacts.exists()
    assert not (challenge_root / "solve" / "lanes").exists()
    assert not any(logs_dir.iterdir())
    assert (challenge_root / "metadata.yml").exists()
    assert (challenge_root / "solve" / "result.json").exists()


def test_reset_runtime_state_dirs_ignores_missing_state(tmp_path: Path) -> None:
    challenge_root = tmp_path / "aeBPF"
    challenge_root.mkdir()
    (challenge_root / "metadata.yml").write_text("name: aeBPF\n", encoding="utf-8")

    summary = _reset_runtime_state_dirs([challenge_root], log_dir=tmp_path / "logs")

    assert summary.touched is False


def test_reset_runtime_state_dirs_falls_back_to_docker_on_permission_error(monkeypatch, tmp_path: Path) -> None:
    challenge_root = tmp_path / "aeBPF"
    challenge_root.mkdir()
    (challenge_root / "metadata.yml").write_text("name: aeBPF\n", encoding="utf-8")
    lane_state = challenge_root / ".lane-state"
    lane_state.mkdir()
    recorded: dict[str, object] = {}

    def fake_rmtree(path: Path) -> None:
        raise PermissionError("permission denied")

    def fake_run(cmd: list[str], **kwargs):
        recorded["cmd"] = cmd
        recorded["kwargs"] = kwargs
        lane_state.rmdir()
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("backend.cli.shutil.rmtree", fake_rmtree)
    monkeypatch.setattr("backend.cli.subprocess.run", fake_run)

    summary = _reset_runtime_state_dirs([challenge_root], log_dir=tmp_path / "logs")

    assert summary.lane_state_dirs == 1
    assert recorded["cmd"][:4] == ["docker", "run", "--rm", "-v"]
    assert not lane_state.exists()


def test_memory_budget_summary_reports_single_challenge_pressure() -> None:
    gib = 1024 * 1024 * 1024
    summary = _memory_budget_summary(
        "4g",
        lane_count=7,
        challenge_count=10,
        host_memory_bytes=16 * gib,
    )

    assert summary["per_lane_bytes"] == 4 * gib
    assert summary["one_challenge_bytes"] == 28 * gib
    assert summary["max_total_bytes"] == 280 * gib
    assert summary["warn_single"] is True
    assert summary["warn_total"] is True


def test_memory_budget_summary_distinguishes_total_concurrency_warning() -> None:
    gib = 1024 * 1024 * 1024
    summary = _memory_budget_summary(
        "4g",
        lane_count=2,
        challenge_count=10,
        host_memory_bytes=16 * gib,
    )

    assert summary["one_challenge_bytes"] == 8 * gib
    assert summary["warn_single"] is False
    assert summary["warn_total"] is True


def test_default_run_log_path_points_under_repo_logs() -> None:
    path = _default_run_log_path()

    assert path.parent.name == "logs"
    assert path.name.startswith("ctf-solve-")
    assert path.suffix == ".log"


def test_setup_logging_creates_file_handler(tmp_path: Path) -> None:
    log_path = tmp_path / "ctf-solve.log"

    resolved = _setup_logging(False, log_path=log_path)
    try:
        import logging

        logging.getLogger("test.cli").info("hello from file handler")
        for handler in logging.getLogger().handlers:
            handler.flush()
    finally:
        logging.shutdown()

    assert resolved == log_path.resolve()
    assert log_path.exists()
    assert "hello from file handler" in log_path.read_text(encoding="utf-8")


def test_signal_handler_requests_shutdown(monkeypatch) -> None:
    class FakeLoop:
        def __init__(self) -> None:
            self.handlers: dict[object, tuple[object, ...]] = {}
            self.removed: list[object] = []

        def add_signal_handler(self, sig, callback, *args) -> None:  # type: ignore[no-untyped-def]
            self.handlers[sig] = (callback, *args)

        def remove_signal_handler(self, sig) -> None:  # type: ignore[no-untyped-def]
            self.removed.append(sig)

    class FakeEvent:
        def __init__(self) -> None:
            self._set = False

        def is_set(self) -> bool:
            return self._set

        def set(self) -> None:
            self._set = True

    loop = FakeLoop()
    deps = SimpleNamespace(shutdown_event=FakeEvent(), shutdown_reason="")
    forced: dict[str, int] = {}

    monkeypatch.setattr("backend.cli.asyncio.get_running_loop", lambda: loop)
    monkeypatch.setattr("backend.cli.os._exit", lambda code: forced.setdefault("code", code))

    installed = _install_shutdown_signal_handlers(deps)
    assert installed

    callback, *args = loop.handlers[installed[0]]
    callback(*args)

    assert deps.shutdown_event.is_set() is True
    assert deps.shutdown_reason.startswith("signal ")

    callback(*args)

    assert deps.shutdown_reason.startswith("forced signal ")
    assert forced["code"] == 130

    _remove_shutdown_signal_handlers(installed)
    assert loop.removed == installed
