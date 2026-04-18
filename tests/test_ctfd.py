from __future__ import annotations

import httpx
import pytest

from backend.ctfd import CTFdClient


class _TimeoutClient:
    async def get(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise httpx.ReadTimeout("timed out")


@pytest.mark.asyncio
async def test_ctfd_get_includes_path_on_timeout() -> None:
    client = CTFdClient(base_url="https://ctfd.example", token="token")
    client._client = _TimeoutClient()  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match=r"CTFd GET timed out: /api/v1/challenges\?per_page=500"):
        await client._get("/challenges?per_page=500")
