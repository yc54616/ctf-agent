"""Click CLI entry point."""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
import webbrowser
from collections import Counter
from pathlib import Path

import click
from rich import box
from rich.console import Console
from rich.table import Table

from backend.agents.codex_coordinator import run_codex_coordinator
from backend.agents.coordinator_loop import build_deps, cleanup_coordinator_runtime
from backend.auth import AuthValidationError, validate_required_auth
from backend.config import Settings
from backend.models import DEFAULT_MODELS, provider_from_spec

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


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("aiodocker").setLevel(logging.WARNING)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)-8s %(message)s", datefmt="%X"))
    logging.basicConfig(level=level, handlers=[handler], force=True)


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
    digest_path = _clean_status_text(payload.get("digest_path") or "", limit=120)
    pointer_path = _clean_status_text(payload.get("pointer_path") or "", limit=120)
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
    current_tool = str(agent.get("current_tool") or "")
    last_tool = str(agent.get("last_tool") or "")
    current_command = _clean_status_text(_preview_line(agent.get("current_command", ""), limit=140))
    last_command = _clean_status_text(_preview_line(agent.get("last_command", ""), limit=140))
    exit_hint = _clean_status_text(_preview_line(agent.get("last_exit_hint", ""), limit=80))
    findings = _clean_status_text(_preview_line(agent.get("findings", ""), limit=100))

    parts = [lifecycle]
    if current_command:
        label = current_tool or "tool"
        parts.append(f"now/{label}: {current_command}")
    elif last_command:
        label = last_tool or "tool"
        parts.append(f"last/{label}: {last_command}")

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
        step_count = _swarm_step_count(agents)
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
        "Finished Challenges",
        data.get("finished_swarms", {}),
        verbose=verbose,
    )

    advisor_rows: list[tuple[str, str, str]] = []
    for swarms in (_object_dict(data.get("active_swarms", {})), _object_dict(data.get("finished_swarms", {}))):
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
    for swarms in (_object_dict(data.get("active_swarms", {})), _object_dict(data.get("finished_swarms", {}))):
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
    for swarms in (_object_dict(data.get("active_swarms", {})), _object_dict(data.get("finished_swarms", {}))):
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
        step_count = _swarm_step_count(agents)
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


def _build_latest_advisory_table(active: dict[str, object], finished: dict[str, object]) -> Table | None:
    rows: list[tuple[str, str, str]] = []
    for swarms in (active, finished):
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


def _build_latest_shared_finding_table(active: dict[str, object], finished: dict[str, object]) -> Table | None:
    rows: list[tuple[str, str, str]] = []
    for swarms in (active, finished):
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


def _build_signals_table(active: dict[str, object], finished: dict[str, object]) -> Table | None:
    rows: list[tuple[str, dict[str, object]]] = []
    for swarms in (active, finished):
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
    renderables.append(_build_summary_table("Finished Challenges", data.get("finished_swarms", {})))
    if compact:
        renderables.extend(_build_compact_lane_renderables("Finished Lanes", data.get("finished_swarms", {}), verbose=verbose))
    else:
        renderables.append(_build_lane_table("Finished Lanes", data.get("finished_swarms", {}), verbose=verbose))
    renderables.append(
        _build_latest_advisory_table(
            data.get("active_swarms", {}),
            data.get("finished_swarms", {}),
        )
    )
    renderables.append(
        _build_latest_shared_finding_table(
            data.get("active_swarms", {}),
            data.get("finished_swarms", {}),
        )
    )
    renderables.append(
        _build_signals_table(
            data.get("active_swarms", {}),
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
        f"http://{host}:{port}/status",
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
    needs_codex = coordinator_backend == "codex"
    needs_claude = coordinator_backend == "claude"
    needs_gemini = False

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


@click.command()
@click.option("--ctfd-url", default=None, help="CTFd URL (overrides .env)")
@click.option("--ctfd-token", default=None, help="CTFd API token (overrides .env)")
@click.option("--image", default="ctf-sandbox", help="Docker sandbox image name")
@click.option("--models", multiple=True, help="Model specs (default: all configured)")
@click.option("--challenge", default=None, help="Solve a single challenge directory")
@click.option("--challenges-dir", default="challenges", help="Directory for challenge files")
@click.option("--no-submit", is_flag=True, help="Dry run — don't submit flags")
@click.option("--coordinator-model", default=None, help="Model for coordinator (default: backend-specific)")
@click.option("--coordinator", default="claude", type=click.Choice(["claude", "codex"]), help="Coordinator backend")
@click.option("--max-challenges", default=10, type=int, help="Max challenges solved concurrently")
@click.option("--msg-port", default=9400, type=int, help="Operator message port (use 0 for auto)")
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging")
def main(
    ctfd_url: str | None,
    ctfd_token: str | None,
    image: str,
    models: tuple[str, ...],
    challenge: str | None,
    challenges_dir: str,
    no_submit: bool,
    coordinator_model: str | None,
    coordinator: str,
    max_challenges: int,
    msg_port: int,
    verbose: bool,
) -> None:
    """CTF Agent — multi-model solver swarm.

    Run without --challenge to start the full coordinator (Ctrl+C to stop).
    """
    _setup_logging(verbose)

    settings = Settings(sandbox_image=image)
    if ctfd_url:
        settings.ctfd_url = ctfd_url
    if ctfd_token:
        settings.ctfd_token = ctfd_token
    settings.max_concurrent_challenges = max_challenges

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
    console.print(f"  CTFd: {settings.ctfd_url}")
    console.print(f"  Models: {', '.join(model_specs)}")
    console.print(f"  Image: {settings.sandbox_image}")
    console.print(f"  Max challenges: {max_challenges}")
    console.print()

    if challenge:
        asyncio.run(_run_single(settings, challenge, model_specs, no_submit, max_challenges))
    else:
        asyncio.run(_run_coordinator(settings, model_specs, challenges_dir, no_submit, coordinator_model, coordinator, max_challenges, msg_port))


async def _run_single(
    settings: Settings,
    challenge_dir: str,
    model_specs: list[str],
    no_submit: bool,
    max_challenges: int,
) -> None:
    """Run a single challenge with a swarm."""
    from backend.agents.swarm import ChallengeSwarm
    from backend.cost_tracker import CostTracker
    from backend.ctfd import CTFdClient
    from backend.prompts import ChallengeMeta
    from backend.sandbox import cleanup_orphan_containers, configure_semaphore

    max_containers = max_challenges * len(model_specs)
    configure_semaphore(max_containers)
    await cleanup_orphan_containers()

    challenge_path = Path(challenge_dir)
    meta_path = challenge_path / "metadata.yml"
    if not meta_path.exists():
        console.print(f"[red]No metadata.yml found in {challenge_dir}[/red]")
        sys.exit(1)

    meta = ChallengeMeta.from_yaml(meta_path)
    console.print(f"[bold]Challenge:[/bold] {meta.name} ({meta.category}, {meta.value} pts)")

    ctfd = CTFdClient(
        base_url=settings.ctfd_url,
        token=settings.ctfd_token,
        username=settings.ctfd_user,
        password=settings.ctfd_pass,
    )
    cost_tracker = CostTracker()

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_path),
        meta=meta,
        ctfd=ctfd,
        cost_tracker=cost_tracker,
        settings=settings,
        model_specs=model_specs,
        no_submit=no_submit,
    )

    try:
        result = await swarm.run()
        from backend.solver_base import FLAG_FOUND
        if result and result.status == FLAG_FOUND:
            console.print(f"\n[bold green]FLAG FOUND:[/bold green] {result.flag}")
        else:
            console.print("\n[bold red]No flag found.[/bold red]")

        console.print("\n[bold]Cost Summary:[/bold]")
        for agent_name in cost_tracker.by_agent:
            console.print(f"  {agent_name}: {cost_tracker.format_usage(agent_name)}")
        console.print(f"  [bold]Total: ${cost_tracker.total_cost_usd:.2f}[/bold]")
    finally:
        await ctfd.close()


