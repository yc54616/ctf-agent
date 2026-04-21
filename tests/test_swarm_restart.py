from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from backend.agents.swarm import ChallengeSwarm, LaneRestartState
from backend.message_bus import SharedFindingRef
from backend.prompts import ChallengeMeta, build_prompt
from backend.solver_base import CANCELLED, ERROR, GAVE_UP, SolverResult


class _FakeSandbox:
    pass


class _FakeSolver:
    def __init__(
        self,
        *,
        model_spec: str,
        sandbox: object,
        runtime_status: Mapping[str, object],
    ) -> None:
        self.model_spec = model_spec
        self.agent_name = f"agent/{model_spec}"
        self.sandbox = sandbox
        self._runtime_status = dict(runtime_status)
        self.bumped: list[str] = []
        self.advisory_bumped: list[str] = []
        self.started = 0
        self.stopped = 0
        self.process_stopped = 0

    async def start(self) -> None:
        self.started += 1

    async def run_until_done_or_gave_up(self) -> SolverResult:
        raise NotImplementedError

    def bump(self, insights: str) -> None:
        self.bumped.append(insights)

    def bump_advisory(self, insights: str) -> None:
        self.advisory_bumped.append(insights)

    def get_runtime_status(self) -> dict[str, object]:
        return dict(self._runtime_status)

    def mark_terminal_status(self, status: str) -> None:
        return None

    async def stop_process(self) -> None:
        self.process_stopped += 1

    async def stop(self) -> None:
        self.stopped += 1


class _QueuedSolver(_FakeSolver):
    def __init__(
        self,
        *,
        model_spec: str,
        sandbox: object,
        runtime_status: Mapping[str, object],
        results: list[SolverResult],
    ) -> None:
        super().__init__(model_spec=model_spec, sandbox=sandbox, runtime_status=runtime_status)
        self._results = list(results)

    async def run_until_done_or_gave_up(self) -> SolverResult:
        if not self._results:
            raise AssertionError("No queued result left for solver")
        return self._results.pop(0)


def _make_result(trace_path: Path) -> SolverResult:
    return SolverResult(
        flag=None,
        status=GAVE_UP,
        findings_summary="dead-end while probing the deployment surface",
        step_count=12,
        cost_usd=0.25,
        log_path=str(trace_path),
    )


def _make_stalled_result(trace_path: Path) -> SolverResult:
    return SolverResult(
        flag=None,
        status=ERROR,
        findings_summary="stalled: post_tool_inactivity after 30s (bash)",
        step_count=12,
        cost_usd=0.25,
        log_path=str(trace_path),
    )


def test_build_prompt_pushes_noisy_output_to_shared_artifacts() -> None:
    prompt = build_prompt(
        ChallengeMeta(name="midnight", category="web"),
        ["index.html"],
    )

    assert "Treat `/challenge/shared-artifacts/` as shared evidence." in prompt
    assert "Artifact path: /challenge/shared-artifacts/..." in prompt
    assert "/challenge/shared-artifacts/<name>.txt" in prompt
    assert "grep -R" in prompt
    assert "ffuf" in prompt
    assert "you may run build or compose commands early" in prompt
    assert "Never reread `/challenge/agent-repo`, `/challenge/host-logs`, prior `solve/` output" in prompt
    assert "Large saved output may come back with only a path, not a preview." in prompt
    assert "`docker compose`" in prompt
    assert "`docker-compose`" in prompt
    assert "`podman-compose`" in prompt
    assert "`timeout_seconds` (for example 300 or 600)" in prompt
    assert "/challenge/shared-artifacts/<name>.log" in prompt
    assert "verify artifacts or service state first" in prompt
    assert "fs_query" not in prompt
    assert "/opt/wordlists/seclists" in prompt
    assert "`httpx`" in prompt
    assert "`katana`" in prompt
    assert prompt.count("Artifact path: /challenge/shared-artifacts/...") == 1
    assert len(prompt) < 5200


