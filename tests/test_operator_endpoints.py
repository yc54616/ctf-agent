from __future__ import annotations

import asyncio
import json
from typing import Any, cast

from backend.agents.coordinator_loop import _start_msg_server
from backend.cost_tracker import CostTracker
from backend.deps import CoordinatorDeps


class _FakeSolver:
    def __init__(self) -> None:
        self.bumped: list[str] = []
        self.operator_bumped: list[str] = []

    def bump(self, insights: str) -> None:
        self.bumped.append(insights)

    def bump_operator(self, insights: str) -> None:
        self.operator_bumped.append(insights)


class _LegacyFakeSolver:
    def __init__(self) -> None:
        self.bumped: list[str] = []

    def bump(self, insights: str) -> None:
        self.bumped.append(insights)


class _FakeSwarm:
    def __init__(self, model_spec: str, solver: object) -> None:
        self.solvers = {model_spec: solver}


async def _get_json(port: int, path: str) -> tuple[str, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    request = (
        f"GET {path} HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode()
    writer.write(request)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()

    header, response_body = raw.split(b"\r\n\r\n", 1)
    status_line = header.splitlines()[0].decode()
    return status_line, json.loads(response_body.decode())


async def _get_text(port: int, path: str) -> tuple[str, str]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    request = (
        f"GET {path} HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode()
    writer.write(request)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()

    header, response_body = raw.split(b"\r\n\r\n", 1)
    status_line = header.splitlines()[0].decode()
    return status_line, response_body.decode()


async def _get_stream_prefix(port: int, path: str, *, size: int = 512) -> tuple[str, str]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    request = (
        f"GET {path} HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Accept: text/event-stream\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode()
    writer.write(request)
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(size), timeout=5)
    writer.close()
    await writer.wait_closed()

    header, response_body = raw.split(b"\r\n\r\n", 1)
    status_line = header.splitlines()[0].decode()
    return status_line, response_body.decode()


async def _post_json(port: int, path: str, payload: dict[str, object]) -> tuple[str, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    body = json.dumps(payload).encode()
    request = (
        f"POST {path} HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode() + body
    writer.write(request)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()

    header, response_body = raw.split(b"\r\n\r\n", 1)
    status_line = header.splitlines()[0].decode()
    return status_line, json.loads(response_body.decode())


def test_bump_endpoint_targets_requested_lane() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
    )
    solver = _FakeSolver()
    deps.swarms["Midnight Roulette"] = _FakeSwarm("codex/gpt-5.4", solver)

    async def _exercise() -> tuple[str, dict]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            return await _post_json(
                port,
                "/bump",
                {
                    "challenge_name": "Midnight Roulette",
                    "model_spec": "codex/gpt-5.4",
                    "insights": "Check the authenticated /ctfd/api/v1/challenges path.",
                },
            )
        finally:
            server.close()
            await server.wait_closed()

    status_line, payload = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 200")
    assert payload == {"ok": True, "result": "Bumped codex/gpt-5.4 on Midnight Roulette"}
    assert solver.operator_bumped == ["Check the authenticated /ctfd/api/v1/challenges path."]
    assert solver.bumped == []


def test_bump_endpoint_falls_back_to_legacy_bump() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
    )
    solver = _LegacyFakeSolver()
    deps.swarms["Midnight Roulette"] = _FakeSwarm("codex/gpt-5.4", solver)

    async def _exercise() -> tuple[str, dict]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            return await _post_json(
                port,
                "/bump",
                {
                    "challenge_name": "Midnight Roulette",
                    "model_spec": "codex/gpt-5.4",
                    "insights": "Switch to the token-authenticated API path first.",
                },
            )
        finally:
            server.close()
            await server.wait_closed()

    status_line, payload = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 200")
    assert payload == {"ok": True, "result": "Bumped codex/gpt-5.4 on Midnight Roulette"}
    assert solver.bumped == ["Switch to the token-authenticated API path first."]


def test_bump_endpoint_rejects_missing_fields() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
    )

    async def _exercise() -> tuple[str, dict]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            return await _post_json(port, "/bump", {"challenge_name": "PickleRick"})
        finally:
            server.close()
            await server.wait_closed()

    status_line, payload = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 400")
    assert payload["error"] == "challenge_name, model_spec, and insights are required"


def test_ui_endpoint_serves_browser_console() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
    )

    async def _exercise() -> tuple[str, str]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            return await _get_text(port, "/ui")
        finally:
            server.close()
            await server.wait_closed()

    status_line, body = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 200")
    assert "<title>CTF Operator Console</title>" in body


def test_status_stream_endpoint_emits_sse_payload() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
    )

    async def _exercise() -> tuple[str, str]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            return await _get_stream_prefix(port, "/status/stream")
        finally:
            server.close()
            await server.wait_closed()

    status_line, body = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 200")
    assert "event: status" in body
    assert '"active_swarm_count": 0' in body


