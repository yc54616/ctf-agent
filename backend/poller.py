"""Background CTFd poller — detects new and solved challenges with quiet retries."""

import asyncio
import logging
from dataclasses import dataclass, field

from backend.ctfd import CTFdClient

logger = logging.getLogger(__name__)

MAX_POLL_BACKOFF_SECONDS = 300.0
POLL_BACKOFF_MULTIPLIER = 2.0
POLL_WARNING_REPEAT_INTERVAL = 5


@dataclass
class PollEvent:
    kind: str  # "new_challenge" | "challenge_solved"
    challenge_name: str
    details: dict = field(default_factory=dict)


@dataclass
class CTFdPoller:
    """Polls CTFd every interval_s seconds, emits events for new/solved challenges."""

    ctfd: CTFdClient
    interval_s: float = 5.0

    _known_challenges: set[str] = field(default_factory=set)
    _known_solved: set[str] = field(default_factory=set)
    _event_queue: asyncio.Queue[PollEvent] = field(default_factory=asyncio.Queue)
    _task: asyncio.Task | None = field(default=None, repr=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _failure_count: int = field(default=0, init=False, repr=False)
    _current_interval_s: float = field(default=0.0, init=False, repr=False)
    _last_error_text: str = field(default="", init=False, repr=False)
    _suppressed_warning_count: int = field(default=0, init=False, repr=False)

    async def start(self) -> None:
        """Do initial poll (silent — no events) and start the background loop."""
        self._current_interval_s = float(self.interval_s)
        await self._seed()
        logger.info(
            "Poller initialized: %d challenges, %d solved",
            len(self._known_challenges),
            len(self._known_solved),
        )
        self._task = asyncio.create_task(self._loop(), name="ctfd-poller")

    async def _seed(self) -> None:
        """Initial fetch — just populate known state, no events."""
        try:
            stubs = await self.ctfd.fetch_challenge_stubs()
            self._known_challenges = {ch["name"] for ch in stubs}
            self._known_solved = await self.ctfd.fetch_solved_names()
            self._mark_poll_success()
        except Exception as e:
            self._record_poll_failure(e, initial=True)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def get_event(self, timeout: float = 1.0) -> PollEvent | None:
        """Non-blocking get — returns None if no event within timeout."""
        try:
            return await asyncio.wait_for(self._event_queue.get(), timeout=timeout)
        except (TimeoutError, asyncio.CancelledError):
            return None

    def drain_events(self) -> list[PollEvent]:
        """Drain all pending events without blocking."""
        events: list[PollEvent] = []
        while not self._event_queue.empty():
            try:
                events.append(self._event_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return events

    @property
    def known_challenges(self) -> set[str]:
        return set(self._known_challenges)

    @property
    def known_solved(self) -> set[str]:
        return set(self._known_solved)

    async def _poll_once(self) -> None:
        try:
            stubs = await self.ctfd.fetch_challenge_stubs()
            current_names = {ch["name"] for ch in stubs}
            current_solved = await self.ctfd.fetch_solved_names()

            # Sanity check: if results look bogus compared to what we know, skip.
            if self._known_challenges and len(current_names) < len(self._known_challenges) // 2:
                logger.warning(f"Poll returned suspicious data ({len(current_names)} challenges vs {len(self._known_challenges)} known) — skipping")
                return
            # Don't let solved count regress (API might return empty on errors)
            if self._known_solved and not current_solved:
                logger.warning("Poll returned 0 solved (had %d) — skipping", len(self._known_solved))
                return

            # Detect new challenges
            new_challenges = current_names - self._known_challenges
            for name in new_challenges:
                logger.info("New challenge detected: %s", name)
                self._event_queue.put_nowait(
                    PollEvent("new_challenge", name)
                )

            # Detect newly solved
            new_solves = current_solved - self._known_solved
            for name in new_solves:
                logger.info("Challenge solved: %s", name)
                self._event_queue.put_nowait(
                    PollEvent("challenge_solved", name)
                )

            self._known_challenges = current_names
            self._known_solved = current_solved
            self._mark_poll_success()

        except Exception as e:
            self._record_poll_failure(e)

    def _mark_poll_success(self) -> None:
        if self._failure_count > 0:
            logger.info(
                "CTFd poll recovered after %d failures",
                self._failure_count,
            )
        self._failure_count = 0
        self._current_interval_s = float(self.interval_s)
        self._last_error_text = ""
        self._suppressed_warning_count = 0

    def _record_poll_failure(self, exc: Exception, *, initial: bool = False) -> None:
        self._failure_count += 1
        self._current_interval_s = min(
            float(self.interval_s) * (POLL_BACKOFF_MULTIPLIER ** self._failure_count),
            MAX_POLL_BACKOFF_SECONDS,
        )
        prefix = "Initial poll error" if initial else "Poll error"
        error_text = str(exc)
        should_log = (
            self._failure_count == 1
            or error_text != self._last_error_text
            or self._failure_count % POLL_WARNING_REPEAT_INTERVAL == 0
        )
        if should_log:
            if self._suppressed_warning_count > 0 and error_text == self._last_error_text:
                logger.warning(
                    "%s persists: %s (%d consecutive failures, suppressed %d similar warnings, retry in %ds)",
                    prefix,
                    error_text,
                    self._failure_count,
                    self._suppressed_warning_count,
                    int(self._current_interval_s),
                )
            else:
                logger.warning(
                    "%s: %s (%d consecutive failures, retry in %ds)",
                    prefix,
                    error_text,
                    self._failure_count,
                    int(self._current_interval_s),
                )
            self._suppressed_warning_count = 0
        else:
            self._suppressed_warning_count += 1
        self._last_error_text = error_text

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self._current_interval_s or self.interval_s)
            if self._stop.is_set():
                break
            await self._poll_once()
