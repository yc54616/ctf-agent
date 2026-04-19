"""Claude Agent SDK coordinator — uses the shared event loop with a Claude SDK client."""

from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    create_sdk_mcp_server,
    tool,
)

from backend.agents.coordinator_core import (
    COORDINATOR_PROMPT,
    do_broadcast,
    do_bump_agent,
    do_check_swarm_status,
    do_fetch_challenges,
    do_get_solve_status,
    do_kill_swarm,
    do_read_solver_trace,
    do_spawn_swarm,
    do_submit_flag,
)
from backend.agents.coordinator_loop import build_deps, run_event_loop
from backend.config import Settings
from backend.cost_tracker import CostTracker
from backend.ctfd import CTFdClient
from backend.deps import CoordinatorDeps

logger = logging.getLogger(__name__)


class ClaudeCoordinatorInactiveError(RuntimeError):
    """Raised when Claude coordinator does not respond with any messages."""


COORDINATOR_PREFLIGHT_PROMPT = """\
Preflight readiness check only.
Call fetch_challenges and get_solve_status, then respond with one short readiness note.
Do not spawn, submit, kill, bump, or broadcast during this preflight.
"""


def _next_inactive_turn_count(
    *,
    msg_count: int,
    tool_calls_delta: int,
    previous_inactive_turns: int,
) -> int:
    if msg_count == 0:
        return previous_inactive_turns + 1
    if tool_calls_delta == 0:
        return previous_inactive_turns + 1
    return 0


def _validate_turn_activity(
    *,
    msg_count: int,
    tool_calls_delta: int,
    previous_inactive_turns: int,
    require_tool_action: bool = False,
) -> int:
    inactive_turns = _next_inactive_turn_count(
        msg_count=msg_count,
        tool_calls_delta=tool_calls_delta,
        previous_inactive_turns=previous_inactive_turns,
    )
    if msg_count == 0:
        raise ClaudeCoordinatorInactiveError("Claude coordinator produced no messages")
    if require_tool_action and tool_calls_delta == 0:
        raise ClaudeCoordinatorInactiveError("Claude coordinator preflight produced no tool actions")
    if inactive_turns >= 2:
        raise ClaudeCoordinatorInactiveError("Claude coordinator produced no tool actions")
    return inactive_turns


def _text(s: str) -> dict:
    """Wrap a string in the Claude SDK MCP tool return format."""
    return {"content": [{"type": "text", "text": s}]}


def _build_coordinator_mcp(deps: CoordinatorDeps, on_tool_call=None):
    """Build MCP server — thin wrappers around coordinator_core functions."""

    def _mark_tool_call() -> None:
        if on_tool_call is not None:
            on_tool_call()

    @tool("fetch_challenges", "List all challenges with category, points, solve count, and status.", {})
    async def fetch_challenges(args: dict) -> dict:
        _mark_tool_call()
        return _text(await do_fetch_challenges(deps))

    @tool("get_solve_status", "Check which challenges are solved and which swarms are running.", {})
    async def get_solve_status(args: dict) -> dict:
        _mark_tool_call()
        return _text(await do_get_solve_status(deps))

    @tool("spawn_swarm", "Launch all solver models on a challenge.", {"challenge_name": str})
    async def spawn_swarm(args: dict) -> dict:
        _mark_tool_call()
        return _text(await do_spawn_swarm(deps, args["challenge_name"]))

    @tool("check_swarm_status", "Get per-agent progress for a swarm.", {"challenge_name": str})
    async def check_swarm_status(args: dict) -> dict:
        _mark_tool_call()
        return _text(await do_check_swarm_status(deps, args["challenge_name"]))

    @tool("submit_flag", "Submit a flag to CTFd.", {"challenge_name": str, "flag": str})
    async def submit_flag(args: dict) -> dict:
        _mark_tool_call()
        return _text(await do_submit_flag(deps, args["challenge_name"], args["flag"]))

    @tool("kill_swarm", "Cancel all agents for a challenge.", {"challenge_name": str})
    async def kill_swarm(args: dict) -> dict:
        _mark_tool_call()
        return _text(await do_kill_swarm(deps, args["challenge_name"]))

    @tool("bump_agent", "Send targeted insights to a stuck agent.", {"challenge_name": str, "model_spec": str, "insights": str})
    async def bump_agent(args: dict) -> dict:
        _mark_tool_call()
        return _text(await do_bump_agent(deps, args["challenge_name"], args["model_spec"], args["insights"]))

    @tool("broadcast", "Broadcast a strategic hint to ALL solvers on a challenge.", {"challenge_name": str, "message": str})
    async def broadcast(args: dict) -> dict:
        _mark_tool_call()
        return _text(await do_broadcast(deps, args["challenge_name"], args["message"]))

    @tool("read_solver_trace", "Read recent trace events from a specific solver. Use this to understand what a solver is doing, what it tried, and where it's stuck.", {"challenge_name": str, "model_spec": str, "last_n": int})
    async def read_solver_trace(args: dict) -> dict:
        _mark_tool_call()
        return _text(await do_read_solver_trace(deps, args["challenge_name"], args["model_spec"], args.get("last_n", 20)))

    return create_sdk_mcp_server(
        name="coordinator", version="1.0.0",
        tools=[fetch_challenges, get_solve_status, spawn_swarm, check_swarm_status,
               submit_flag, kill_swarm, bump_agent, broadcast, read_solver_trace],
    )


