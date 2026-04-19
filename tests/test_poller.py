from __future__ import annotations

import logging
from typing import Any, cast

from backend.poller import CTFdPoller


class _FakeCTFd:
    async def fetch_challenge_stubs(self):
        return []

    async def fetch_solved_names(self):
        return set()


def test_poller_uses_exponential_backoff_without_disabling() -> None:
    poller = CTFdPoller(ctfd=cast(Any, _FakeCTFd()), interval_s=5.0)

    poller._record_poll_failure(RuntimeError("offline"))
    assert poller._current_interval_s == 10.0

    poller._record_poll_failure(RuntimeError("offline"))
    assert poller._current_interval_s == 20.0

    poller._record_poll_failure(RuntimeError("offline"))
    assert poller._current_interval_s == 40.0
    assert poller._stop.is_set() is False


def test_poller_success_resets_failure_count_and_backoff() -> None:
    poller = CTFdPoller(ctfd=cast(Any, _FakeCTFd()), interval_s=5.0)

    poller._record_poll_failure(RuntimeError("offline"))
    poller._record_poll_failure(RuntimeError("offline"))
    assert poller._failure_count == 2
    assert poller._current_interval_s == 20.0

    poller._mark_poll_success()

    assert poller._failure_count == 0
    assert poller._current_interval_s == 5.0
    assert poller._suppressed_warning_count == 0


def test_poller_suppresses_repeated_warning_noise(caplog) -> None:
    poller = CTFdPoller(ctfd=cast(Any, _FakeCTFd()), interval_s=5.0)

    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            poller._record_poll_failure(RuntimeError("offline"))

    assert "Poll error: offline (1 consecutive failures, retry in 10s)" in caplog.text
    assert "Poll error persists: offline (5 consecutive failures, suppressed 3 similar warnings, retry in 160s)" in caplog.text
    assert "Disabling CTFd poller" not in caplog.text
