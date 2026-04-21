"""Click CLI entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import webbrowser
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import click
from rich import box
from rich.console import Console
from rich.table import Table

from backend.agents.codex_coordinator import run_codex_coordinator
from backend.agents.coordinator_core import (
    PENDING_REASON_PRIORITY_WAITING,
    restore_pending_swarms_from_results,
)
from backend.agents.coordinator_loop import build_deps, cleanup_coordinator_runtime
from backend.auth import AuthValidationError, validate_claude_auth, validate_required_auth
from backend.config import Settings
from backend.models import DEFAULT_MODELS, provider_from_spec
from backend.tracing import _sanitize as _sanitize_trace_component

console = Console()
ADVISOR_LABEL_RE = re.compile(r"^\[(?:claude\s+)?advisor\]\s*", re.IGNORECASE)


def _object_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _agent_dict(value: object) -> dict[str, dict[str, object]]:
    agents: dict[str, dict[str, object]] = {}
    for key, item in _object_dict(value).items():
        if isinstance(item, dict):
            agents[key] = {str(inner_key): inner_value for inner_key, inner_value in item.items()}
    return agents


def _int_from_object(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _parse_memory_limit_bytes(value: object) -> int | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    try:
        if text.endswith("g"):
            return int(text[:-1]) * 1024 * 1024 * 1024
        if text.endswith("m"):
            return int(text[:-1]) * 1024 * 1024
        if text.endswith("k"):
            return int(text[:-1]) * 1024
        return int(text)
    except (TypeError, ValueError):
        return None


def _host_memory_bytes() -> int | None:
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        total_pages = int(os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, ValueError):
        return None
    total = page_size * total_pages
    return total if total > 0 else None


def _format_gib(value: int | None) -> str:
    if not value or value <= 0:
        return "unknown"
    return f"{value / (1024 ** 3):.1f} GiB"


def _memory_budget_summary(
    memory_limit: object,
    *,
    lane_count: int,
    challenge_count: int,
    host_memory_bytes: int | None = None,
) -> dict[str, object]:
    per_lane_bytes = _parse_memory_limit_bytes(memory_limit)
    safe_lane_count = max(1, lane_count)
    safe_challenge_count = max(1, challenge_count)
    one_challenge_bytes = (per_lane_bytes or 0) * safe_lane_count
    max_total_bytes = one_challenge_bytes * safe_challenge_count
    host_bytes = host_memory_bytes if host_memory_bytes is not None else _host_memory_bytes()
    warn_single = bool(host_bytes and one_challenge_bytes > host_bytes)
    warn_total = bool(host_bytes and max_total_bytes > host_bytes)
    return {
        "per_lane_bytes": per_lane_bytes or 0,
        "one_challenge_bytes": one_challenge_bytes,
        "max_total_bytes": max_total_bytes,
        "host_memory_bytes": host_bytes or 0,
        "per_lane_display": str(memory_limit or "unknown"),
        "one_challenge_display": _format_gib(one_challenge_bytes),
        "max_total_display": _format_gib(max_total_bytes),
        "host_memory_display": _format_gib(host_bytes),
        "warn_single": warn_single,
        "warn_total": warn_total,
    }


def _discover_challenge_dirs(root: str | Path) -> list[Path]:
    base = Path(root).resolve()
    if (base / "metadata.yml").exists():
        return [base]
    if not base.exists():
        return []
    challenge_dirs: list[Path] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        if (entry / "metadata.yml").exists():
            challenge_dirs.append(entry.resolve())
    return challenge_dirs


@dataclass
class _RuntimeResetSummary:
    lane_state_dirs: int = 0
    shared_artifact_dirs: int = 0
    solve_lane_dirs: int = 0
    trace_files: int = 0

    @property
    def touched(self) -> bool:
        return any(
            (
                self.lane_state_dirs,
                self.shared_artifact_dirs,
                self.solve_lane_dirs,
                self.trace_files,
            )
        )


def _challenge_trace_stems(challenge_dir: Path) -> set[str]:
    stems = {_sanitize_trace_component(challenge_dir.name)}
    metadata_path = challenge_dir / "metadata.yml"
    if metadata_path.exists():
        try:
            from backend.prompts import ChallengeMeta

            stems.add(_sanitize_trace_component(ChallengeMeta.from_yaml(metadata_path).name))
        except Exception:
            pass
    return {stem for stem in stems if stem}


def _remove_matching_trace_files(challenge_dir: Path, *, log_dir: str | Path = "logs") -> int:
    root = Path(log_dir).resolve()
    if not root.exists():
        return 0
    removed = 0
    for stem in _challenge_trace_stems(challenge_dir):
        for trace_path in root.glob(f"trace-{stem}-*.jsonl"):
            if trace_path.name.startswith("ctf-solve-"):
                continue
            if trace_path.exists():
                _remove_runtime_path(trace_path)
                removed += 1
    return removed


def _reset_runtime_state_dirs(
    challenge_dirs: Iterable[str | Path],
    *,
    log_dir: str | Path = "logs",
) -> _RuntimeResetSummary:
    summary = _RuntimeResetSummary()
    seen: set[Path] = set()
    for raw_dir in challenge_dirs:
        challenge_dir = Path(raw_dir).resolve()
        if challenge_dir in seen:
            continue
        seen.add(challenge_dir)
        lane_state_dir = challenge_dir / ".lane-state"
        shared_artifacts_dir = challenge_dir / ".shared-artifacts"
        solve_lanes_dir = challenge_dir / "solve" / "lanes"
        if lane_state_dir.exists():
            _remove_runtime_path(lane_state_dir)
            summary.lane_state_dirs += 1
        if shared_artifacts_dir.exists():
            _remove_runtime_path(shared_artifacts_dir)
            summary.shared_artifact_dirs += 1
        if solve_lanes_dir.exists():
            _remove_runtime_path(solve_lanes_dir)
            summary.solve_lane_dirs += 1
        summary.trace_files += _remove_matching_trace_files(challenge_dir, log_dir=log_dir)
    return summary


def _remove_runtime_path(path: Path) -> None:
    if not path.exists():
        return
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        return
    except PermissionError:
        _force_remove_runtime_path(path)


def _force_remove_runtime_path(path: Path) -> None:
    parent_dir = path.parent
    target_name = path.name
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{parent_dir}:/lane-parent",
            "ctf-sandbox",
            "bash",
            "-lc",
            f"rm -rf -- /lane-parent/{shlex_quote(target_name)}",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def shlex_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _default_run_log_path() -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    log_dir = repo_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    return log_dir / f"ctf-solve-{timestamp}.log"


def _setup_logging(verbose: bool = False, *, log_path: Path | None = None) -> Path | None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("aiodocker").setLevel(logging.WARNING)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)-8s %(message)s", datefmt="%X"))
    handlers: list[logging.Handler] = [handler]
    resolved_log_path = log_path.resolve() if log_path is not None else _default_run_log_path()
    file_handler = logging.FileHandler(resolved_log_path, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    handlers.append(file_handler)
    logging.basicConfig(level=level, handlers=handlers, force=True)
    return resolved_log_path


def _install_shutdown_signal_handlers(deps) -> list[signal.Signals]:
    """Request graceful shutdown instead of letting asyncio.run cancel everything."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return []

    installed: list[signal.Signals] = []
    logger = logging.getLogger(__name__)
    signal_count = {"count": 0}

    def _request_shutdown(signame: str) -> None:
        signal_count["count"] += 1
        if signal_count["count"] == 1:
            deps.shutdown_reason = f"signal {signame}"
            deps.shutdown_event.set()
            logger.info("Received %s; requesting graceful shutdown (press Ctrl+C again to force exit)", signame)
            return

        deps.shutdown_reason = f"forced signal {signame}"
        logger.warning("Received %s again; forcing exit", signame)
        logging.shutdown()
        os._exit(130 if signame == "SIGINT" else 143)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig.name)
        except (NotImplementedError, RuntimeError, ValueError):
            continue
        installed.append(sig)
    return installed


