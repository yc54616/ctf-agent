"""Host-side wrapper for the in-sandbox lane runtime."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from backend.config import Settings
from backend.prompts import ChallengeMeta
from backend.runtime_control import (
    HEARTBEAT_STALE_AFTER_SECONDS,
    LaneHostState,
    append_jsonl,
    build_runtime_config,
    ensure_lane_host_state,
    lane_control_paths,
    read_json,
    read_new_jsonl,
)
from backend.sandbox import TRACE_CONTAINER_ROOT, DockerSandbox
from backend.solver_base import ERROR, LaneRuntimeStatus, SolverResult, lifecycle_for_result


class InSandboxRuntimeSolver:
    """Proxy that delegates full provider/tool execution to an in-container runtime."""

    def __init__(
        self,
        *,
        model_spec: str,
        challenge_dir: str,
        meta: ChallengeMeta,
        settings: object,
        cost_tracker,
        cancel_event: asyncio.Event | None = None,
        no_submit: bool = False,
        local_mode: bool = False,
        report_flag_candidate_fn=None,
        notify_coordinator=None,
        sandbox: DockerSandbox | None = None,
        initial_step_count: int = 0,
        warm_container_id: str = "",
    ) -> None:
        self.model_spec = model_spec
        self.challenge_dir = challenge_dir
        self.meta = meta
        self.settings = settings
        self.cost_tracker = cost_tracker
        self.cancel_event = cancel_event or asyncio.Event()
        self.no_submit = no_submit
        self.local_mode = local_mode
        self.report_flag_candidate_fn = report_flag_candidate_fn
        self.notify_coordinator = notify_coordinator
        self.agent_name = f"{meta.name}/{model_spec}"
        self.provider = model_spec.split("/", 1)[0]
        self._initial_step_count = initial_step_count
        repo_root = Path(__file__).resolve().parents[2]
        self._host_state: LaneHostState = ensure_lane_host_state(
            challenge_dir,
            model_spec,
            repo_root=repo_root,
        )
        self._control = lane_control_paths(self._host_state.control_dir)
        auth_seed_mounts = self._auth_seed_mounts(cast_settings=settings)
        self.sandbox = sandbox or DockerSandbox(
            image=getattr(settings, "sandbox_image", "ctf-sandbox"),
            challenge_dir=challenge_dir,
            memory_limit=getattr(settings, "container_memory_limit", "4g"),
            exec_output_spill_threshold_bytes=getattr(settings, "exec_output_spill_threshold_bytes", 65_536),
            read_file_spill_threshold_bytes=getattr(settings, "read_file_spill_threshold_bytes", 262_144),
            artifact_preview_bytes=getattr(settings, "artifact_preview_bytes", 8_192),
            workspace_dir=str(self._host_state.workspace_dir),
            shared_artifacts_dir=str(self._host_state.shared_artifacts_dir),
            control_dir=str(self._host_state.control_dir),
            provider_home_dir=str(self._host_state.provider_home_dir),
            trace_dir=str(self._host_state.trace_dir),
            repo_root_dir=str(repo_root),
            challenge_src_dir=str(Path(challenge_dir).resolve()),
            auth_seed_mounts=auth_seed_mounts,
            existing_container_id=str(warm_container_id or "").strip(),
            preserve_stopped_container=True,
        )
        self._runtime = LaneRuntimeStatus()
        self._event_offset = 0
        self._soft_reset_count = 0
        self._hard_reset_count = 0
        self._last_reset_reason = ""
        self._last_heartbeat: dict[str, Any] = {}
        self._last_result: dict[str, Any] = {}
        self._pending_results: list[tuple[SolverResult, bool]] = []
        self._started = False
        self._stop_reason = "host stop"

    @staticmethod
    def _auth_seed_mounts(cast_settings: object) -> dict[str, str]:
        settings = cast_settings if isinstance(cast_settings, Settings) else Settings()
        mounts: dict[str, str] = {}
        codex_auth = Path(settings.codex_auth_path).expanduser() if settings.codex_auth_path else Path.home() / ".codex" / "auth.json"
        gemini_auth = Path(settings.gemini_auth_path).expanduser() if settings.gemini_auth_path else Path.home() / ".gemini" / "oauth_creds.json"
        if codex_auth.exists():
            mounts["codex-auth.json"] = str(codex_auth)
        if gemini_auth.exists():
            mounts["gemini-oauth.json"] = str(gemini_auth)
        return mounts

    def _config_payload(self) -> dict[str, Any]:
        settings = self.settings if isinstance(self.settings, Settings) else Settings()
        meta_payload = asdict(self.meta)
        return build_runtime_config(
            model_spec=self.model_spec,
            provider=self.provider,
            challenge_dir_host=self.challenge_dir,
            meta=meta_payload,
            settings={
                "ctfd_url": settings.ctfd_url,
                "ctfd_user": settings.ctfd_user,
                "ctfd_pass": settings.ctfd_pass,
                "ctfd_token": settings.ctfd_token,
                "sandbox_image": settings.sandbox_image,
                "container_memory_limit": settings.container_memory_limit,
                "exec_output_spill_threshold_bytes": settings.exec_output_spill_threshold_bytes,
                "read_file_spill_threshold_bytes": settings.read_file_spill_threshold_bytes,
                "artifact_preview_bytes": settings.artifact_preview_bytes,
                "no_submit": self.no_submit,
                "local_mode": self.local_mode,
                "initial_step_count": self._initial_step_count,
            },
        ) | {"no_submit": self.no_submit, "local_mode": self.local_mode}

    async def start(self) -> None:
        await self.sandbox.start()
        await self._verify_runtime_prerequisites()
        self._write_runtime_config()
        self._prime_runtime_state()
        await self._start_runtime_process()
        await self._wait_for_heartbeat()
        self._started = True

    async def run_until_done_or_gave_up(self) -> SolverResult:
        if not self._started:
            await self.start()

        cancel_sent = False
        while True:
            await self._process_events()
            if self._pending_results:
                result, is_final = self._pending_results.pop(0)
                if is_final:
                    self._runtime.mark_terminal(lifecycle_for_result(result.status), result.status)
                return result
            payload = read_json(self._control.result_path, default={})
            if isinstance(payload, dict) and str(payload.get("type") or "") == "final_result":
                self._last_result = payload
                raw_result = payload.get("result", {})
                if isinstance(raw_result, dict):
                    mapped = self._map_result(raw_result)
                    self._runtime.mark_terminal(lifecycle_for_result(mapped.status), mapped.status)
                    return mapped
            if self.cancel_event.is_set() and not cancel_sent:
                append_jsonl(self._control.commands_path, {"type": "cancel", "ts": time.time()})
                cancel_sent = True
            if await self._heartbeat_stale():
                await self._recover_runtime("heartbeat stale during turn")
            await asyncio.sleep(0.2)

    def bump(self, insights: str) -> None:
        append_jsonl(
            self._control.commands_path,
            {"type": "auto_bump", "ts": time.time(), "insights": insights},
        )

    def bump_advisory(self, insights: str) -> None:
        append_jsonl(
            self._control.commands_path,
            {"type": "advisory", "ts": time.time(), "insights": insights},
        )

    def bump_operator(self, insights: str) -> None:
        append_jsonl(
            self._control.commands_path,
            {"type": "operator_bump", "ts": time.time(), "insights": insights},
        )

    def get_runtime_status(self) -> dict[str, object]:
        heartbeat = self._load_heartbeat()
        terminal_result = self._load_terminal_result()
        age = self._heartbeat_age(heartbeat)
        raw_runtime = heartbeat.get("raw_runtime") if isinstance(heartbeat, dict) else {}
        if not isinstance(raw_runtime, dict):
            raw_runtime = {}
        lifecycle = str(
            terminal_result.get("lifecycle")
            or heartbeat.get("lifecycle")
            or raw_runtime.get("lifecycle")
            or self._runtime.lifecycle
            or "pending"
        )
        commentary_preview = str(
            heartbeat.get("commentary_preview")
            or raw_runtime.get("commentary_preview")
            or ""
        )
        if raw_runtime.get("current_command"):
            activity = str(raw_runtime.get("current_command") or "").strip()
        elif commentary_preview:
            activity = commentary_preview
        else:
            activity = str(
                terminal_result.get("findings_summary")
                or terminal_result.get("status")
                or heartbeat.get("activity")
                or raw_runtime.get("last_command")
                or raw_runtime.get("last_exit_hint")
                or ""
            ).strip()
        runtime_health = self._runtime_health(heartbeat)
        if terminal_result.get("lifecycle"):
            runtime_health = str(terminal_result.get("lifecycle"))
        status = {
            "lifecycle": lifecycle,
            "status": "running" if lifecycle not in {"won", "finished", "error", "quota_error", "cancelled"} else lifecycle,
            "provider": str(heartbeat.get("provider") or self.provider),
            "runtime_health": runtime_health,
            "activity_state": self._activity_state(heartbeat, raw_runtime, lifecycle, commentary_preview),
            "heartbeat_at": heartbeat.get("ts"),
            "heartbeat_age_sec": age,
            "step_count": int(heartbeat.get("step_count", raw_runtime.get("step_count", 0)) or 0),
            "activity": activity,
            "commentary_preview": commentary_preview,
            "commentary_at": heartbeat.get("commentary_at", raw_runtime.get("commentary_at")),
            "session": heartbeat.get("session") if isinstance(heartbeat.get("session"), dict) else {},
            "reset_counts": {
                "soft": self._soft_reset_count,
                "hard": self._hard_reset_count,
            },
            "last_reset_reason": self._last_reset_reason,
            "last_event": str(heartbeat.get("last_event") or ""),
            "findings": "",
            "advisor_note": "",
            "last_exit_hint": str(
                terminal_result.get("findings_summary")
                or heartbeat.get("last_exit_hint")
                or raw_runtime.get("last_exit_hint")
                or ""
            ),
            "current_tool": "",
            "current_command": str(raw_runtime.get("current_command") or ""),
            "current_started_at": raw_runtime.get("current_started_at"),
            "last_tool": "",
            "last_command": str(raw_runtime.get("last_command") or ""),
            "last_completed_at": raw_runtime.get("last_completed_at"),
        }
        return status

    def _load_terminal_result(self) -> dict[str, str]:
        payload = read_json(self._control.result_path, default={})
        if not isinstance(payload, dict) or str(payload.get("type") or "") != "final_result":
            return {}
        raw_result = payload.get("result", {})
        if not isinstance(raw_result, dict):
            return {}
        status = str(raw_result.get("status") or "").strip()
        if not status:
            return {}
        return {
            "status": status,
            "lifecycle": lifecycle_for_result(status),
            "findings_summary": str(raw_result.get("findings_summary") or "").strip(),
        }

    def _activity_state(
        self,
        heartbeat: dict[str, Any],
        raw_runtime: dict[str, Any],
        lifecycle: str,
        commentary_preview: str,
    ) -> str:
        runtime_health = self._runtime_health(heartbeat)
        if runtime_health in {"stale", "resetting"}:
            return runtime_health
        if lifecycle in {"won", "finished", "error", "quota_error", "cancelled"}:
            return lifecycle
        current_command = str(raw_runtime.get("current_command") or "").strip()
        if current_command:
            return "tool"
        if commentary_preview:
            return "thinking"
        if lifecycle == "busy":
            return "thinking"
        return "idle"

    def mark_terminal_status(self, status: str) -> None:
        self._runtime.mark_terminal(lifecycle_for_result(status), status)

    def set_stop_reason(self, reason: str) -> None:
        cleaned = " ".join(str(reason or "").split()).strip()
        if cleaned:
            self._stop_reason = cleaned[:500]

    async def stop_process(self, reason: str | None = None) -> None:
        if reason:
            self.set_stop_reason(reason)
        append_jsonl(
            self._control.commands_path,
            {"type": "shutdown", "ts": time.time(), "reason": self._stop_reason},
        )
        await asyncio.sleep(0.5)
        payload = read_json(self._control.runtime_pid_path, default={})
        pid = ""
        if isinstance(payload, dict):
            pid = str(payload.get("pid") or "").strip()
        if pid:
            try:
                await self.sandbox.exec(f"kill {pid}", timeout_s=10)
            except Exception:
                pass

    async def stop(self) -> None:
        await self.stop_process()
        await self.sandbox.stop()
        self._started = False

    def _write_runtime_config(self) -> None:
        from backend.runtime_control import write_json_atomic

        write_json_atomic(self._control.config_path, self._config_payload())

    def _prime_runtime_state(self) -> None:
        try:
            self._event_offset = self._control.events_path.stat().st_size
        except FileNotFoundError:
            self._event_offset = 0
        self._control.heartbeat_path.unlink(missing_ok=True)
        self._control.result_path.unlink(missing_ok=True)

    async def _verify_runtime_prerequisites(self) -> None:
        import_check = """python3 - <<'PY'
