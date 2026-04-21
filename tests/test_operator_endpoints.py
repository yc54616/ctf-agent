from __future__ import annotations

import asyncio
import json
from pathlib import Path
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
        self.approved: list[str] = []
        self.rejected: list[str] = []
        self.external_solved: list[str] = []
        self.killed: list[str] = []
        self.requeue_requested: tuple[bool, str] | None = None

    async def approve_flag_candidate(self, flag: str, *, approved_by: str = "operator_local") -> str:
        self.approved.append(f"{approved_by}:{flag}")
        if approved_by == "operator_local":
            return f'USER CONFIRMED LOCALLY — "{flag}" marked solved in local mode.'
        return f'USER CONFIRMED MANUALLY — "{flag}" marked solved without CTFd confirmation.'

    async def reject_flag_candidate(self, flag: str, *, rejected_by: str = "operator_local") -> str:
        self.rejected.append(f"{rejected_by}:{flag}")
        if rejected_by == "operator_local":
            return f'USER REJECTED — "{flag}" dismissed in local mode.'
        return f'USER REJECTED — "{flag}" dismissed by operator review.'

    async def mark_solved_externally(
        self,
        flag: str,
        *,
        note: str = "",
        approved_by: str = "operator_external",
    ) -> str:
        entry = f"{approved_by}:{flag}"
        if note:
            entry = f"{entry}:{note}"
        self.external_solved.append(entry)
        display = f'USER REPORTED EXTERNAL SOLVE — "{flag}" marked solved from operator input.'
        if note:
            display = f"{display} Note: {note}"
        return display

    def request_requeue(self, *, priority: bool = False, reason: str = "queued") -> None:
        self.requeue_requested = (priority, reason)

    def kill(self, reason: str = "swarm cancelled") -> None:
        self.killed.append(reason)


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
                "/api/runtime/lane-bump",
                {
                    "challenge_name": "Midnight Roulette",
                    "lane_id": "codex/gpt-5.4",
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
                "/api/runtime/lane-bump",
                {
                    "challenge_name": "Midnight Roulette",
                    "lane_id": "codex/gpt-5.4",
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
            return await _post_json(port, "/api/runtime/lane-bump", {"challenge_name": "PickleRick"})
        finally:
            server.close()
            await server.wait_closed()

    status_line, payload = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 400")
    assert payload["error"] == "challenge_name, lane_id, and insights are required"


def test_approve_candidate_endpoint_marks_local_candidate_solved() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
        no_submit=True,
        local_mode=True,
    )
    solver = _FakeSolver()
    swarm = _FakeSwarm("codex/gpt-5.4", solver)
    deps.swarms["Local Only"] = swarm
    deps.results["Local Only"] = {
        "status": "candidate_pending",
        "flag_candidates": {"flag{local}": {"status": "pending"}},
    }

    async def _exercise() -> tuple[str, dict]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            return await _post_json(
                port,
                "/api/runtime/approve-candidate",
                {
                    "challenge_name": "Local Only",
                    "flag": "flag{local}",
                },
            )
        finally:
            server.close()
            await server.wait_closed()

    status_line, payload = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 200")
    assert payload == {"ok": True, "result": 'USER CONFIRMED LOCALLY — "flag{local}" marked solved in local mode.'}
    assert swarm.approved == ["operator_local:flag{local}"]


def test_approve_candidate_endpoint_marks_no_submit_candidate_solved() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
        no_submit=True,
        local_mode=False,
    )
    solver = _FakeSolver()
    swarm = _FakeSwarm("codex/gpt-5.4", solver)
    deps.swarms["Manual Confirm"] = swarm
    deps.results["Manual Confirm"] = {
        "status": "candidate_pending",
        "flag_candidates": {"flag{manual}": {"status": "reviewing"}},
    }

    async def _exercise() -> tuple[str, dict]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            return await _post_json(
                port,
                "/api/runtime/approve-candidate",
                {
                    "challenge_name": "Manual Confirm",
                    "flag": "flag{manual}",
                },
            )
        finally:
            server.close()
            await server.wait_closed()

    status_line, payload = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 200")
    assert payload == {
        "ok": True,
        "result": 'USER CONFIRMED MANUALLY — "flag{manual}" marked solved without CTFd confirmation.',
    }
    assert swarm.approved == ["operator_manual:flag{manual}"]


