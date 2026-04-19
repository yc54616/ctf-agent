from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from backend.agents.advisor_base import CandidateReview
from backend.agents.coordinator_loop import build_deps
from backend.agents.swarm import ChallengeSwarm
from backend.config import Settings
from backend.cost_tracker import CostTracker
from backend.message_bus import CandidateRef
from backend.prompts import ChallengeMeta
from backend.solver_base import FLAG_FOUND, SolverResult


class _FakeSandbox:
    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = str(workspace_dir)


class _FakeSolver:
    def __init__(self, workspace_dir: Path) -> None:
        self.sandbox = _FakeSandbox(workspace_dir)


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    challenge_dir = tmp_path / "challenge-a"
    challenge_dir.mkdir()
    result_store: dict[str, dict[str, object]] = {}
    inbox: asyncio.Queue[object] = asyncio.Queue()

    class _Advisor:
        async def annotate_finding(self, **_: object) -> str:
            return ""

        async def annotate_coordinator_message(self, **_: object) -> str:
            return ""

        async def suggest_lane_hint(self, **_: object) -> str:
            return ""

        async def review_flag_candidate(self, **_: object) -> CandidateReview:
            return CandidateReview("likely", "evidence looks strong")

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="challenge-a", category="web", value=100),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        result_store=result_store,
        model_specs=["codex/gpt-5.4"],
        coordinator_inbox=inbox,
    )
    monkeypatch.setattr(swarm, "_get_advisor", lambda _model_spec: _Advisor())

    await swarm.report_flag_candidate(
        "flag{candidate}",
        "codex/gpt-5.4",
        evidence="matched hidden admin route",
        confidence="high",
        step_count=12,
        trace_path="/tmp/trace.jsonl",
    )
    for task in list(swarm._background_tasks):
        await task

    result_path = challenge_dir / "solve" / "result.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["status"] == "candidate_pending"
    assert payload["flag_candidates"]["flag{candidate}"]["status"] == "pending_coordinator"
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
        coordinator_inbox=inbox,
    )
    monkeypatch.setattr(restored, "_get_advisor", lambda _model_spec: _Advisor())

    assert restored.flag_candidates["flag{candidate}"].status == "pending_coordinator"

    await restored._resume_pending_candidate_reviews()
    for task in list(restored._background_tasks):
        await task

    queued = await inbox.get()
    assert isinstance(queued, CandidateRef)
    assert queued.flag == "flag{candidate}"
    assert queued.advisor_decision == "likely"
    assert queued.evidence_digest_paths["codex/gpt-5.4"].startswith("/challenge/shared-artifacts/.advisor/")


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