def _remove_shutdown_signal_handlers(installed_signals: list[signal.Signals]) -> None:
    if not installed_signals:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for sig in installed_signals:
        try:
            loop.remove_signal_handler(sig)
        except (RuntimeError, ValueError):
            continue


def _preview_line(value: object, limit: int = 120) -> str:
    text = str(value or "")
    lines = text.splitlines()
    if not lines:
        return ""
    return lines[0][:limit]


def _clean_status_text(value: object, limit: int = 110) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    lower = text.lower()
    if "yolo mode is enabled" in lower:
        return ""
    if "usage limit" in lower:
        return "usage limit hit"
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _format_advisor_note(value: object, limit: int = 100) -> str:
    note = _clean_status_text(value, limit=max(1, limit - 11))
    if not note:
        return ""
    note = ADVISOR_LABEL_RE.sub("", note).strip()
    if not note:
        return ""
    return f"[Advisor] {note}"


def _swarm_advisor_note(swarm: dict[str, object], *, limit: int = 100) -> str:
    return _format_advisor_note(swarm.get("advisor_note") or "", limit=limit) or "-"


def _agent_advisor_note(agent: dict[str, object], *, limit: int = 100) -> str:
    return _format_advisor_note(agent.get("advisor_note") or "", limit=limit) or "-"


def _shared_finding_entries(swarm: dict[str, object]) -> list[tuple[str, dict[str, object]]]:
    entries: list[tuple[str, dict[str, object]]] = []
    raw_shared_findings = _object_dict(swarm.get("shared_findings", {}))
    for model_spec in sorted(raw_shared_findings):
        payload = _object_dict(raw_shared_findings[model_spec])
        if payload:
            entries.append((model_spec, payload))
    if entries:
        return entries

    legacy = _clean_status_text(swarm.get("shared_finding") or "", limit=160)
    if not legacy:
        return []
    return [("-", {"summary": legacy})]


def _render_shared_finding_payload(
    payload: dict[str, object],
    *,
    limit: int = 100,
    include_paths: bool = False,
) -> str:
    summary = _clean_status_text(payload.get("summary") or payload.get("content") or "", limit=limit)
    if not summary:
        summary = "-"

    extras: list[str] = []
    artifact_path = _clean_status_text(payload.get("artifact_path") or "", limit=120)
    digest_path = _clean_status_text(payload.get("digest_path") or "", limit=120)
    pointer_path = _clean_status_text(payload.get("pointer_path") or "", limit=120)
    if include_paths and artifact_path:
        extras.append(f"artifact {artifact_path}")
    if digest_path:
        extras.append(f"digest {digest_path}")
    if include_paths and pointer_path:
        extras.append(f"ptr {pointer_path}")
    if not extras:
        return summary
    return f"{summary} | {' | '.join(extras)}"


def _swarm_shared_finding(swarm: dict[str, object], *, limit: int = 100) -> str:
    entries = _shared_finding_entries(swarm)
    if not entries:
        return "-"
    _, payload = entries[0]
    return _render_shared_finding_payload(payload, limit=limit) or "-"


def _short_model_name(spec: str) -> str:
    if spec.startswith("gemini/"):
        suffix = spec.split("/", 1)[1]
        mapping = {
            "gemini-2.5-flash": "g-flash",
            "gemini-2.5-flash-lite": "g-flash-lite",
            "gemini-2.5-pro": "g-pro",
        }
        return mapping.get(suffix, suffix.replace("gemini-", "g-"))
    if spec.startswith("codex/"):
        suffix = spec.split("/", 1)[1]
        mapping = {
            "gpt-5.4": "5.4",
            "gpt-5.4-mini": "5.4-mini",
            "gpt-5.3-codex": "5.3-codex",
            "gpt-5.3-codex-spark": "5.3-spark",
        }
        return mapping.get(suffix, suffix.replace("gpt-", ""))
    if spec.startswith("claude-sdk/"):
        suffix = spec.split("/", 1)[1]
        return suffix.replace("claude-", "c-")
    return spec


def _table_model_name(spec: str) -> str:
    return str(spec)