modules = ("pydantic_ai", "httpx", "aiodocker", "claude_agent_sdk")
missing = []
for module in modules:
    try:
        __import__(module)
    except Exception:
        missing.append(module)
if missing:
    raise SystemExit("missing python modules: " + ", ".join(missing))
print("python runtime deps ok")
PY"""
        await self._require_success(
            import_check,
            hint="runtime Python dependencies are missing",
        )
        cli_command = "codex --version" if self.provider == "codex" else "gemini --version"
        await self._require_success(
            cli_command,
            hint=f"{self.provider} CLI is unavailable inside the sandbox",
        )

    async def _require_success(self, command: str, *, hint: str) -> None:
        result = await self.sandbox.exec(command, timeout_s=60)
        if result.exit_code == 0:
            return
        detail = (result.stderr or result.stdout or "").strip()
        message = (
            f"Sandbox runtime preflight failed for {self.agent_name}: {hint}. "
            "Rebuild ctf-sandbox from sandbox/Dockerfile.sandbox."
        )
        if detail:
            message = f"{message}\n{detail}"
        raise RuntimeError(message)

    async def _start_runtime_process(self) -> None:
        command = "python3 -m backend.agents.lane_runtime --control-dir /challenge/control"
        await self.sandbox.exec_detached(
            command,
            cwd="/challenge/agent-repo",
            env={
                "PYTHONPATH": "/challenge/agent-repo",
                "CTF_AGENT_LOG_DIR": TRACE_CONTAINER_ROOT,
            },
        )

    async def _wait_for_heartbeat(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await self._process_events()
            heartbeat = self._load_heartbeat()
            if heartbeat:
                return
            await asyncio.sleep(0.2)
        raise RuntimeError(f"Timed out waiting for runtime heartbeat for {self.agent_name}")

    def _load_heartbeat(self) -> dict[str, Any]:
        payload = read_json(self._control.heartbeat_path, default={})
        if isinstance(payload, dict):
            self._last_heartbeat = {str(key): value for key, value in payload.items()}
            return self._last_heartbeat
        return dict(self._last_heartbeat)

    def _heartbeat_age(self, heartbeat: dict[str, Any]) -> float | None:
        raw_ts = heartbeat.get("ts")
        if isinstance(raw_ts, (int, float)):
            return max(0.0, time.time() - float(raw_ts))
        return None

    def _runtime_health(self, heartbeat: dict[str, Any]) -> str:
        age = self._heartbeat_age(heartbeat)
        if age is None:
            return "starting"
        if age > HEARTBEAT_STALE_AFTER_SECONDS:
            return "stale"
        if self._hard_reset_count or self._soft_reset_count:
            return "healthy"
        return str(heartbeat.get("runtime_health") or "healthy")

    async def _heartbeat_stale(self) -> bool:
        heartbeat = self._load_heartbeat()
        age = self._heartbeat_age(heartbeat)
        return age is None or age > HEARTBEAT_STALE_AFTER_SECONDS

    async def _recover_runtime(self, reason: str) -> None:
        self._last_reset_reason = reason
        if self._soft_reset_count < 1:
            self._soft_reset_count += 1
            await self.stop_process(reason=f"runtime recovery: {reason}")
            self._write_runtime_config()
            self._prime_runtime_state()
            await self._start_runtime_process()
            await self._wait_for_heartbeat()
            return
        self._hard_reset_count += 1
        self._soft_reset_count = 0
        old_sandbox = self.sandbox
        workspace_dir = old_sandbox.workspace_dir
        shared_artifacts_dir = old_sandbox.shared_artifacts_dir
        control_dir = old_sandbox.control_dir
        provider_home_dir = old_sandbox.provider_home_dir
        trace_dir = old_sandbox.trace_dir
        repo_root_dir = old_sandbox.repo_root_dir
        challenge_src_dir = old_sandbox.challenge_src_dir
        auth_seed_mounts = dict(old_sandbox.auth_seed_mounts)
        preserve_stopped_container = old_sandbox.preserve_stopped_container
        await old_sandbox.stop(delete=True)
        self.sandbox = DockerSandbox(
            image=old_sandbox.image,
            challenge_dir=old_sandbox.challenge_dir,
            memory_limit=old_sandbox.memory_limit,
            exec_output_spill_threshold_bytes=old_sandbox.exec_output_spill_threshold_bytes,
            read_file_spill_threshold_bytes=old_sandbox.read_file_spill_threshold_bytes,
            artifact_preview_bytes=old_sandbox.artifact_preview_bytes,
            workspace_dir=workspace_dir,
            shared_artifacts_dir=shared_artifacts_dir,
            control_dir=control_dir,
            provider_home_dir=provider_home_dir,
            trace_dir=trace_dir,
            repo_root_dir=repo_root_dir,
            challenge_src_dir=challenge_src_dir,
            auth_seed_mounts=auth_seed_mounts,
            preserve_stopped_container=preserve_stopped_container,
        )
        await self.sandbox.start()
        self._write_runtime_config()
        self._prime_runtime_state()
        await self._start_runtime_process()
        await self._wait_for_heartbeat()

    async def _process_events(self) -> None:
        self._event_offset, events = read_new_jsonl(
            self._control.events_path,
            offset=self._event_offset,
        )
        for event in events:
            event_type = str(event.get("type") or "").strip()
            if event_type == "usage_delta":
                self.cost_tracker.record_tokens(
                    str(event.get("agent_name") or self.agent_name),
                    str(event.get("model_name") or ""),
                    input_tokens=int(event.get("input_tokens", 0) or 0),
                    output_tokens=int(event.get("output_tokens", 0) or 0),
                    cache_read_tokens=int(event.get("cache_read_tokens", 0) or 0),
                    provider_spec=str(event.get("provider_spec") or self.provider),
                    duration_seconds=float(event.get("duration_seconds", 0.0) or 0.0),
                )
                continue
            if event_type in {"turn_result", "final_result"}:
                raw_result = event.get("result", {})
                if isinstance(raw_result, dict):
                    self._pending_results.append(
                        (self._map_result(raw_result), event_type == "final_result")
                    )
                continue
            if event_type == "candidate" and self.report_flag_candidate_fn:
                await self.report_flag_candidate_fn(
                    str(event.get("flag") or "").strip(),
                    evidence=str(event.get("evidence") or ""),
                    confidence=str(event.get("confidence") or "medium"),
                    step_count=int(event.get("step_count", 0) or 0),
                    trace_path=self._host_trace_path(str(event.get("trace_path") or "")),
                )
                continue
            if event_type == "coordinator_note" and self.notify_coordinator:
                await self.notify_coordinator(str(event.get("message") or ""))
                continue
            if event_type == "reset":
                self._last_reset_reason = str(
                    event.get("detail") or event.get("reason") or self._last_reset_reason
                )

    def _host_trace_path(self, raw_trace_path: str) -> str:
        if not raw_trace_path.startswith(f"{TRACE_CONTAINER_ROOT}/"):
            return raw_trace_path
        return str(self._host_state.trace_dir / Path(raw_trace_path).name)

    def _map_result(self, raw_result: dict[str, Any]) -> SolverResult:
        return SolverResult(
            flag=raw_result.get("flag") if isinstance(raw_result.get("flag"), str) else None,
            status=str(raw_result.get("status") or ERROR),
            findings_summary=str(raw_result.get("findings_summary") or ""),
            step_count=int(raw_result.get("step_count", 0) or 0),
            cost_usd=float(raw_result.get("cost_usd", 0.0) or 0.0),
            log_path=self._host_trace_path(str(raw_result.get("log_path") or "")),
            candidate_flag=raw_result.get("candidate_flag") if isinstance(raw_result.get("candidate_flag"), str) else None,
            candidate_evidence=str(raw_result.get("candidate_evidence") or ""),
            candidate_confidence=str(raw_result.get("candidate_confidence") or ""),
        )
