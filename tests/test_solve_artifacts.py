from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from backend.agents.coordinator_loop import build_deps
from backend.agents.swarm import ChallengeSwarm, FlagCandidateRecord
from backend.config import Settings
from backend.cost_tracker import CostTracker
from backend.prompts import ChallengeMeta
from backend.solver_base import FLAG_FOUND, SolverResult


class _FakeSandbox:
    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = str(workspace_dir)


class _FakeSolver:
    def __init__(self, workspace_dir: Path) -> None:
        self.sandbox = _FakeSandbox(workspace_dir)
        self.process_stopped = 0

    async def stop_process(self) -> None:
        self.process_stopped += 1


def test_persist_solved_artifacts_writes_workspace_trace_and_writeup(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge-a"
    challenge_dir.mkdir()

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "exploit.py").write_text("print('pwnd')\n", encoding="utf-8")

    shared_dir = challenge_dir / ".shared-artifacts"
    shared_dir.mkdir()
    (shared_dir / "artifact.txt").write_text("artifact\n", encoding="utf-8")

    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "tool_call",
                        "tool": "bash",
                        "step": 1,
                        "args": "python3 exploit.py",
                    }
                ),
                json.dumps(
                    {
                        "type": "tool_call",
                        "tool": "web_fetch",
                        "step": 2,
                        "args": "https://example.test/flag",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="challenge-a", category="web", value=100),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
    )
    result = SolverResult(
        flag="flag{done}",
        status=FLAG_FOUND,
        findings_summary="Used the hidden admin route to recover the flag.",
        step_count=7,
        cost_usd=1.5,
        log_path=str(trace_path),
    )
    swarm.last_advisor_note = "Verify the hidden admin route before final submit."

    asyncio.run(
        swarm._persist_solved_artifacts(
            model_spec="codex/gpt-5.4",
            solver=cast(Any, _FakeSolver(workspace_dir)),
            result=result,
        )
    )

    solve_dir = challenge_dir / "solve"
    assert (solve_dir / "flag.txt").read_text(encoding="utf-8").strip() == "flag{done}"
    assert (solve_dir / "workspace" / "exploit.py").exists()
    assert (solve_dir / "trace.jsonl").exists()

    result_payload = json.loads((solve_dir / "result.json").read_text(encoding="utf-8"))
    assert result_payload["winner_model"] == "codex/gpt-5.4"
    assert result_payload["flag"] == "flag{done}"
    assert result_payload["step_count"] == 7
    assert result_payload["advisor_note"] == "Verify the hidden admin route before final submit."
    assert result_payload["shared_findings"] == {}
    assert result_payload["workspace_path"].endswith("/solve/workspace")
    assert result_payload["shared_artifacts_path"].endswith("/.shared-artifacts")

    writeup = (solve_dir / "writeup.md").read_text(encoding="utf-8")
    assert "flag{done}" in writeup
    assert "Winner model: codex/gpt-5.4" in writeup
    assert "Used the hidden admin route" in writeup
    assert "step 2: web_fetch https://example.test/flag" in writeup


def test_build_deps_restores_saved_results(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge-a"
    challenge_dir.mkdir()
    (challenge_dir / "metadata.yml").write_text(
        "\n".join(
            [
                "name: challenge-a",
                "category: web",
                "value: 100",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    solve_dir = challenge_dir / "solve"
    solve_dir.mkdir()
    (solve_dir / "result.json").write_text(
        json.dumps(
            {
                "status": "flag_found",
                "flag": "flag{restored}",
                "step_count": 9,
                "winner_model": "codex/gpt-5.4",
            }
        ),
        encoding="utf-8",
    )

    _, _, deps = build_deps(Settings(), challenges_root=str(tmp_path))

    assert deps.results["challenge-a"]["flag"] == "flag{restored}"
    assert deps.results["challenge-a"]["step_count"] == 9
    assert deps.challenge_dirs["challenge-a"] == str(challenge_dir)


@pytest.mark.asyncio
async def test_candidate_snapshot_persists_and_restores_after_restart(
    tmp_path: Path,
) -> None:
    challenge_dir = tmp_path / "challenge-a"
    challenge_dir.mkdir()
    result_store: dict[str, dict[str, object]] = {}

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="challenge-a", category="web", value=100),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        result_store=result_store,
        model_specs=["codex/gpt-5.4"],
        no_submit=True,
    )

    await swarm.report_flag_candidate(
        "flag{candidate}",
        "codex/gpt-5.4",
        evidence="matched hidden admin route",
        confidence="high",
        step_count=12,
        trace_path="/tmp/trace.jsonl",
    )

    result_path = challenge_dir / "solve" / "result.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["status"] == "candidate_pending"
    assert payload["flag_candidates"]["flag{candidate}"]["status"] == "pending"
    assert payload["flag_candidates"]["flag{candidate}"]["evidence_digest_paths"]["codex/gpt-5.4"].startswith(
        "/challenge/shared-artifacts/.advisor/"
    )
    assert payload["flag_candidates"]["flag{candidate}"]["evidence_pointer_paths"]["codex/gpt-5.4"].startswith(
        "/challenge/shared-artifacts/"
    )

    restored = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="challenge-a", category="web", value=100),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        result_store=result_store,
        model_specs=["codex/gpt-5.4"],
        no_submit=True,
    )

    assert restored.flag_candidates["flag{candidate}"].status == "pending"

    await restored._resume_pending_candidate_reviews()
    assert restored.flag_candidates["flag{candidate}"].status == "pending"


