"""Gemini CLI hook entrypoint for sandboxed CTF solving."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
import uuid
from pathlib import Path
from typing import Any

REPORT_FLAG_RE = re.compile(
    r"""^report_flag_candidate\s+['"]?(.+?)['"]?(?:\s+['"]?(.+?)['"]?)?(?:\s+['"]?(.+?)['"]?)?\s*$""",
    re.DOTALL,
)
NOTIFY_COORDINATOR_RE = re.compile(
    r"""^notify_coordinator\s+['"]?(.+?)['"]?\s*$""",
    re.DOTALL,
)
PSEUDO_TOOL_NAMES = {"fs_query"}


def handle_hook(payload: dict[str, Any]) -> dict[str, Any]:
    """Handle a Gemini CLI hook invocation."""
    event_name = str(payload.get("hook_event_name", ""))

    if event_name == "BeforeToolSelection":
        return {
            "hookSpecificOutput": {
                "hookEventName": "BeforeToolSelection",
                "toolConfig": {
                    "mode": "ANY",
                    "allowedFunctionNames": ["run_shell_command"],
                },
            }
        }

    if event_name != "BeforeTool":
        return {}

    tool_name = str(payload.get("tool_name", ""))
    if tool_name != "run_shell_command":
        return {
            "decision": "deny",
            "reason": f"{tool_name} is blocked. Use run_shell_command only.",
        }

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    command = str(tool_input.get("command", ""))

    submit_match = REPORT_FLAG_RE.match(command.strip())
    if submit_match:
        response = _ipc_request(
            "report_flag_candidate",
            {
                "flag": (submit_match.group(1) or "").strip(),
                "evidence": (submit_match.group(2) or "").strip(),
                "confidence": (submit_match.group(3) or "medium").strip() or "medium",
            },
        )
        return _reply_command(response["message"])

    notify_match = NOTIFY_COORDINATOR_RE.match(command.strip())
    if notify_match:
        response = _ipc_request(
            "notify_coordinator",
            {"message": notify_match.group(1).strip()},
        )
        return _reply_command(response["message"])

    try:
        pseudo = _parse_pseudo_command(command)
    except ValueError as exc:
        return _reply_command(f"Tool command error: {exc}")
    if pseudo is not None:
        action, payload = pseudo
        response = _ipc_request(action, payload)
        return _reply_command(
            str(
                response.get("output")
                or response.get("message")
                or response.get("findings")
                or ""
            )
        )

    container_id = os.environ["CTF_AGENT_GEMINI_CONTAINER_ID"]
    rewritten = _rewrite_shell_command(command, container_id=container_id)
    return _rewrite_tool_input(rewritten)


def _rewrite_tool_input(command: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "BeforeTool",
            "tool_input": {"command": command},
        }
    }


def _reply_command(text: str) -> dict[str, Any]:
    return _rewrite_tool_input(f"printf '%s\\n' {shlex.quote(text)}")


def _rewrite_shell_command(
    command: str,
    *,
    container_id: str,
) -> str:
    return f"docker exec -i {shlex.quote(container_id)} bash -lc {shlex.quote(command)}"


def _parse_bool(token: str) -> bool:
    lowered = token.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {token}")


def _parse_int(token: str, option: str) -> int:
    try:
        return int(token)
    except ValueError as exc:
        raise ValueError(f"{option} expects an integer") from exc


def _parse_pseudo_command(command: str) -> tuple[str, dict[str, Any]] | None:
    tokens = shlex.split(command)
    if not tokens or tokens[0] not in PSEUDO_TOOL_NAMES:
        return None

    if len(tokens) < 3:
        raise ValueError("fs_query requires ACTION and PATH arguments")
    payload: dict[str, Any] = {
        "query_action": tokens[1],
        "path": tokens[2],
    }
    idx = 3

    while idx < len(tokens):
        option = tokens[idx]
        idx += 1
        if idx >= len(tokens):
            raise ValueError(f"missing value for {option}")
        value = tokens[idx]
        idx += 1

        if option == "--maxdepth":
            payload["maxdepth"] = _parse_int(value, option)
        elif option == "--kind":
            payload["kind"] = value
        elif option == "--pattern":
            payload["pattern"] = value
        elif option == "--limit":
            payload["limit"] = _parse_int(value, option)
        elif option == "--mode":
            payload["mode"] = value
        elif option == "--start-line":
            payload["start_line"] = _parse_int(value, option)
        elif option == "--line-count":
            payload["line_count"] = _parse_int(value, option)
        elif option == "--byte-offset":
            payload["byte_offset"] = _parse_int(value, option)
        elif option == "--byte-count":
            payload["byte_count"] = _parse_int(value, option)
        elif option == "--query":
            payload["query"] = value
        elif option == "--glob":
            payload["glob"] = value
        elif option == "--ignore-case":
            payload["ignore_case"] = _parse_bool(value)
        elif option == "--context-lines":
            payload["context_lines"] = _parse_int(value, option)
        else:
            raise ValueError(f"unknown option for fs_query: {option}")

    if payload.get("query_action") == "search" and not payload.get("query"):
        raise ValueError("fs_query search requires --query TEXT")
    return "fs_query", payload

def _ipc_request(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    ipc_root = Path(os.environ["CTF_AGENT_GEMINI_IPC_DIR"])
    requests_dir = ipc_root / "requests"
    responses_dir = ipc_root / "responses"
    requests_dir.mkdir(parents=True, exist_ok=True)
    responses_dir.mkdir(parents=True, exist_ok=True)

    request_id = uuid.uuid4().hex
    request_path = requests_dir / f"{request_id}.json"
    response_path = responses_dir / f"{request_id}.json"
    request_path.write_text(
        json.dumps({"id": request_id, "action": action, **payload}),
        encoding="utf-8",
    )

    timeout_seconds = float(os.environ.get("CTF_AGENT_GEMINI_IPC_TIMEOUT", "60"))
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if response_path.exists():
            data = json.loads(response_path.read_text(encoding="utf-8"))
            response_path.unlink(missing_ok=True)
            request_path.unlink(missing_ok=True)
            if not isinstance(data, dict):
                raise RuntimeError("Invalid IPC response payload")
            return data
        time.sleep(0.05)

    request_path.unlink(missing_ok=True)
    raise TimeoutError(f"Timed out waiting for Gemini hook IPC response for {action}")


def main() -> int:
    payload = json.load(sys.stdin)
    result = handle_hook(payload)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
