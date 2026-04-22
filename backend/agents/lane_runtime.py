"""Long-lived lane runtime executed inside the sandbox container."""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import signal
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pydantic_ai.usage import RunUsage

from backend.config import Settings
from backend.cost_tracker import AgentUsage, CostTracker
from backend.local_sandbox import LocalSandbox
from backend.models import provider_from_spec
from backend.platforms import PlatformClient, build_platform_client
from backend.prompts import ChallengeMeta
from backend.runtime_control import (
    HEARTBEAT_INTERVAL_SECONDS,
    LaneControlPaths,
    append_jsonl,
    heartbeat_payload,
    lane_control_paths,
    read_json,
    read_new_jsonl,
    write_json_atomic,
)
from backend.solver_base import SolverResult


def _dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


class RuntimeCostTrackerProxy:
    """Emit usage deltas to the host while keeping the solver-side API stable."""

    def __init__(self, events_path: Path, *, provider_spec: str) -> None:
        self.events_path = events_path
        self.provider_spec = provider_spec
        self._tracker = CostTracker()

    @property
    def by_agent(self) -> dict[str, AgentUsage]:
        return self._tracker.by_agent

    @property
    def total_cost_usd(self) -> float:
        return self._tracker.total_cost_usd

    @property
    def total_tokens(self) -> int:
        return self._tracker.total_tokens

    def record_tokens(
        self,
        agent_name: str,
        model_name: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        provider_spec: str = "",
        duration_seconds: float = 0.0,
    ) -> None:
        append_jsonl(
            self.events_path,
            {
                "type": "usage_delta",
                "ts": time.time(),
                "agent_name": agent_name,
                "model_name": model_name,
                "provider_spec": provider_spec or self.provider_spec,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": cache_read_tokens,
                "duration_seconds": duration_seconds,
            },
        )
        self._tracker.record_tokens(
            agent_name=agent_name,
            model_name=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            provider_spec=provider_spec or self.provider_spec,
            duration_seconds=duration_seconds,
        )

    def record(
        self,
        agent_name: str,
        usage: RunUsage,
        model_name: str,
        provider_spec: str = "",
        duration_seconds: float = 0.0,
    ) -> None:
        self.record_tokens(
            agent_name=agent_name,
            model_name=model_name,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            provider_spec=provider_spec or self.provider_spec,
            duration_seconds=duration_seconds,
        )

    def format_usage(self, agent_name: str) -> str:
        return self._tracker.format_usage(agent_name)

    def get_usage_by_model(self) -> dict[str, dict[str, Any]]:
        return self._tracker.get_usage_by_model()