@pytest.mark.asyncio
async def test_manual_candidate_approval_persists_solved_artifacts(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge-local"
    challenge_dir.mkdir()
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "exploit.sh").write_text("#!/bin/sh\necho win\n", encoding="utf-8")
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(
        json.dumps({"type": "tool_call", "tool": "bash", "step": 3, "args": "./exploit.sh"}) + "\n",
        encoding="utf-8",
    )

    result_store: dict[str, dict[str, object]] = {}
    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="challenge-local", category="pwn", value=300),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        result_store=result_store,
        model_specs=["codex/gpt-5.4"],
        no_submit=True,
    )
    swarm.solvers["codex/gpt-5.4"] = cast(Any, _FakeSolver(workspace_dir))
    swarm.flag_candidates["flag{local}"] = FlagCandidateRecord(
        normalized_flag="flag{local}",
        raw_flag="flag{local}",
        status="pending",
        source_models={"codex/gpt-5.4"},
        step_counts={"codex/gpt-5.4": 13},
        trace_paths={"codex/gpt-5.4": str(trace_path)},
        evidence_snippets=["matched local exploit chain"],
    )

    display = await swarm.approve_flag_candidate("flag{local}", approved_by="operator_manual")

    assert display.startswith('USER CONFIRMED MANUALLY — "flag{local}" marked solved without CTFd confirmation.')
    assert swarm.confirmed_flag == "flag{local}"
    assert swarm.winner_confirmation_source == "operator_manual"
    assert swarm.flag_candidates["flag{local}"].confirmation_source == "operator_manual"

    result_payload = json.loads((challenge_dir / "solve" / "result.json").read_text(encoding="utf-8"))
    assert result_payload["status"] == "flag_found"
    assert result_payload["flag"] == "flag{local}"
    assert result_payload["confirmation_source"] == "operator_manual"
    assert result_payload["flag_candidates"]["flag{local}"]["status"] == "confirmed"
    assert result_payload["flag_candidates"]["flag{local}"]["confirmation_source"] == "operator_manual"
    assert (challenge_dir / "solve" / "workspace" / "exploit.sh").exists()
    assert (challenge_dir / "solve" / "flag.txt").read_text(encoding="utf-8").strip() == "flag{local}"
    assert cast(_FakeSolver, swarm.solvers["codex/gpt-5.4"]).process_stopped == 1


@pytest.mark.asyncio
async def test_external_solve_persists_solved_artifacts(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge-external"
    challenge_dir.mkdir()
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "manual.sh").write_text("#!/bin/sh\necho external\n", encoding="utf-8")

    result_store: dict[str, dict[str, object]] = {}
    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="challenge-external", category="misc", value=150),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        result_store=result_store,
        model_specs=["codex/gpt-5.4"],
        no_submit=False,
    )
    swarm.solvers["codex/gpt-5.4"] = cast(Any, _FakeSolver(workspace_dir))

    display = await swarm.mark_solved_externally(
        "flag{external}",
        note="Solved manually outside the lane runtime",
    )

    assert display.startswith('USER REPORTED EXTERNAL SOLVE — "flag{external}" marked solved from operator input.')
    assert swarm.confirmed_flag == "flag{external}"
    assert swarm.winner_confirmation_source == "operator_external"
    assert swarm.flag_candidates["flag{external}"].confirmation_source == "operator_external"
    assert swarm.flag_candidates["flag{external}"].status == "confirmed"
    assert "Solved manually outside the lane runtime" in swarm.flag_candidates["flag{external}"].evidence_snippets

    result_payload = json.loads((challenge_dir / "solve" / "result.json").read_text(encoding="utf-8"))
    assert result_payload["status"] == "flag_found"
    assert result_payload["flag"] == "flag{external}"
    assert result_payload["confirmation_source"] == "operator_external"
    assert (challenge_dir / "solve" / "workspace" / "manual.sh").exists()
    assert (challenge_dir / "solve" / "flag.txt").read_text(encoding="utf-8").strip() == "flag{external}"
    assert cast(_FakeSolver, swarm.solvers["codex/gpt-5.4"]).process_stopped == 1