async def run_claude_coordinator(
    settings: Settings,
    model_specs: list[str] | None = None,
    challenges_root: str = "challenges",
    no_submit: bool = False,
    coordinator_model: str | None = None,
    msg_port: int = 0,
    *,
    ctfd: CTFdClient | None = None,
    cost_tracker: CostTracker | None = None,
    deps: CoordinatorDeps | None = None,
    cleanup_runtime_on_exit: bool = True,
) -> dict[str, Any]:
    """Run the Claude Agent SDK coordinator with the shared event loop."""
    if ctfd is None or cost_tracker is None or deps is None:
        ctfd, cost_tracker, deps = build_deps(
            settings, model_specs, challenges_root, no_submit,
        )
    deps.msg_port = msg_port

    tool_call_counter = {"count": 0}
    consecutive_inactive_turns = 0

    def _record_tool_call() -> None:
        tool_call_counter["count"] += 1

    mcp_server = _build_coordinator_mcp(deps, on_tool_call=_record_tool_call)
    resolved_model = coordinator_model or "claude-opus-4-7"

    allowed = {
        "mcp__coordinator__fetch_challenges", "mcp__coordinator__get_solve_status",
        "mcp__coordinator__spawn_swarm", "mcp__coordinator__check_swarm_status",
        "mcp__coordinator__submit_flag", "mcp__coordinator__kill_swarm",
        "mcp__coordinator__bump_agent", "mcp__coordinator__broadcast",
        "mcp__coordinator__read_solver_trace",
        "ToolSearch",
        "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput", "TaskStop",
    }

    async def enforce_allowlist(input_data, tool_use_id, context):
        if input_data.get("hook_event_name") != "PreToolUse":
            return {}
        tool = input_data.get("tool_name", "")
        if tool in allowed:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"{tool} not available to coordinator.",
            }
        }

    options = ClaudeAgentOptions(
        model=resolved_model,
        system_prompt=COORDINATOR_PROMPT,
        env={"CLAUDECODE": ""},
        mcp_servers={"coordinator": mcp_server},
        allowed_tools=list(allowed),
        permission_mode="bypassPermissions",
        hooks={
            "PreToolUse": [HookMatcher(hooks=[enforce_allowlist])],
        },
    )

    async with ClaudeSDKClient(options=options) as client:
        async def _run_checked_turn(msg: str, *, require_tool_action: bool = False) -> None:
            nonlocal consecutive_inactive_turns
            logger.debug(f"Coordinator query: {msg[:200]}")
            before_tool_calls = tool_call_counter["count"]
            await client.query(msg)
            msg_count = 0
            async for message in client.receive_response():
                msg_count += 1
                msg_type = type(message).__name__
                logger.debug(f"Coordinator received: {msg_type}")
                if isinstance(message, ResultMessage):
                    cost = getattr(message, "total_cost_usd", 0)
                    session = getattr(message, "session_id", None)
                    logger.info(f"Claude coordinator turn done (messages={msg_count}, cost=${cost:.4f}, session={session})")
            tool_calls_delta = tool_call_counter["count"] - before_tool_calls
            consecutive_inactive_turns = _validate_turn_activity(
                msg_count=msg_count,
                tool_calls_delta=tool_calls_delta,
                previous_inactive_turns=consecutive_inactive_turns,
                require_tool_action=require_tool_action,
            )
            if tool_calls_delta == 0:
                logger.warning(
                    "Coordinator turn stayed inactive (messages=%d, tool_calls=%d, consecutive=%d)",
                    msg_count,
                    tool_calls_delta,
                    consecutive_inactive_turns,
                )

        async def turn_fn(msg: str) -> None:
            await _run_checked_turn(msg)

        await _run_checked_turn(COORDINATOR_PREFLIGHT_PROMPT, require_tool_action=True)
        logger.info("Claude coordinator preflight passed")

        return await run_event_loop(
            deps,
            ctfd,
            cost_tracker,
            turn_fn,
            propagate_fatal=True,
            cleanup_runtime_on_exit=cleanup_runtime_on_exit,
        )
