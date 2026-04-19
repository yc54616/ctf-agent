from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from backend.agents.solver import Solver, TracingToolset
from backend.cost_tracker import CostTracker
from backend.loop_detect import LoopDetector
from backend.prompts import ChallengeMeta
from backend.solver_base import CANCELLED, GAVE_UP, LaneRuntimeStatus
from backend.tracing import SolverTracer


class _FakeWrappedToolset:
    async def call_tool(self, name, tool_args, ctx, tool):
        return f"ok:{name}"


@pytest.mark.asyncio
async def test_tracing_toolset_updates_runtime_status_for_read_only_tool(tmp_path) -> None:
    runtime = LaneRuntimeStatus()
    tracer = SolverTracer("chal", "model")
    toolset = TracingToolset(
        wrapped=cast(Any, _FakeWrappedToolset()),
        tracer=tracer,
        loop_detector=LoopDetector(),
        step_counter=[0],
        runtime=runtime,
    )

    result = await toolset.call_tool(
        "fs_query",
        {"action": "find", "path": "/tmp"},
        cast(Any, None),
        cast(Any, None),
    )

    assert result == "ok:fs_query"
    snapshot = runtime.snapshot()
    assert snapshot["lifecycle"] == "idle"
    assert snapshot["step_count"] == 1
    assert snapshot["last_tool"] == "fs_query"
    assert snapshot["read_only_streak"] == 1
    assert snapshot["last_progress_kind"] == "read_only_tool"
    tracer.close()


def test_generic_solver_exposes_runtime_status_and_terminal_marking(tmp_path) -> None:
    solver = Solver(
        model_spec="openai/gpt-4.1-mini",
        challenge_dir=str(tmp_path),
        meta=ChallengeMeta(name="chal"),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=SimpleNamespace(sandbox_image="ctf-sandbox"),
        sandbox=cast(Any, object()),
        owns_sandbox=False,
    )
    solver._step_count[0] = 7

    initial = solver.get_runtime_status()
    assert initial["lifecycle"] == "starting"
    assert initial["step_count"] == 7

    solver.mark_terminal_status(GAVE_UP)

    final = solver.get_runtime_status()
    assert final["lifecycle"] == "finished"
    assert final["last_exit_hint"] == "finished"


class _SlowAgent:
    async def run(self, *args, **kwargs):
        await __import__("asyncio").sleep(60)


@pytest.mark.asyncio
async def test_generic_solver_stop_process_cancels_active_run(tmp_path) -> None:
    solver = Solver(
        model_spec="openai/gpt-4.1-mini",
        challenge_dir=str(tmp_path),
        meta=ChallengeMeta(name="chal"),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=SimpleNamespace(sandbox_image="ctf-sandbox"),
        sandbox=cast(Any, object()),
        owns_sandbox=False,
    )
    solver._agent = cast(Any, _SlowAgent())

    task = __import__("asyncio").create_task(solver.run_until_done_or_gave_up())
    await __import__("asyncio").sleep(0)
    await solver.stop_process()
    result = await task

    assert result.status == CANCELLED
    assert solver.get_runtime_status()["lifecycle"] == "cancelled"