def _format_models_line(models: list[str], *, compact: bool = False) -> str:
    if compact:
        short = [_short_model_name(spec) for spec in models]
        preview = ", ".join(short[:4])
        if len(short) > 4:
            preview += f", +{len(short) - 4} more"
        return f"Models: {len(models)} lanes ({preview})"
    return f"Models: {', '.join(models)}"


def _format_agent_activity(agent: dict[str, object]) -> str:
    lifecycle = str(agent.get("lifecycle") or agent.get("status") or "?")
    runtime_health = str(agent.get("runtime_health") or "")
    activity_state = str(agent.get("activity_state") or "")
    activity = _clean_status_text(_preview_line(agent.get("activity", ""), limit=140))
    commentary = _clean_status_text(_preview_line(agent.get("commentary_preview", ""), limit=140))
    current_tool = str(agent.get("current_tool") or "")
    last_tool = str(agent.get("last_tool") or "")
    current_command = _clean_status_text(_preview_line(agent.get("current_command", ""), limit=140))
    last_command = _clean_status_text(_preview_line(agent.get("last_command", ""), limit=140))
    exit_hint = _clean_status_text(_preview_line(agent.get("last_exit_hint", ""), limit=80))
    findings = _clean_status_text(_preview_line(agent.get("findings", ""), limit=100))
    heartbeat_age = agent.get("heartbeat_age_sec")

    parts = [lifecycle]
    if activity_state and activity_state not in {"idle", lifecycle}:
        parts.append(f"state: {activity_state}")
    if current_command:
        label = current_tool or "tool"
        parts.append(f"now/{label}: {current_command}")
    elif activity and activity_state == "thinking":
        parts.append(f"thinking: {activity}")
    elif commentary:
        parts.append(f"thinking: {commentary}")
    elif activity:
        parts.append(f"activity: {activity}")
    elif last_command:
        label = last_tool or "tool"
        parts.append(f"last/{label}: {last_command}")

    if runtime_health and runtime_health not in {"healthy", lifecycle}:
        parts.append(f"health: {runtime_health}")
    if heartbeat_age is not None and runtime_health in {"stale", "resetting"}:
        try:
            parts.append(f"heartbeat: {float(heartbeat_age):.1f}s")
        except (TypeError, ValueError):
            pass

    if exit_hint and exit_hint not in {lifecycle, current_command, last_command}:
        parts.append(f"note: {exit_hint}")
    elif findings and findings not in {current_command, last_command, exit_hint}:
        parts.append(f"finding: {findings}")
    elif lifecycle == "quota_error" and not any((current_command, last_command, exit_hint, findings)):
        parts.append("quota hit")
    return " | ".join(part for part in parts if part)


def _format_agent_detail(agent: dict[str, object]) -> str:
    status = str(agent.get("lifecycle") or agent.get("status") or "?")
    activity = _format_agent_activity(agent)
    if activity.startswith(f"{status} | "):
        return activity[len(status) + 3:]
    return activity


def _format_agent_row(spec: str, agent: dict[str, object]) -> str:
    status = str(agent.get("lifecycle") or agent.get("status") or "?")
    step_count = str(agent.get("step_count", 0))
    detail = _format_agent_detail(agent)
    return f"    {_table_model_name(spec):<30}  {step_count:>4}  {status:<12}  {detail}"