async def _run_coordinator(
    settings: Settings,
    model_specs: list[str],
    challenges_dir: str,
    no_submit: bool,
    coordinator_model: str | None,
    coordinator_backend: str,
    max_challenges: int,
    msg_port: int = 0,
) -> None:
    """Run the full coordinator (continuous until Ctrl+C)."""
    from backend.sandbox import cleanup_orphan_containers, configure_semaphore

    max_containers = max_challenges * len(model_specs)
    configure_semaphore(max_containers)
    await cleanup_orphan_containers()
    console.print(f"[bold]Starting coordinator ({coordinator_backend}, Ctrl+C to stop)...[/bold]\n")
    ctfd, cost_tracker, deps = build_deps(
        settings,
        model_specs,
        challenges_dir,
        no_submit,
    )
    results: dict[str, object]

    try:
        if coordinator_backend == "codex":
            results = await run_codex_coordinator(
                settings=settings,
                model_specs=model_specs,
                challenges_root=challenges_dir,
                no_submit=no_submit,
                coordinator_model=coordinator_model,
                msg_port=msg_port,
                ctfd=ctfd,
                cost_tracker=cost_tracker,
                deps=deps,
                cleanup_runtime_on_exit=False,
            )
        else:
            from backend.agents.claude_coordinator import (
                ClaudeCoordinatorInactiveError,
                run_claude_coordinator,
            )
            results = await run_claude_coordinator(
                settings=settings,
                model_specs=model_specs,
                challenges_root=challenges_dir,
                no_submit=no_submit,
                coordinator_model=coordinator_model,
                msg_port=msg_port,
                ctfd=ctfd,
                cost_tracker=cost_tracker,
                deps=deps,
                cleanup_runtime_on_exit=False,
            )
    except Exception as exc:
        if coordinator_backend != "claude":
            raise
        from backend.agents.claude_coordinator import ClaudeCoordinatorInactiveError

        reason = "inactive" if isinstance(exc, ClaudeCoordinatorInactiveError) else "unavailable"
        console.print(
            f"[yellow]Claude coordinator {reason} ({exc}). "
            "Falling back to Codex coordinator without resetting active swarms.[/yellow]"
        )
        results = await run_codex_coordinator(
            settings=settings,
            model_specs=model_specs,
            challenges_root=challenges_dir,
            no_submit=no_submit,
            coordinator_model=None,
            msg_port=msg_port,
            ctfd=ctfd,
            cost_tracker=cost_tracker,
            deps=deps,
            cleanup_runtime_on_exit=False,
        )
    finally:
        await cleanup_coordinator_runtime(deps, ctfd, cost_tracker)

    console.print("\n[bold]Final Results:[/bold]")
    for challenge, data in results.get("results", {}).items():
        console.print(f"  {challenge}: {data.get('flag', 'no flag')}")
    console.print(f"\n[bold]Total cost: ${results.get('total_cost_usd', 0):.2f}[/bold]")


@click.command()
@click.argument("message")
@click.option("--port", default=9400, type=int, help="Coordinator message port")
@click.option("--host", default="127.0.0.1", help="Coordinator host")
def msg(message: str, port: int, host: str) -> None:
    """Send a message to the running coordinator."""
    try:
        data = _post_operator_json(host, port, "/msg", {"message": message})
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
            "/bump",
            {
                "challenge_name": challenge_name,
                "model_spec": model_spec,
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