class LaneRuntime:
    def __init__(self, control_paths: LaneControlPaths) -> None:
        self.control_paths = control_paths
        raw_config = read_json(control_paths.config_path, default={})
        if not isinstance(raw_config, dict):
            raise RuntimeError(f"Invalid runtime config: {control_paths.config_path}")
        self.config = _dict(raw_config)
        self.model_spec = str(self.config.get("model_spec") or "")
        self.provider = str(self.config.get("provider") or provider_from_spec(self.model_spec))
        self.meta = ChallengeMeta.from_dict(_dict(self.config.get("meta")))
        self.settings = self._build_settings()
        self.cancel_event = asyncio.Event()
        self._commands_offset = self._initial_commands_offset()
        self._last_event = "starting"
        self._running = True
        self._solver: Any = None
        self._ctfd: PlatformClient | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._turn_task: asyncio.Task[SolverResult] | None = None
        self._consecutive_errors = 0

    def _initial_commands_offset(self) -> int:
        try:
            return self.control_paths.commands_path.stat().st_size
        except FileNotFoundError:
            return 0

    def _build_settings(self) -> Settings:
        payload = _dict(self.config.get("settings"))
        provider_home = str(self.config.get("provider_home_dir") or "/challenge/provider-home")
        codex_auth_path = str(Path(provider_home) / ".codex" / "auth.json")
        gemini_auth_path = str(Path(provider_home) / ".gemini" / "oauth_creds.json")
        return Settings(
            ctfd_url=str(payload.get("ctfd_url") or "http://localhost:8000"),
            ctfd_user=str(payload.get("ctfd_user") or "admin"),
            ctfd_pass=str(payload.get("ctfd_pass") or "admin"),
            ctfd_token=str(payload.get("ctfd_token") or ""),
            remote_cookie_header=str(payload.get("remote_cookie_header") or ""),
            sandbox_image=str(payload.get("sandbox_image") or "ctf-sandbox"),
            container_memory_limit=str(payload.get("container_memory_limit") or "4g"),
            exec_output_spill_threshold_bytes=int(payload.get("exec_output_spill_threshold_bytes") or 65_536),
            read_file_spill_threshold_bytes=int(payload.get("read_file_spill_threshold_bytes") or 262_144),
            artifact_preview_bytes=int(payload.get("artifact_preview_bytes") or 8_192),
            codex_auth_path=codex_auth_path,
            gemini_auth_path=gemini_auth_path,
        )

    def _copy_auth_seeds(self) -> None:
        provider_home = Path(str(self.config.get("provider_home_dir") or "/challenge/provider-home"))
        provider_home.mkdir(parents=True, exist_ok=True)
        auth_seed_dir = Path(str(self.config.get("auth_seed_dir") or "/challenge/auth-seeds"))
        if auth_seed_dir.exists():
            for source_name, target_rel in (
                ("codex-auth.json", Path(".codex") / "auth.json"),
                ("gemini-oauth.json", Path(".gemini") / "oauth_creds.json"),
            ):
                source = auth_seed_dir / source_name
                if not source.exists():
                    continue
                target = provider_home / target_rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    target.write_bytes(source.read_bytes())

    def _apply_provider_runtime_env(self) -> None:
        provider_home = str(self.config.get("provider_home_dir") or "/challenge/provider-home")
        control_dir = Path(str(self.config.get("control_dir") or "/challenge/control"))
        workspace_dir = Path(str(self.config.get("workspace_dir") or "/challenge/workspace"))
        gemini_ipc_dir = control_dir / "gemini-ipc"
        legacy_gemini_ipc_dir = workspace_dir / ".gemini-ipc"
        if legacy_gemini_ipc_dir.exists():
            shutil.rmtree(legacy_gemini_ipc_dir, ignore_errors=True)
        for path in (gemini_ipc_dir, gemini_ipc_dir / "requests", gemini_ipc_dir / "responses"):
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o777)
        os.environ["CTF_AGENT_PROVIDER_HOME"] = provider_home
        os.environ["HOME"] = provider_home
        os.environ["CODEX_HOME"] = str(Path(provider_home) / ".codex")
        os.environ["CTF_AGENT_GEMINI_IPC_DIR"] = str(gemini_ipc_dir)

    async def start(self) -> None:
        self._copy_auth_seeds()
        self._apply_provider_runtime_env()
        os.environ["CTF_AGENT_LOG_DIR"] = str(self.config.get("trace_dir") or "/challenge/host-logs")
        os.environ["CTF_AGENT_CODEX_THREAD_PATH"] = str(self.control_paths.codex_thread_path)

        self._ctfd = build_platform_client(
            self.settings,
            {self.meta.name: self.meta},
            local_mode=bool(self.config.get("local_mode", False)),
            cookie_header=self.settings.remote_cookie_header,
        )
        sandbox = LocalSandbox(
            challenge_dir=str(self.config.get("challenge_dir") or "/challenge/challenge-src"),
            workspace_dir=str(self.config.get("workspace_dir") or "/challenge/workspace"),
            shared_artifacts_dir=str(
                self.config.get("shared_artifacts_dir") or "/challenge/shared-artifacts"
            ),
            exec_output_spill_threshold_bytes=self.settings.exec_output_spill_threshold_bytes,
            read_file_spill_threshold_bytes=self.settings.read_file_spill_threshold_bytes,
            artifact_preview_bytes=self.settings.artifact_preview_bytes,
        )
        await sandbox.start()

        cost_tracker = RuntimeCostTrackerProxy(
            self.control_paths.events_path,
            provider_spec=self.provider,
        )
        initial_step_count = 0
        heartbeat = _dict(read_json(self.control_paths.heartbeat_path, default={}))
        raw_step = heartbeat.get("step_count", 0)
        if isinstance(raw_step, (int, float)):
            initial_step_count = int(raw_step)

        solver_kwargs: dict[str, Any] = {
            "model_spec": self.model_spec,
            "challenge_dir": str(self.config.get("challenge_dir") or "/challenge/challenge-src"),
            "meta": self.meta,
            "ctfd": self._ctfd,
            "cost_tracker": cost_tracker,
            "settings": self.settings,
            "cancel_event": self.cancel_event,
            "no_submit": bool(self.config.get("no_submit", False)),
            "report_flag_candidate_fn": self._report_flag_candidate,
            "message_bus": None,
            "notify_coordinator": self._notify_coordinator,
            "sandbox": sandbox,
            "initial_step_count": initial_step_count,
        }
        if self.provider == "codex":
            from backend.agents.codex_solver import CodexSolver

            self._solver = CodexSolver(**solver_kwargs)
        elif self.provider in {"gemini", "google"}:
            from backend.agents.gemini_solver import GeminiSolver

            self._solver = GeminiSolver(**solver_kwargs)
        else:
            raise RuntimeError(f"Unsupported runtime provider: {self.provider}")

        await self._solver.start()
        self.control_paths.result_path.unlink(missing_ok=True)
        write_json_atomic(self.control_paths.runtime_pid_path, {"pid": os.getpid()}, mode=0o644)
        await self._write_heartbeat()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        append_jsonl(
            self.control_paths.events_path,
            {"type": "reset", "ts": time.time(), "reason": "runtime_started"},
        )

    async def _report_flag_candidate(
        self,
        flag: str,
        evidence: str = "",
        confidence: str = "medium",
        step_count: int = 0,
        trace_path: str = "",
    ) -> str:
        append_jsonl(
            self.control_paths.events_path,
            {
                "type": "candidate",
                "ts": time.time(),
                "flag": flag,
                "evidence": evidence,
                "confidence": confidence,
                "step_count": step_count,
                "trace_path": trace_path,
            },
        )
        self._last_event = "candidate"
        return f"Queued candidate for host review: {flag}"

    async def _notify_coordinator(self, message: str) -> None:
        append_jsonl(
            self.control_paths.events_path,
            {"type": "coordinator_note", "ts": time.time(), "message": message},
        )
        self._last_event = "coordinator_note"

    async def _heartbeat_loop(self) -> None:
        try:
            while self._running:
                await self._write_heartbeat()
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            append_jsonl(
                self.control_paths.events_path,
                {"type": "fatal", "ts": time.time(), "error": f"heartbeat failed: {exc}"},
            )
            self._last_event = "fatal"
            self._running = False

    async def _write_heartbeat(self) -> None:
        runtime = self._solver.get_runtime_status() if self._solver else {}
        thread_id = str(getattr(self._solver, "_thread_id", "") or "")
        session_id = str(getattr(self._solver, "_session_id", "") or "")
        payload = heartbeat_payload(
            provider=self.provider,
            model_spec=self.model_spec,
            runtime_status=runtime,
            last_event=self._last_event,
            thread_id=thread_id,
            session_id=session_id,
        )
        write_json_atomic(self.control_paths.heartbeat_path, payload, mode=0o644)

    async def run(self) -> None:
        await self.start()
        try:
            while self._running:
                self._commands_offset, commands = read_new_jsonl(
                    self.control_paths.commands_path,
                    offset=self._commands_offset,
                )
                for command in commands:
                    try:
                        await self._handle_command(command)
                    except Exception as exc:
                        append_jsonl(
                            self.control_paths.events_path,
                            {
                                "type": "fatal",
                                "ts": time.time(),
                                "error": str(exc),
                            },
                        )
                        await self._write_terminal_result(
                            SolverResult(
                                flag=None,
                                status="error",
                                findings_summary=f"Runtime fatal: {exc}",
                                step_count=int(
                                    self._solver.get_runtime_status().get("step_count", 0)
                                    if self._solver
                                    else 0
                                ),
                                cost_usd=0.0,
                                log_path="",
                            )
                        )

                if self._turn_task is None and self._running and not self.cancel_event.is_set():
                    self._turn_task = asyncio.create_task(self._solver.run_until_done_or_gave_up())

                if self._turn_task is not None and self._turn_task.done():
                    try:
                        result = await self._turn_task
                    except Exception as exc:
                        append_jsonl(
                            self.control_paths.events_path,
                            {
                                "type": "fatal",
                                "ts": time.time(),
                                "error": f"lane turn crashed: {exc}",
                            },
                        )
                        await self._write_terminal_result(
                            SolverResult(
                                flag=None,
                                status="error",
                                findings_summary=f"Runtime fatal: {exc}",
                                step_count=int(
                                    self._solver.get_runtime_status().get("step_count", 0)
                                    if self._solver
                                    else 0
                                ),
                                cost_usd=0.0,
                                log_path="",
                            )
                        )
                    else:
                        await self._handle_turn_result(result)
                    finally:
                        self._turn_task = None

                if self.cancel_event.is_set() and self._turn_task is None:
                    await self._write_terminal_result(
                        SolverResult(
                            flag=None,
                            status="cancelled",
                            findings_summary="cancelled",
                            step_count=int(
                                self._solver.get_runtime_status().get("step_count", 0)
                                if self._solver
                                else 0
                            ),
                            cost_usd=0.0,
                            log_path="",
                        )
                    )

                await asyncio.sleep(0.2)
        finally:
            await self.stop()

    async def _handle_command(self, command: dict[str, Any]) -> None:
        cmd_type = str(command.get("type") or "").strip()
        if not cmd_type:
            return
        self._last_event = cmd_type

        if cmd_type == "advisory":
            insights = str(command.get("insights") or "").strip()
            if insights:
                self._solver.bump_advisory(insights)
                append_jsonl(
                    self.control_paths.events_path,
                    {"type": "advisory_applied", "ts": time.time(), "source": "advisor", "insights": insights},
                )
            return

        if cmd_type == "operator_bump":
            insights = str(command.get("insights") or "").strip()
            if insights:
                bump_operator = getattr(self._solver, "bump_operator", None)
                if callable(bump_operator):
                    bump_operator(insights)
                else:
                    self._solver.bump(insights)
                append_jsonl(
                    self.control_paths.events_path,
                    {"type": "advisory_applied", "ts": time.time(), "source": "operator", "insights": insights},
                )
            return

        if cmd_type == "auto_bump":
            insights = str(command.get("insights") or "").strip()
            if insights:
                self._solver.bump(insights)
                append_jsonl(
                    self.control_paths.events_path,
                    {"type": "advisory_applied", "ts": time.time(), "source": "auto", "insights": insights},
                )
            return

        if cmd_type == "cancel":
            reason = " ".join(str(command.get("reason") or "").split()).strip()
            self.cancel_event.set()
            append_jsonl(
                self.control_paths.events_path,
                {
                    "type": "reset",
                    "ts": time.time(),
                    "reason": "cancel",
                    "detail": reason,
                },
            )
            return

        if cmd_type == "shutdown":
            reason = " ".join(str(command.get("reason") or "").split()).strip()
            self.cancel_event.set()
            self._running = False
            append_jsonl(
                self.control_paths.events_path,
                {
                    "type": "reset",
                    "ts": time.time(),
                    "reason": "shutdown",
                    "detail": reason,
                },
            )
            return

    async def _handle_turn_result(self, result: SolverResult) -> None:
        append_jsonl(
            self.control_paths.events_path,
            {
                "type": "turn_result",
                "ts": time.time(),
                "result": asdict(result),
            },
        )
        self._last_event = result.status

        if result.status == "flag_candidate":
            self._consecutive_errors = 0
            self._solver.bump(
                "Flag candidate processed through the guarded path. "
                "If it was not confirmed, keep exploring, gather stronger evidence, and do not resubmit the same candidate."
            )
            return

        if result.status in {"flag_found", "quota_error", "cancelled"}:
            self._consecutive_errors = 0
            await self._write_terminal_result(result)
            return

        if result.status == "error":
            self._consecutive_errors += 1
            broken_runtime = result.step_count == 0 and result.cost_usd == 0
            if broken_runtime or self._consecutive_errors >= 3:
                await self._write_terminal_result(result)
            return

        self._consecutive_errors = 0

    async def _write_terminal_result(self, result: SolverResult) -> None:
        if self._solver is not None:
            self._solver.mark_terminal_status(result.status)
        self._last_event = result.status
        await self._write_heartbeat()
        payload = {
            "type": "final_result",
            "ts": time.time(),
            "result": asdict(result),
        }
        write_json_atomic(self.control_paths.result_path, payload, mode=0o644)
        append_jsonl(self.control_paths.events_path, payload)
        self._running = False

    async def stop(self) -> None:
        self._running = False
        if self._turn_task is not None:
            self._turn_task.cancel()
            await asyncio.gather(self._turn_task, return_exceptions=True)
            self._turn_task = None
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            self._heartbeat_task = None
        if self._solver is not None:
            await self._solver.stop()
        if self._ctfd is not None:
            await self._ctfd.close()
        self.control_paths.runtime_pid_path.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the in-sandbox lane runtime.")
    parser.add_argument(
        "--control-dir",
        default="/challenge/control",
        help="Host-mounted control directory containing commands/events/state files.",
    )
    return parser.parse_args()


async def _amain() -> int:
    args = parse_args()
    control_paths = lane_control_paths(args.control_dir)
    runtime = LaneRuntime(control_paths)
    stop_event = asyncio.Event()

    def _handle_signal(_signum: int, _frame: Any) -> None:
        stop_event.set()
        runtime.cancel_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    runner = asyncio.create_task(runtime.run())
    stopper = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)
    if stopper in done:
        append_jsonl(control_paths.events_path, {"type": "reset", "ts": time.time(), "reason": "signal"})
        runtime._running = False
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.gather(runner, return_exceptions=True)
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