def _summarize_swarm_agents(agents: dict[str, dict[str, object]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for agent in agents.values():
        lifecycle = str(agent.get("lifecycle") or agent.get("status") or "pending")
        counts[lifecycle] += 1
    return {
        "busy": counts.get("busy", 0),
        "idle": counts.get("idle", 0),
        "won": counts.get("won", 0),
        "quota": counts.get("quota_error", 0),
        "error": counts.get("error", 0),
        "cancelled": counts.get("cancelled", 0),
        "finished": counts.get("finished", 0),
        "pending": counts.get("pending", 0),
    }


def _swarm_step_count(agents: dict[str, dict[str, object]]) -> int:
    total = 0
    for agent in agents.values():
        total += _int_from_object(agent.get("step_count", 0))
    return total


def _problem_specs(
    agents: dict[str, dict[str, object]],
    *,
    verbose: bool,
) -> list[str]:
    del verbose
    return sorted(agents)


def _render_swarm_section(
    lines: list[str],
    title: str,
    swarms: dict[str, object],
    *,
    verbose: bool,
) -> None:
    if not swarms:
        return

    lines.append("")
    lines.append(f"[bold]{title}[/bold]")
    lines.append("  Challenge             Steps  Busy  Idle  Won  Quota  Error  Cancel  Winner")
    lines.append("  -------------------- -----  ----- ----- ---- ------ ------ ------- ------------")
    for challenge in sorted(swarms):
        swarm = _object_dict(swarms[challenge])
        agents = _agent_dict(swarm.get("agents", {}))
        counts = _summarize_swarm_agents(agents)
        step_count = max(_swarm_step_count(agents), _int_from_object(swarm.get("step_count", 0)))
        winner = _clean_status_text(swarm.get("winner") or "-", limit=12) or "-"
        lines.append(
            "  "
            f"{challenge:<20} "
            f"{step_count:>5} "
            f"{counts['busy']:>2} "
            f"{counts['idle']:>2} "
            f"{counts['won']:>2} "
            f"{counts['quota']:>2} "
            f"{counts['error']:>2} "
            f"{counts['cancelled']:>2} "
            f"{winner:<12}"
        )
        specs = _problem_specs(agents, verbose=verbose)
        if specs:
            lines.append("    Lane                              Step  State        Detail")
            lines.append("    --------------------------------  ----  -----------  -----------------------------------------------")
            for spec in specs:
                lines.append(_format_agent_row(spec, agents[spec]))


def _render_status_lines(
    data: dict | None,
    *,
    fetch_error: str = "",
    updated_at: float | None = None,
    verbose: bool = False,
) -> list[str]:
    lines: list[str] = ["[bold]Coordinator Status[/bold]"]

    if updated_at is not None:
        lines[0] += f" [dim](updated {time.strftime('%H:%M:%S', time.localtime(updated_at))})[/dim]"

    if fetch_error:
        lines.append(f"[red]Status fetch failed:[/red] {fetch_error}")

    if not data:
        lines.append("No status data yet.")
        return lines

    lines.append(_format_models_line(list(data.get("models", []))))
    lines.append(
        "Challenges: "
        f"{data.get('known_challenge_count', 0)}"
        f" | Solved: {data.get('known_solved_count', 0)}"
        " | Active: "
        f"{data.get('active_swarm_count', 0)}"
        f" | Limit: {data.get('max_concurrent_challenges', 0)}"
        f" | Pending: {data.get('pending_challenge_count', 0)}"
        f" | Finished: {data.get('finished_swarm_count', 0)}"
        f" | Steps: {data.get('total_step_count', 0)}"
        f" | Cost: ${data.get('cost_usd', 0):.2f}"
        f" | Tokens: {data.get('total_tokens', 0)}"
    )
    lines.append(
        f"Queues: coordinator={data.get('coordinator_queue_depth', 0)}, "
        f"operator={data.get('operator_queue_depth', 0)}"
    )

    _render_swarm_section(
        lines,
        "Active Challenges",
        data.get("active_swarms", {}),
        verbose=verbose,
    )
    _render_swarm_section(
        lines,
        "Pending Challenges",
        data.get("pending_swarms", {}),
        verbose=verbose,
    )
    _render_swarm_section(
        lines,
        "Finished Challenges",
        data.get("finished_swarms", {}),
        verbose=verbose,
    )

    advisor_rows: list[tuple[str, str, str]] = []
    for swarms in (
        _object_dict(data.get("active_swarms", {})),
        _object_dict(data.get("pending_swarms", {})),
        _object_dict(data.get("finished_swarms", {})),
    ):
        for challenge in sorted(swarms):
            swarm = _object_dict(swarms[challenge])
            agents = _agent_dict(swarm.get("agents", {}))
            for spec in sorted(agents):
                note = _agent_advisor_note(agents[spec], limit=80)
                if note != "-" and (challenge, spec, note) not in advisor_rows:
                    advisor_rows.append((challenge, spec, note))
    lines.append("")
    lines.append("[bold]Latest Advisory[/bold]")
    lines.append("  Challenge             Lane                              Advisory")
    lines.append("  --------------------  --------------------------------  ----------------------------------------")
    if advisor_rows:
        for challenge, spec, note in advisor_rows:
            lines.append(f"  {challenge:<20}  {spec:<32}  {note}")
    else:
        lines.append("  (none yet)            -                                 -")

    finding_rows: list[tuple[str, str, str]] = []
    for swarms in (
        _object_dict(data.get("active_swarms", {})),
        _object_dict(data.get("pending_swarms", {})),
        _object_dict(data.get("finished_swarms", {})),
    ):
        for challenge in sorted(swarms):
            swarm = _object_dict(swarms[challenge])
            for spec, payload in _shared_finding_entries(swarm):
                finding = _render_shared_finding_payload(payload, limit=90, include_paths=verbose)
                row = (challenge, spec, finding)
                if finding != "-" and row not in finding_rows:
                    finding_rows.append(row)
    lines.append("")
    lines.append("[bold]Latest Shared Finding[/bold]")
    lines.append("  Challenge             Lane                              Finding")
    lines.append("  --------------------  --------------------------------  ----------------------------------------")
    if finding_rows:
        for challenge, spec, finding in finding_rows:
            lines.append(f"  {challenge:<20}  {spec:<32}  {finding}")
    else:
        lines.append("  (none yet)            -                                 -")

    signal_rows: list[tuple[str, dict[str, object]]] = []
    for swarms in (
        _object_dict(data.get("active_swarms", {})),
        _object_dict(data.get("pending_swarms", {})),
        _object_dict(data.get("finished_swarms", {})),
    ):
        for challenge in sorted(swarms):
            swarm = _object_dict(swarms[challenge])
            signals = _object_dict(swarm.get("signals", {}))
            if signals:
                signal_rows.append((challenge, signals))
    lines.append("")
    lines.append("[bold]Signals[/bold]")
    lines.append("  Challenge             Posts  Reads  Delivered  CoordMsg  LaneAdv  AdvMsg")
    lines.append("  --------------------  -----  -----  ---------  --------  -------  ------")
    if signal_rows:
        for challenge, signals in signal_rows:
            lines.append(
                "  "
                f"{challenge:<20}  "
                f"{_int_from_object(signals.get('total_posts', 0)):>5}  "
                f"{_int_from_object(signals.get('total_checks', 0)):>5}  "
                f"{_int_from_object(signals.get('total_delivered', 0)):>9}  "
                f"{_int_from_object(signals.get('coordinator_messages', 0)):>8}  "
                f"{_int_from_object(signals.get('advisor_lane_hints', signals.get('advisor_finding_posts', 0))):>7}  "
                f"{_int_from_object(signals.get('advisor_coordinator_appends', 0)):>6}"
            )
    else:
        lines.append("  (none yet)                0      0          0         0        0       0")

    results = data.get("results", {})
    if results:
        lines.append("")
        lines.append("[bold]Flags[/bold]")
        lines.append("  Challenge             Flag")
        lines.append("  --------------------  ----------------------------------------")
        for challenge in sorted(results):
            result = results[challenge]
            flag = result.get("flag", "-")
            if flag and flag != "-":
                lines.append(f"  {challenge:<20}  {_clean_status_text(flag, limit=40)}")

    return lines


def _build_summary_table(title: str, swarms: dict[str, object]) -> Table | None:
    if not swarms:
        return None

    table = Table(title=title, box=box.ASCII2, expand=True)
    table.add_column("Challenge", no_wrap=True)
    table.add_column("Steps", justify="right", width=6)
    table.add_column("Busy", justify="right", width=5)
    table.add_column("Idle", justify="right", width=5)
    table.add_column("Won", justify="right", width=4)
    table.add_column("Quota", justify="right", width=6)
    table.add_column("Error", justify="right", width=6)
    table.add_column("Cancel", justify="right", width=7)
    table.add_column("Winner", overflow="fold")

    for challenge in sorted(swarms):
        swarm = _object_dict(swarms[challenge])
        agents = _agent_dict(swarm.get("agents", {}))
        counts = _summarize_swarm_agents(agents)
        step_count = max(_swarm_step_count(agents), _int_from_object(swarm.get("step_count", 0)))
        winner = _clean_status_text(swarm.get("winner") or "-", limit=32) or "-"
        table.add_row(
            challenge,
            str(step_count),
            str(counts["busy"]),
            str(counts["idle"]),
            str(counts["won"]),
            str(counts["quota"]),
            str(counts["error"]),
            str(counts["cancelled"]),
            winner,
        )
    return table


def _build_lane_table(title: str, swarms: dict[str, object], *, verbose: bool) -> Table | None:
    rows: list[tuple[str, str, str, str, str]] = []
    for challenge in sorted(swarms):
        swarm = _object_dict(swarms[challenge])
        agents = _agent_dict(swarm.get("agents", {}))
        if not agents:
            continue
        for spec in _problem_specs(agents, verbose=verbose):
            agent = agents[spec]
            rows.append(
                (
                    challenge,
                    _table_model_name(spec),
                    str(agent.get("step_count", 0)),
                    str(agent.get("lifecycle") or agent.get("status") or "?"),
                    _format_agent_detail(agent),
                )
            )

    if not rows:
        return None

    table = Table(title=title, box=box.ASCII2, expand=True)
    table.add_column("Challenge", no_wrap=True)
    table.add_column("Lane", overflow="fold", min_width=24)
    table.add_column("Step", justify="right", width=6)
    table.add_column("State", overflow="fold", min_width=12)
    table.add_column("Detail", overflow="fold")
    for row in rows:
        table.add_row(*row)
    return table


def _build_compact_lane_renderables(
    title: str,
    swarms: dict[str, object],
    *,
    verbose: bool,
) -> list[object]:
    if not swarms:
        return []

    renderables: list[object] = [f"[bold]{title}[/bold]"]
    for challenge in sorted(swarms):
        swarm = _object_dict(swarms[challenge])
        agents = _agent_dict(swarm.get("agents", {}))
        if not agents:
            continue
        specs = _problem_specs(agents, verbose=verbose)
        if not specs:
            continue
        counts = _summarize_swarm_agents(agents)
        challenge_title = (
            f"{challenge}  "
            f"(steps={_swarm_step_count(agents)}, "
            f"busy={counts['busy']}, idle={counts['idle']}, quota={counts['quota']})"
        )
        table = Table(title=challenge_title, box=box.SIMPLE, expand=True)
        table.add_column("Lane", no_wrap=True, width=14)
        table.add_column("Step", justify="right", width=4)
        table.add_column("State", no_wrap=True, width=8)
        table.add_column("What", overflow="fold")
        for spec in specs:
            agent = agents[spec]
            table.add_row(
                _short_model_name(spec),
                str(agent.get("step_count", 0)),
                _clean_status_text(agent.get("lifecycle") or agent.get("status") or "?", limit=12),
                _clean_status_text(_format_agent_detail(agent), limit=96),
            )
        renderables.append(table)
    return renderables


def _build_flags_table(results: dict[str, object]) -> Table | None:
    rows: list[tuple[str, str]] = []
    for challenge in sorted(results):
        result = _object_dict(results[challenge])
        flag = _clean_status_text(result.get("flag") or "", limit=80)
        if flag:
            rows.append((challenge, flag))

    table = Table(title="Flags", box=box.ASCII2, expand=True)
    table.add_column("Challenge", no_wrap=True)
    table.add_column("Flag", overflow="fold")
    if not rows:
        table.add_row("(none yet)", "-")
    else:
        for row in rows:
            table.add_row(*row)
    return table


def _build_latest_advisory_table(
    active: dict[str, object],
    pending: dict[str, object],
    finished: dict[str, object],
) -> Table | None:
    rows: list[tuple[str, str, str]] = []
    for swarms in (active, pending, finished):
        for challenge in sorted(swarms):
            swarm = _object_dict(swarms[challenge])
            agents = _agent_dict(swarm.get("agents", {}))
            if not agents:
                continue
            for spec in sorted(agents):
                note = _agent_advisor_note(agents[spec], limit=100)
                if note != "-":
                    rows.append((challenge, spec, note))

    table = Table(title="Latest Advisory", box=box.ASCII2, expand=True)
    table.add_column("Challenge", no_wrap=True)
    table.add_column("Lane", no_wrap=True)
    table.add_column("Advisory", overflow="fold")
    if not rows:
        table.add_row("(none yet)", "-", "-")
    else:
        for row in rows:
            table.add_row(*row)
    return table


def _build_latest_shared_finding_table(
    active: dict[str, object],
    pending: dict[str, object],
    finished: dict[str, object],
) -> Table | None:
    rows: list[tuple[str, str, str]] = []
    for swarms in (active, pending, finished):
        for challenge in sorted(swarms):
            swarm = _object_dict(swarms[challenge])
            for spec, payload in _shared_finding_entries(swarm):
                finding = _render_shared_finding_payload(payload, limit=100, include_paths=True)
                if finding != "-":
                    rows.append((challenge, spec, finding))

    table = Table(title="Latest Shared Finding", box=box.ASCII2, expand=True)
    table.add_column("Challenge", no_wrap=True)
    table.add_column("Lane", no_wrap=True)
    table.add_column("Finding", overflow="fold")
    if not rows:
        table.add_row("(none yet)", "-", "-")
    else:
        for row in rows:
            table.add_row(*row)
    return table


def _build_signals_table(
    active: dict[str, object],
    pending: dict[str, object],
    finished: dict[str, object],
) -> Table | None:
    rows: list[tuple[str, dict[str, object]]] = []
    for swarms in (active, pending, finished):
        for challenge in sorted(swarms):
            swarm = _object_dict(swarms[challenge])
            signals = swarm.get("signals")
            if isinstance(signals, dict):
                rows.append((challenge, _object_dict(signals)))

    table = Table(title="Signals", box=box.ASCII2, expand=True)
    table.add_column("Challenge", no_wrap=True)
    table.add_column("Posts", justify="right", width=5)
    table.add_column("Reads", justify="right", width=5)
    table.add_column("Delivered", justify="right", width=9)
    table.add_column("CoordMsg", justify="right", width=8)
    table.add_column("LaneAdv", justify="right", width=7)
    table.add_column("AdvMsg", justify="right", width=6)
    if not rows:
        table.add_row("(none yet)", "0", "0", "0", "0", "0", "0")
    else:
        for challenge, signals in rows:
            table.add_row(
                challenge,
                str(_int_from_object(signals.get("total_posts", 0))),
                str(_int_from_object(signals.get("total_checks", 0))),
                str(_int_from_object(signals.get("total_delivered", 0))),
                str(_int_from_object(signals.get("coordinator_messages", 0))),
                str(_int_from_object(signals.get("advisor_lane_hints", signals.get("advisor_finding_posts", 0)))),
                str(_int_from_object(signals.get("advisor_coordinator_appends", 0))),
            )
    return table


def _print_status_snapshot(
    data: dict | None,
    *,
    fetch_error: str = "",
    updated_at: float | None = None,
    clear: bool = False,
    verbose: bool = False,
) -> None:
    compact = console.is_terminal and console.width < 170
    if clear and console.is_terminal:
        console.clear()
    if not data:
        console.print(
            "\n".join(
                _render_status_lines(
                    data,
                    fetch_error=fetch_error,
                    updated_at=updated_at,
                    verbose=verbose,
                )
            )
        )
        return

    title = "[bold]Coordinator Status[/bold]"
    if updated_at is not None:
        title += f" [dim](updated {time.strftime('%H:%M:%S', time.localtime(updated_at))})[/dim]"
    console.print(title)
    if fetch_error:
        console.print(f"[red]Status fetch failed:[/red] {fetch_error}")
    console.print(_format_models_line(list(data.get("models", [])), compact=compact))
    console.print(
        "Challenges: "
        f"{data.get('known_challenge_count', 0)}"
        f" | Solved: {data.get('known_solved_count', 0)}"
        " | Active: "
        f"{data.get('active_swarm_count', 0)}"
        f" | Limit: {data.get('max_concurrent_challenges', 0)}"
        f" | Pending: {data.get('pending_challenge_count', 0)}"
        f" | Finished: {data.get('finished_swarm_count', 0)}"
        f" | Steps: {data.get('total_step_count', 0)}"
        f" | Cost: ${data.get('cost_usd', 0):.2f}"
        f" | Tokens: {data.get('total_tokens', 0)}"
    )
    console.print(
        f"Queues: coordinator={data.get('coordinator_queue_depth', 0)}, "
        f"operator={data.get('operator_queue_depth', 0)}"
    )

    renderables: list[object | None] = [
        _build_summary_table("Active Challenges", data.get("active_swarms", {})),
    ]
    if compact:
        renderables.extend(_build_compact_lane_renderables("Active Lanes", data.get("active_swarms", {}), verbose=verbose))
    else:
        renderables.append(_build_lane_table("Active Lanes", data.get("active_swarms", {}), verbose=verbose))
    renderables.append(_build_summary_table("Pending Challenges", data.get("pending_swarms", {})))
    renderables.append(_build_summary_table("Finished Challenges", data.get("finished_swarms", {})))
    if compact:
        renderables.extend(_build_compact_lane_renderables("Finished Lanes", data.get("finished_swarms", {}), verbose=verbose))
    else:
        renderables.append(_build_lane_table("Finished Lanes", data.get("finished_swarms", {}), verbose=verbose))
    renderables.append(
        _build_latest_advisory_table(
            data.get("active_swarms", {}),
            data.get("pending_swarms", {}),
            data.get("finished_swarms", {}),
        )
    )
    renderables.append(
        _build_latest_shared_finding_table(
            data.get("active_swarms", {}),
            data.get("pending_swarms", {}),
            data.get("finished_swarms", {}),
        )
    )
    renderables.append(
        _build_signals_table(
            data.get("active_swarms", {}),
            data.get("pending_swarms", {}),
            data.get("finished_swarms", {}),
        )
    )
    renderables.append(_build_flags_table(data.get("results", {})))
    for renderable in renderables:
        if renderable is not None:
            console.print()
            console.print(renderable)


def _fetch_status_data(host: str, port: int) -> dict:
    import json
    import urllib.request

    req = urllib.request.Request(
        f"http://{host}:{port}/api/runtime/snapshot",
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _post_operator_json(host: str, port: int, path: str, payload: dict[str, object]) -> dict:
    import json
    import urllib.request

    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://{host}:{port}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _validate_runtime_auth(
    settings: Settings,
    model_specs: list[str],
    coordinator_backend: str,
) -> None:
    """Validate home-directory auth sources needed for this run."""
    needs_codex = True
    needs_claude = False
    needs_gemini = False

    if coordinator_backend != "codex":
        logging.getLogger(__name__).warning(
            "Ignoring unsupported coordinator backend %s; Codex coordinator is required.",
            coordinator_backend,
        )

    for spec in model_specs:
        provider = provider_from_spec(spec)
        if provider == "codex":
            needs_codex = True
        elif provider == "claude-sdk":
            raise ValueError(
                f"Claude solver lanes are disabled for {spec}. "
                "Use Claude as coordinator/advisor only."
            )
        elif provider in ("gemini", "google"):
            needs_gemini = True

    if not settings.use_home_auth:
        return

    validated = validate_required_auth(
        settings,
        needs_codex=needs_codex,
        needs_claude=needs_claude,
        needs_gemini=needs_gemini,
    )

    if validated.get("codex"):
        logging.getLogger(__name__).info("Codex home auth validated")
    if validated.get("claude"):
        logging.getLogger(__name__).info("Claude home auth validated")
    if validated.get("gemini"):
        logging.getLogger(__name__).info("Gemini home auth validated")
    if settings.use_home_auth:
        try:
            validate_claude_auth(settings)
        except AuthValidationError as exc:
            logging.getLogger(__name__).warning(
                "Claude advisor auth unavailable; advisory fallback will stay on Codex: %s",
                exc,
            )
        else:
            logging.getLogger(__name__).info("Claude home auth validated")


@click.command()
@click.option("--ctfd-url", default=None, help="CTFd URL (overrides .env)")
@click.option("--ctfd-token", default=None, help="CTFd API token (overrides .env)")
@click.option("--image", default="ctf-sandbox", help="Docker sandbox image name")
@click.option("--models", multiple=True, help="Model specs (default: all configured)")
@click.option("--challenges-dir", default="challenges", help="Directory for challenge files")
@click.option("--no-submit", is_flag=True, help="Disable flag submission while still using CTFd challenge sync")
@click.option(
    "--local",
    "local_mode",
    is_flag=True,
    help="Run from local challenge dirs only; skip all CTFd fetch/submit operations",
)
@click.option("--coordinator-model", default=None, help="Model for coordinator (default: backend-specific)")
@click.option("--coordinator", default="codex", type=click.Choice(["codex"]), help="Coordinator backend")
@click.option("--max-challenges", default=10, type=int, help="Max challenges solved concurrently")
@click.option(
    "--resume",
    "resume_mode",
    is_flag=True,
    help="Resume paused/requeueable challenge work from saved runtime state instead of clearing it first",
)
@click.option("--msg-port", default=9400, type=int, help="Operator message port (use 0 for auto)")
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging")
def main(
    ctfd_url: str | None,
    ctfd_token: str | None,
    image: str,
    models: tuple[str, ...],
    challenges_dir: str,
    no_submit: bool,
    local_mode: bool,
    coordinator_model: str | None,
    coordinator: str,
    max_challenges: int,
    resume_mode: bool,
    msg_port: int,
    verbose: bool,
) -> None:
    """CTF Agent — multi-model coordinator over challenge directories."""
    run_log_path = _setup_logging(verbose)

    settings = Settings(sandbox_image=image)
    if ctfd_url:
        settings.ctfd_url = ctfd_url
    if ctfd_token:
        settings.ctfd_token = ctfd_token
    settings.max_concurrent_challenges = max_challenges
    effective_no_submit = no_submit or local_mode

    model_specs = list(models) if models else list(DEFAULT_MODELS)
    try:
        _validate_runtime_auth(settings, model_specs, coordinator)
    except AuthValidationError as exc:
        console.print(f"[red]Auth validation failed:[/red] {exc}")
        sys.exit(1)
    except ValueError as exc:
        console.print(f"[red]Model validation failed:[/red] {exc}")
        sys.exit(1)

    console.print("[bold]CTF Agent v2[/bold]")
    console.print(f"  Mode: {'local' if local_mode else 'ctfd'}")
    console.print(f"  CTFd: {'disabled' if local_mode else settings.ctfd_url}")
    if local_mode:
        console.print("  Submission: operator approval only")
    elif no_submit:
        console.print("  Submission: disabled (--no-submit)")
    else:
        console.print("  Submission: enabled")
    console.print(f"  Models: {', '.join(model_specs)}")
    console.print(f"  Challenges dir: {Path(challenges_dir).resolve()}")
    console.print(f"  Image: {settings.sandbox_image}")
    console.print(f"  Max challenges: {max_challenges}")
    console.print(f"  Startup: {'resume previous work' if resume_mode else 'fresh runtime reset'}")
    if run_log_path is not None:
        console.print(f"  Run log: {run_log_path}")
    memory_budget = _memory_budget_summary(
        settings.container_memory_limit,
        lane_count=len(model_specs),
        challenge_count=max_challenges,
    )
    console.print(
        f"  Lane memory: {memory_budget['per_lane_display']}"
        f" | 1 challenge worst-case: {memory_budget['one_challenge_display']}"
        f" | max configured worst-case: {memory_budget['max_total_display']}"
    )
    if memory_budget["host_memory_display"] != "unknown":
        console.print(f"  Host RAM: {memory_budget['host_memory_display']}")
    if memory_budget["warn_single"]:
        console.print(
            "[yellow]Warning:[/yellow] one challenge can reserve more memory than the host. "
            "Lower CONTAINER_MEMORY_LIMIT or reduce the active model set."
        )
    elif memory_budget["warn_total"]:
        console.print(
            "[yellow]Warning:[/yellow] max configured concurrency can overcommit host RAM. "
            "Lower --max-challenges, reduce models, or lower CONTAINER_MEMORY_LIMIT."
        )
    console.print()

    try:
        asyncio.run(
            _run_coordinator(
                settings,
                model_specs,
                challenges_dir,
                effective_no_submit,
                local_mode,
                coordinator_model,
                coordinator,
                max_challenges,
                resume_mode,
                msg_port,
            )
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted while shutting down coordinator.[/yellow]")


async def _run_coordinator(
    settings: Settings,
    model_specs: list[str],
    challenges_dir: str,
    no_submit: bool,
    local_mode: bool,
    coordinator_model: str | None,
    coordinator_backend: str,
    max_challenges: int,
    resume_mode: bool = False,
    msg_port: int = 0,
) -> None:
    """Run the full coordinator (continuous until Ctrl+C)."""
    from backend.sandbox import cleanup_orphan_containers, configure_semaphore

    max_containers = max_challenges * len(model_specs)
    configure_semaphore(max_containers)
    if resume_mode:
        logging.getLogger(__name__).info(
            "Resume mode enabled; preserving runtime state and warm sandboxes under %s",
            Path(challenges_dir).resolve(),
        )
    else:
        await cleanup_orphan_containers()
        reset_summary = _reset_runtime_state_dirs(_discover_challenge_dirs(challenges_dir))
        if reset_summary.touched:
            logging.getLogger(__name__).info(
                "Cleared runtime state under %s (lane-state=%d, shared-artifacts=%d, solve-lanes=%d, traces=%d)",
                Path(challenges_dir).resolve(),
                reset_summary.lane_state_dirs,
                reset_summary.shared_artifact_dirs,
                reset_summary.solve_lane_dirs,
                reset_summary.trace_files,
            )
    resolved_backend = "codex"
    console.print(f"[bold]Starting coordinator ({resolved_backend}, Ctrl+C to stop)...[/bold]\n")
    ctfd, cost_tracker, deps = build_deps(
        settings,
        model_specs,
        challenges_dir,
        no_submit,
        local_mode,
    )
    if resume_mode:
        restored = restore_pending_swarms_from_results(deps)
        if restored:
            held = sum(
                1
                for challenge_name in restored
                if str(deps.pending_swarm_meta.get(challenge_name, {}).get("reason") or "")
                == PENDING_REASON_PRIORITY_WAITING
            )
            logging.getLogger(__name__).info(
                "Restored %d queued challenge(s) from saved runtime state (%d held in priority waiting)",
                len(restored),
                held,
            )
        else:
            logging.getLogger(__name__).info("Resume mode found no queued challenge state to restore")
    results: dict[str, object] = {}
    installed_signals = _install_shutdown_signal_handlers(deps)

    try:
        results = await run_codex_coordinator(
            settings=settings,
            model_specs=model_specs,
            challenges_root=challenges_dir,
            no_submit=no_submit,
            local_mode=local_mode,
            coordinator_model=coordinator_model,
            msg_port=msg_port,
            ctfd=ctfd,
            cost_tracker=cost_tracker,
            deps=deps,
            cleanup_runtime_on_exit=False,
        )
    finally:
        _remove_shutdown_signal_handlers(installed_signals)
        await cleanup_coordinator_runtime(
            deps,
            ctfd,
            cost_tracker,
            reason=str(results.get("shutdown_reason") or getattr(deps, "shutdown_reason", "") or ""),
        )

    console.print("\n[bold]Final Results:[/bold]")
    for challenge, data in results.get("results", {}).items():
        console.print(f"  {challenge}: {data.get('flag', 'no flag')}")
    shutdown_reason = str(results.get("shutdown_reason") or getattr(deps, "shutdown_reason", "") or "").strip()
    if shutdown_reason:
        console.print(f"\n[bold]Shutdown reason:[/bold] {shutdown_reason}")
    console.print(f"\n[bold]Total cost: ${results.get('total_cost_usd', 0):.2f}[/bold]")


@click.command()
@click.argument("message")
@click.option("--port", default=9400, type=int, help="Coordinator message port")
@click.option("--host", default="127.0.0.1", help="Coordinator host")
def msg(message: str, port: int, host: str) -> None:
    """Send a message to the running coordinator."""
    try:
        data = _post_operator_json(host, port, "/api/runtime/coordinator-message", {"message": message})
        console.print(f"[green]Sent:[/green] {data.get('queued', message[:200])}")
    except Exception as e:
        console.print(f"[red]Failed:[/red] {e}")
        console.print("Is the coordinator running?")
        sys.exit(1)


@click.command()
@click.option("--challenge", "challenge_name", required=True, help="Challenge name")
@click.option("--model", "model_spec", required=True, help="Lane model spec")
@click.option("--port", default=9400, type=int, help="Coordinator message port")
@click.option("--host", default="127.0.0.1", help="Coordinator host")
@click.argument("insights")
def bump(challenge_name: str, model_spec: str, port: int, host: str, insights: str) -> None:
    """Send targeted guidance directly to a running lane."""
    try:
        data = _post_operator_json(
            host,
            port,
            "/api/runtime/lane-bump",
            {
                "challenge_name": challenge_name,
                "lane_id": model_spec,
                "insights": insights,
            },
        )
        console.print(f"[green]Bumped:[/green] {data.get('result', '')}")
    except Exception as e:
        console.print(f"[red]Failed:[/red] {e}")
        console.print("Is the coordinator running, and does that lane exist?")
        sys.exit(1)


@click.command()
@click.option("--port", default=9400, type=int, help="Coordinator status port")
@click.option("--host", default="127.0.0.1", help="Coordinator host")
@click.option("--once", is_flag=True, help="Print one snapshot and exit")
@click.option("--text", "text_view", is_flag=True, help="Use the legacy terminal dashboard")
@click.option("--json-output", is_flag=True, help="Print raw JSON")
@click.option("--verbose", "verbose_view", is_flag=True, help="Show every lane, not just busy/error lanes")
def status(port: int, host: str, once: bool, text_view: bool, json_output: bool, verbose_view: bool) -> None:
    """Read status from the running coordinator."""
    import json

    if json_output:
        try:
            data = _fetch_status_data(host, port)
        except Exception as e:
            console.print(f"[red]Failed:[/red] {e}")
            console.print("Is the coordinator running?")
            sys.exit(1)
        console.print_json(json.dumps(data))
        return

    if once:
        try:
            data = _fetch_status_data(host, port)
        except Exception as e:
            console.print(f"[red]Failed:[/red] {e}")
            console.print("Is the coordinator running?")
            sys.exit(1)
        _print_status_snapshot(data, updated_at=time.time(), verbose=verbose_view)
        return

    if not text_view:
        try:
            _fetch_status_data(host, port)
        except Exception as e:
            console.print(f"[red]Failed:[/red] {e}")
            console.print("Is the coordinator running?")
            sys.exit(1)
        url = f"http://{host}:{port}/ui"
        opened = webbrowser.open(url)
        if opened:
            console.print(f"[green]Opened:[/green] {url}")
        else:
            console.print(url)
        return

    last_data: dict | None = None
    last_error = ""
    last_updated: float | None = None
    try:
        while True:
            try:
                last_data = _fetch_status_data(host, port)
                last_error = ""
                last_updated = time.time()
            except Exception as e:
                last_error = str(e)
            _print_status_snapshot(
                last_data,
                fetch_error=last_error,
                updated_at=last_updated,
                clear=True,
                verbose=verbose_view,
            )
            time.sleep(2)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
