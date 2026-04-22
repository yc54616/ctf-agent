"""Codex solver — drives `codex app-server` via JSON-RPC 2.0 over stdio.

Protocol shapes verified against codex-cli 0.116.0 schema:
- thread/start returns {thread: {id, ...}, ...}
- turn/start takes {threadId, input: UserInput[]}
- Dynamic tool calls arrive as item/tool/call server requests with DynamicToolCallParams
  {tool, arguments, callId, threadId, turnId}
- Client responds with DynamicToolCallResponse {contentItems: [{type, text}], success}
- Token usage via thread/tokenUsage/updated notification
- Commentary/reasoning stream via item/agentMessage/delta and item/reasoning/summaryTextDelta
- Turn completion via turn/completed notification with {threadId, turn: Turn}
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from backend.agents.codex_rpc_io import read_jsonrpc_line
from backend.cost_tracker import CostTracker
from backend.loop_detect import LoopDetector
from backend.models import model_id_from_spec, supports_vision
from backend.output_types import solver_output_json_schema
from backend.platforms import PlatformClient
from backend.prompts import (
    ChallengeMeta,
    build_lane_bump_prompt,
    build_named_tool_sandbox_preamble,
    build_prompt,
    list_distfiles,
)
from backend.sandbox import DockerSandbox
from backend.solver_base import (
    CANCELLED,
    ERROR,
    FLAG_CANDIDATE,
    FLAG_FOUND,
    GAVE_UP,
    QUOTA_ERROR,
    LaneRuntimeStatus,
    SolverResult,
    candidate_report_was_accepted,
    lifecycle_for_result,
    summarize_tool_input,
    summarize_tool_result,
)
from backend.tools.core import (
    do_bash,
    do_view_image,
)
from backend.tracing import SolverTracer

logger = logging.getLogger(__name__)

_rpc_counter = itertools.count(1)
WATCHDOG_SAMPLE_SECONDS = 5
WATCHDOG_TURN_START_SECONDS = 120.0
WATCHDOG_TURN_ACTIVITY_SECONDS = 300.0
WATCHDOG_SHORT_TOOL_SECONDS = 60.0
WATCHDOG_DEFAULT_TOOL_SECONDS = 120.0
WATCHDOG_TOOL_TIMEOUT_PADDING_SECONDS = 30.0
WATCHDOG_MAX_BASH_TOOL_SECONDS = 600.0
PROACTIVE_COMPACT_CONTEXT_FRACTION = 0.7
PROACTIVE_COMPACT_ABSOLUTE_TOKENS = 250_000
THREAD_STATE_VERSION = 2

WATCHDOG_SHORT_TOOLS = {
    "report_flag_candidate",
    "notify_coordinator",
}

# Per-model reasoning effort (only for models that support it)
REASONING_EFFORT: dict[str, str] = {
    "gpt-5.3-codex": "xhigh",
}


def _next_id() -> int:
    return next(_rpc_counter)


# DynamicToolSpec[] for thread/start
BASE_SANDBOX_TOOLS = [
    {
        "name": "bash",
        "description": "Execute a bash command in the Docker sandbox.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout_seconds": {"type": "integer", "default": 60},
            },
            "required": ["command"],
        },
    },
    {
        "name": "report_flag_candidate",
        "description": "Queue a candidate flag for advisor and coordinator review without submitting it automatically.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "flag": {"type": "string"},
                "evidence": {"type": "string", "default": ""},
                "confidence": {"type": "string", "default": "medium"},
            },
            "required": ["flag"],
        },
    },
    {
        "name": "notify_coordinator",
        "description": "Send a strategic message to the coordinator (e.g. flag format discovery, shared vulnerability, request for help).",
        "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]},
    },
]

VISION_TOOL = {
    "name": "view_image",
    "description": "View an image file from the sandbox for visual/steg analysis.",
    "inputSchema": {"type": "object", "properties": {"filename": {"type": "string"}}, "required": ["filename"]},
}


def _sandbox_tools(use_vision: bool) -> list[dict[str, Any]]:
    tools = list(BASE_SANDBOX_TOOLS)
    if use_vision:
        tools.append(VISION_TOOL)
    return tools


SANDBOX_TOOLS = BASE_SANDBOX_TOOLS


class CodexSolver:
    """Codex solver speaking the actual app-server JSON-RPC 2.0 protocol."""

    def __init__(
        self,
        model_spec: str,
        challenge_dir: str,
        meta: ChallengeMeta,
        ctfd: PlatformClient,
        cost_tracker: CostTracker,
        settings: object,
        cancel_event: asyncio.Event | None = None,
        no_submit: bool = False,
        report_flag_candidate_fn=None,
        message_bus=None,
        notify_coordinator=None,
        sandbox: DockerSandbox | None = None,
        initial_step_count: int = 0,
    ) -> None:
        self.model_spec = model_spec
        self.model_id = model_id_from_spec(model_spec)
        self.challenge_dir = challenge_dir
        self.meta = meta
        self.message_bus = message_bus
        self.notify_coordinator = notify_coordinator
        self.ctfd = ctfd
        self.cost_tracker = cost_tracker
        self.settings = settings
        self.cancel_event = cancel_event or asyncio.Event()
        self.no_submit = no_submit
        self.report_flag_candidate_fn = report_flag_candidate_fn

        self.sandbox = sandbox or DockerSandbox(
            image=getattr(settings, "sandbox_image", "ctf-sandbox"),
            challenge_dir=challenge_dir,
            memory_limit=getattr(settings, "container_memory_limit", "4g"),
            exec_output_spill_threshold_bytes=getattr(settings, "exec_output_spill_threshold_bytes", 65_536),
            read_file_spill_threshold_bytes=getattr(settings, "read_file_spill_threshold_bytes", 262_144),
            artifact_preview_bytes=getattr(settings, "artifact_preview_bytes", 8_192),
        )
        self.use_vision = supports_vision(model_spec)
        self.loop_detector = LoopDetector()
        self.tracer = SolverTracer(meta.name, self.model_id)
        self.agent_name = f"{meta.name}/{self.model_id}"
        self._runtime = LaneRuntimeStatus()

        self._proc: asyncio.subprocess.Process | None = None
        self._thread_id: str | None = None
        self._step_count = initial_step_count
        self._flag: str | None = None
        self._candidate_flag: str | None = None
        self._candidate_evidence: str = ""
        self._candidate_confidence: str = ""
        self._confirmed = False
        self._findings = ""
        self._cost_usd = 0.0
        self._bump_insights: str | None = None
        self._advisory_bump_insights: str | None = None
        self._operator_bump_insights: str | None = None
        self._structured_output: dict | None = None
        self._turn_error: str | None = None
        self._compact_requested = False
        self._compact_task: asyncio.Task | None = None
        self._pending_responses: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._turn_done: asyncio.Event = asyncio.Event()
        self._watchdog_phase = ""
        self._watchdog_step = 0
        self._watchdog_tool = ""
        self._watchdog_started_monotonic = 0.0
        self._watchdog_started_at: float | None = None
        self._watchdog_deadline_seconds = 0.0
        self._agent_message_buffers: dict[str, str] = {}
        self._reasoning_summary_buffers: dict[tuple[str, int], str] = {}
        self._turn_commentary_events = 0
        thread_state = os.environ.get("CTF_AGENT_CODEX_THREAD_PATH", "").strip()
        self._thread_state_path = Path(thread_state) if thread_state else None

    def _build_thread_params(self, system_prompt: str) -> dict[str, Any]:
        tools = _sandbox_tools(self.use_vision)
        tool_names = [str(t["name"]) for t in tools]
        sandbox_preamble = build_named_tool_sandbox_preamble(tool_names)
        thread_params: dict[str, Any] = {
            "model": self.model_id,
            "personality": "pragmatic",
            "baseInstructions": sandbox_preamble + system_prompt,
            "cwd": "/challenge",
            "approvalPolicy": "on-request",
            "sandbox": "read-only",
            "dynamicTools": tools,
        }
        reasoning = REASONING_EFFORT.get(self.model_id)
        if reasoning:
            thread_params["reasoningEffort"] = reasoning
        return thread_params

    async def start(self) -> None:
        await self.sandbox.start()

        arch_result = await self.sandbox.exec("uname -m", timeout_s=10)
        container_arch = arch_result.stdout.strip() or "unknown"

        distfile_names = list_distfiles(self.challenge_dir)
        system_prompt = build_prompt(
            self.meta, distfile_names, container_arch=container_arch,
            has_named_tools=True,
        )

        self._proc = await asyncio.create_subprocess_exec(
            "codex", "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        self._reader_task = asyncio.create_task(self._read_loop())

        # Initialize handshake: send initialize request, then initialized notification
        await self._rpc("initialize", {
            "clientInfo": {"name": "ctf-agent", "version": "2.0.0"},
            "capabilities": {"experimentalApi": True},
        })
        await self._send_notification("initialized", {})

        # thread/start — personality is enum, system prompt in baseInstructions
        thread_params = self._build_thread_params(system_prompt)
        resume_thread_id = self._load_thread_id()
        resp: dict[str, Any]
        if resume_thread_id:
            try:
                resp = await self._rpc("thread/resume", {"threadId": resume_thread_id})
                self._thread_id = resp.get("result", {}).get("thread", {}).get("id", "") or resume_thread_id
                self.tracer.event("thread_resumed", thread_id=self._thread_id)
            except Exception as exc:
                logger.warning(
                    "[%s] Codex thread resume failed for %s: %s",
                    self.agent_name,
                    resume_thread_id,
                    exc,
                )
                resp = await self._rpc("thread/start", thread_params)
                self._thread_id = resp.get("result", {}).get("thread", {}).get("id", "")
                self.tracer.event("thread_started_fresh", previous_thread_id=resume_thread_id)
        else:
            resp = await self._rpc("thread/start", thread_params)
            self._thread_id = resp.get("result", {}).get("thread", {}).get("id", "")
            self.tracer.event("thread_started")
        self._persist_thread_id()

        self._runtime.mark_ready()
        self.tracer.event("start", challenge=self.meta.name, model=self.model_id)
        logger.info(f"[{self.agent_name}] Codex solver started (thread={self._thread_id})")

    def _load_thread_id(self) -> str:
        if not self._thread_state_path or not self._thread_state_path.exists():
            return ""
        try:
            payload = json.loads(self._thread_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        if isinstance(payload, dict):
            if int(payload.get("version", 0) or 0) != THREAD_STATE_VERSION:
                return ""
            return str(payload.get("thread_id") or "").strip()
        return ""

    def _persist_thread_id(self) -> None:
        if not self._thread_state_path or not self._thread_id:
            return
        self._thread_state_path.parent.mkdir(parents=True, exist_ok=True)
        self._thread_state_path.write_text(
            json.dumps({"thread_id": self._thread_id, "version": THREAD_STATE_VERSION}, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _should_request_compaction(context_window: int | None, total_tokens: int) -> bool:
        if total_tokens <= 0:
            return False
        if context_window and total_tokens > context_window * PROACTIVE_COMPACT_CONTEXT_FRACTION:
            return True
        return total_tokens >= PROACTIVE_COMPACT_ABSOLUTE_TOKENS

    async def _rpc(self, method: str, params: dict | None = None) -> dict:
        assert self._proc and self._proc.stdin
        msg_id = _next_id()
        msg: dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            msg["params"] = params

        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending_responses[msg_id] = future

        self.tracer.rpc_message("out", msg)
        self._proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self._proc.stdin.drain()
        try:
            return await asyncio.wait_for(future, timeout=300)
        finally:
            self._pending_responses.pop(msg_id, None)

    async def _respond_to_request(self, request_id: int, result: Any) -> None:
        """Send a JSON-RPC response to a server request (e.g. item/tool/call)."""
        assert self._proc and self._proc.stdin
        resp = {"id": request_id, "result": result}
        self.tracer.rpc_message("out", resp)
        self._proc.stdin.write((json.dumps(resp) + "\n").encode())
        await self._proc.stdin.drain()

    async def _send_notification(self, method: str, params: dict | None = None) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        assert self._proc and self._proc.stdin
        msg: dict[str, Any] = {"method": method}
        if params:
            msg["params"] = params
        self.tracer.rpc_message("out", msg)
        self._proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        """Read JSON-RPC messages: responses, notifications, and server requests."""
        assert self._proc and self._proc.stdout
        while True:
            line = await read_jsonrpc_line(self._proc.stdout)
            if not line:
                self._turn_done.set()
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.tracer.rpc_message("in", msg)

            msg_id = msg.get("id")
            if msg_id is not None and ("result" in msg or "error" in msg):
                future = self._pending_responses.pop(msg_id, None)
                if future and not future.done():
                    if "error" in msg:
                        err = msg["error"]
                        logger.error(f"[{self.agent_name}] RPC error: {err}")
                        future.set_exception(RuntimeError(f"Codex RPC error: {err}"))
                    else:
                        future.set_result(msg)
                continue

            method = msg.get("method", "")
            params = msg.get("params", {})

            # Server requests that require an explicit client response.
            if msg_id is not None:
                if method == "item/tool/call":
                    await self._handle_tool_call(msg_id, params)
                    continue
                if method in {
                    "item/commandExecution/requestApproval",
                    "item/fileChange/requestApproval",
                }:
                    await self._handle_approval_request(msg_id, method, params)
                    continue

            await self._handle_notification(method, params)

    async def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "item/started":
            item = params.get("item")
            if not isinstance(item, dict):
                item = {}
            self.tracer.event(
                "rpc_item_started",
                item_type=str(item.get("type") or ""),
                item_id=str(item.get("id") or ""),
                step=self._step_count,
            )
            return

        if method == "item/agentMessage/delta":
            item_id = str(params.get("itemId") or "")
            delta = str(params.get("delta") or "")
            if item_id and delta:
                self._turn_commentary_events += 1
                current = self._agent_message_buffers.get(item_id, "")
                current = f"{current}{delta}"
                self._agent_message_buffers[item_id] = current
                self._touch_watchdog()
                self._runtime.append_commentary(delta)
            return

        if method == "item/reasoning/summaryPartAdded":
            item_id = str(params.get("itemId") or "")
            try:
                summary_index = int(params.get("summaryIndex", 0) or 0)
            except Exception:
                summary_index = 0
            if item_id:
                self._reasoning_summary_buffers.setdefault((item_id, summary_index), "")
            return

        if method == "item/reasoning/summaryTextDelta":
            item_id = str(params.get("itemId") or "")
            try:
                summary_index = int(params.get("summaryIndex", 0) or 0)
            except Exception:
                summary_index = 0
            delta = str(params.get("delta") or "")
            if item_id and delta:
                self._turn_commentary_events += 1
                key = (item_id, summary_index)
                current = self._reasoning_summary_buffers.get(key, "")
                current = f"{current}{delta}"
                self._reasoning_summary_buffers[key] = current
                self._touch_watchdog()
                self._runtime.append_commentary(delta)
            return

        if method == "item/reasoning/textDelta":
            # Do not surface raw reasoning text. If this path appears, the app-server is
            # emitting a lower-level stream than the summary/commentary channels we show.
            self.tracer.event("reasoning_text_delta_seen", step=self._step_count)
            return

        # Notification: item completed — assistant text arrives here
        if method == "item/completed":
            item = params.get("item", params)
            item_type = str(item.get("type") or "")
            self.tracer.event(
                "rpc_item_completed",
                item_type=item_type,
                item_id=str(item.get("id") or ""),
                step=self._step_count,
            )
            if item_type == "agentMessage":
                item_id = str(item.get("id") or "")
                text = str(item.get("text") or "")
                phase = item.get("phase")  # "commentary" | "final_answer" | null
                if not text and item_id:
                    text = self._agent_message_buffers.get(item_id, "")
                if item_id:
                    self._agent_message_buffers.pop(item_id, None)
                if text:
                    self._touch_watchdog()
                    self._runtime.note_commentary(text)
                    if phase == "commentary":
                        self.tracer.model_response(text, self._step_count)
                    self._findings = text[:2000]
                    if phase != "commentary" and text.lstrip()[:1] == "{":
                        try:
                            parsed = json.loads(text)
                            if isinstance(parsed, dict) and "type" in parsed:
                                self._structured_output = parsed
                        except (json.JSONDecodeError, ValueError):
                            pass
                return
            if item_type == "reasoning":
                text = self._extract_reasoning_summary_text(item)
                if text:
                    self._touch_watchdog()
                    self._runtime.note_commentary(text)
                return
            return

        # Notification: turn completed — signals the turn is done
        if method == "turn/completed":
            turn = params.get("turn", {})
            status = turn.get("status", "")
            if status == "failed":
                error = turn.get("error", {})
                if isinstance(error, dict):
                    # Include all error fields for robust quota classification
                    parts = [error.get("message", "unknown error")]
                    codex_info = error.get("codexErrorInfo", {})
                    if isinstance(codex_info, dict):
                        parts.append(str(codex_info))
                    additional = error.get("additionalDetails")
                    if additional:
                        parts.append(str(additional))
                    error_msg = " | ".join(parts)
                else:
                    error_msg = str(error)
                self._turn_error = error_msg
                logger.error(f"[{self.agent_name}] Turn failed: {error_msg}")
                self.tracer.event("turn_failed", error=error_msg, step=self._step_count)
                self._findings = f"Turn failed: {error_msg}"
                self._structured_output = None
            else:
                self._turn_error = None
            self._turn_done.set()
            return

        # Notification: token usage updated
        # params: {threadId, turnId, tokenUsage: {last: TokenUsageBreakdown, total: TokenUsageBreakdown}}
        if method == "thread/tokenUsage/updated":
            token_usage = params.get("tokenUsage", {})
            last = token_usage.get("last", {})
            total = token_usage.get("total", {})

            context_window = token_usage.get("modelContextWindow")
            total_tokens = total.get("totalTokens", 0)
            if (
                self._should_request_compaction(context_window, total_tokens)
                and not self._compact_requested
            ):
                self._compact_requested = True
                logger.info(f"[{self.agent_name}] Requesting compaction ({total_tokens}/{context_window} tokens)")
                self._compact_task = asyncio.create_task(
                    self._request_compaction(total_tokens=total_tokens, context_window=context_window),
                    name=f"compact-{self.agent_name}",
                )

            self.cost_tracker.record_tokens(
                self.agent_name, self.model_id,
                input_tokens=last.get("inputTokens", 0),
                output_tokens=last.get("outputTokens", 0),
                cache_read_tokens=last.get("cachedInputTokens", 0),
                provider_spec="codex",
            )
            agent_usage = self.cost_tracker.by_agent.get(self.agent_name)
            self._cost_usd = agent_usage.cost_usd if agent_usage else 0.0
            self.tracer.usage(
                total.get("inputTokens", 0),
                total.get("outputTokens", 0),
                total.get("cachedInputTokens", 0),
                self._cost_usd,
            )
            return

        if method.startswith("item/"):
            self.tracer.event("rpc_notification_ignored", method=method, step=self._step_count)

    async def _request_compaction(self, *, total_tokens: int, context_window: int | None) -> None:
        try:
            await self._rpc("thread/compact/start", {"threadId": self._thread_id})
            self.tracer.event("compact_requested", tokens=total_tokens, window=context_window)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[{self.agent_name}] Compaction request failed: {e}")
            self._compact_requested = False
        finally:
            self._compact_task = None

    def _extract_reasoning_summary_text(self, item: dict[str, Any]) -> str:
        summary = item.get("summary")
        if not isinstance(summary, list):
            return ""
        parts: list[str] = []
        for part in summary:
            if not isinstance(part, dict):
                continue
            text = str(part.get("text") or "").strip()
            if text:
                parts.append(text)
        return " ".join(parts).strip()

    def _clear_watchdog(self) -> None:
        self._watchdog_phase = ""
        self._watchdog_step = 0
        self._watchdog_tool = ""
        self._watchdog_started_monotonic = 0.0
        self._watchdog_started_at = None
        self._watchdog_deadline_seconds = 0.0

    def _arm_watchdog(
        self,
        *,
        phase: str,
        step: int,
        deadline_seconds: float,
        tool: str = "",
    ) -> None:
        self._watchdog_phase = phase
        self._watchdog_step = step
        self._watchdog_tool = tool
        self._watchdog_started_monotonic = time.monotonic()
        self._watchdog_started_at = time.time()
        self._watchdog_deadline_seconds = float(deadline_seconds)

    def _touch_watchdog(self) -> None:
        if self._watchdog_phase in {"turn_start", "turn_active"}:
            self._watchdog_started_monotonic = time.monotonic()
            self._watchdog_started_at = time.time()

    @staticmethod
    def _tool_call_watchdog_seconds(tool_name: str, args: dict[str, Any]) -> float:
        if tool_name == "bash":
            try:
                timeout_seconds = int(args.get("timeout_seconds", 60) or 60)
            except Exception:
                timeout_seconds = 60
            return float(
                max(
                    WATCHDOG_DEFAULT_TOOL_SECONDS,
                    min(
                        timeout_seconds + int(WATCHDOG_TOOL_TIMEOUT_PADDING_SECONDS),
                        int(WATCHDOG_MAX_BASH_TOOL_SECONDS),
                    ),
                )
            )
        if tool_name in WATCHDOG_SHORT_TOOLS:
            return WATCHDOG_SHORT_TOOL_SECONDS
        return WATCHDOG_DEFAULT_TOOL_SECONDS

    @staticmethod
    def _turn_activity_watchdog_seconds() -> float:
        return WATCHDOG_TURN_ACTIVITY_SECONDS

    def _watchdog_expired(self) -> bool:
        if not self._watchdog_phase:
            return False
        return (time.monotonic() - self._watchdog_started_monotonic) > self._watchdog_deadline_seconds

    def _watchdog_error(self) -> tuple[str, str]:
        deadline = int(self._watchdog_deadline_seconds)
        if self._watchdog_phase == "turn_start":
            return f"stalled: turn_start_inactivity after {deadline}s", "turn_start_inactivity"
        if self._watchdog_phase == "tool_call":
            tool_suffix = f" ({self._watchdog_tool})" if self._watchdog_tool else ""
            return f"stalled: tool_call_timeout after {deadline}s{tool_suffix}", "tool_call_timeout"
        return f"stalled: turn_inactivity after {deadline}s", "turn_inactivity"

    async def _watch_turn_progress(self) -> None:
        while not self._turn_done.is_set():
            await asyncio.sleep(WATCHDOG_SAMPLE_SECONDS)
            if self._turn_done.is_set():
                return
            if not self._watchdog_expired():
                continue

            reason, kind = self._watchdog_error()
            self._turn_error = reason
            self._findings = reason
            self._structured_output = None
            self._runtime.mark_terminal("error", reason)
            self.tracer.event(
                "turn_stalled",
                reason=reason,
                step=self._step_count,
                watchdog_phase=self._watchdog_phase,
                watchdog_kind=kind,
                deadline_seconds=int(self._watchdog_deadline_seconds),
                tool=self._watchdog_tool or None,
            )
            self._turn_done.set()
            return

    async def _handle_tool_call(self, request_id: int, params: dict) -> None:
        """Handle item/tool/call server request. Params are DynamicToolCallParams."""
        tool_name = params.get("tool", "")
        try:
            args = params.get("arguments", {})
            if isinstance(args, str):
                args = json.loads(args)
        except Exception:
            args = {}

        self._step_count += 1
        self._arm_watchdog(
            phase="tool_call",
            step=self._step_count,
            deadline_seconds=self._tool_call_watchdog_seconds(tool_name, args),
            tool=tool_name,
        )
        self.tracer.tool_call(tool_name, args, self._step_count)

        loop_status = self.loop_detector.check(tool_name, args)
        if loop_status == "break":
            self.tracer.event("loop_break", tool=tool_name, step=self._step_count)
            result = "Loop detected — try a completely different approach."
        else:
            result = await self._exec_tool(tool_name, args)
            if loop_status == "warn" and isinstance(result, str):
                from backend.loop_detect import LOOP_WARNING_MESSAGE
                result = f"{result}\n\n{LOOP_WARNING_MESSAGE}"

        # Build content items — handle image tuples from view_image
        if isinstance(result, tuple):
            image_bytes, mime_type = result
            data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode()}"
            content_items = [{"type": "inputImage", "imageUrl": data_url}]
            self.tracer.tool_result(tool_name, f"image:{mime_type}:{len(image_bytes)}b", self._step_count)
        else:
            result_text = str(result)
            self.tracer.tool_result(tool_name, result_text[:500], self._step_count)

            content_items = [{"type": "inputText", "text": result_text}]

        await self._respond_to_request(request_id, {
            "contentItems": content_items,
            "success": True,
        })
        self._arm_watchdog(
            phase="turn_active",
            step=self._step_count,
            deadline_seconds=self._turn_activity_watchdog_seconds(),
            tool=tool_name,
        )

    async def _handle_approval_request(self, request_id: int, method: str, params: dict[str, Any]) -> None:
        """Decline built-in approval requests so Codex falls back to our sandbox tools."""
        item_id = str(params.get("itemId") or "")
        reason = str(params.get("reason") or "").strip()
        command = str(params.get("command") or "").strip()
        self._touch_watchdog()
        self.tracer.event(
            "approval_request_declined",
            method=method,
            item_id=item_id or None,
            step=self._step_count,
            reason=reason or None,
            command=command[:200] or None,
        )
        logger.info(
            "[%s] Declining Codex approval request (%s)%s%s",
            self.agent_name,
            method,
            f" reason={reason!r}" if reason else "",
            f" command={command[:120]!r}" if command else "",
        )
        await self._respond_to_request(request_id, {"decision": "decline"})
        self._arm_watchdog(
            phase="turn_active",
            step=self._step_count,
            deadline_seconds=self._turn_activity_watchdog_seconds(),
        )

    async def _exec_tool(self, name: str, args: dict) -> str | tuple[bytes, str]:
        self._runtime.mark_busy(name, summarize_tool_input(name, args), step_count=self._step_count)
        try:
            if name == "bash":
                result = await do_bash(
                    self.sandbox,
                    args.get("command", ""),
                    args.get("timeout_seconds", 60),
                )
            elif name == "report_flag_candidate":
                result = await self._report_flag_candidate(
                    str(args.get("flag", "")),
                    evidence=str(args.get("evidence", "")),
                    confidence=str(args.get("confidence", "medium")),
                )
            elif name == "view_image":
                result = await do_view_image(self.sandbox, args.get("filename", ""), use_vision=self.use_vision)
            elif name == "notify_coordinator":
                if self.notify_coordinator:
                    await self.notify_coordinator(args.get("message", ""))
                    result = "Message sent to coordinator."
                else:
                    result = "No coordinator connected."
            else:
                result = f"Unknown tool: {name}"
        except Exception as exc:
            self._runtime.mark_idle(str(exc))
            raise

        if isinstance(result, tuple):
            image_bytes, mime_type = result
            self._runtime.mark_idle(f"image:{mime_type}:{len(image_bytes)}b")
            return result

        self._runtime.mark_idle(summarize_tool_result(result))
        return result

    async def _report_flag_candidate(
        self,
        flag: str,
        *,
        evidence: str = "",
        confidence: str = "medium",
    ) -> str:
        cleaned_flag = flag.strip()
        if not cleaned_flag:
            return "Flag candidate rejected: empty flag."
        if not self.report_flag_candidate_fn:
            self._candidate_flag = cleaned_flag
            self._candidate_evidence = evidence.strip()
            self._candidate_confidence = confidence.strip() or "medium"
            return f"Flag candidate noted locally: {cleaned_flag}"
        ack = await self.report_flag_candidate_fn(
            cleaned_flag,
            evidence.strip(),
            confidence.strip() or "medium",
            self._step_count,
            self.tracer.path,
        )
        if candidate_report_was_accepted(ack):
            self._candidate_flag = cleaned_flag
            self._candidate_evidence = evidence.strip()
            self._candidate_confidence = confidence.strip() or "medium"
        return ack

    def get_runtime_status(self) -> dict[str, object]:
        snapshot = self._runtime.snapshot()
        snapshot["watchdog_phase"] = self._watchdog_phase
        snapshot["watchdog_tool"] = self._watchdog_tool
        snapshot["watchdog_started_at"] = self._watchdog_started_at
        return snapshot

    def mark_terminal_status(self, status: str) -> None:
        self._runtime.mark_terminal(lifecycle_for_result(status), status)

    def _consume_turn_prompt(self) -> str:
        if self._operator_bump_insights:
            prompt_text = build_lane_bump_prompt(self._operator_bump_insights, operator=True)
            self._operator_bump_insights = None
            self._advisory_bump_insights = None
            self._bump_insights = None
            return prompt_text

        if self._advisory_bump_insights:
            prompt_text = build_lane_bump_prompt(self._advisory_bump_insights, advisory=True)
            self._advisory_bump_insights = None
            return prompt_text

        if self._bump_insights:
            prompt_text = build_lane_bump_prompt(self._bump_insights)
            self._bump_insights = None
            return prompt_text

        if self._step_count == 0:
            return "Solve this CTF challenge."

        return "Continue solving. Try a different approach."

    async def run_until_done_or_gave_up(self) -> SolverResult:
        if not self._proc:
            await self.start()
        assert self._thread_id

        t0 = time.monotonic()
        prompt_text = self._consume_turn_prompt()

        try:
            self._turn_done.clear()
            self._structured_output = None
            self._turn_error = None
            self._turn_commentary_events = 0
            self._clear_watchdog()
            await self._rpc("turn/start", {
                "threadId": self._thread_id,
                "input": [{"type": "text", "text": prompt_text}],
                "outputSchema": solver_output_json_schema(),
            })
            self._arm_watchdog(
                phase="turn_start",
                step=self._step_count,
                deadline_seconds=WATCHDOG_TURN_START_SECONDS,
            )
            watchdog_task = asyncio.create_task(self._watch_turn_progress())
            try:
                await self._turn_done.wait()
            finally:
                watchdog_task.cancel()
                await asyncio.gather(watchdog_task, return_exceptions=True)

            duration = time.monotonic() - t0
            self.tracer.event("turn_complete", duration=round(duration, 1), steps=self._step_count)
            self.tracer.event(
                "turn_commentary_summary",
                commentary_events=self._turn_commentary_events,
                step=self._step_count,
            )
            if not (self._turn_error or "").startswith("stalled:"):
                self._clear_watchdog()

            if self._turn_error:
                err = self._turn_error.lower()
                # Context overflow is terminal — don't fallback, just error
                if "context_length" in err or "context window" in err:
                    return self._result(ERROR)
                if any(k in err for k in ("quota", "rate", "capacity", "usage")):
                    return self._result(QUOTA_ERROR)
                return self._result(ERROR)

            if self._structured_output and self._structured_output.get("type") == "flag_candidate":
                candidate_flag = str(self._structured_output.get("flag") or "").strip()
                candidate_evidence = str(self._structured_output.get("method", "") or "").strip()
                if candidate_flag:
                    ack = await self._report_flag_candidate(
                        candidate_flag,
                        evidence=candidate_evidence,
                        confidence="medium",
                    )
                    self._findings = (
                        f"Flag candidate via {candidate_evidence or '?'}: {candidate_flag}\n{ack}"
                    )[:2000]
                    if candidate_report_was_accepted(ack):
                        return self._result(FLAG_CANDIDATE)

            if self._confirmed and self._flag:
                return self._result(FLAG_FOUND)
            return self._result(GAVE_UP)

        except asyncio.CancelledError:
            return self._result(CANCELLED)
        except Exception as e:
            error_str = str(e)
            logger.error(f"[{self.agent_name}] Error: {e}", exc_info=True)
            self._findings = f"Error: {e}"
            self.tracer.event("error", error=error_str)
            if "quota" in error_str.lower() or "rate" in error_str.lower():
                return self._result(QUOTA_ERROR)
            return self._result(ERROR)

    def bump(self, insights: str) -> None:
        if self._bump_insights and insights not in self._bump_insights:
            self._bump_insights = f"{self._bump_insights}\n\n---\n\n{insights}"
        else:
            self._bump_insights = insights
        self.loop_detector.reset()
        self.tracer.event("bump", source="auto", insights=insights[:500])

    def bump_advisory(self, insights: str) -> None:
        self._advisory_bump_insights = insights
        self.loop_detector.reset()
        self.tracer.event("bump", source="auto", channel="advisory", insights=insights[:500])

    def bump_operator(self, insights: str) -> None:
        self._operator_bump_insights = insights
        self._advisory_bump_insights = None
        self._bump_insights = None
        self.loop_detector.reset()
        self.tracer.event("bump", source="operator", insights=insights[:500])

    def _result(self, status: str) -> SolverResult:
        self.tracer.event("finish", status=status, flag=self._flag, confirmed=self._confirmed)
        return SolverResult(
            flag=self._flag, status=status,
            findings_summary=self._findings[:2000],
            step_count=self._step_count,
            cost_usd=self._cost_usd, log_path=self.tracer.path,
            candidate_flag=self._candidate_flag,
            candidate_evidence=self._candidate_evidence,
            candidate_confidence=self._candidate_confidence,
        )

    async def stop(self) -> None:
        self.tracer.event("stop", step_count=self._step_count)
        await self.stop_process()
        if self.sandbox:
            await self.sandbox.stop()

    async def stop_process(self) -> None:
        self.tracer.close()
        if self._compact_task:
            self._compact_task.cancel()
            try:
                await self._compact_task
            except (asyncio.CancelledError, Exception):
                pass
            self._compact_task = None
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
