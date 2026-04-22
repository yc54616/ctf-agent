"""Shared dependency types — avoids circular imports between agents and tools."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from backend.cost_tracker import CostTracker
from backend.platforms import PlatformClient
from backend.sandbox import DockerSandbox

if TYPE_CHECKING:
    from backend.message_bus import CandidateRef, ChallengeMessageBus, CoordinatorNoteRef
    CoordinatorQueueEvent = CandidateRef | CoordinatorNoteRef
else:
    CoordinatorQueueEvent = object

# Type for the deduped submit callback: (flag) -> (display, is_confirmed)
SubmitFn = Callable[[str], Coroutine[Any, Any, tuple[str, bool]]]
ReportFlagCandidateFn = Callable[[str, str, str, int, str], Coroutine[Any, Any, str]]
RuntimeStatusGetter = Callable[[], dict[str, object]]


@dataclass
class SolverDeps:
    sandbox: DockerSandbox
    ctfd: PlatformClient
    challenge_dir: str
    challenge_name: str
    workspace_dir: str
    use_vision: bool
    cost_tracker: CostTracker | None = None
    confirmed_flag: str | None = None
    message_bus: ChallengeMessageBus | None = None
    model_spec: str = ""
    submit_fn: SubmitFn | None = None  # Deduped flag submission via swarm
    report_flag_candidate_fn: ReportFlagCandidateFn | None = None
    no_submit: bool = False
    local_mode: bool = False
    notify_coordinator: Callable[[str], Coroutine[Any, Any, None]] | None = None
    runtime_status_getter: RuntimeStatusGetter | None = None
    trace_path: str = ""


@dataclass
class CoordinatorDeps:
    ctfd: PlatformClient
    cost_tracker: CostTracker
    settings: Any
    model_specs: list[str] = field(default_factory=list)
    challenges_root: str = "challenges"
    no_submit: bool = False
    local_mode: bool = False
    max_concurrent_challenges: int = 10

    msg_port: int = 0  # 0 = auto-pick free port

    # Runtime state
    coordinator_inbox: asyncio.Queue[CoordinatorQueueEvent] = field(default_factory=asyncio.Queue)
    operator_inbox: asyncio.Queue = field(default_factory=asyncio.Queue)
    swarms: dict[str, Any] = field(default_factory=dict)
    swarm_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    results: dict[str, dict] = field(default_factory=dict)
    challenge_dirs: dict[str, str] = field(default_factory=dict)
    challenge_metas: dict[str, Any] = field(default_factory=dict)
    pending_swarm_queue: deque[str] = field(default_factory=deque)
    pending_swarm_set: set[str] = field(default_factory=set)
    pending_swarm_meta: dict[str, dict[str, object]] = field(default_factory=dict)
    quota_exhausted_model_specs: set[str] = field(default_factory=set)
    known_challenge_count: int = 0
    known_solved_count: int = 0
    session_started_at: float = field(default_factory=time.time)
    ctfd_refresh_backoff_until: float = 0.0
    ctfd_refresh_backoff_failures: int = 0
    ctfd_refresh_backoff_reason: str = ""
    shutdown_reason: str = ""
    shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)