@pytest.mark.asyncio
async def test_local_candidate_rejection_marks_candidate_rejected(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge-local"
    challenge_dir.mkdir()
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()

    result_store: dict[str, dict[str, object]] = {}
    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="challenge-local", category="pwn", value=300),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        result_store=result_store,
        model_specs=["codex/gpt-5.4"],
        no_submit=True,
        local_mode=True,
    )
    swarm.solvers["codex/gpt-5.4"] = cast(Any, _FakeSolver(workspace_dir))
    swarm.flag_candidates["flag{local}"] = FlagCandidateRecord(
        normalized_flag="flag{local}",
        raw_flag="flag{local}",
        status="pending",
        source_models={"codex/gpt-5.4"},
        step_counts={"codex/gpt-5.4": 13},
        evidence_snippets=["matched local exploit chain"],
    )

    display = await swarm.reject_flag_candidate("flag{local}")

    assert display.startswith('USER REJECTED — "flag{local}" dismissed in local mode.')
    assert swarm.confirmed_flag is None
    assert swarm.flag_candidates["flag{local}"].status == "rejected"
    assert swarm.flag_candidates["flag{local}"].confirmation_source == "operator_local"


@pytest.mark.asyncio
async def test_candidate_placeholder_is_rejected_before_queueing(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge-local"
    challenge_dir.mkdir()
    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="challenge-local", category="pwn", value=300),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        result_store={},
        model_specs=["codex/gpt-5.4"],
        no_submit=True,
        local_mode=True,
    )

    display = await swarm.report_flag_candidate("DH{fake_flag}", "codex/gpt-5.4")

    assert display == "Flag candidate rejected: placeholder sentinel."
    assert swarm.flag_candidates == {}


@pytest.mark.asyncio
async def test_candidate_reflect_sentinel_is_rejected_before_queueing(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge-local"
    challenge_dir.mkdir()
    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="challenge-local", category="pwn", value=300),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        result_store={},
        model_specs=["codex/gpt-5.4"],
        no_submit=True,
        local_mode=True,
    )

    display = await swarm.report_flag_candidate("REFLECT", "codex/gpt-5.4")

    assert display == "Flag candidate rejected: placeholder sentinel."
    assert swarm.flag_candidates == {}


@pytest.mark.asyncio
async def test_candidate_ctf_placeholder_body_is_rejected_before_queueing(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge-local"
    challenge_dir.mkdir()
    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="challenge-local", category="pwn", value=300),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        result_store={},
        model_specs=["codex/gpt-5.4"],
        no_submit=True,
        local_mode=True,
    )

    display = await swarm.report_flag_candidate("CTF{flag}", "codex/gpt-5.4")

    assert display == "Flag candidate rejected: placeholder sentinel."
    assert swarm.flag_candidates == {}


@pytest.mark.asyncio
async def test_candidate_realistic_flag_with_placeholder_token_is_not_rejected(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge-local"
    challenge_dir.mkdir()
    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="challenge-local", category="pwn", value=300),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        result_store={},
        model_specs=["codex/gpt-5.4"],
        no_submit=True,
        local_mode=True,
    )

    display = await swarm.report_flag_candidate("flag{dummy_driver_bug}", "codex/gpt-5.4")

    assert 'Queued flag candidate "flag{dummy_driver_bug}"' in display
    assert "flag{dummy_driver_bug}" in swarm.flag_candidates


@pytest.mark.asyncio
async def test_shared_finding_persists_pointer_metadata(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge-b"
    challenge_dir.mkdir()
    result_store: dict[str, dict[str, object]] = {}
    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="challenge-b", category="web", value=100),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        result_store=result_store,
        model_specs=["codex/gpt-5.4"],
    )

    finding = swarm._make_finding_event(
        model_spec="codex/gpt-5.4",
        prefix="finding-challenge-b-codex-gpt-5.4",
        content="Potential admin API at /api/v1/k8s/get with token reuse behavior.",
    )
    swarm._record_shared_finding("codex/gpt-5.4", finding)
    await swarm._persist_runtime_state()

    payload = json.loads((challenge_dir / "solve" / "result.json").read_text(encoding="utf-8"))
    shared = payload["shared_findings"]["codex/gpt-5.4"]
    assert shared["kind"] == "finding_ref"
    assert shared["summary"].startswith("Potential admin API")
    assert shared["digest_path"].startswith("/challenge/shared-artifacts/.advisor/")
    assert shared["pointer_path"].startswith("/challenge/shared-artifacts/")
    assert payload["shared_finding"].startswith("Potential admin API")
    digest_host_path = challenge_dir / ".shared-artifacts" / ".advisor" / Path(shared["digest_path"]).name
    assert digest_host_path.exists()
