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


@pytest.mark.asyncio
async def test_fetch_solved_names_uses_cached_set_on_timeout(caplog) -> None:
    client = CTFdClient(base_url="https://ctfd.example", token="token")
    client._solved_names_cache = {"welcome", "web-100"}

    async def _boom(_path: str):  # type: ignore[no-untyped-def]
        raise RuntimeError("CTFd GET timed out: /api/v1/users/me")

    client._get = cast(Any, _boom)

    with caplog.at_level(logging.WARNING):
        solved = await client.fetch_solved_names()

    assert solved == {"welcome", "web-100"}
    assert "using cached solved set (2 entries)" in caplog.text
    assert "Traceback" not in caplog.text


@pytest.mark.asyncio
async def test_fetch_all_challenges_skips_missing_detail_404(caplog) -> None:
    client = CTFdClient(base_url="https://ctfd.example", token="token")

    async def _fake_get(path: str):  # type: ignore[no-untyped-def]
        if path == "/challenges?per_page=500":
            return {
                "data": [
                    {"id": 44, "name": "kept", "type": "standard"},
                    {"id": 45, "name": "missing", "type": "standard"},
                ]
            }
        if path == "/challenges/44":
            return {"data": {"id": 44, "name": "kept", "solves": 3}}
        if path == "/challenges/45":
            request = httpx.Request("GET", "https://ctfd.example/api/v1/challenges/45")
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("404 NOT FOUND", request=request, response=response)
        raise AssertionError(f"unexpected path: {path}")

    client._get = cast(Any, _fake_get)

    with caplog.at_level(logging.WARNING):
        challenges = await client.fetch_all_challenges()

    assert challenges == [{"id": 44, "name": "kept", "solves": 3}]
    assert "Skipping missing CTFd challenge detail for id=45 name='missing'" in caplog.text
