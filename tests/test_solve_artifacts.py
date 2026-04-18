from __future__ import annotations

import asyncio
import json
from pathlib import Path

from backend.agents.coordinator_loop import build_deps
from backend.agents.swarm import ChallengeSwarm
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
        ctfd=object(),  # type: ignore[arg-type]
        cost_tracker=CostTracker(),
        settings=object(),  # type: ignore[arg-type]
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
            solver=_FakeSolver(workspace_dir),
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
