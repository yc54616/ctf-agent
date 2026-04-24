"""Human coordinator — replaces the LLM decision-maker with a human operator.

The coordinator event loop still runs (platform polling, swarm lifecycle,
inbox draining) but instead of forwarding events to an LLM the events are:

  1. Written to ``deps.human_event_log`` (AsyncIO queue) so the operator UI
     can stream them via ``GET /api/runtime/human-events``.
  2. Logged at INFO level so they appear in the run log.

All decisions are made by the human through the operator UI (or API calls):

  ┌─────────────────────────┬──────────────────────────────────────────────┐
  │ Coordinator role         │ How the human does it                        │
  ├─────────────────────────┼──────────────────────────────────────────────┤
  │ Flag submission          │ Approve/reject candidates in the UI          │
  │                          │  POST /api/runtime/approve-candidate         │
  │                          │  POST /api/runtime/reject-candidate          │
  ├─────────────────────────┼──────────────────────────────────────────────┤
  │ Swarm management         │ Spawn / kill / restart via the UI            │
  │                          │  POST /api/runtime/spawn-swarm               │
  │                          │  POST /api/runtime/kill-swarm                │
  │                          │  POST /api/runtime/restart-challenge         │
  ├─────────────────────────┼──────────────────────────────────────────────┤
  │ Problem list             │ Human manages the challenges/ directory, or  │
  │                          │ submits a URL for the parser agent:          │
  │                          │  POST /api/runtime/parse-challenge-url       │
  ├─────────────────────────┼──────────────────────────────────────────────┤
  │ Solver guidance          │ Bump a specific lane or broadcast to all:    │
  │                          │  POST /api/runtime/lane-bump                 │
  │                          │  POST /api/runtime/challenge-bump            │
  │                          │  POST /api/runtime/broadcast                 │
  └─────────────────────────┴──────────────────────────────────────────────┘

The coordinator still auto-kills swarms when a challenge is solved elsewhere
and still drains the pending queue when capacity is available — both are
mechanical bookkeeping that does not require LLM judgement.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from backend.agents.coordinator_loop import build_deps, run_event_loop
from backend.config import Settings
from backend.cost_tracker import CostTracker
from backend.deps import CoordinatorDeps
from backend.platforms import PlatformClient

logger = logging.getLogger(__name__)


async def run_human_coordinator(
    settings: Settings,
    model_specs: list[str] | None = None,
    challenges_root: str = "challenges",
    local_mode: bool = False,
    msg_port: int = 0,
    *,
    ctfd: PlatformClient | None = None,
    cost_tracker: CostTracker | None = None,
    deps: CoordinatorDeps | None = None,
    cleanup_runtime_on_exit: bool = True,
    cookie_header: str = "",
) -> dict[str, Any]:
    """Run the coordinator in human-driven mode.

    Key differences from LLM mode:
    - ``no_submit`` is forced to ``True`` — all flag candidates go to the
      human for review rather than being submitted automatically.
    - ``auto_spawn`` is ``False`` — the human picks which challenges to tackle.
    - The ``turn_fn`` passed to the event loop is a lightweight async logger
      that queues events for the SSE stream instead of calling an LLM.
    """
    if ctfd is None or cost_tracker is None or deps is None:
        ctfd, cost_tracker, deps = build_deps(
            settings,
            model_specs,
            challenges_root,
            no_submit=True,   # human approves all candidates
            local_mode=local_mode,
            cookie_header=cookie_header,
        )
    else:
        # Force no-submit regardless of caller setting
        deps.no_submit = True

    deps.msg_port = msg_port
    deps.human_mode = True

    async def _human_turn_fn(msg: str) -> None:
        """Receive coordinator events.

        Instead of feeding messages to an LLM, we:
        1. Log at INFO so they appear in the run log.
        2. Push to the human_event_log queue for the SSE stream.
        """
        logger.info("[Human coordinator event]\n%s", msg[:600])
        event: dict[str, Any] = {
            "ts": time.time(),
            "message": msg,
        }
        try:
            deps.human_event_log.put_nowait(event)
        except asyncio.QueueFull:
            # Ring buffer full — silently discard (event is already in the log)
            pass

    return await run_event_loop(
        deps,
        ctfd,
        cost_tracker,
        _human_turn_fn,
        auto_spawn=False,           # human decides which challenges to spawn
        propagate_fatal=True,
        cleanup_runtime_on_exit=cleanup_runtime_on_exit,
    )