def test_build_prompt_only_adds_binary_analysis_for_binary_like_challenges() -> None:
    web_prompt = build_prompt(
        ChallengeMeta(name="webby", category="web"),
        ["index.html"],
    )
    reverse_prompt = build_prompt(
        ChallengeMeta(name="re", category="reverse"),
        ["readme.txt"],
    )
    heuristic_prompt = build_prompt(
        ChallengeMeta(name="mystery", category="misc"),
        ["challenge"],
    )

    assert "## Binary Analysis" not in web_prompt
    assert "## Binary Analysis" in reverse_prompt
    assert "## Binary Analysis" in heuristic_prompt
    assert "pyghidra" not in reverse_prompt
    assert "ghidra-headless" in reverse_prompt


def test_stalled_lane_restart_reuses_same_sandbox_and_writes_handoff(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge"
    challenge_dir.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "tool_call",
                        "tool": "bash",
                        "step": 12,
                        "args": "grep -nE 'script|k8s' /challenge/shared-artifacts/deployed_now.html",
                    }
                ),
                json.dumps(
                    {
                        "type": "tool_call",
                        "tool": "bash",
                        "step": 12,
                        "args": "sed -n '1,120p' /challenge/shared-artifacts/deployed_now.html",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="Midnight Roulette", category="web"),
        ctfd=cast(Any, object()),
        cost_tracker=cast(Any, object()),
        settings=cast(Any, SimpleNamespace()),
        model_specs=["codex/gpt-5.4"],
    )
    model_spec = "codex/gpt-5.4"
    sandbox = _FakeSandbox()
    runtime_status = {
        "lifecycle": "idle",
        "step_count": 12,
        "last_tool": "bash",
        "last_command": "grep -nE 'script|k8s' /challenge/shared-artifacts/deployed_now.html",
        "last_exit_hint": "17: <script src=\"/ctfd/themes/core/static/assets/color_mode_switcher.52334129.js\"",
    }
    original_solver = _FakeSolver(
        model_spec=model_spec,
        sandbox=sandbox,
        runtime_status=runtime_status,
    )
    replacement_solver = _FakeSolver(
        model_spec=model_spec,
        sandbox=sandbox,
        runtime_status={"lifecycle": "starting", "step_count": 12},
    )

    created: list[tuple[object | None, int]] = []

    def _fake_create_solver(spec: str, *, sandbox=None, initial_step_count: int = 0):
        assert spec == model_spec
        created.append((sandbox, initial_step_count))
        return replacement_solver

    swarm._create_solver = cast(Any, _fake_create_solver)

    first = asyncio.run(
        swarm._maybe_restart_stalled_lane(model_spec, original_solver, _make_result(trace_path))
    )
    second = asyncio.run(
        swarm._maybe_restart_stalled_lane(model_spec, original_solver, _make_result(trace_path))
    )

    assert first is None
    assert second is replacement_solver
    assert original_solver.process_stopped == 1
    assert replacement_solver.started == 1
    assert created == [(sandbox, 12)]
    assert replacement_solver.bumped
    assert "do not repeat the same approach" in replacement_solver.bumped[0].lower()
    assert "/challenge/shared-artifacts/<name>.txt" in replacement_solver.bumped[0]
    assert "/challenge/shared-artifacts/lane-resume-codex-gpt-5.4.md" in replacement_solver.bumped[0]
    assert swarm.solvers[model_spec] is replacement_solver
    assert "stalled after repeated dead-end" in swarm._lane_restart_notes[model_spec]

    handoff_path = challenge_dir / "solve" / "lanes" / "codex-gpt-5.4.handoff.jsonl"
    lines = [json.loads(line) for line in handoff_path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    assert lines[0]["restart_reason"] == ""
    assert "stalled after repeated dead-end" in lines[1]["restart_reason"]
    assert lines[1]["step_count"] == 12
    assert "recent_trace_tail" not in lines[1]

    resume_path = challenge_dir / ".shared-artifacts" / "lane-resume-codex-gpt-5.4.md"
    resume_text = resume_path.read_text(encoding="utf-8")
    assert "Lane Resume: Midnight Roulette / codex/gpt-5.4" in resume_text
    assert "Recent Commands To Avoid Repeating Blindly" in resume_text
    assert "grep -nE 'script|k8s'" in resume_text
    assert "Recent Trace Tail" not in resume_text
    assert "/challenge/shared-artifacts/<name>.txt" in resume_text


def test_in_turn_stall_restarts_on_first_occurrence(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge"
    challenge_dir.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text("", encoding="utf-8")

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="Midnight Roulette", category="web"),
        ctfd=cast(Any, object()),
        cost_tracker=cast(Any, object()),
        settings=cast(Any, SimpleNamespace()),
        model_specs=["codex/gpt-5.4"],
    )
    model_spec = "codex/gpt-5.4"
    sandbox = _FakeSandbox()
    original_solver = _FakeSolver(
        model_spec=model_spec,
        sandbox=sandbox,
        runtime_status={
            "lifecycle": "idle",
            "step_count": 12,
            "last_tool": "bash",
            "last_command": "find /challenge/distfiles/b440add5 -maxdepth 6 -type f | sed -n '1,400p'",
            "last_exit_hint": "stalled: post_tool_inactivity after 30s (bash)",
        },
    )
    replacement_solver = _FakeSolver(
        model_spec=model_spec,
        sandbox=sandbox,
        runtime_status={"lifecycle": "starting", "step_count": 12},
    )

    created: list[tuple[object | None, int]] = []

    def _fake_create_solver(spec: str, *, sandbox=None, initial_step_count: int = 0):
        assert spec == model_spec
        created.append((sandbox, initial_step_count))
        return replacement_solver

    swarm._create_solver = cast(Any, _fake_create_solver)

    replacement = asyncio.run(
        swarm._maybe_restart_stalled_lane(model_spec, original_solver, _make_stalled_result(trace_path))
    )

    assert replacement is replacement_solver
    assert original_solver.process_stopped == 1
    assert replacement_solver.started == 1
    assert created == [(sandbox, 12)]
    assert replacement_solver.bumped
    assert "/challenge/shared-artifacts/lane-resume-codex-gpt-5.4.md" in replacement_solver.bumped[0]
    assert replacement_solver.advisory_bumped == []
    assert "stalled: post_tool_inactivity after 30s (bash)" in swarm._lane_restart_notes[model_spec]

    handoff_path = challenge_dir / "solve" / "lanes" / "codex-gpt-5.4.handoff.jsonl"
    lines = [json.loads(line) for line in handoff_path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    assert lines[0]["restart_reason"] == "stalled: post_tool_inactivity after 30s (bash)"

    resume_path = challenge_dir / ".shared-artifacts" / "lane-resume-codex-gpt-5.4.md"
    resume_text = resume_path.read_text(encoding="utf-8")
    assert "stalled: post_tool_inactivity after 30s (bash)" in resume_text
    assert "find /challenge/distfiles/b440add5 -maxdepth 6 -type f | sed -n '1,400p'" in resume_text
    assert "/challenge/shared-artifacts/manifest.md" in resume_text
    assert "/challenge/shared-artifacts/.advisor/" in resume_text


def test_high_step_lane_gets_context_refresh_restart(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge"
    challenge_dir.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text("", encoding="utf-8")

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="Midnight Roulette", category="web"),
        ctfd=cast(Any, object()),
        cost_tracker=cast(Any, object()),
        settings=cast(Any, SimpleNamespace()),
        model_specs=["codex/gpt-5.4"],
    )
    model_spec = "codex/gpt-5.4"
    sandbox = _FakeSandbox()
    original_solver = _FakeSolver(
        model_spec=model_spec,
        sandbox=sandbox,
        runtime_status={
            "lifecycle": "idle",
            "step_count": 185,
            "last_tool": "bash",
            "last_command": "python3 spectrogram.py /challenge/shared-artifacts/hidden_message.wav",
            "last_exit_hint": "spectrogram still ambiguous",
        },
    )
    replacement_solver = _FakeSolver(
        model_spec=model_spec,
        sandbox=sandbox,
        runtime_status={"lifecycle": "starting", "step_count": 185},
    )

    created: list[tuple[object | None, int]] = []

    def _fake_create_solver(spec: str, *, sandbox=None, initial_step_count: int = 0):
        assert spec == model_spec
        created.append((sandbox, initial_step_count))
        return replacement_solver

    swarm._create_solver = cast(Any, _fake_create_solver)

    replacement = asyncio.run(
        swarm._maybe_restart_stalled_lane(model_spec, original_solver, _make_result(trace_path))
    )

    assert replacement is replacement_solver
    assert original_solver.process_stopped == 1
    assert replacement_solver.started == 1
    assert created == [(sandbox, 185)]
    assert replacement_solver.bumped
    assert "lane-resume-codex-gpt-5.4.md" in replacement_solver.bumped[0]
    assert "context refresh after 185 total steps" in swarm._lane_restart_notes[model_spec]
    assert swarm._lane_restart_state[model_spec].restart_count == 0
    assert swarm._lane_restart_state[model_spec].last_context_refresh_step == 185


