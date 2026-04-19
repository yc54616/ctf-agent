"""Gemini CLI solver using local home OAuth plus command hooks."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, cast

from backend.auth import refresh_gemini_oauth, resolve_home_auth_paths
from backend.config import Settings
from backend.cost_tracker import CostTracker
from backend.ctfd import CTFdClient
from backend.models import model_id_from_spec
from backend.prompts import (
    ChallengeMeta,
    build_lane_bump_prompt,
    build_prompt,
    build_shell_solver_preamble,
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
    is_read_only_tool,
    lifecycle_for_result,
    summarize_tool_input,
    summarize_tool_result,
)
from backend.tools.core import do_fs_query
from backend.tracing import SolverTracer

logger = logging.getLogger(__name__)

FLAG_LINE_RE = re.compile(r"FLAG:\s*(\S+)")
WATCHDOG_SAMPLE_SECONDS = 5
WATCHDOG_TURN_START_SECONDS = 120.0
WATCHDOG_SHORT_TOOL_SECONDS = 60.0
WATCHDOG_DEFAULT_TOOL_SECONDS = 120.0
WATCHDOG_POST_TOOL_SECONDS = 30.0
WATCHDOG_TOOL_TIMEOUT_PADDING_SECONDS = 30.0
WATCHDOG_MAX_BASH_TOOL_SECONDS = 600.0

WATCHDOG_SHORT_TOOLS = {
    "fs_query",
    "report_flag_candidate",
    "notify_coordinator",
}


class GeminiSolver:
    """Gemini CLI solver with Docker sandbox redirection via hooks."""

    def __init__(
        self,
        model_spec: str,
        challenge_dir: str,
        meta: ChallengeMeta,
        ctfd: CTFdClient,
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
        self.ctfd = ctfd
        self.cost_tracker = cost_tracker
        self.settings = settings
        self.cancel_event = cancel_event or asyncio.Event()
        self.no_submit = no_submit
        self.report_flag_candidate_fn = report_flag_candidate_fn
        self.message_bus = message_bus
        self.notify_coordinator = notify_coordinator

        self.sandbox = sandbox or DockerSandbox(
            image=getattr(settings, "sandbox_image", "ctf-sandbox"),
            challenge_dir=challenge_dir,
            memory_limit=getattr(settings, "container_memory_limit", "4g"),
            exec_output_spill_threshold_bytes=getattr(settings, "exec_output_spill_threshold_bytes", 65_536),
            read_file_spill_threshold_bytes=getattr(settings, "read_file_spill_threshold_bytes", 262_144),
            artifact_preview_bytes=getattr(settings, "artifact_preview_bytes", 8_192),
        )
        self.tracer = SolverTracer(meta.name, self.model_id)
        self.agent_name = f"{meta.name}/{self.model_id}"
        self._runtime = LaneRuntimeStatus()

        self._proc: asyncio.subprocess.Process | None = None
        self._step_count = initial_step_count
        self._findings_poll_count = 0
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
        self._session_started = False
        self._session_id: str | None = None
        self._gemini_home_dir: str = ""
        self._project_dir: str = ""
        self._ipc_dir: str = ""
        self._ipc_stop = asyncio.Event()
        self._ipc_task: asyncio.Task | None = None
        self._tool_names: dict[str, str] = {}
        self._tool_args: dict[str, dict[str, Any]] = {}
        self._watchdog_phase = ""
        self._watchdog_step = 0
        self._watchdog_tool = ""
        self._watchdog_started_monotonic = 0.0
        self._watchdog_started_at: float | None = None
        self._watchdog_deadline_seconds = 0.0
        self._read_only_streak = 0
        self._last_progress_kind = "turn_start"

    async def start(self) -> None:
        await self.sandbox.start()

        arch_result = await self.sandbox.exec("uname -m", timeout_s=10)
        container_arch = arch_result.stdout.strip() or "unknown"
        distfile_names = list_distfiles(self.challenge_dir)
        sandbox_preamble = build_shell_solver_preamble()
        self._system_prompt = sandbox_preamble + build_prompt(
            self.meta,
            distfile_names,
            container_arch=container_arch,
            has_named_tools=False,
        )

        self._prepare_gemini_dirs()
        self._runtime.mark_ready()
        self.tracer.event("start", challenge=self.meta.name, model=self.model_id)
        logger.info(f"[{self.agent_name}] Gemini solver started")

    def _prepare_gemini_dirs(self) -> None:
        if self._gemini_home_dir:
            return
        settings = cast(Settings, self.settings)

        self._project_dir = self.sandbox.workspace_dir
        self._ipc_dir = str(Path(self._project_dir) / ".gemini-ipc")
        requests_dir = Path(self._ipc_dir) / "requests"
        responses_dir = Path(self._ipc_dir) / "responses"
        requests_dir.mkdir(parents=True, exist_ok=True)
        responses_dir.mkdir(parents=True, exist_ok=True)

        self._gemini_home_dir = tempfile.mkdtemp(prefix="ctf-gemini-home-")
        gemini_home = Path(self._gemini_home_dir) / ".gemini"
        gemini_home.mkdir(parents=True, exist_ok=True)

        auth_path = resolve_home_auth_paths(settings).gemini
        raw_auth = json.loads(auth_path.read_text(encoding="utf-8"))
        oauth = refresh_gemini_oauth(settings)
        raw_auth["access_token"] = oauth.access_token
        raw_auth["token_type"] = oauth.token_type
        raw_auth["refresh_token"] = oauth.refresh_token
        if oauth.expiry_date_ms is not None:
            raw_auth["expiry_date"] = oauth.expiry_date_ms
        (gemini_home / "oauth_creds.json").write_text(
            json.dumps(raw_auth),
            encoding="utf-8",
        )
        (gemini_home / "settings.json").write_text(
            json.dumps(
                {
                    "security": {
                        "auth": {
                            "selectedType": "oauth-personal",
                        }
                    },
                    "hooks": {
                        "BeforeToolSelection": [
                            {
                                "matcher": "*",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": f"python3 {Path(__file__).resolve().with_name('gemini_hook.py')}",
                                        "timeout": 60000,
                                    }
                                ],
                            }
                        ],
                        "BeforeTool": [
                            {
                                "matcher": "run_shell_command",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": f"python3 {Path(__file__).resolve().with_name('gemini_hook.py')}",
                                        "timeout": 60000,
                                    }
                                ],
                            }
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )

        (Path(self._project_dir) / "GEMINI.md").write_text(
            self._system_prompt,
            encoding="utf-8",
        )

    async def run_until_done_or_gave_up(self) -> SolverResult:
        if not self._project_dir:
            await self.start()

        t0 = time.monotonic()
        cost_before = self._cost_usd
        steps_before = self._step_count
        prompt = self._consume_turn_prompt()

        stdout_state: dict[str, Any] = {
            "response_chunks": [],
            "stderr_chunks": [],
            "quota_error": False,
            "result_stats": None,
            "stalled_reason": "",
        }

        self._ipc_stop = asyncio.Event()
        self._ipc_task = asyncio.create_task(self._ipc_loop())

        args = [
            "gemini",
            "--model",
            self.model_id,
            "--output-format",
            "stream-json",
            "--approval-mode",
            "yolo",
            "--prompt",
            prompt,
        ]
        if self._session_started:
            args.extend(["--resume", "latest"])

        env = os.environ.copy()
        env.update(
            {
                "HOME": self._gemini_home_dir,
                "GEMINI_HOME": str(Path(self._gemini_home_dir) / ".gemini"),
                "GOOGLE_GENAI_USE_GCA": "true",
                "CTF_AGENT_GEMINI_CONTAINER_ID": self.sandbox.container_id,
                "CTF_AGENT_GEMINI_IPC_DIR": self._ipc_dir,
                "CTF_AGENT_GEMINI_IPC_TIMEOUT": "60",
            }
        )

        try:
            self._clear_watchdog()
            self._reset_turn_progress_tracking()
            self._tool_names.clear()
            self._tool_args.clear()
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=self._project_dir,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert self._proc.stdout is not None
            assert self._proc.stderr is not None

            self._arm_watchdog(
                phase="turn_start",
                step=self._step_count,
                deadline_seconds=WATCHDOG_TURN_START_SECONDS,
            )
            stdout_task = asyncio.create_task(
                self._consume_stdout(self._proc.stdout, stdout_state)
            )
            stderr_task = asyncio.create_task(
                self._consume_stderr(self._proc.stderr, stdout_state)
            )
            wait_task = asyncio.create_task(self._proc.wait())
            cancel_task = asyncio.create_task(self.cancel_event.wait())
            watchdog_task = asyncio.create_task(self._watch_turn_progress(stdout_state))

            done, pending = await asyncio.wait(
                {wait_task, cancel_task, watchdog_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done and self.cancel_event.is_set():
                await self._terminate_proc()
                await wait_task
                await stdout_task
                await stderr_task
                return self._result(CANCELLED)

            if watchdog_task in done and wait_task not in done:
                await wait_task

            for task in pending:
                if task is wait_task:
                    continue
                task.cancel()
            await asyncio.gather(
                *(task for task in pending if task is not wait_task),
                return_exceptions=True,
            )

            await stdout_task
            await stderr_task
            exit_code = await wait_task

            response_text = "".join(stdout_state["response_chunks"]).strip()
            if response_text:
                self._findings = response_text[:2000]
                flag_match = FLAG_LINE_RE.search(response_text)
                if flag_match:
                    self._candidate_flag = flag_match.group(1).strip()
                    self._candidate_evidence = "Gemini terminal response"
                    self._candidate_confidence = "medium"
                    ack = await self._report_flag_candidate(
                        self._candidate_flag,
                        evidence=self._candidate_evidence,
                        confidence=self._candidate_confidence,
                    )
                    self._findings = (
                        f"Flag candidate via {self._candidate_evidence}: {self._candidate_flag}\n{ack}"
                    )[:2000]

            self._record_usage(stdout_state.get("result_stats"), time.monotonic() - t0)
            self.tracer.event(
                "turn_complete",
                duration=round(time.monotonic() - t0, 1),
                steps=self._step_count,
            )
            if not str(stdout_state.get("stalled_reason") or "").startswith("stalled:"):
                self._clear_watchdog()

            stderr_text = "".join(stdout_state["stderr_chunks"])
            if self._is_quota_error(stderr_text) or stdout_state["quota_error"]:
                self._findings = (stderr_text or self._findings or "Gemini quota error")[:2000]
                return self._result(
                    QUOTA_ERROR,
                    run_steps=self._step_count - steps_before,
                    run_cost=self._cost_usd - cost_before,
                )
            if exit_code != 0:
                stalled_reason = str(stdout_state.get("stalled_reason") or "").strip()
                self._findings = (stalled_reason or stderr_text or f"Gemini exited with {exit_code}")[:2000]
                return self._result(
                    ERROR,
                    run_steps=self._step_count - steps_before,
                    run_cost=self._cost_usd - cost_before,
                )
            if self._confirmed and self._flag:
                return self._result(
                    FLAG_FOUND,
                    run_steps=self._step_count - steps_before,
                    run_cost=self._cost_usd - cost_before,
                )
            if self._candidate_flag:
                return self._result(
                    FLAG_CANDIDATE,
                    run_steps=self._step_count - steps_before,
                    run_cost=self._cost_usd - cost_before,
                )
            return self._result(
                GAVE_UP,
                run_steps=self._step_count - steps_before,
                run_cost=self._cost_usd - cost_before,
            )
        except asyncio.CancelledError:
            return self._result(CANCELLED)
        except Exception as e:
            error_str = str(e)
            logger.error(f"[{self.agent_name}] Error: {e}", exc_info=True)
            self._findings = f"Error: {e}"
            self.tracer.event("error", error=error_str)
            if self._is_quota_error(error_str):
                return self._result(QUOTA_ERROR)
            return self._result(ERROR)
        finally:
            self._ipc_stop.set()
            if self._ipc_task:
                try:
                    await self._ipc_task
                except Exception:
                    pass
                self._ipc_task = None
            self._proc = None

    async def _consume_stdout(
        self,
        stream: asyncio.StreamReader,
        state: dict[str, Any],
    ) -> None:
        while True:
            line = await stream.readline()
            if not line:
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")
            if event_type == "init":
                self._session_started = True
                self._session_id = event.get("session_id")
            elif event_type == "message" and event.get("role") == "assistant":
                content = event.get("content")
                if isinstance(content, str):
                    self._touch_watchdog()
                    self._set_progress_kind("assistant_message")
                    state["response_chunks"].append(content)
            elif event_type == "tool_use":
                tool_name = str(event.get("tool_name", "tool"))
                tool_id = str(event.get("tool_id", ""))
                self._tool_names[tool_id] = tool_name
                raw_args = event.get("parameters", {})
                args = raw_args if isinstance(raw_args, dict) else {}
                self._tool_args[tool_id] = args
                self._step_count += 1
                self._runtime.mark_busy(
                    tool_name,
                    summarize_tool_input(tool_name, args),
                    step_count=self._step_count,
                )
                self._arm_watchdog(
                    phase="tool_call",
                    step=self._step_count,
                    deadline_seconds=self._tool_call_watchdog_seconds(tool_name, args),
                    tool=tool_name,
                )
                self.tracer.tool_call(tool_name, args, self._step_count)
            elif event_type == "tool_result":
                tool_id = str(event.get("tool_id", ""))
                tool_name = self._tool_names.get(tool_id, tool_id or "tool")
                tool_args = self._tool_args.pop(tool_id, {})
                output = event.get("output")
                error = event.get("error")
                text = str(output) if output is not None else ""
                if error:
                    text = f"{text}\n{error}".strip()
                self._record_tool_progress(tool_name)
                self._runtime.mark_idle(summarize_tool_result(text))
                self.tracer.tool_result(tool_name, text[:500], self._step_count)
                self._arm_watchdog(
                    phase="post_tool",
                    step=self._step_count,
                    deadline_seconds=self._post_tool_watchdog_seconds(tool_name, tool_args),
                    tool=tool_name,
                )
                if error and self._is_quota_error(str(error)):
                    state["quota_error"] = True
            elif event_type == "error":
                message = str(event.get("message", ""))
                state["stderr_chunks"].append(message + "\n")
                self._runtime.mark_idle(message)
                if self._is_quota_error(message):
                    state["quota_error"] = True
            elif event_type == "result":
                state["result_stats"] = event.get("stats")

    async def _consume_stderr(
        self,
        stream: asyncio.StreamReader,
        state: dict[str, Any],
    ) -> None:
        while True:
            chunk = await stream.readline()
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            state["stderr_chunks"].append(text)
            if self._is_quota_error(text):
                state["quota_error"] = True

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
        if self._watchdog_phase in {"turn_start", "post_tool"}:
            self._watchdog_started_monotonic = time.monotonic()
            self._watchdog_started_at = time.time()

    def _reset_turn_progress_tracking(self) -> None:
        self._read_only_streak = 0
        self._set_progress_kind("turn_start")

    def _set_progress_kind(self, kind: str) -> None:
        self._last_progress_kind = kind
        self._runtime.last_progress_kind = kind
        self._runtime.read_only_streak = self._read_only_streak

    def _record_tool_progress(self, tool_name: str) -> None:
        if is_read_only_tool(tool_name):
            self._read_only_streak += 1
            self._set_progress_kind("read_only_tool")
            return
        self._read_only_streak = 0
        self._set_progress_kind("exec_tool")

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
    def _post_tool_watchdog_seconds(tool_name: str, args: dict[str, Any]) -> float:
        return WATCHDOG_POST_TOOL_SECONDS

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
        tool_suffix = f" ({self._watchdog_tool})" if self._watchdog_tool else ""
        if self._watchdog_tool and is_read_only_tool(self._watchdog_tool):
            tool_suffix = f" ({self._watchdog_tool}, read_only_streak={self._read_only_streak})"
        return f"stalled: post_tool_inactivity after {deadline}s{tool_suffix}", "post_tool_inactivity"

    async def _watch_turn_progress(self, state: dict[str, Any]) -> None:
        while self._proc and self._proc.returncode is None:
            await asyncio.sleep(WATCHDOG_SAMPLE_SECONDS)
            if not self._proc or self._proc.returncode is not None:
                return
            if not self._watchdog_expired():
                continue

            reason, kind = self._watchdog_error()
            state["stalled_reason"] = reason
            self._findings = reason
            self._runtime.mark_terminal("error", reason)
            self.tracer.event(
                "turn_stalled",
                reason=reason,
                step=self._step_count,
                watchdog_phase=self._watchdog_phase,
                watchdog_kind=kind,
                deadline_seconds=int(self._watchdog_deadline_seconds),
                tool=self._watchdog_tool or None,
                read_only_streak=self._read_only_streak,
                last_progress_kind=self._last_progress_kind,
            )
            await self._terminate_proc()
            return

    def _record_usage(self, stats: Any, duration_seconds: float) -> None:
        if not isinstance(stats, dict):
            return
        model_stats = stats.get("models", {})
        selected = model_stats.get(self.model_id, {}) if isinstance(model_stats, dict) else {}
        input_tokens = selected.get("input_tokens", stats.get("input_tokens", 0))
        output_tokens = selected.get("output_tokens", stats.get("output_tokens", 0))
        cache_read_tokens = selected.get("cached", stats.get("cached", 0))
        self.cost_tracker.record_tokens(
            self.agent_name,
            self.model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            provider_spec="gemini",
            duration_seconds=duration_seconds,
        )
        agent_usage = self.cost_tracker.by_agent.get(self.agent_name)
        self._cost_usd = agent_usage.cost_usd if agent_usage else 0.0
        self.tracer.usage(
            input_tokens,
            output_tokens,
            cache_read_tokens,
            self._cost_usd,
        )

    async def _ipc_loop(self) -> None:
        requests_dir = Path(self._ipc_dir) / "requests"
        responses_dir = Path(self._ipc_dir) / "responses"
        requests_dir.mkdir(parents=True, exist_ok=True)
        responses_dir.mkdir(parents=True, exist_ok=True)

        while not self._ipc_stop.is_set():
            for request_path in sorted(requests_dir.glob("*.json")):
                processing_path = request_path.with_suffix(".processing")
                try:
                    request_path.rename(processing_path)
                except FileNotFoundError:
                    continue

                try:
                    request = json.loads(processing_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    logger.warning(f"[{self.agent_name}] Invalid Gemini IPC request: {exc}")
                    processing_path.unlink(missing_ok=True)
                    continue

                response = await self._handle_ipc_request(request)
                response_path = responses_dir / f"{request['id']}.json"
                response_path.write_text(json.dumps(response), encoding="utf-8")
                processing_path.unlink(missing_ok=True)

            await asyncio.sleep(0.05)

    async def _handle_ipc_request(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request.get("action")
        if action == "report_flag_candidate":
            message = await self._report_flag_candidate(
                str(request.get("flag", "")),
                evidence=str(request.get("evidence", "")),
                confidence=str(request.get("confidence", "medium")),
            )
            return {"message": message}

        if action == "notify_coordinator":
            message = str(request.get("message", "")).strip()
            if self.notify_coordinator:
                await self.notify_coordinator(message)
                return {"message": "Message sent to coordinator."}
            return {"message": "No coordinator connected."}

        if action == "fs_query":
            output = await do_fs_query(
                self.sandbox,
                action=str(request.get("query_action", "")),
                path=str(request.get("path", "")),
                maxdepth=int(request.get("maxdepth", 3)),
                kind=str(request.get("kind", "files")),
                pattern=str(request.get("pattern", "")),
                limit=int(request.get("limit", 200)),
                mode=str(request.get("mode", "text")),
                start_line=int(request.get("start_line", 1)),
                line_count=int(request.get("line_count", 120)),
                byte_offset=int(request.get("byte_offset", 0)),
                byte_count=int(request.get("byte_count", 256)),
                query=str(request.get("query", "")),
                glob=str(request.get("glob", "")),
                ignore_case=bool(request.get("ignore_case", True)),
                context_lines=int(request.get("context_lines", 2)),
            )
            return {"output": output}

        return {"message": f"Unsupported Gemini IPC action: {action}"}

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
        self._candidate_flag = cleaned_flag
        self._candidate_evidence = evidence.strip()
        self._candidate_confidence = confidence.strip() or "medium"
        if not self.report_flag_candidate_fn:
            return f"Flag candidate noted locally: {cleaned_flag}"
        return await self.report_flag_candidate_fn(
            cleaned_flag,
            self._candidate_evidence,
            self._candidate_confidence,
            self._step_count,
            self.tracer.path,
        )

    async def _terminate_proc(self) -> None:
        if not self._proc:
            return
        if self._proc.returncode is not None:
            return
        try:
            self._proc.terminate()
            await asyncio.wait_for(self._proc.wait(), timeout=5)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass

    @staticmethod
    def _is_quota_error(text: str) -> bool:
        lowered = text.lower()
        return any(
            marker in lowered
            for marker in (
                "resource_exhausted",
                "rate limit",
                "ratelimit",
                "model_capacity_exhausted",
                "no capacity available",
                "quota",
                "429",
            )
        )

    def bump(self, insights: str) -> None:
        if self._bump_insights and insights not in self._bump_insights:
            self._bump_insights = f"{self._bump_insights}\n\n---\n\n{insights}"
        else:
            self._bump_insights = insights
        self.tracer.event("bump", source="auto", insights=insights[:500])
        logger.info(f"[{self.agent_name}] Bumped with insights")

    def bump_advisory(self, insights: str) -> None:
        self._advisory_bump_insights = insights
        self.tracer.event("bump", source="auto", channel="advisory", insights=insights[:500])
        logger.info(f"[{self.agent_name}] Bumped with lane advisory")

    def bump_operator(self, insights: str) -> None:
        self._operator_bump_insights = insights
        self._advisory_bump_insights = None
        self._bump_insights = None
        self.tracer.event("bump", source="operator", insights=insights[:500])
        logger.info(f"[{self.agent_name}] Bumped with operator guidance")

    def _consume_turn_prompt(self) -> str:
        if self._operator_bump_insights:
            prompt = build_lane_bump_prompt(self._operator_bump_insights, operator=True)
            self._operator_bump_insights = None
            self._advisory_bump_insights = None
            self._bump_insights = None
            return prompt

        if self._advisory_bump_insights:
            prompt = build_lane_bump_prompt(self._advisory_bump_insights, advisory=True)
            self._advisory_bump_insights = None
            return prompt

        if self._bump_insights:
            prompt = build_lane_bump_prompt(self._bump_insights)
            self._bump_insights = None
            return prompt

        if self._session_started:
            return "Continue solving. Try a different approach."

        return "Solve this CTF challenge."

    def get_runtime_status(self) -> dict[str, object]:
        snapshot = self._runtime.snapshot()
        snapshot["watchdog_phase"] = self._watchdog_phase
        snapshot["watchdog_tool"] = self._watchdog_tool
        snapshot["watchdog_started_at"] = self._watchdog_started_at
        snapshot["read_only_streak"] = self._read_only_streak
        snapshot["last_progress_kind"] = self._last_progress_kind
        return snapshot

    def mark_terminal_status(self, status: str) -> None:
        self._runtime.mark_terminal(lifecycle_for_result(status), status)

    def _result(
        self,
        status: str,
        run_steps: int | None = None,
        run_cost: float | None = None,
    ) -> SolverResult:
        self.tracer.event(
            "finish",
            status=status,
            flag=self._flag,
            confirmed=self._confirmed,
            cost_usd=round(self._cost_usd, 4),
        )
        return SolverResult(
            flag=self._flag,
            status=status,
            findings_summary=self._findings[:2000],
            step_count=run_steps if run_steps is not None else self._step_count,
            cost_usd=run_cost if run_cost is not None else self._cost_usd,
            log_path=self.tracer.path,
            candidate_flag=self._candidate_flag,
            candidate_evidence=self._candidate_evidence,
            candidate_confidence=self._candidate_confidence,
        )

    async def stop(self) -> None:
        self.tracer.event("stop", step_count=self._step_count)
        await self.stop_process()
        if self.sandbox:
            await self.sandbox.stop()
        self._project_dir = ""

    async def stop_process(self) -> None:
        self.tracer.close()
        self._ipc_stop.set()
        await self._terminate_proc()
        if self._ipc_task:
            try:
                await self._ipc_task
            except Exception:
                pass
            self._ipc_task = None
        if self._gemini_home_dir:
            shutil.rmtree(self._gemini_home_dir, ignore_errors=True)
            self._gemini_home_dir = ""
