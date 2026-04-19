"""Per-model solver agent — one model, one container, one challenge."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, cast

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_ai.toolsets.wrapper import WrapperToolset

from backend.config import Settings
from backend.cost_tracker import CostTracker
from backend.ctfd import CTFdClient
from backend.deps import SolverDeps
from backend.loop_detect import LOOP_WARNING_MESSAGE, LoopDetector
from backend.models import (
    model_id_from_spec,
    provider_from_spec,
    resolve_model,
    resolve_model_settings,
    supports_vision,
)
from backend.output_types import FlagCandidate
from backend.prompts import ChallengeMeta, build_lane_bump_prompt, build_prompt, list_distfiles
from backend.sandbox import DockerSandbox
from backend.solver_base import (
    CANCELLED,
    ERROR,
    FLAG_CANDIDATE,
    FLAG_FOUND,
    GAVE_UP,
    LaneRuntimeStatus,
    SolverResult,
    is_read_only_tool,
    lifecycle_for_result,
    summarize_tool_input,
)
from backend.tools.sandbox import (
    bash,
    fs_query,
    notify_coordinator,
    report_flag_candidate,
)
from backend.tools.vision import view_image
from backend.tracing import SolverTracer

logger = logging.getLogger(__name__)


@dataclass
class TracingToolset(WrapperToolset[SolverDeps]):
    """Wraps a toolset to add per-call tracing and loop detection."""

    tracer: SolverTracer = field(repr=False)
    loop_detector: LoopDetector = field(repr=False)
    step_counter: list[int] = field(repr=False)
    runtime: LaneRuntimeStatus = field(repr=False)

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[SolverDeps], tool: ToolsetTool[SolverDeps]
    ) -> Any:
        self.step_counter[0] += 1
        step = self.step_counter[0]
        self.runtime.mark_busy(name, summarize_tool_input(name, tool_args), step)

        self.tracer.tool_call(name, tool_args, step)

        # Loop detection
        loop_status = self.loop_detector.check(name, tool_args)
        if loop_status == "break":
            logger.warning(f"Loop break on {name} at step {step}")
            self.tracer.event("loop_break", tool=name, step=step)
            self.runtime.last_progress_kind = "exec_tool"
            self.runtime.read_only_streak = 0
            self.runtime.mark_idle(LOOP_WARNING_MESSAGE)
            # Inject loop warning by returning it as the tool result
            return LOOP_WARNING_MESSAGE

        result = await self.wrapped.call_tool(name, tool_args, ctx, tool)

        result_str = str(result) if result is not None else ""
        self.tracer.tool_result(name, result_str, step)
        if is_read_only_tool(name):
            self.runtime.read_only_streak += 1
            self.runtime.last_progress_kind = "read_only_tool"
        else:
            self.runtime.read_only_streak = 0
            self.runtime.last_progress_kind = "exec_tool"
        self.runtime.mark_idle(result_str)

        # Inject loop warning alongside result on "warn" level
        if loop_status == "warn":
            result = f"{result}\n\n{LOOP_WARNING_MESSAGE}" if isinstance(result, str) else result

        # Check for confirmed flag
        return result


def _build_toolset(deps: SolverDeps) -> FunctionToolset[SolverDeps]:
    """Build the raw toolset for a solver agent."""
    tools: list[Any] = [
        bash,
        fs_query,
        report_flag_candidate,
        notify_coordinator,
    ]
    if deps.use_vision:
        tools.append(view_image)
    return FunctionToolset(tools=tools, max_retries=4)


class Solver:
    """A single solver: one model, one container, one challenge."""

    def __init__(
        self,
        model_spec: str,
        challenge_dir: str,
        meta: ChallengeMeta,
        ctfd: CTFdClient,
        cost_tracker: CostTracker,
        settings: object,
        cancel_event: asyncio.Event | None = None,
        sandbox: DockerSandbox | None = None,
        owns_sandbox: bool | None = None,
    ) -> None:
        self.model_spec = model_spec
        self.model_id = model_id_from_spec(model_spec)
        self.challenge_dir = challenge_dir
        self.meta = meta
        self.ctfd = ctfd
        self.cost_tracker = cost_tracker
        self.settings = settings
        self.cancel_event = cancel_event or asyncio.Event()
        self._owns_sandbox = owns_sandbox if owns_sandbox is not None else (sandbox is None)

        self.sandbox = sandbox or DockerSandbox(
            image=getattr(settings, "sandbox_image", "ctf-sandbox"),
            challenge_dir=challenge_dir,
            memory_limit=getattr(settings, "container_memory_limit", "4g"),
            exec_output_spill_threshold_bytes=getattr(settings, "exec_output_spill_threshold_bytes", 65_536),
            read_file_spill_threshold_bytes=getattr(settings, "read_file_spill_threshold_bytes", 262_144),
            artifact_preview_bytes=getattr(settings, "artifact_preview_bytes", 8_192),
        )
        self.use_vision = supports_vision(model_spec)
        self.deps = SolverDeps(
            sandbox=self.sandbox,
            ctfd=ctfd,
            challenge_dir=challenge_dir,
            challenge_name=meta.name,
            workspace_dir="",
            use_vision=self.use_vision,
            cost_tracker=cost_tracker,
        )
        self.loop_detector = LoopDetector()
        self.tracer = SolverTracer(meta.name, self.model_id)
        self.agent_name = f"{meta.name}/{self.model_id}"
        self._runtime = LaneRuntimeStatus()
        self._agent: Agent[SolverDeps, FlagCandidate] | None = None
        self._messages: list = []
        self._step_count = [0]  # mutable ref shared with TracingToolset
        self._flag: str | None = None
        self._candidate_flag: str | None = None
        self._candidate_evidence: str = ""
        self._candidate_confidence: str = ""
        self._confirmed: bool = False
        self._findings: str = ""
        self._advisory_bump_insights: str | None = None
        self._run_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the sandbox and build the agent."""
        if not self.sandbox._container:
            await self.sandbox.start()
        self.deps.workspace_dir = self.sandbox.workspace_dir
        self.deps.runtime_status_getter = self.get_runtime_status
        self.deps.trace_path = self.tracer.path

        arch_result = await self.sandbox.exec("uname -m", timeout_s=10)
        container_arch = arch_result.stdout.strip() or "unknown"

        distfile_names = list_distfiles(self.challenge_dir)
        system_prompt = build_prompt(
            self.meta,
            distfile_names,
            container_arch=container_arch,
        )

        model = resolve_model(self.model_spec, cast(Settings, self.settings))
        model_settings = resolve_model_settings(self.model_spec)
        raw_toolset = _build_toolset(self.deps)
        toolset = TracingToolset(
            wrapped=raw_toolset,
            tracer=self.tracer,
            loop_detector=self.loop_detector,
            step_counter=self._step_count,
            runtime=self._runtime,
        )

        self._agent = cast(
            Agent[SolverDeps, FlagCandidate],
            Agent(
                model,
                deps_type=SolverDeps,
                system_prompt=system_prompt,
                model_settings=model_settings,
                toolsets=[toolset],
                output_type=FlagCandidate,
            ),
        )

        self.tracer.event("start", challenge=self.meta.name, model=self.model_id)
        self._runtime.mark_ready()
        logger.info(f"[{self.agent_name}] Solver started")

    async def run_until_done_or_gave_up(self) -> SolverResult:
        """Run the solver loop until flag found, gave up, or cancelled."""
        if not self._agent:
            await self.start()
        assert self._agent is not None

        t0 = time.monotonic()
        try:
            from pydantic_ai.usage import UsageLimits
            self._run_task = asyncio.create_task(
                self._agent.run(
                    "Solve this CTF challenge." if not self._messages else "Continue solving.",
                    deps=self.deps,
                    message_history=self._messages if self._messages else None,
                    usage_limits=UsageLimits(request_limit=None),
                )
            )
            cancel_wait = asyncio.create_task(self.cancel_event.wait())
            try:
                done, _ = await asyncio.wait(
                    {self._run_task, cancel_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_wait in done and self.cancel_event.is_set():
                    if self._run_task and not self._run_task.done():
                        self._run_task.cancel()
                        await asyncio.gather(self._run_task, return_exceptions=True)
                    self._runtime.mark_terminal(lifecycle_for_result(CANCELLED), "cancelled")
                    return self._result(CANCELLED)
                result = await self._run_task
            finally:
                cancel_wait.cancel()
                await asyncio.gather(cancel_wait, return_exceptions=True)
                self._run_task = None

            duration = time.monotonic() - t0
            usage = result.usage()

            self.cost_tracker.record(
                self.agent_name, usage, self.model_id,
                provider_spec=provider_from_spec(self.model_spec),
                duration_seconds=duration,
            )

            agent_usage = self.cost_tracker.by_agent.get(self.agent_name)
            self.tracer.usage(
                usage.input_tokens, usage.output_tokens,
                usage.cache_read_tokens,
                agent_usage.cost_usd if agent_usage else 0.0,
            )

            self._messages = result.all_messages()

            # Trace model responses from new messages
            from pydantic_ai.messages import ModelResponse, TextPart
            for msg in result.new_messages():
                if isinstance(msg, ModelResponse):
                    text_parts = [p.content for p in msg.parts if isinstance(p, TextPart)]
                    text = " ".join(text_parts)
                    msg_usage = msg.usage
                    self.tracer.model_response(
                        text[:500], self._step_count[0],
                        input_tokens=msg_usage.input_tokens if msg_usage else 0,
                        output_tokens=msg_usage.output_tokens if msg_usage else 0,
                    )

            output = result.output
            if isinstance(output, FlagCandidate):
                self._candidate_flag = output.flag
                self._candidate_evidence = output.method
                self._candidate_confidence = "medium"
                self._findings = f"Flag candidate via {output.method}: {output.flag}"
                if self.deps.report_flag_candidate_fn:
                    ack = await self.deps.report_flag_candidate_fn(
                        output.flag,
                        output.method,
                        self._candidate_confidence,
                        self._step_count[0],
                        self.tracer.path,
                    )
                    if ack:
                        self._findings = f"{self._findings}\n{ack}"[:2000]
                self._runtime.mark_terminal(lifecycle_for_result(FLAG_CANDIDATE), self._findings)
                return self._result(FLAG_CANDIDATE)
            # CTFd confirmation always counts (the primary path when not in dry-run)
            if self.deps.confirmed_flag:
                self._confirmed = True
                self._flag = self._flag or self.deps.confirmed_flag

            if self._confirmed and self._flag:
                self._runtime.mark_terminal(lifecycle_for_result(FLAG_FOUND), self._findings)
                return self._result(FLAG_FOUND)
            self._runtime.mark_terminal(lifecycle_for_result(GAVE_UP), self._findings)
            return self._result(GAVE_UP)

        except asyncio.CancelledError:
            self._runtime.mark_terminal(lifecycle_for_result(CANCELLED), "cancelled")
            return self._result(CANCELLED)
        except Exception as e:
            logger.error(f"[{self.agent_name}] Error: {e}", exc_info=True)
            self._findings = f"Error: {e}"
            self.tracer.event("error", error=str(e))
            self._runtime.mark_terminal(lifecycle_for_result(ERROR), self._findings)
            return self._result(ERROR)

    def bump(self, insights: str) -> None:
        """Inject insights from siblings and prepare to resume."""
        bump_msg = self._build_bump_message(insights)
        self._messages.append(bump_msg)
        self.loop_detector.reset()
        self.tracer.event("bump", source="auto", insights=insights[:500])
        logger.info(f"[{self.agent_name}] Bumped with sibling insights")

    def bump_advisory(self, insights: str) -> None:
        bump_msg = self._build_bump_message(insights, advisory=True)
        self._messages.append(bump_msg)
        self._advisory_bump_insights = insights
        self.loop_detector.reset()
        self.tracer.event("bump", source="auto", channel="advisory", insights=insights[:500])
        logger.info(f"[{self.agent_name}] Bumped with lane advisory")

    def bump_operator(self, insights: str) -> None:
        bump_msg = self._build_bump_message(insights, operator=True)
        self._messages.append(bump_msg)
        self._advisory_bump_insights = None
        self.loop_detector.reset()
        self.tracer.event("bump", source="operator", insights=insights[:500])
        logger.info(f"[{self.agent_name}] Bumped with operator guidance")

    @staticmethod
    def _build_bump_message(
        insights: str, *, operator: bool = False, advisory: bool = False
    ) -> ModelRequest:
        content = build_lane_bump_prompt(
            insights,
            operator=operator,
            advisory=advisory,
        )
        return ModelRequest(parts=[UserPromptPart(content=content)])

    def _result(self, status: str, run_steps: int | None = None, run_cost: float | None = None) -> SolverResult:
        agent_usage = self.cost_tracker.by_agent.get(self.agent_name)
        cost = agent_usage.cost_usd if agent_usage else 0.0
        self.tracer.event("finish", status=status, flag=self._flag, confirmed=self._confirmed, cost_usd=round(cost, 4))
        return SolverResult(
            flag=self._flag,
            status=status,
            findings_summary=self._findings[:2000],
            step_count=run_steps if run_steps is not None else self._step_count[0],
            cost_usd=run_cost if run_cost is not None else cost,
            log_path=self.tracer.path,
            candidate_flag=self._candidate_flag,
            candidate_evidence=self._candidate_evidence,
            candidate_confidence=self._candidate_confidence,
        )

    async def stop(self) -> None:
        self.tracer.event("stop", step_count=self._step_count[0])
        self.tracer.close()
        if self._owns_sandbox and self.sandbox:
            await self.sandbox.stop()

    def get_runtime_status(self) -> dict[str, object]:
        snapshot = self._runtime.snapshot()
        snapshot["step_count"] = self._step_count[0]
        return snapshot

    def mark_terminal_status(self, status: str) -> None:
        self._runtime.mark_terminal(lifecycle_for_result(status), self._findings)

    async def stop_process(self) -> None:
        self.cancel_event.set()
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