def test_restart_budget_resets_only_after_ten_new_steps(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge"
    challenge_dir.mkdir()

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="Midnight Roulette", category="web"),
        ctfd=cast(Any, object()),
        cost_tracker=cast(Any, object()),
        settings=cast(Any, SimpleNamespace()),
        model_specs=["codex/gpt-5.4"],
    )
    model_spec = "codex/gpt-5.4"
    swarm._lane_restart_state[model_spec] = LaneRestartState(
        last_total_steps=25,
        restart_count=3,
        restart_budget_baseline_step=20,
    )

    small_progress_reason = swarm._compute_restart_reason(
        model_spec,
        {
            "step_count": 29,
            "status": GAVE_UP,
            "last_command": "python3 analyze.py",
            "last_exit_hint": "still analyzing",
            "findings_summary": "need more reversing",
        },
    )
    assert small_progress_reason == ""
    assert swarm._lane_restart_state[model_spec].restart_count == 3

    reset_reason = swarm._compute_restart_reason(
        model_spec,
        {
            "step_count": 30,
            "status": GAVE_UP,
            "last_command": "python3 analyze.py",
            "last_exit_hint": "still analyzing",
            "findings_summary": "need more reversing",
        },
    )
    assert reset_reason == ""
    assert swarm._lane_restart_state[model_spec].restart_count == 0