def test_approve_candidate_endpoint_marks_normal_ctfd_candidate_solved() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
        no_submit=False,
        local_mode=False,
    )
    solver = _FakeSolver()
    swarm = _FakeSwarm("codex/gpt-5.4", solver)
    deps.swarms["Local Only"] = swarm

    async def _exercise() -> tuple[str, dict]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            return await _post_json(
                port,
                "/api/runtime/approve-candidate",
                {
                    "challenge_name": "Local Only",
                    "flag": "flag{local}",
                },
            )
        finally:
            server.close()
            await server.wait_closed()

    status_line, payload = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 200")
    assert payload == {
        "ok": True,
        "result": 'USER CONFIRMED MANUALLY — "flag{local}" marked solved without CTFd confirmation.',
    }
    assert swarm.approved == ["operator_manual:flag{local}"]


def test_approve_candidate_endpoint_uses_stored_candidate_without_active_swarm() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
        no_submit=False,
        local_mode=False,
    )
    deps.results["Stored Candidate"] = {
        "status": "candidate_pending",
        "flag_candidates": {
            "flag{stored}": {
                "status": "pending",
                "flag": "flag{stored}",
            }
        },
    }

    async def _exercise() -> tuple[str, dict]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            return await _post_json(
                port,
                "/api/runtime/approve-candidate",
                {
                    "challenge_name": "Stored Candidate",
                    "flag": "flag{stored}",
                },
            )
        finally:
            server.close()
            await server.wait_closed()

    status_line, payload = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 200")
    assert payload == {
        "ok": True,
        "result": 'USER CONFIRMED MANUALLY — "flag{stored}" marked solved without CTFd confirmation.',
    }
    assert deps.results["Stored Candidate"]["status"] == "flag_found"
    assert deps.results["Stored Candidate"]["confirmation_source"] == "operator_manual"


def test_approve_candidate_endpoint_uses_stored_incorrect_candidate_without_active_swarm() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
        no_submit=False,
        local_mode=False,
    )
    deps.results["Stored Candidate"] = {
        "status": "candidate_pending",
        "flag_candidates": {
            "flag{stored}": {
                "status": "incorrect",
                "flag": "flag{stored}",
                "submit_display": 'INCORRECT — "flag{stored}" rejected.',
            }
        },
    }

    async def _exercise() -> tuple[str, dict]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            return await _post_json(
                port,
                "/api/runtime/approve-candidate",
                {
                    "challenge_name": "Stored Candidate",
                    "flag": "flag{stored}",
                },
            )
        finally:
            server.close()
            await server.wait_closed()

    status_line, payload = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 200")
    assert payload == {
        "ok": True,
        "result": 'USER CONFIRMED MANUALLY — "flag{stored}" marked solved without CTFd confirmation.',
    }
    assert deps.results["Stored Candidate"]["status"] == "flag_found"
    assert deps.results["Stored Candidate"]["confirmation_source"] == "operator_manual"
    assert deps.results["Stored Candidate"]["flag_candidates"]["flag{stored}"]["status"] == "confirmed"


def test_reject_candidate_endpoint_marks_local_candidate_rejected() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
        no_submit=True,
        local_mode=True,
    )
    solver = _FakeSolver()
    swarm = _FakeSwarm("codex/gpt-5.4", solver)
    deps.swarms["Local Only"] = swarm
    deps.results["Local Only"] = {
        "status": "candidate_pending",
        "flag_candidates": {"flag{local}": {"status": "pending"}},
    }

    async def _exercise() -> tuple[str, dict]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            return await _post_json(
                port,
                "/api/runtime/reject-candidate",
                {
                    "challenge_name": "Local Only",
                    "flag": "flag{local}",
                },
            )
        finally:
            server.close()
            await server.wait_closed()

    status_line, payload = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 200")
    assert payload == {"ok": True, "result": 'USER REJECTED — "flag{local}" dismissed in local mode.'}
    assert swarm.rejected == ["operator_local:flag{local}"]


def test_reject_candidate_endpoint_marks_no_submit_candidate_rejected() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
        no_submit=True,
        local_mode=False,
    )
    solver = _FakeSolver()
    swarm = _FakeSwarm("codex/gpt-5.4", solver)
    deps.swarms["Manual Confirm"] = swarm
    deps.results["Manual Confirm"] = {
        "status": "candidate_pending",
        "flag_candidates": {"flag{manual}": {"status": "reviewing"}},
    }

    async def _exercise() -> tuple[str, dict]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            return await _post_json(
                port,
                "/api/runtime/reject-candidate",
                {
                    "challenge_name": "Manual Confirm",
                    "flag": "flag{manual}",
                },
            )
        finally:
            server.close()
            await server.wait_closed()

    status_line, payload = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 200")
    assert payload == {
        "ok": True,
        "result": 'USER REJECTED — "flag{manual}" dismissed by operator review.',
    }
    assert swarm.rejected == ["operator_manual:flag{manual}"]


