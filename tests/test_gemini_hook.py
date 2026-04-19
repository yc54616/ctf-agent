from __future__ import annotations

from backend.agents import gemini_hook


def test_before_tool_selection_allows_run_shell_command_only() -> None:
    result = gemini_hook.handle_hook({"hook_event_name": "BeforeToolSelection"})

    assert result["hookSpecificOutput"]["toolConfig"]["allowedFunctionNames"] == [
        "run_shell_command"
    ]


def test_before_tool_rewrites_shell_command(monkeypatch) -> None:
    monkeypatch.setenv("CTF_AGENT_GEMINI_CONTAINER_ID", "deadbeef")

    result = gemini_hook.handle_hook(
        {
            "hook_event_name": "BeforeTool",
            "tool_name": "run_shell_command",
            "tool_input": {"command": "ls /challenge"},
        }
    )

    rewritten = result["hookSpecificOutput"]["tool_input"]["command"]
    assert rewritten.startswith("docker exec -i ")
    assert "deadbeef" in rewritten
    assert "ls /challenge" in rewritten


def test_before_tool_report_flag_candidate_uses_ipc(monkeypatch) -> None:
    monkeypatch.setattr(
        gemini_hook,
        "_ipc_request",
        lambda action, payload: {"message": f"{action}:{payload['flag']}"},
    )

    result = gemini_hook.handle_hook(
        {
            "hook_event_name": "BeforeTool",
            "tool_name": "run_shell_command",
            "tool_input": {"command": "report_flag_candidate 'flag{test}'"},
        }
    )

    assert result["hookSpecificOutput"]["tool_input"]["command"] == (
        "printf '%s\\n' 'report_flag_candidate:flag{test}'"
    )


def test_before_tool_does_not_append_shared_findings(monkeypatch) -> None:
    monkeypatch.setenv("CTF_AGENT_GEMINI_CONTAINER_ID", "deadbeef")

    result = gemini_hook.handle_hook(
        {
            "hook_event_name": "BeforeTool",
            "tool_name": "run_shell_command",
            "tool_input": {"command": "ls /challenge"},
        }
    )

    rewritten = result["hookSpecificOutput"]["tool_input"]["command"]
    assert "docker exec -i " in rewritten
    assert "[Shared findings from other lanes]" not in rewritten


def test_before_tool_fs_query_uses_ipc(monkeypatch) -> None:
    monkeypatch.setattr(
        gemini_hook,
        "_ipc_request",
        lambda action, payload: {"output": f"{action}:{payload['query_action']}:{payload['path']}:{payload['maxdepth']}"},
    )

    result = gemini_hook.handle_hook(
        {
            "hook_event_name": "BeforeTool",
            "tool_name": "run_shell_command",
            "tool_input": {"command": "fs_query find /challenge/distfiles --maxdepth 5 --kind files"},
        }
    )

    rewritten = result["hookSpecificOutput"]["tool_input"]["command"]
    assert "printf '%s\\n'" in rewritten
    assert "fs_query:find:/challenge/distfiles:5" in rewritten


def test_before_tool_fs_query_search_requires_query() -> None:
    result = gemini_hook.handle_hook(
        {
            "hook_event_name": "BeforeTool",
            "tool_name": "run_shell_command",
            "tool_input": {"command": "fs_query search /challenge/distfiles"},
        }
    )

    rewritten = result["hookSpecificOutput"]["tool_input"]["command"]
    assert "Tool command error: fs_query search requires --query TEXT" in rewritten