def test_in_turn_stall_resets_restart_budget_after_ten_new_steps(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge"
    challenge_dir.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text("", encoding="utf-8")

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="Midnight Roulette", category="web"),
        ctfd=cast(Any, object()),
        cost_tracker=cast(Any, object()),
        settings=cast(Any, SimpleNamespace()),
        model_specs=["codex/gpt-5.4"],
    )
    model_spec = "codex/gpt-5.4"
    swarm._lane_restart_state[model_spec] = LaneRestartState(
        last_total_steps=25,
        restart_count=3,
        restart_budget_baseline_step=20,
    )
    sandbox = _FakeSandbox()
    original_solver = _FakeSolver(
        model_spec=model_spec,
        sandbox=sandbox,
        runtime_status={
            "lifecycle": "idle",
            "step_count": 30,
            "last_tool": "bash",
            "last_command": "python3 analyze.py",
            "last_exit_hint": "stalled: post_tool_inactivity after 30s (bash)",
        },
    )
    replacement_solver = _FakeSolver(
        model_spec=model_spec,
        sandbox=sandbox,
        runtime_status={"lifecycle": "starting", "step_count": 30},
    )

    def _fake_create_solver(spec: str, *, sandbox=None, initial_step_count: int = 0):
        assert spec == model_spec
        assert sandbox is original_solver.sandbox
        assert initial_step_count == 30
        return replacement_solver

    swarm._create_solver = cast(Any, _fake_create_solver)

    replacement = asyncio.run(
        swarm._maybe_restart_stalled_lane(model_spec, original_solver, _make_stalled_result(trace_path))
    )

    assert replacement is replacement_solver
    assert swarm._lane_restart_state[model_spec].restart_count == 0