def test_reject_candidate_endpoint_marks_normal_ctfd_candidate_rejected() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
        no_submit=False,
        local_mode=False,
    )
    solver = _FakeSolver()
    swarm = _FakeSwarm("codex/gpt-5.4", solver)
    deps.swarms["Normal Reject"] = swarm

    async def _exercise() -> tuple[str, dict]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            return await _post_json(
                port,
                "/api/runtime/reject-candidate",
                {
                    "challenge_name": "Normal Reject",
                    "flag": "flag{manual}",
                },
            )
        finally:
            server.close()
            await server.wait_closed()

    status_line, payload = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 200")
    assert payload == {
        "ok": True,
        "result": 'USER REJECTED — "flag{manual}" dismissed by operator review.',
    }
    assert swarm.rejected == ["operator_manual:flag{manual}"]


def test_reject_candidate_endpoint_uses_stored_candidate_without_active_swarm() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
        no_submit=False,
        local_mode=False,
    )
    deps.results["Stored Candidate"] = {
        "status": "candidate_pending",
        "flag_candidates": {
            "flag{stored}": {
                "status": "pending",
                "flag": "flag{stored}",
            }
        },
    }

    async def _exercise() -> tuple[str, dict]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            return await _post_json(
                port,
                "/api/runtime/reject-candidate",
                {
                    "challenge_name": "Stored Candidate",
                    "flag": "flag{stored}",
                },
            )
        finally:
            server.close()
            await server.wait_closed()

    status_line, payload = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 200")
    assert payload == {
        "ok": True,
        "result": 'USER REJECTED — "flag{stored}" dismissed by operator review.',
    }
    assert deps.results["Stored Candidate"]["status"] == "pending"
    assert deps.results["Stored Candidate"]["flag_candidates"]["flag{stored}"]["status"] == "rejected"


def test_mark_solved_endpoint_marks_active_swarm_solved() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
        no_submit=False,
        local_mode=False,
    )
    solver = _FakeSolver()
    swarm = _FakeSwarm("codex/gpt-5.4", solver)
    deps.swarms["Operator Solved"] = swarm

    async def _exercise() -> tuple[str, dict]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            return await _post_json(
                port,
                "/api/runtime/mark-solved",
                {
                    "challenge_name": "Operator Solved",
                    "flag": "flag{external}",
                    "note": "solved manually outside the swarm",
                },
            )
        finally:
            server.close()
            await server.wait_closed()

    status_line, payload = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 200")
    assert payload == {
        "ok": True,
        "result": 'USER REPORTED EXTERNAL SOLVE — "flag{external}" marked solved from operator input. Note: solved manually outside the swarm',
    }
    assert swarm.external_solved == ["operator_external:flag{external}:solved manually outside the swarm"]


def test_mark_solved_endpoint_persists_result_without_active_swarm(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge-external"
    challenge_dir.mkdir()
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
        no_submit=False,
        local_mode=False,
    )
    deps.challenge_dirs["Operator Solved"] = str(challenge_dir)

    async def _exercise() -> tuple[str, dict]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            return await _post_json(
                port,
                "/api/runtime/mark-solved",
                {
                    "challenge_name": "Operator Solved",
                    "flag": "flag{external}",
                    "note": "solved on another box",
                },
            )
        finally:
            server.close()
            await server.wait_closed()

    status_line, payload = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 200")
    assert payload["result"].startswith('USER REPORTED EXTERNAL SOLVE — "flag{external}" marked solved from operator input.')
    result_json = json.loads((challenge_dir / "solve" / "result.json").read_text(encoding="utf-8"))
    assert result_json["status"] == "flag_found"
    assert result_json["flag"] == "flag{external}"
    assert result_json["confirmation_source"] == "operator_external"
    assert result_json["submit"] == "reported solved by operator"
    assert result_json["external_note"] == "solved on another box"
    assert (challenge_dir / "solve" / "flag.txt").read_text(encoding="utf-8").strip() == "flag{external}"


def test_set_max_challenges_endpoint_updates_runtime_limit() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=type("Settings", (), {"max_concurrent_challenges": 4})(),
        max_concurrent_challenges=4,
    )

    async def _exercise() -> tuple[str, dict]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            return await _post_json(
                port,
                "/api/runtime/set-max-challenges",
                {"max_active": 6},
            )
        finally:
            server.close()
            await server.wait_closed()

    status_line, payload = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 200")
    assert payload["max_concurrent_challenges"] == 6
    assert deps.max_concurrent_challenges == 6


