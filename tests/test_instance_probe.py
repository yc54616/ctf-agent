from __future__ import annotations

import asyncio

import pytest

from backend.instance_probe import probe_instance_connection


@pytest.mark.asyncio
async def test_probe_instance_connection_reports_missing_details_for_manual_instance() -> None:
    payload = await probe_instance_connection(
        {
            "name": "Ring of IO",
            "description": "deploy first",
            "needs_instance": True,
            "current_stage": "public_lab",
            "current_stage_title": "Public Lab",
            "connection_info": "",
        }
    )

    assert payload["ready"] is False
    assert payload["kind"] == "missing"
    assert payload["needs_instance"] is True
    assert payload["current_stage"] == "public_lab"
    assert payload["current_stage_title"] == "Public Lab"
    assert payload["current_stage_endpoint"] == ""
    assert payload["current_stage_endpoint_title"] == ""


@pytest.mark.asyncio
async def test_probe_instance_connection_connects_to_tcp_target() -> None:
    async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(_handle_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        payload = await probe_instance_connection(
            {
                "name": "Ring of IO",
                "connection": {
                    "scheme": "tcp",
                    "host": "127.0.0.1",
                    "port": port,
                },
                "connection_info": f"nc 127.0.0.1 {port}",
            }
        )
    finally:
        server.close()
        await server.wait_closed()

    assert payload["ready"] is True
    assert payload["kind"] == "tcp"
    assert payload["target"] == f"127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_probe_instance_connection_includes_current_endpoint_metadata() -> None:
    payload = await probe_instance_connection(
        {
            "name": "Ring of IO",
            "needs_instance": True,
            "current_stage": "internal_vm",
            "current_stage_title": "Internal VM",
            "current_stage_endpoint": "shell",
            "current_stage_endpoint_title": "Shell",
            "connection": {
                "scheme": "tcp",
                "host": "127.0.0.1",
                "port": 1,
            },
            "connection_info": "nc 127.0.0.1 1",
        }
    )

    assert payload["current_stage"] == "internal_vm"
    assert payload["current_stage_title"] == "Internal VM"
    assert payload["current_stage_endpoint"] == "shell"
    assert payload["current_stage_endpoint_title"] == "Shell"