def test_run_solver_loop_restarts_error_lane_and_continues_with_replacement(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge"
    challenge_dir.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text("", encoding="utf-8")

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="Midnight Roulette", category="web"),
        ctfd=cast(Any, object()),
        cost_tracker=cast(Any, object()),
        settings=cast(Any, SimpleNamespace()),
        model_specs=["codex/gpt-5.4"],
    )
    model_spec = "codex/gpt-5.4"
    original_solver = _QueuedSolver(
        model_spec=model_spec,
        sandbox=_FakeSandbox(),
        runtime_status={"lifecycle": "error", "step_count": 16},
        results=[
            SolverResult(
                flag=None,
                status=ERROR,
                findings_summary="stalled: turn_inactivity after 300s",
                step_count=16,
                cost_usd=0.4,
                log_path=str(trace_path),
            )
        ],
    )
    replacement_solver = _QueuedSolver(
        model_spec=model_spec,
        sandbox=original_solver.sandbox,
        runtime_status={"lifecycle": "finished", "step_count": 17},
        results=[
            SolverResult(
                flag=None,
                status=GAVE_UP,
                findings_summary="",
                step_count=17,
                cost_usd=0.45,
                log_path=str(trace_path),
            )
        ],
    )

    calls: list[str] = []

    async def _fake_restart(spec: str, solver: _FakeSolver, result: SolverResult) -> _FakeSolver | None:
        calls.append(f"{spec}:{result.status}:{result.step_count}")
        if len(calls) == 1:
            swarm.solvers[spec] = replacement_solver
            return replacement_solver
        return None

    swarm._maybe_restart_stalled_lane = cast(Any, _fake_restart)

    result, final_solver = asyncio.run(swarm._run_solver_loop(original_solver, model_spec))

    assert calls == [
        "codex/gpt-5.4:error:16",
        "codex/gpt-5.4:gave_up:17",
    ]
    assert result.status == GAVE_UP
    assert result.step_count == 17
    assert final_solver is replacement_solver


def test_artifact_finding_posts_fact_only_summary_and_manifest(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge"
    challenge_dir.mkdir()

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="Midnight Roulette", category="web"),
        ctfd=cast(Any, object()),
        cost_tracker=cast(Any, object()),
        settings=cast(Any, SimpleNamespace()),
        model_specs=["codex/gpt-5.4", "gemini/gemini-2.5-flash"],
    )
    shared_dir = challenge_dir / ".shared-artifacts"
    shared_dir.mkdir(parents=True, exist_ok=True)
    (shared_dir / "k8s_dashboard.html").write_text(
        "<html><form><input name='csrf' value='token-123'></form><script>fetch('/api/v1/k8s/get')</script></html>\n",
        encoding="utf-8",
    )
    model_spec = "codex/gpt-5.4"
    solver = _FakeSolver(
        model_spec=model_spec,
        sandbox=_FakeSandbox(),
        runtime_status={
            "lifecycle": "idle",
            "step_count": 18,
            "last_tool": "bash",
            "last_command": "sed -n '1,120p' /challenge/shared-artifacts/k8s_dashboard.html",
            "last_exit_hint": "HTTP 200 OK /challenge/shared-artifacts/k8s_dashboard.html",
        },
    )
    result = SolverResult(
        flag=None,
        status=GAVE_UP,
        findings_summary="Potential admin API at /api/v1/k8s/get",
        step_count=18,
        cost_usd=0.4,
        log_path="",
    )

    asyncio.run(swarm._maybe_share_artifact_finding(model_spec, solver, result))

    unread = asyncio.run(swarm.message_bus.check("gemini/gemini-2.5-flash"))
    assert len(unread) == 1
    assert unread[0].model == model_spec
    assert unread[0].content == "Artifact path: /challenge/shared-artifacts/k8s_dashboard.html"
    assert "Artifact path: /challenge/shared-artifacts/k8s_dashboard.html" in swarm.last_shared_finding
    assert "Digest: /challenge/shared-artifacts/.advisor/" in swarm.last_shared_finding

    manifest_path = challenge_dir / ".shared-artifacts" / "manifest.md"
    manifest = manifest_path.read_text(encoding="utf-8")
    assert "Shared Artifact Manifest" in manifest
    assert "fact: Potential admin API at /api/v1/k8s/get" in manifest
    assert "path: /challenge/shared-artifacts/k8s_dashboard.html" in manifest
    assert "digest: /challenge/shared-artifacts/.advisor/" in manifest
    assert swarm.advisor_lane_hint_count == 0