def test_trace_endpoints_list_and_read_matching_lane_files(tmp_path, monkeypatch) -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
    )
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    older = logs_dir / "trace-Midnight_Roulette-gpt-5.4-mini-20260418-200000.jsonl"
    newer = logs_dir / "trace-Midnight_Roulette-gpt-5.4-mini-20260418-200100.jsonl"
    other = logs_dir / "trace-Other-gpt-5.4-mini-20260418-200100.jsonl"
    older.write_text(
        "\n".join(
            [
                json.dumps({"ts": 1.0, "type": "tool_call", "tool": "bash", "step": 1, "args": "pwd"}),
                json.dumps({"ts": 2.0, "type": "tool_result", "tool": "bash", "step": 1, "result": "/challenge"}),
            ]
        ),
        encoding="utf-8",
    )
    newer.write_text(
        "\n".join(
            [
                json.dumps({"ts": 3.0, "type": "tool_call", "tool": "bash", "step": 2, "args": "ls"}),
                json.dumps({"ts": 4.0, "type": "model_response", "step": 2, "text": "next step"}),
                json.dumps({"ts": 5.0, "type": "usage", "input_tokens": 10, "output_tokens": 5}),
            ]
        ),
        encoding="utf-8",
    )
    other.write_text(json.dumps({"ts": 9.0, "type": "tool_call"}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    async def _exercise() -> tuple[tuple[str, dict], tuple[str, dict], tuple[str, dict]]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            files = await _get_json(
                port,
                "/trace-files?challenge_name=Midnight%20Roulette&model_spec=codex/gpt-5.4-mini",
            )
            latest = await _get_json(
                port,
                (
                    "/trace?challenge_name=Midnight%20Roulette"
                    "&model_spec=codex/gpt-5.4-mini"
                    "&trace_name=trace-Midnight_Roulette-gpt-5.4-mini-20260418-200100.jsonl"
                    "&limit=2"
                ),
            )
            older_window = await _get_json(
                port,
                (
                    "/trace?challenge_name=Midnight%20Roulette"
                    "&model_spec=codex/gpt-5.4-mini"
                    "&trace_name=trace-Midnight_Roulette-gpt-5.4-mini-20260418-200100.jsonl"
                    "&cursor=0&limit=2"
                ),
            )
            return files, latest, older_window
        finally:
            server.close()
            await server.wait_closed()

    files_resp, latest_resp, older_resp = asyncio.run(_exercise())
    files_status, files_payload = files_resp
    latest_status, latest_payload = latest_resp
    older_status, older_payload = older_resp

    assert files_status.startswith("HTTP/1.1 200")
    assert files_payload["trace_files"] == [newer.name, older.name]

    assert latest_status.startswith("HTTP/1.1 200")
    assert latest_payload["trace_name"] == newer.name
    assert latest_payload["cursor"] == 1
    assert latest_payload["has_older"] is True
    assert [event["type"] for event in latest_payload["events"]] == ["model_response", "usage"]

    assert older_status.startswith("HTTP/1.1 200")
    assert older_payload["cursor"] == 0
    assert older_payload["next_cursor"] == 2
    assert [event["type"] for event in older_payload["events"]] == ["tool_call", "model_response"]


def test_trace_files_do_not_mix_codex_and_codex_spark(tmp_path, monkeypatch) -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
    )
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    codex_trace = logs_dir / "trace-aeBPF-gpt-5.3-codex-20260419-003545.jsonl"
    spark_trace = logs_dir / "trace-aeBPF-gpt-5.3-codex-spark-20260419-003545.jsonl"
    codex_trace.write_text(json.dumps({"ts": 1.0, "type": "start"}), encoding="utf-8")
    spark_trace.write_text(json.dumps({"ts": 2.0, "type": "start"}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    async def _exercise() -> tuple[tuple[str, dict], tuple[str, dict]]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            codex_files = await _get_json(
                port,
                "/trace-files?challenge_name=aeBPF&model_spec=codex/gpt-5.3-codex",
            )
            spark_files = await _get_json(
                port,
                "/trace-files?challenge_name=aeBPF&model_spec=codex/gpt-5.3-codex-spark",
            )
            return codex_files, spark_files
        finally:
            server.close()
            await server.wait_closed()

    codex_resp, spark_resp = asyncio.run(_exercise())
    codex_status, codex_payload = codex_resp
    spark_status, spark_payload = spark_resp

    assert codex_status.startswith("HTTP/1.1 200")
    assert spark_status.startswith("HTTP/1.1 200")
    assert codex_payload["trace_files"] == [codex_trace.name]
    assert spark_payload["trace_files"] == [spark_trace.name]


def test_trace_files_do_not_mix_codex_and_codex_mini(tmp_path, monkeypatch) -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
    )
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    regular_trace = logs_dir / "trace-aeBPF-gpt-5.4-20260419-003545.jsonl"
    mini_trace = logs_dir / "trace-aeBPF-gpt-5.4-mini-20260419-003545.jsonl"
    regular_trace.write_text(json.dumps({"ts": 1.0, "type": "start"}), encoding="utf-8")
    mini_trace.write_text(json.dumps({"ts": 2.0, "type": "start"}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    async def _exercise() -> tuple[tuple[str, dict], tuple[str, dict]]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            regular_files = await _get_json(
                port,
                "/trace-files?challenge_name=aeBPF&model_spec=codex/gpt-5.4",
            )
            mini_files = await _get_json(
                port,
                "/trace-files?challenge_name=aeBPF&model_spec=codex/gpt-5.4-mini",
            )
            return regular_files, mini_files
        finally:
            server.close()
            await server.wait_closed()

    regular_resp, mini_resp = asyncio.run(_exercise())
    regular_status, regular_payload = regular_resp
    mini_status, mini_payload = mini_resp

    assert regular_status.startswith("HTTP/1.1 200")
    assert mini_status.startswith("HTTP/1.1 200")
    assert regular_payload["trace_files"] == [regular_trace.name]
    assert mini_payload["trace_files"] == [mini_trace.name]


def test_advisories_endpoint_returns_recent_unique_lane_notes(tmp_path, monkeypatch) -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
    )
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    trace = logs_dir / "trace-Midnight_Roulette-gpt-5.4-mini-20260418-200100.jsonl"
    trace.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": 1.0,
                        "type": "bump",
                        "source": "auto",
                        "insights": "Private advisor note for this lane:\nFirst idea",
                    }
                ),
                json.dumps(
                    {
                        "ts": 2.0,
                        "type": "bump",
                        "source": "auto",
                        "insights": "Private advisor note for this lane:\nFirst idea",
                    }
                ),
                json.dumps(
                    {
                        "ts": 3.0,
                        "type": "bump",
                        "source": "auto",
                        "insights": "Private advisor note for this lane:\nSecond idea",
                    }
                ),
                json.dumps(
                    {
                        "ts": 4.0,
                        "type": "bump",
                        "source": "operator",
                        "insights": "Operator override",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    async def _exercise() -> tuple[str, dict]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            return await _get_json(port, "/advisories?challenge_name=Midnight%20Roulette&limit=5")
        finally:
            server.close()
            await server.wait_closed()

    status_line, payload = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 200")
    assert [entry["text"] for entry in payload["entries"]] == ["Second idea", "First idea"]
    assert payload["entries"][0]["model_id"] == "gpt-5.4-mini"
