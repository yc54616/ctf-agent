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

SUBMIT_FLAG_RE = re.compile(r"""^submit_flag\s+['"]?(.+?)['"]?\s*$""", re.DOTALL)
NOTIFY_COORDINATOR_RE = re.compile(
    r"""^notify_coordinator\s+['"]?(.+?)['"]?\s*$""",
    re.DOTALL,
)


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

    submit_match = SUBMIT_FLAG_RE.match(command.strip())
    if submit_match:
        response = _ipc_request("submit_flag", {"flag": submit_match.group(1).strip()})
        return _rewrite_tool_input(f"echo {shlex.quote(response['message'])}")

    notify_match = NOTIFY_COORDINATOR_RE.match(command.strip())
    if notify_match:
        response = _ipc_request(
            "notify_coordinator",
            {"message": notify_match.group(1).strip()},
        )
        return _rewrite_tool_input(f"echo {shlex.quote(response['message'])}")

    container_id = os.environ["CTF_AGENT_GEMINI_CONTAINER_ID"]
    rewritten = (
        f"docker exec -i {shlex.quote(container_id)} "
        f"bash -lc {shlex.quote(command)}"
    )
    return _rewrite_tool_input(rewritten)


def _rewrite_tool_input(command: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "BeforeTool",
            "tool_input": {"command": command},
        }
    }

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