def test_artifact_finding_deduplicates_and_ignores_generic_spill_files(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge"
    challenge_dir.mkdir()

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="Midnight Roulette", category="web"),
        ctfd=cast(Any, object()),
        cost_tracker=cast(Any, object()),
        settings=cast(Any, SimpleNamespace()),
        model_specs=["codex/gpt-5.4", "gemini/gemini-2.5-flash"],
    )
    shared_dir = challenge_dir / ".shared-artifacts"
    shared_dir.mkdir(parents=True, exist_ok=True)
    (shared_dir / "ffuf_hits_small.txt").write_text(
        "/admin/login 200\n/api/v1/k8s/get 200\n",
        encoding="utf-8",
    )
    meaningful_solver = _FakeSolver(
        model_spec="codex/gpt-5.4",
        sandbox=_FakeSandbox(),
        runtime_status={
            "lifecycle": "idle",
            "step_count": 20,
            "last_tool": "bash",
            "last_command": "sed -n '1,120p' /challenge/shared-artifacts/ffuf_hits_small.txt",
            "last_exit_hint": "status 200 /challenge/shared-artifacts/ffuf_hits_small.txt",
        },
    )
    duplicate_result = SolverResult(
        flag=None,
        status=GAVE_UP,
        findings_summary="Potential admin API at /api/v1/k8s/get",
        step_count=20,
        cost_usd=0.1,
        log_path="",
    )

    asyncio.run(swarm._maybe_share_artifact_finding("codex/gpt-5.4", meaningful_solver, duplicate_result))
    asyncio.run(swarm._maybe_share_artifact_finding("codex/gpt-5.4", meaningful_solver, duplicate_result))

    generic_solver = _FakeSolver(
        model_spec="codex/gpt-5.4",
        sandbox=_FakeSandbox(),
        runtime_status={
            "lifecycle": "idle",
            "step_count": 21,
            "last_tool": "bash",
            "last_command": "cat /challenge/shared-artifacts/stdout-123.log",
            "last_exit_hint": "HTTP 200 /challenge/shared-artifacts/stdout-123.log",
        },
    )
    asyncio.run(swarm._maybe_share_artifact_finding("codex/gpt-5.4", generic_solver, duplicate_result))

    unread = asyncio.run(swarm.message_bus.check("gemini/gemini-2.5-flash"))
    assert len(unread) == 1
    assert unread[0].content == "Artifact path: /challenge/shared-artifacts/ffuf_hits_small.txt"

    manifest_path = challenge_dir / ".shared-artifacts" / "manifest.md"
    manifest = manifest_path.read_text(encoding="utf-8")
    assert manifest.count("path: /challenge/shared-artifacts/ffuf_hits_small.txt") == 1
    assert "digest: /challenge/shared-artifacts/.advisor/" in manifest
    assert "stdout-123.log" not in manifest


def test_artifact_finding_shares_low_signal_facts_and_no_longer_caps_per_model(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge"
    challenge_dir.mkdir()

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="Midnight Roulette", category="web"),
        ctfd=cast(Any, object()),
        cost_tracker=cast(Any, object()),
        settings=cast(Any, SimpleNamespace()),
        model_specs=["codex/gpt-5.4", "gemini/gemini-2.5-flash"],
    )

    low_signal_solver = _FakeSolver(
        model_spec="codex/gpt-5.4",
        sandbox=_FakeSandbox(),
        runtime_status={
            "lifecycle": "idle",
            "step_count": 10,
            "last_tool": "bash",
            "last_command": "sed -n '1,120p' /challenge/shared-artifacts/page_head.html",
            "last_exit_hint": "meta viewport /challenge/shared-artifacts/page_head.html",
        },
    )
    low_signal_result = SolverResult(
        flag=None,
        status=GAVE_UP,
        findings_summary="meta viewport found in page head",
        step_count=10,
        cost_usd=0.1,
        log_path="",
    )
    asyncio.run(swarm._maybe_share_artifact_finding("codex/gpt-5.4", low_signal_solver, low_signal_result))
    initial_unread = asyncio.run(swarm.message_bus.check("gemini/gemini-2.5-flash"))
    assert len(initial_unread) == 1
    assert initial_unread[0].content == "Artifact path: /challenge/shared-artifacts/page_head.html"

    for idx in range(4):
        high_signal_solver = _FakeSolver(
            model_spec="codex/gpt-5.4",
            sandbox=_FakeSandbox(),
            runtime_status={
                "lifecycle": "idle",
                "step_count": 20 + idx,
                "last_tool": "bash",
                "last_command": f"sed -n '1,120p' /challenge/shared-artifacts/admin_api_{idx}.txt",
                "last_exit_hint": f"admin api token /challenge/shared-artifacts/admin_api_{idx}.txt",
            },
        )
        high_signal_result = SolverResult(
            flag=None,
            status=GAVE_UP,
            findings_summary=f"admin api token found {idx}",
            step_count=20 + idx,
            cost_usd=0.1,
            log_path="",
        )
        asyncio.run(swarm._maybe_share_artifact_finding("codex/gpt-5.4", high_signal_solver, high_signal_result))

    unread = asyncio.run(swarm.message_bus.check("gemini/gemini-2.5-flash"))
    assert len(unread) == 4


