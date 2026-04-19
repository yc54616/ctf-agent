from __future__ import annotations

import logging
from typing import Any, cast

import httpx
import pytest

from backend.ctfd import CTFdClient


class _TimeoutClient:
    async def get(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise httpx.ReadTimeout("timed out")


@pytest.mark.asyncio
async def test_ctfd_get_includes_path_on_timeout() -> None:
    client = CTFdClient(base_url="https://ctfd.example", token="token")
    client._client = cast(Any, _TimeoutClient())

    with pytest.raises(RuntimeError, match=r"CTFd GET timed out: /api/v1/challenges\?per_page=500"):
        await client._get("/challenges?per_page=500")


@pytest.mark.asyncio
async def test_fetch_solved_names_logs_single_line_on_connect_error(caplog) -> None:
    client = CTFdClient(base_url="https://ctfd.example", token="token")

    async def _boom(_path: str):  # type: ignore[no-untyped-def]
        raise httpx.ConnectError("All connection attempts failed")

    client._get = cast(Any, _boom)

    with caplog.at_level(logging.WARNING):
        solved = await client.fetch_solved_names()

    assert solved == set()
    assert "Could not fetch solved challenges: All connection attempts failed" in caplog.text
    assert "Traceback" not in caplog.text