def test_set_challenge_priority_endpoint_marks_pending_priority_waiting() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
    )
    deps.pending_swarm_queue.append("queued-one")
    deps.pending_swarm_set.add("queued-one")
    deps.pending_swarm_meta["queued-one"] = {
        "priority": False,
        "reason": "queued",
        "enqueued_at": 1.0,
    }
    deps.results["queued-one"] = {"status": "pending"}

    async def _exercise() -> tuple[str, dict]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            return await _post_json(
                port,
                "/api/runtime/set-challenge-priority",
                {"challenge_name": "queued-one", "priority": True},
            )
        finally:
            server.close()
            await server.wait_closed()

    status_line, payload = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 200")
    assert payload["result"] == 'Challenge "queued-one" moved to priority waiting.'
    assert deps.pending_swarm_meta["queued-one"]["priority"] is True
    assert deps.pending_swarm_meta["queued-one"]["reason"] == "priority_waiting"


def test_set_challenge_priority_endpoint_pauses_active_swarm() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
    )
    solver = _FakeSolver()
    swarm = _FakeSwarm("codex/gpt-5.4", solver)
    deps.swarms["Priority Me"] = swarm

    async def _exercise() -> tuple[str, dict]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            return await _post_json(
                port,
                "/api/runtime/set-challenge-priority",
                {"challenge_name": "Priority Me", "priority": True},
            )
        finally:
            server.close()
            await server.wait_closed()

    status_line, payload = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 200")
    assert payload["result"] == 'Pausing "Priority Me" and returning it to priority waiting.'
    assert swarm.requeue_requested == (True, "priority_waiting")
    assert swarm.killed == ["operator moved Priority Me to priority waiting"]


def test_restart_challenge_endpoint_requeues_active_swarm() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
    )
    solver = _FakeSolver()
    swarm = _FakeSwarm("codex/gpt-5.4", solver)
    deps.swarms["Restart Me"] = swarm

    async def _exercise() -> tuple[str, dict]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            return await _post_json(
                port,
                "/api/runtime/resume-challenge",
                {"challenge_name": "Restart Me"},
            )
        finally:
            server.close()
            await server.wait_closed()

    status_line, payload = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 200")
    assert payload["result"] == 'Resuming "Restart Me" after the current run stops.'
    assert swarm.requeue_requested == (True, "resume_requested")
    assert swarm.killed == ["operator resuming Restart Me"]


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


def test_runtime_stream_endpoint_emits_sse_payload() -> None:
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
            return await _get_stream_prefix(port, "/api/runtime/stream")
        finally:
            server.close()
            await server.wait_closed()

    status_line, body = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 200")
    assert "event: snapshot" in body
    assert '"challenge_summary"' in body