def test_artifact_finding_accepts_useful_non_high_signal_fact(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge"
    challenge_dir.mkdir()

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="Midnight Roulette", category="web"),
        ctfd=cast(Any, object()),
        cost_tracker=cast(Any, object()),
        settings=cast(Any, SimpleNamespace()),
        model_specs=["codex/gpt-5.4", "gemini/gemini-2.5-flash"],
    )
    solver = _FakeSolver(
        model_spec="codex/gpt-5.4",
        sandbox=_FakeSandbox(),
        runtime_status={
            "lifecycle": "idle",
            "step_count": 16,
            "last_tool": "bash",
            "last_command": "sed -n '1,120p' /challenge/shared-artifacts/app_bundle.js",
            "last_exit_hint": "JavaScript bundle references challenge slug /challenge/shared-artifacts/app_bundle.js",
        },
    )
    result = SolverResult(
        flag=None,
        status=GAVE_UP,
        findings_summary="JavaScript bundle references challenge slug",
        step_count=16,
        cost_usd=0.1,
        log_path="",
    )

    asyncio.run(swarm._maybe_share_artifact_finding("codex/gpt-5.4", solver, result))

    unread = asyncio.run(swarm.message_bus.check("gemini/gemini-2.5-flash"))
    assert len(unread) == 1
    assert unread[0].content == "Artifact path: /challenge/shared-artifacts/app_bundle.js"


def test_live_artifact_monitor_shares_idle_lane_runtime_paths(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge"
    challenge_dir.mkdir()

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="Midnight Roulette", category="web"),
        ctfd=cast(Any, object()),
        cost_tracker=cast(Any, object()),
        settings=cast(Any, SimpleNamespace()),
        model_specs=["codex/gpt-5.4", "gemini/gemini-2.5-flash"],
    )
    swarm.solvers["codex/gpt-5.4"] = _FakeSolver(
        model_spec="codex/gpt-5.4",
        sandbox=_FakeSandbox(),
        runtime_status={
            "lifecycle": "idle",
            "step_count": 25,
            "last_tool": "bash",
            "last_command": "sed -n '1,120p' /challenge/shared-artifacts/k8s_dashboard.html",
            "last_exit_hint": "k8s dashboard login page /challenge/shared-artifacts/k8s_dashboard.html",
        },
    )

    async def _run_monitor() -> None:
        task = asyncio.create_task(swarm._monitor_live_artifact_sharing())
        await asyncio.sleep(0.1)
        swarm.cancel_event.set()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_run_monitor())

    unread = asyncio.run(swarm.message_bus.check("gemini/gemini-2.5-flash"))
    assert len(unread) == 1
    assert unread[0].content == "Artifact path: /challenge/shared-artifacts/k8s_dashboard.html"


