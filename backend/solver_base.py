"""Solver result type, status constants, and solver protocol — shared across all backends."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

# Status constants
FLAG_FOUND = "flag_found"
FLAG_CANDIDATE = "flag_candidate"
GAVE_UP = "gave_up"
CANCELLED = "cancelled"
ERROR = "error"
QUOTA_ERROR = "quota_error"

# Flag confirmation markers from CTFd
CORRECT_MARKERS = ("CORRECT", "ALREADY SOLVED")
READ_ONLY_TOOL_NAMES = frozenset(
    {
        "fs_query",
        "view_image",
    }
)


def is_read_only_tool(tool_name: str) -> bool:
    return tool_name in READ_ONLY_TOOL_NAMES


def _compact_runtime_text(value: object, limit: int = 160) -> str:
    text = str(value or "")
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit]


def summarize_tool_input(tool_name: str, payload: object) -> str:
    if isinstance(payload, dict):
        payload_dict = {str(key): value for key, value in payload.items()}
        if tool_name == "fs_query":
            action = payload_dict.get("action")
            path = payload_dict.get("path")
            if action:
                target = f"{action} {path}".strip() if path else str(action)
                return _compact_runtime_text(f"{tool_name} {target}")
        for key in ("command", "path", "filename", "url", "flag", "message", "uuid"):
            value = payload_dict.get(key)
            if value:
                if key == "command":
                    return _compact_runtime_text(value)
                return _compact_runtime_text(f"{tool_name} {value}")
    if isinstance(payload, str):
        return _compact_runtime_text(payload)
    return _compact_runtime_text(tool_name)


def summarize_tool_result(value: object) -> str:
    return _compact_runtime_text(value)


def lifecycle_for_result(status: str) -> str:
    if status == FLAG_FOUND:
        return "won"
    if status == FLAG_CANDIDATE:
        return "finished"
    if status == CANCELLED:
        return "cancelled"
    if status == QUOTA_ERROR:
        return "quota_error"
    if status == ERROR:
        return "error"
    if status == GAVE_UP:
        return "finished"
    return "finished"


@dataclass
class LaneRuntimeStatus:
    lifecycle: str = "starting"
    step_count: int = 0
    current_tool: str = ""
    current_command: str = ""
    current_started_at: float | None = None
    last_tool: str = ""
    last_command: str = ""
    last_completed_at: float | None = None
    last_exit_hint: str = ""
    read_only_streak: int = 0
    last_progress_kind: str = "turn_start"

    def mark_ready(self) -> None:
        if self.lifecycle == "starting":
            self.lifecycle = "idle"

    def mark_busy(self, tool_name: str, command_preview: str = "", step_count: int | None = None) -> None:
        self.lifecycle = "busy"
        if step_count is not None:
            self.step_count = step_count
        self.current_tool = _compact_runtime_text(tool_name, limit=64)
        self.current_command = _compact_runtime_text(command_preview)
        self.current_started_at = time.time()

    def mark_idle(self, exit_hint: str = "") -> None:
        self._roll_current_to_last()
        self.lifecycle = "idle"
        self.last_exit_hint = summarize_tool_result(exit_hint) if exit_hint else ""

    def mark_terminal(self, lifecycle: str, exit_hint: str = "") -> None:
        self._roll_current_to_last()
        self.lifecycle = lifecycle
        if exit_hint:
            self.last_exit_hint = summarize_tool_result(exit_hint)
        elif lifecycle in {"cancelled", "error", "quota_error", "won", "finished"}:
            self.last_exit_hint = self.last_exit_hint or lifecycle

    def snapshot(self) -> dict[str, object]:
        return {
            "lifecycle": self.lifecycle,
            "step_count": self.step_count,
            "current_tool": self.current_tool,
            "current_command": self.current_command,
            "current_started_at": self.current_started_at,
            "last_tool": self.last_tool,
            "last_command": self.last_command,
            "last_completed_at": self.last_completed_at,
            "last_exit_hint": self.last_exit_hint,
            "read_only_streak": self.read_only_streak,
            "last_progress_kind": self.last_progress_kind,
        }

    def _roll_current_to_last(self) -> None:
        if self.current_tool:
            self.last_tool = self.current_tool
        if self.current_command:
            self.last_command = self.current_command
        self.current_tool = ""
        self.current_command = ""
        self.current_started_at = None
        self.last_completed_at = time.time()


@dataclass
class SolverResult:
    flag: str | None
    status: str
    findings_summary: str
    step_count: int
    cost_usd: float
    log_path: str
    candidate_flag: str | None = None
    candidate_evidence: str = ""
    candidate_confidence: str = ""


class SolverProtocol(Protocol):
    """Common interface for all solver backends (Pydantic AI, Claude SDK, Codex)."""

    model_spec: str
    agent_name: str
    sandbox: object

    async def start(self) -> None: ...
    async def run_until_done_or_gave_up(self) -> SolverResult: ...
    def bump(self, insights: str) -> None: ...
    def bump_advisory(self, insights: str) -> None: ...
    def get_runtime_status(self) -> dict[str, object]: ...
    def mark_terminal_status(self, status: str) -> None: ...
    async def stop_process(self) -> None: ...
    async def stop(self) -> None: ...