def test_runtime_challenge_bump_fans_out_to_non_final_lanes() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
    )
    busy = _FakeSolver()
    won = _FakeSolver()
    swarm = _FakeSwarm("codex/gpt-5.4", busy)
    swarm.solvers["gemini/gemini-2.5-flash"] = won
    swarm.get_status = lambda: {
        "agents": {
            "codex/gpt-5.4": {"lifecycle": "busy"},
            "gemini/gemini-2.5-flash": {"lifecycle": "won"},
        }
    }
    deps.swarms["Midnight Roulette"] = swarm

    async def _exercise() -> tuple[str, dict]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            return await _post_json(
                port,
                "/api/runtime/challenge-bump",
                {
                    "challenge_name": "Midnight Roulette",
                    "insights": "Focus on the authenticated API route first.",
                },
            )
        finally:
            server.close()
            await server.wait_closed()

    status_line, payload = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 200")
    assert payload["challenge_name"] == "Midnight Roulette"
    assert payload["results"] == [{"lane_id": "codex/gpt-5.4", "result": "Bumped codex/gpt-5.4 on Midnight Roulette"}]
    assert busy.operator_bumped == ["Focus on the authenticated API route first."]
    assert won.operator_bumped == []


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
                "/api/runtime/traces?challenge_name=Midnight%20Roulette&lane_id=codex/gpt-5.4-mini",
            )
            latest = await _get_json(
                port,
                (
                    "/api/runtime/trace-window?challenge_name=Midnight%20Roulette"
                    "&lane_id=codex/gpt-5.4-mini"
                    "&trace_name=trace-Midnight_Roulette-gpt-5.4-mini-20260418-200100.jsonl"
                    "&limit=2"
                ),
            )
            older_window = await _get_json(
                port,
                (
                    "/api/runtime/trace-window?challenge_name=Midnight%20Roulette"
                    "&lane_id=codex/gpt-5.4-mini"
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
    assert files_payload["challenge_name"] == "Midnight Roulette"
    assert files_payload["lane_id"] == "codex/gpt-5.4-mini"
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
                "/api/runtime/traces?challenge_name=aeBPF&lane_id=codex/gpt-5.3-codex",
            )
            spark_files = await _get_json(
                port,
                "/api/runtime/traces?challenge_name=aeBPF&lane_id=codex/gpt-5.3-codex-spark",
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
                "/api/runtime/traces?challenge_name=aeBPF&lane_id=codex/gpt-5.4",
            )
            mini_files = await _get_json(
                port,
                "/api/runtime/traces?challenge_name=aeBPF&lane_id=codex/gpt-5.4-mini",
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


def test_trace_files_allow_challenge_level_fallback_and_saved_solve_trace(tmp_path, monkeypatch) -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
    )
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    challenge_dir = tmp_path / "challenges" / "sanity-check"
    solve_dir = challenge_dir / "solve"
    solve_dir.mkdir(parents=True)
    lane_trace = logs_dir / "trace-sanity_check-gpt-5.4-20260421-120000.jsonl"
    solve_trace = solve_dir / "trace.jsonl"
    lane_trace.write_text(json.dumps({"ts": 1.0, "type": "start"}), encoding="utf-8")
    solve_trace.write_text(
        "\n".join(
            [
                json.dumps({"ts": 2.0, "type": "tool_call", "tool": "bash"}),
                json.dumps({"ts": 3.0, "type": "model_response", "text": "saved trace"}),
            ]
        ),
        encoding="utf-8",
    )
    deps.challenge_dirs["sanity check"] = str(challenge_dir)
    monkeypatch.chdir(tmp_path)

    async def _exercise() -> tuple[tuple[str, dict], tuple[str, dict]]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            files = await _get_json(
                port,
                "/api/runtime/traces?challenge_name=sanity%20check",
            )
            window = await _get_json(
                port,
                "/api/runtime/trace-window?challenge_name=sanity%20check&trace_name=trace.jsonl&limit=10",
            )
            return files, window
        finally:
            server.close()
            await server.wait_closed()

    files_resp, window_resp = asyncio.run(_exercise())
    files_status, files_payload = files_resp
    window_status, window_payload = window_resp

    assert files_status.startswith("HTTP/1.1 200")
    assert files_payload["lane_id"] == ""
    assert files_payload["trace_files"] == [lane_trace.name, "trace.jsonl"]

    assert window_status.startswith("HTTP/1.1 200")
    assert window_payload["trace_name"] == "trace.jsonl"
    assert [event["type"] for event in window_payload["events"]] == ["tool_call", "model_response"]


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
            return await _get_json(port, "/api/runtime/advisories?challenge_name=Midnight%20Roulette&limit=5")
        finally:
            server.close()
            await server.wait_closed()

    status_line, payload = asyncio.run(_exercise())

    assert status_line.startswith("HTTP/1.1 200")
    assert [entry["text"] for entry in payload["entries"]] == ["Second idea", "First idea"]
    assert payload["entries"][0]["model_id"] == "gpt-5.4-mini"


def test_legacy_operator_endpoints_are_rejected() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
    )

    async def _exercise() -> tuple[tuple[str, dict], tuple[str, dict], tuple[str, dict]]:
        server = await _start_msg_server(deps.operator_inbox, deps, 0)
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        try:
            status_resp = await _get_json(port, "/status")
            traces_resp = await _get_json(
                port,
                "/trace-files?challenge_name=Midnight%20Roulette&model_spec=codex/gpt-5.4-mini",
            )
            bump_resp = await _post_json(
                port,
                "/bump",
                {
                    "challenge_name": "Midnight Roulette",
                    "model_spec": "codex/gpt-5.4-mini",
                    "insights": "legacy path should fail",
                },
            )
            return status_resp, traces_resp, bump_resp
        finally:
            server.close()
            await server.wait_closed()

    status_resp, traces_resp, bump_resp = asyncio.run(_exercise())
    for status_line, payload in (status_resp, traces_resp, bump_resp):
        assert status_line.startswith("HTTP/1.1 400")
        assert payload["error"] == "Unsupported request"
