"""Claude Code CLI solver lane — host-side alternative to codex-app-server.

Uses ``claude_agent_sdk.ClaudeSDKClient`` to run the Claude Code agent
against a Docker sandbox.  Exposes a custom MCP server so the agent can:

  • run shell commands inside the sandbox (``bash``)
  • read / write files in the sandbox workspace (``read_file``, ``write_file``)
  • notify the coordinator (``notify_coordinator``)
  • report a flag candidate (``report_flag_candidate``)
  • submit flag (``submit_flag``) — coordinator routes to platform or
    human-approval flow depending on ``no_submit`` / ``local_mode``.

This is a **minimal** implementation compared to the codex_solver — it
covers the SolverProtocol surface so the lane works end-to-end, and
relies on the outer swarm loop (operator bumps, interrupt-and-restart,
notify/report hooks) inherited from the shared plumbing.  No watchdog,
no compaction, no handoff-between-sessions machinery yet.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

from backend.config import Settings
from backend.cost_tracker import CostTracker
from backend.prompts import ChallengeMeta, build_lane_bump_prompt, build_prompt
from backend.sandbox import DockerSandbox
from backend.solver_base import (
    CANCELLED,
    ERROR,
    FLAG_CANDIDATE,
    FLAG_FOUND,
    GAVE_UP,
    QUOTA_ERROR,
    RETRY_SOON,
    LaneRuntimeStatus,
    SolverResult,
)
from backend.tracing import SolverTracer

logger = logging.getLogger(__name__)

CLAUDE_CODE_DEFAULT_MODEL = "claude-sonnet-4-6"


class ClaudeCodeSolver:
    """Host-side solver using the Claude Code CLI via claude_agent_sdk."""

    model_spec: str
    agent_name: str
    sandbox: DockerSandbox

    def __init__(
        self,
        *,
        model_spec: str,
        challenge_dir: str,
        meta: ChallengeMeta,
        cost_tracker: CostTracker,
        settings: Settings,
        cancel_event: asyncio.Event,
        no_submit: bool,
        local_mode: bool,
        report_flag_candidate_fn: Any = None,
        notify_coordinator: Any = None,
        initial_step_count: int = 0,
        sandbox: DockerSandbox | None = None,
        **_kwargs: Any,  # absorb extra kwargs for API compatibility
    ) -> None:
        self.model_spec = model_spec
        self.challenge_dir = challenge_dir
        self.meta = meta
        self.cost_tracker = cost_tracker
        self.settings = settings
        self.cancel_event = cancel_event
        self.no_submit = no_submit
        self.local_mode = local_mode
        self._report_flag_candidate_fn = report_flag_candidate_fn
        self._notify_coordinator = notify_coordinator
        if sandbox is None:
            raise ValueError("ClaudeCodeSolver requires a sandbox")
        self.sandbox = sandbox

        parts = model_spec.split("/", 2)
        self.model_id = parts[1] if len(parts) >= 2 else CLAUDE_CODE_DEFAULT_MODEL
        self.agent_name = f"{meta.name}/{model_spec}"
        self.tracer = SolverTracer(meta.name, self.model_id)
        self._runtime = LaneRuntimeStatus()
        self._step_count = initial_step_count
        self._cost_usd = 0.0
        self._client: ClaudeSDKClient | None = None
        self._bump_insights: str | None = None
        self._advisory_bump_insights: str | None = None
        self._operator_bump_insights: str | None = None
        self._was_interrupted_for_operator_bump = False
        self._flag: str | None = None
        self._confirmed = False
        self._findings = ""
        self._pending_candidate: dict[str, Any] | None = None

    # ── SolverProtocol: lifecycle ─────────────────────────────────────
    async def start(self) -> None:
        await self.sandbox.start()
        self._runtime.mark_ready()

    async def stop(self) -> None:
        await self.stop_process()
        try:
            await self.sandbox.stop()
        except Exception:  # noqa: BLE001
            pass

    async def stop_process(self) -> None:
        self.tracer.close()
        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    def mark_terminal_status(self, status: str) -> None:
        from backend.solver_base import lifecycle_for_result
        self._runtime.mark_terminal(lifecycle_for_result(status), status)

    def get_runtime_status(self) -> dict[str, object]:
        snap = self._runtime.snapshot()
        snap.update({
            "provider": "claude-sdk",
            "runtime_health": "healthy",
            "step_count": self._step_count,
        })
        return snap

    # ── SolverProtocol: bump / interrupt ──────────────────────────────
    def bump(self, insights: str) -> None:
        if self._bump_insights and insights not in self._bump_insights:
            self._bump_insights = f"{self._bump_insights}\n\n---\n\n{insights}"
        else:
            self._bump_insights = insights
        self.tracer.event("bump", source="auto", insights=insights[:500])

    def bump_advisory(self, insights: str) -> None:
        if self._advisory_bump_insights and insights not in self._advisory_bump_insights:
            self._advisory_bump_insights = f"{self._advisory_bump_insights}\n\n---\n\n{insights}"
        else:
            self._advisory_bump_insights = insights
        self.tracer.event("bump", source="auto", channel="advisory", insights=insights[:500])

    def bump_operator(self, insights: str) -> None:
        if self._operator_bump_insights and insights not in self._operator_bump_insights:
            self._operator_bump_insights = f"{self._operator_bump_insights}\n\n---\n\n{insights}"
        else:
            self._operator_bump_insights = insights
        self._advisory_bump_insights = None
        self._bump_insights = None
        self.tracer.event("bump", source="operator", insights=insights[:500])

    def interrupt_and_bump_operator(self, insights: str) -> None:
        """Instant operator interrupt — see CodexSolver for full context.

        For Claude SDK we don't have a subprocess to terminate; instead we
        mark the flag and let the next turn pick up the bump immediately.
        Session is client-side so there's no in-flight tool call to abort.
        """
        if self._operator_bump_insights and insights not in self._operator_bump_insights:
            self._operator_bump_insights = f"{self._operator_bump_insights}\n\n---\n\n{insights}"
        else:
            self._operator_bump_insights = insights
        self._advisory_bump_insights = None
        self._bump_insights = None
        self._was_interrupted_for_operator_bump = True
        self.tracer.event("bump", source="operator", channel="instant", insights=insights[:500])
        # Claude SDK doesn't have a long-running subprocess we can terminate;
        # the in-flight client.receive_response() loop will see the next
        # query() fire with the bump and respond.  The "interrupt" here is
        # really just a fast-path — no work loss because turns are shorter.

    # ── SolverProtocol: main loop ─────────────────────────────────────
    def _consume_turn_prompt(self) -> str:
        if self._operator_bump_insights:
            prompt = build_lane_bump_prompt(self._operator_bump_insights, operator=True)
            self._operator_bump_insights = None
            self._advisory_bump_insights = None
            self._bump_insights = None
            return prompt
        if self._advisory_bump_insights:
            prompt = build_lane_bump_prompt(self._advisory_bump_insights, advisory=True)
            self._advisory_bump_insights = None
            return prompt
        if self._bump_insights:
            prompt = build_lane_bump_prompt(self._bump_insights)
            self._bump_insights = None
            return prompt
        if self._step_count == 0:
            return "Solve this CTF challenge."
        return "Continue solving. Try a different approach."

    async def run_until_done_or_gave_up(self) -> SolverResult:
        if self._client is None:
            await self._spawn_client()

        # If the operator interrupted us, burn the flag, consume the bump,
        # and come back as RETRY_SOON so the swarm loop iterates.
        if self._was_interrupted_for_operator_bump:
            self._was_interrupted_for_operator_bump = False

        prompt_text = self._consume_turn_prompt()
        t0 = time.monotonic()
        assert self._client is not None
        try:
            await self._client.query(prompt_text)
            msg_count = 0
            assistant_text_parts: list[str] = []
            async for message in self._client.receive_response():
                msg_count += 1
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            assistant_text_parts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            self._step_count += 1
                elif isinstance(message, ResultMessage):
                    cost = float(getattr(message, "total_cost_usd", 0) or 0)
                    self._cost_usd += cost
            duration = time.monotonic() - t0
            self.tracer.event("turn_complete", duration=round(duration, 1), steps=self._step_count)

            # Update findings + check for flag candidate via pending_candidate
            if assistant_text_parts:
                self._findings = "\n".join(assistant_text_parts)[:2000]

            if self._pending_candidate is not None:
                candidate = self._pending_candidate
                self._pending_candidate = None
                if self._confirmed:
                    return self._result(FLAG_FOUND)
                return self._result(FLAG_CANDIDATE)

            if self._confirmed and self._flag:
                return self._result(FLAG_FOUND)
            return self._result(GAVE_UP)
        except asyncio.CancelledError:
            return self._result(CANCELLED)
        except Exception as exc:  # noqa: BLE001
            err_str = str(exc)
            logger.error(f"[{self.agent_name}] Error: {exc}", exc_info=True)
            self._findings = f"Error: {err_str}"
            self.tracer.event("error", error=err_str)
            if any(k in err_str.lower() for k in ("quota", "rate", "capacity", "usage")):
                return self._result(QUOTA_ERROR)
            return self._result(ERROR)

    # ── MCP server + client setup ─────────────────────────────────────
    async def _spawn_client(self) -> None:
        """Build the Claude SDK client with our sandbox-aware MCP tools."""
        container_arch = "x86_64"
        try:
            arch_result = await self.sandbox.exec("uname -m", timeout_s=10)
            container_arch = (arch_result.stdout or "").strip() or "x86_64"
        except Exception:  # noqa: BLE001
            pass

        from backend.prompts import list_distfiles
        distfile_names = list_distfiles(self.challenge_dir)
        system_prompt = build_prompt(
            self.meta, distfile_names, container_arch=container_arch,
            has_named_tools=True,
        )

        mcp_server = self._build_mcp_server()

        options = ClaudeAgentOptions(
            model=self.model_id,
            system_prompt=system_prompt,
            mcp_servers={"ctf": mcp_server},
            allowed_tools=[
                "mcp__ctf__bash",
                "mcp__ctf__read_file",
                "mcp__ctf__write_file",
                "mcp__ctf__notify_coordinator",
                "mcp__ctf__report_flag_candidate",
                "mcp__ctf__submit_flag",
            ],
            permission_mode="bypassPermissions",
            env={"CLAUDECODE": ""},
        )
        self._client = ClaudeSDKClient(options=options)
        await self._client.__aenter__()
        logger.info(f"[{self.agent_name}] Claude SDK client ready (model={self.model_id})")

    def _build_mcp_server(self):
        solver = self

        @tool(
            "bash",
            "Execute a shell command inside the challenge sandbox.  Returns combined stdout+stderr + exit code.",
            {"command": str, "timeout_s": int},
        )
        async def bash_tool(args: dict) -> dict:
            cmd = str(args.get("command") or "")
            timeout = int(args.get("timeout_s") or 60)
            solver._step_count += 1
            solver._runtime.mark_busy("bash", cmd[:160], step_count=solver._step_count)
            try:
                result = await solver.sandbox.exec(cmd, timeout_s=timeout)
                out = (result.stdout or "") + (result.stderr or "")
                exit_code = getattr(result, "exit_code", 0)
                solver._runtime.mark_idle(f"exit {exit_code}")
                return {"content": [{"type": "text", "text": f"exit={exit_code}\n{out[:8000]}"}]}
            except Exception as exc:  # noqa: BLE001
                solver._runtime.mark_idle(f"error: {exc}")
                return {"content": [{"type": "text", "text": f"error: {exc}"}]}

        @tool(
            "read_file",
            "Read a file from the challenge sandbox workspace.",
            {"path": str},
        )
        async def read_file_tool(args: dict) -> dict:
            path = str(args.get("path") or "")
            try:
                result = await solver.sandbox.exec(f"cat {path!r}", timeout_s=30)
                return {"content": [{"type": "text", "text": (result.stdout or "")[:16_000]}]}
            except Exception as exc:  # noqa: BLE001
                return {"content": [{"type": "text", "text": f"error: {exc}"}]}

        @tool(
            "write_file",
            "Write text to a file in the sandbox workspace.",
            {"path": str, "content": str},
        )
        async def write_file_tool(args: dict) -> dict:
            path = str(args.get("path") or "")
            content = str(args.get("content") or "")
            import base64 as _b64
            encoded = _b64.b64encode(content.encode()).decode()
            cmd = f"mkdir -p $(dirname {path!r}) && echo {encoded} | base64 -d > {path!r}"
            try:
                result = await solver.sandbox.exec(cmd, timeout_s=30)
                exit_code = getattr(result, "exit_code", 0)
                return {"content": [{"type": "text", "text": f"wrote {len(content)} bytes to {path} (exit={exit_code})"}]}
            except Exception as exc:  # noqa: BLE001
                return {"content": [{"type": "text", "text": f"error: {exc}"}]}

        @tool(
            "notify_coordinator",
            "Send a status note to the coordinator (visible in the human UI Reports tab).",
            {"message": str},
        )
        async def notify_tool(args: dict) -> dict:
            msg = str(args.get("message") or "")
            if solver._notify_coordinator:
                try:
                    await solver._notify_coordinator(msg)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"notify_coordinator failed: {exc}")
            return {"content": [{"type": "text", "text": "delivered"}]}

        @tool(
            "report_flag_candidate",
            "Report a candidate flag for coordinator review.  Supply evidence + confidence.",
            {"flag": str, "evidence": str, "confidence": str},
        )
        async def candidate_tool(args: dict) -> dict:
            flag = str(args.get("flag") or "").strip()
            evidence = str(args.get("evidence") or "")[:1000]
            confidence = str(args.get("confidence") or "medium")
            if not flag:
                return {"content": [{"type": "text", "text": "empty flag"}]}
            solver._pending_candidate = {"flag": flag, "evidence": evidence, "confidence": confidence}
            if solver._report_flag_candidate_fn:
                try:
                    ack = await solver._report_flag_candidate_fn(
                        flag, evidence, confidence, solver._step_count, str(solver.tracer.path),
                    )
                    if "CORRECT" in ack or "ALREADY SOLVED" in ack:
                        solver._flag = flag
                        solver._confirmed = True
                    return {"content": [{"type": "text", "text": ack[:2000]}]}
                except Exception as exc:  # noqa: BLE001
                    return {"content": [{"type": "text", "text": f"error: {exc}"}]}
            return {"content": [{"type": "text", "text": "candidate recorded"}]}

        @tool(
            "submit_flag",
            "Submit a flag directly (alias for report_flag_candidate with high confidence).",
            {"flag": str},
        )
        async def submit_tool(args: dict) -> dict:
            return await candidate_tool({"flag": args.get("flag", ""), "evidence": "direct submit", "confidence": "high"})

        return create_sdk_mcp_server(
            name="ctf",
            version="1.0.0",
            tools=[bash_tool, read_file_tool, write_file_tool, notify_tool, candidate_tool, submit_tool],
        )

    def _result(self, status: str) -> SolverResult:
        self.tracer.event("finish", status=status, flag=self._flag, confirmed=self._confirmed)
        return SolverResult(
            flag=self._flag,
            status=status,
            findings_summary=self._findings[:2000],
            step_count=self._step_count,
            cost_usd=self._cost_usd,
            log_path=str(self.tracer.path),
            candidate_flag=self._pending_candidate.get("flag") if self._pending_candidate else None,
            candidate_evidence=self._pending_candidate.get("evidence", "") if self._pending_candidate else "",
            candidate_confidence=self._pending_candidate.get("confidence", "") if self._pending_candidate else "",
        )
