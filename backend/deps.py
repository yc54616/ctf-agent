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

    msg_port: int = 0        # 0 = auto-pick free port
    msg_host: str = "127.0.0.1"  # "0.0.0.0" for remote access

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
    ui_alerts: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=20))
    known_challenge_count: int = 0
    known_solved_count: int = 0
    session_started_at: float = field(default_factory=time.time)
    ctfd_refresh_backoff_until: float = 0.0
    ctfd_refresh_backoff_failures: int = 0
    ctfd_refresh_backoff_reason: str = ""
    shutdown_reason: str = ""
    shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)

    # Human coordinator mode
    human_mode: bool = False
    # Ring buffer of coordinator events for human-facing SSE stream.
    # maxsize=500 — put_nowait drops new events when full (events are also logged).
    human_event_log: asyncio.Queue = field(
        default_factory=lambda: asyncio.Queue(maxsize=500)
    )
    # Last CTFd /api/v1/challenges payload cached by the Fetch button, so the UI
    # can surface remote-only challenges that aren't yet imported to disk.
    # Key is challenge name, value is the raw record dict (name, category, value, …).
    remote_challenge_cache: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Structured advisor / solver reports for the human UI.  Populated from the
    # coordinator_inbox drain loop in run_event_loop — each entry carries
    # {ts, challenge_name, lane_id, kind, text, advisor_decision?}.
    # maxlen=200 — older entries drop off as new reports arrive.
    advisor_reports: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=200)
    )

    # Provenance string for the currently-configured CTFd session cookie so the
    # UI can display "set via --cookie-file at startup" vs "set via API at …".
    # Never contains the cookie value itself — that lives in settings.remote_cookie_header.
    cookie_source: str = ""

    # Background CTFd poller — run_event_loop stashes its PlatformPoller here so
    # the HTTP handler can surface its status in the snapshot and reset its
    # exponential backoff after a ctfd-config change.  ``None`` in local_mode.
    poller: Any | None = None

    # Shared "solve reports" stream — a structured channel where lanes post
    # discovery / experiment notes, the advisor posts synthesis / hints, and
    # the human UI renders the combined feed for intervene-by-report workflow.
    # Entries: {id, ts, challenge_name, lane_id, kind, title, body, refs, status}.
    # Kind ∈ {discovery, experiment, hypothesis, blocker, synthesis, hint, candidate_review, lane_note}.
    # maxlen=500 so long-running swarms don't balloon memory.
    solve_reports: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=500)
    )