def test_lane_digest_updates_only_on_change(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge"
    challenge_dir.mkdir()

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="Midnight Roulette", category="web"),
        ctfd=cast(Any, object()),
        cost_tracker=cast(Any, object()),
        settings=cast(Any, SimpleNamespace()),
        model_specs=["codex/gpt-5.4", "gemini/gemini-2.5-flash"],
    )
    codex_solver = _FakeSolver(
        model_spec="codex/gpt-5.4",
        sandbox=_FakeSandbox(),
        runtime_status={"lifecycle": "idle", "step_count": 12},
    )
    gemini_solver = _FakeSolver(
        model_spec="gemini/gemini-2.5-flash",
        sandbox=_FakeSandbox(),
        runtime_status={"lifecycle": "idle", "step_count": 12},
    )
    swarm.solvers["codex/gpt-5.4"] = codex_solver
    swarm.solvers["gemini/gemini-2.5-flash"] = gemini_solver

    login_path = challenge_dir / ".shared-artifacts" / "login.html"
    login_path.parent.mkdir(parents=True, exist_ok=True)
    login_path.write_text("<html><form><input name='csrf' value='token-123'></form></html>\n", encoding="utf-8")

    async def _run() -> None:
        await swarm.message_bus.post(
            "codex/gpt-5.4",
            SharedFindingRef(
                model="codex/gpt-5.4",
                content="Artifact path: /challenge/shared-artifacts/login.html",
            ),
        )
        await swarm._maybe_issue_lane_digest_updates()
        await swarm._maybe_issue_lane_digest_updates()
        login_path.write_text(
            "<html><form><input name='csrf' value='token-456'></form><script>fetch('/api/auth')</script></html>\n",
            encoding="utf-8",
        )
        await swarm._maybe_issue_lane_digest_updates()

    asyncio.run(_run())

    assert len(codex_solver.bumped) == 2
    assert len(gemini_solver.bumped) == 2
    assert "/challenge/shared-artifacts/.advisor/login.html-" in codex_solver.bumped[0]
    assert "Prefer digest, then manifest, then the raw artifact." in codex_solver.bumped[0]
    assert "/challenge/shared-artifacts/.advisor/login.html-" in codex_solver.bumped[1]
    digest_files = list((challenge_dir / ".shared-artifacts" / ".advisor").glob("login.html-*.digest.md"))
    assert len(digest_files) == 1
    digest_text = digest_files[0].read_text(encoding="utf-8")
    assert "# Artifact Digest" in digest_text
    assert "token-456" in digest_text
    assert "/api/auth" in digest_text


def test_requeue_resume_packets_restore_into_next_swarm_run(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge"
    challenge_dir.mkdir()
    model_spec = "codex/gpt-5.4"
    result_store = {
        "Resume Me": {
            "status": "pending",
            "resume_packets": {
                model_spec: "Previous challenge run was paused before completion. Continue from the prior work.",
            },
        }
    }

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="Resume Me", category="web"),
        ctfd=cast(Any, object()),
        cost_tracker=cast(Any, object()),
        settings=cast(Any, SimpleNamespace()),
        result_store=result_store,
        model_specs=[model_spec],
    )

    solver = _QueuedSolver(
        model_spec=model_spec,
        sandbox=_FakeSandbox(),
        runtime_status={"lifecycle": "idle", "step_count": 9},
        results=[
            SolverResult(
                flag=None,
                status=CANCELLED,
                findings_summary="paused by operator",
                step_count=9,
                cost_usd=0.0,
                log_path="",
            )
        ],
    )

    def _fake_create_solver(spec: str, *, sandbox=None, initial_step_count: int = 0):
        assert spec == model_spec
        return solver

    swarm._create_solver = cast(Any, _fake_create_solver)

    result = asyncio.run(swarm._run_solver(model_spec))

    assert result is not None
    assert solver.started == 1
    assert solver.bumped == ["Previous challenge run was paused before completion. Continue from the prior work."]
    assert model_spec not in swarm._resume_packets


def test_swarm_runtime_state_tracks_warm_container_ids(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge"
    challenge_dir.mkdir()
    model_spec = "codex/gpt-5.4"

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="Warm Resume", category="web"),
        ctfd=cast(Any, object()),
        cost_tracker=cast(Any, object()),
        settings=cast(Any, SimpleNamespace()),
        model_specs=[model_spec],
    )
    solver = _FakeSolver(
        model_spec=model_spec,
        sandbox=SimpleNamespace(resume_container_id="warm-abc123"),
        runtime_status={"lifecycle": "idle", "step_count": 4},
    )
    swarm.solvers[model_spec] = solver

    payload = swarm._runtime_result_payload()

    assert payload["warm_container_ids"] == {model_spec: "warm-abc123"}

    restored = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="Warm Resume", category="web"),
        ctfd=cast(Any, object()),
        cost_tracker=cast(Any, object()),
        settings=cast(Any, SimpleNamespace()),
        model_specs=[model_spec],
        result_store={"Warm Resume": payload},
    )

    assert restored._warm_container_ids == {model_spec: "warm-abc123"}
