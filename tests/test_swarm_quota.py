from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from backend.agents.swarm import ChallengeSwarm
from backend.prompts import ChallengeMeta
from backend.solver_base import QUOTA_ERROR, SolverResult


class _QuotaSolver:
    def __init__(self, model_spec: str) -> None:
        self.model_spec = model_spec
        self.agent_name = "quota-solver"
        self.sandbox = object()
        self.started = 0
        self.bumped: list[str] = []

    async def start(self) -> None:
        self.started += 1

    async def run_until_done_or_gave_up(self) -> SolverResult:
        return SolverResult(
            flag=None,
            status=QUOTA_ERROR,
            findings_summary="quota exceeded",
            step_count=1,
            cost_usd=0.0,
            log_path="",
        )

    def bump(self, insights: str) -> None:
        self.bumped.append(insights)

    async def stop(self) -> None:
        return None


@pytest.mark.asyncio
async def test_quota_error_stops_lane_without_api_fallback(
    tmp_path,
) -> None:
    swarm = ChallengeSwarm(
        challenge_dir=str(tmp_path / "challenge"),
        meta=ChallengeMeta(name="quota-test"),
        ctfd=cast(Any, object()),
        cost_tracker=cast(Any, object()),
        settings=cast(Any, SimpleNamespace()),
        model_specs=[],
    )
    solver = _QuotaSolver("codex/gpt-5.3-codex-spark")

    result, final_solver = await swarm._run_solver_loop(solver, solver.model_spec)

    assert result.status == QUOTA_ERROR
    assert final_solver is solver
    assert solver.started == 1
    assert solver.bumped == []
