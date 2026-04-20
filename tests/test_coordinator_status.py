from __future__ import annotations

from typing import Any, cast

from click.testing import CliRunner

import backend.cli as cli_module
from backend.agents.coordinator_loop import _status_snapshot
from backend.cli import (
    _build_compact_lane_renderables,
    _format_agent_activity,
    _format_models_line,
    _preview_line,
    _render_status_lines,
    status,
)
from backend.cost_tracker import CostTracker
from backend.deps import CoordinatorDeps


class _FakeSwarm:
    def __init__(self, status: dict) -> None:
        self._status = status

    def get_status(self) -> dict:
        return self._status


class _FakeTask:
    def __init__(self, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


def test_status_snapshot_reports_active_and_finished_swarms() -> None:
    deps = CoordinatorDeps(
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=object(),
        model_specs=["gemini/gemini-2.5-flash", "codex/gpt-5.4"],
        max_concurrent_challenges=3,
    )
    deps.session_started_at = 1_700_000_000.0
    deps.cost_tracker.record_tokens(
        "challenge-a/gpt-5.4",
        "gpt-5.4",
        input_tokens=100,
        output_tokens=50,
        provider_spec="codex",
    )
    deps.swarms["challenge-a"] = _FakeSwarm(
        {
            "challenge": "challenge-a",
            "coordinator_advisor_note": "",
            "shared_finding": "",
            "shared_findings": {},
            "signals": {
                "total_posts": 0,
                "total_checks": 0,
                "total_delivered": 0,
                "coordinator_messages": 0,
                "advisor_lane_hints": 0,
                "advisor_coordinator_appends": 0,
            },
            "agents": {},
        }
    )
    deps.swarms["challenge-b"] = _FakeSwarm(
        {
            "challenge": "challenge-b",
            "winner": "flag{done}",
            "advisor_note": "Check whether the login bypass requires CSRF.",
            "coordinator_advisor_note": "Check whether the login bypass requires CSRF.",
            "shared_finding": "Potential admin API at /api/v1/k8s/get",
            "shared_findings": {
                "codex/gpt-5.4": {
                    "kind": "finding_ref",
                    "summary": "Potential admin API at /api/v1/k8s/get",
                    "pointer_path": "/challenge/shared-artifacts/finding.txt",
                    "digest_path": "/challenge/shared-artifacts/.advisor/finding.digest.md",
                }
            },
            "signals": {
                "total_posts": 3,
                "total_checks": 5,
                "total_delivered": 4,
                "coordinator_messages": 1,
                "advisor_lane_hints": 1,
                "advisor_coordinator_appends": 1,
            },
            "agents": {
                "codex/gpt-5.4": {
                    "status": "flag_found",
                    "lifecycle": "won",
                    "step_count": 7,
                    "findings": "found flag via login bypass",
                    "advisor_note": "Check whether the login bypass requires CSRF.",
                    "last_tool": "bash",
                    "last_command": "grep -R flag /challenge/distfiles",
                    "last_exit_hint": "won",
                }
            },
        }
    )

    deps.swarm_tasks["challenge-a"] = cast(Any, _FakeTask(False))
    deps.swarm_tasks["challenge-b"] = cast(Any, _FakeTask(True))
    deps.pending_swarm_queue.append("challenge-c")
    deps.pending_swarm_set.add("challenge-c")
    deps.known_challenge_count = 10
    deps.known_solved_count = 6
    deps.results["challenge-b"] = {
        "flag": "flag{done}",
        "status": "flag_found",
        "step_count": 7,
        "advisor_note": "Check whether the login bypass requires CSRF.",
        "findings_summary": "found flag via login bypass",
    }
    deps.results["challenge-d"] = {
        "flag": "flag{restored}",
        "status": "flag_found",
        "step_count": 11,
        "findings_summary": "restored solved result",
    }

    snapshot = _status_snapshot(deps)

    assert snapshot["models"] == ["gemini/gemini-2.5-flash", "codex/gpt-5.4"]
    assert snapshot["session_started_at"] == 1_700_000_000.0
    assert snapshot["active_swarm_count"] == 1
    assert snapshot["finished_swarm_count"] == 1
    assert snapshot["pending_challenge_count"] == 1
    assert snapshot["known_challenge_count"] == 10
    assert snapshot["known_solved_count"] == 6
    assert snapshot["pending_challenges"] == ["challenge-c"]
    assert "challenge-a" in snapshot["active_swarms"]
    assert snapshot["finished_swarms"]["challenge-b"]["winner"] == "flag{done}"
    assert (
        snapshot["finished_swarms"]["challenge-b"]["coordinator_advisor_note"]
        == "Check whether the login bypass requires CSRF."
    )
    assert snapshot["finished_swarms"]["challenge-b"]["shared_finding"] == "Potential admin API at /api/v1/k8s/get"
    assert (
        snapshot["finished_swarms"]["challenge-b"]["shared_findings"]["codex/gpt-5.4"]["digest_path"]
        == "/challenge/shared-artifacts/.advisor/finding.digest.md"
    )
    assert snapshot["finished_swarms"]["challenge-b"]["signals"]["total_posts"] == 3
    assert snapshot["finished_swarms"]["challenge-b"]["agents"]["codex/gpt-5.4"]["lifecycle"] == "won"
    assert snapshot["finished_swarms"]["challenge-b"]["agents"]["codex/gpt-5.4"]["findings"] == "found flag via login bypass"
    assert snapshot["results"]["challenge-b"]["flag"] == "flag{done}"
    assert snapshot["results"]["challenge-b"]["findings_summary"] == "found flag via login bypass"
    assert snapshot["total_tokens"] == 150
    assert snapshot["total_step_count"] == 18


def test_preview_line_handles_empty_and_multiline_text() -> None:
    assert _preview_line("") == ""
    assert _preview_line(None) == ""
    assert _preview_line("\n") == ""
    assert _preview_line("line one\nline two") == "line one"


def test_format_models_line_compact_shortens_preview() -> None:
    line = _format_models_line(
        [
            "gemini/gemini-2.5-flash",
            "gemini/gemini-2.5-flash-lite",
            "gemini/gemini-2.5-pro",
            "codex/gpt-5.4",
            "codex/gpt-5.4-mini",
        ],
        compact=True,
    )

    assert line == "Models: 5 lanes (g-flash, g-flash-lite, g-pro, 5.4, +1 more)"


def test_format_agent_activity_prefers_current_command() -> None:
    formatted = _format_agent_activity(
        {
            "lifecycle": "busy",
            "current_tool": "bash",
            "current_command": "strings /challenge/distfiles/blob.bin | head -200",
            "last_command": "file /challenge/distfiles/blob.bin",
            "findings": "possible ZIP header in output",
        }
    )

    assert formatted.startswith("busy | now/bash: strings /challenge/distfiles/blob.bin | head -200")
    assert "finding: possible ZIP header in output" in formatted


def test_format_agent_activity_uses_last_command_for_finished_lane() -> None:
    formatted = _format_agent_activity(
        {
            "lifecycle": "quota_error",
            "last_tool": "bash",
            "last_command": "ffuf -u http://host/FUZZ -w /wordlists/common.txt",
            "last_exit_hint": "quota_error",
        }
    )

    assert formatted == "quota_error | last/bash: ffuf -u http://host/FUZZ -w /wordlists/common.txt"


def test_format_agent_activity_shows_thinking_commentary() -> None:
    formatted = _format_agent_activity(
        {
            "lifecycle": "busy",
            "activity_state": "thinking",
            "commentary_preview": "I found an ELF header; now checking whether the archive footer is fake.",
        }
    )

    assert formatted == (
        "busy | state: thinking | thinking: I found an ELF header; now checking whether the archive footer is fake."
    )


def test_render_status_lines_builds_dashboard_sections() -> None:
    lines = _render_status_lines(
        {
            "models": ["gemini/gemini-2.5-flash", "codex/gpt-5.4"],
            "active_swarm_count": 1,
            "max_concurrent_challenges": 3,
            "finished_swarm_count": 1,
            "pending_challenge_count": 2,
            "known_challenge_count": 10,
            "known_solved_count": 6,
            "total_step_count": 20,
            "cost_usd": 1.23,
            "total_tokens": 456,
            "coordinator_queue_depth": 0,
            "operator_queue_depth": 2,
            "active_swarms": {
                "challenge-a": {
                    "winner": None,
                    "advisor_note": "Verify whether the API route is auth-gated.",
                    "shared_finding": "Potential admin API at /api/v1/k8s/get",
                    "shared_findings": {
                        "gemini/gemini-2.5-flash": {
                            "kind": "finding_ref",
                            "summary": "Potential admin API at /api/v1/k8s/get",
                            "pointer_path": "/challenge/shared-artifacts/finding.txt",
                            "digest_path": "/challenge/shared-artifacts/.advisor/finding.digest.md",
                        }
                    },
                    "signals": {
                        "total_posts": 4,
                        "total_checks": 6,
                        "total_delivered": 5,
                        "coordinator_messages": 1,
                        "advisor_lane_hints": 1,
                        "advisor_coordinator_appends": 1,
                    },
                    "agents": {
                        "gemini/gemini-2.5-flash": {
                            "lifecycle": "busy",
                            "step_count": 3,
                            "current_tool": "bash",
                            "current_command": "strings /challenge/distfiles/blob.bin | head -20",
                            "advisor_note": "Verify whether the API route is auth-gated.",
                        },
                        "codex/gpt-5.4": {
                            "lifecycle": "idle",
                            "step_count": 5,
                            "last_tool": "web_fetch",
                            "last_command": "ignored idle lane",
                        },
                    },
                }
            },
            "finished_swarms": {
                "challenge-b": {
                    "winner": "flag{done}",
                    "advisor_note": "Check whether the login bypass requires CSRF.",
                    "shared_finding": "Recovered hidden ZIP header from audio stream",
                    "shared_findings": {
                        "codex/gpt-5.4": {
                            "kind": "finding_ref",
                            "summary": "Recovered hidden ZIP header from audio stream",
                            "pointer_path": "/challenge/shared-artifacts/audio-find.txt",
                            "digest_path": "/challenge/shared-artifacts/.advisor/audio-find.digest.md",
                        }
                    },
                    "signals": {
                        "total_posts": 7,
                        "total_checks": 9,
                        "total_delivered": 8,
                        "coordinator_messages": 2,
                        "advisor_lane_hints": 2,
                        "advisor_coordinator_appends": 1,
                    },
                    "agents": {
                        "codex/gpt-5.4": {
                            "lifecycle": "won",
                            "step_count": 8,
                            "last_tool": "bash",
                            "last_command": "grep -R flag /challenge/distfiles",
                            "advisor_note": "Check whether the login bypass requires CSRF.",
                        },
                        "gemini/gemini-2.5-pro": {
                            "lifecycle": "quota_error",
                            "step_count": 4,
                            "last_tool": "bash",
                            "last_command": "curl -sS https://target.example/flag",
                        },
                    },
                }
            },
            "results": {
                "challenge-b": {
                    "status": "flag_found",
                    "flag": "flag{done}",
                    "findings_summary": "found flag via login bypass",
                    "winner_model": "codex/gpt-5.4",
                }
            },
        },
        fetch_error="temporary timeout",
        updated_at=1_700_000_000,
    )

    rendered = "\n".join(lines)
    assert "Coordinator Status" in rendered
    assert "Status fetch failed:" in rendered
    assert "Challenges: 10 | Solved: 6 | Active: 1 | Limit: 3 | Pending: 2 | Finished: 1 | Steps: 20" in rendered
    assert "Pending: 2" in rendered
    assert "Active Challenges" in rendered
    assert "Finished Challenges" in rendered
    assert "Latest Advisory" in rendered
    assert "Latest Shared Finding" in rendered
    assert "Signals" in rendered
    assert "Flags" in rendered
    assert "Challenge             Steps  Busy  Idle  Won  Quota  Error  Cancel  Winner" in rendered
    assert "challenge-a" in rendered
    assert "challenge-b" in rendered
    assert "challenge-a              8" in rendered
    assert "challenge-b             12" in rendered
    assert "gemini/gemini-2.5-flash" in rendered
    assert "busy" in rendered
    assert "now/bash" in rendered
    assert "flag{done}" in rendered
    assert "gemini/gemini-2.5-flash" in rendered
    assert "[Advisor] Verify whether the API route is auth-gated." in rendered
    assert "Potential admin API at /api/v1/k8s/get" in rendered
    assert "gemini/gemini-2.5-flash" in rendered
    assert "digest /challenge/shared-artifacts/.advisor/finding.digest.md" in rendered
    assert "challenge-a               4      6          5         1        1       1" in rendered
    assert "ignored idle lane" in rendered
    assert "Lane                              Step  State        Detail" in rendered
    assert "gemini/gemini-2.5-flash" in rendered
    assert "  3  busy" in rendered


def test_render_status_lines_shows_empty_advisory_section_when_no_notes() -> None:
    lines = _render_status_lines(
        {
            "models": ["gemini/gemini-2.5-flash"],
            "active_swarm_count": 0,
            "max_concurrent_challenges": 3,
            "finished_swarm_count": 0,
            "pending_challenge_count": 0,
            "known_challenge_count": 2,
            "known_solved_count": 0,
            "total_step_count": 0,
            "cost_usd": 0.0,
            "total_tokens": 0,
            "coordinator_queue_depth": 0,
            "operator_queue_depth": 0,
            "active_swarms": {},
            "finished_swarms": {},
            "results": {},
        }
    )

    rendered = "\n".join(lines)
    assert "Latest Advisory" in rendered
    assert "Latest Shared Finding" in rendered
    assert "Signals" in rendered
    assert "(none yet)" in rendered

def test_format_agent_activity_suppresses_generic_quota_noise() -> None:
    formatted = _format_agent_activity(
        {
            "lifecycle": "quota_error",
            "findings": "Turn failed: You've hit your usage limit for GPT-5.3-Codex-Spark. Switch to another model now, or try again later.",
            "last_exit_hint": "YOLO mode is enabled. All tool calls will be automatically approved.",
        }
    )

    assert formatted == "quota_error | finding: usage limit hit"


def test_render_status_lines_verbose_shows_idle_and_won_lanes() -> None:
    lines = _render_status_lines(
        {
            "models": ["gemini/gemini-2.5-flash", "codex/gpt-5.4"],
            "active_swarm_count": 1,
            "max_concurrent_challenges": 3,
            "finished_swarm_count": 1,
            "pending_challenge_count": 0,
            "known_challenge_count": 10,
            "known_solved_count": 6,
            "total_step_count": 11,
            "cost_usd": 0.0,
            "total_tokens": 0,
            "coordinator_queue_depth": 0,
            "operator_queue_depth": 0,
            "active_swarms": {
                "challenge-a": {
                    "winner": None,
                    "shared_finding": "Potential admin API at /api/v1/k8s/get",
                    "shared_findings": {
                        "codex/gpt-5.4": {
                            "kind": "finding_ref",
                            "summary": "Potential admin API at /api/v1/k8s/get",
                            "pointer_path": "/challenge/shared-artifacts/finding.txt",
                            "digest_path": "/challenge/shared-artifacts/.advisor/finding.digest.md",
                        }
                    },
                    "signals": {
                        "total_posts": 1,
                        "total_checks": 2,
                        "total_delivered": 1,
                        "coordinator_messages": 0,
                        "advisor_lane_hints": 0,
                        "advisor_coordinator_appends": 0,
                    },
                    "agents": {
                        "codex/gpt-5.4": {
                            "lifecycle": "idle",
                            "step_count": 2,
                            "last_tool": "web_fetch",
                            "last_command": "https://example.test/ignored",
                        }
                    },
                }
            },
            "finished_swarms": {
                "challenge-b": {
                    "winner": "flag{done}",
                    "shared_finding": "Recovered hidden ZIP header from audio stream",
                    "shared_findings": {
                        "codex/gpt-5.4": {
                            "kind": "finding_ref",
                            "summary": "Recovered hidden ZIP header from audio stream",
                            "pointer_path": "/challenge/shared-artifacts/audio-find.txt",
                            "digest_path": "/challenge/shared-artifacts/.advisor/audio-find.digest.md",
                        }
                    },
                    "signals": {
                        "total_posts": 2,
                        "total_checks": 3,
                        "total_delivered": 2,
                        "coordinator_messages": 0,
                        "advisor_lane_hints": 0,
                        "advisor_coordinator_appends": 0,
                    },
                    "agents": {
                        "codex/gpt-5.4": {
                            "lifecycle": "won",
                            "step_count": 9,
                            "last_tool": "bash",
                            "last_command": "grep -R flag /challenge/distfiles",
                        }
                    },
                }
            },
            "results": {},
        },
        verbose=True,
    )

    rendered = "\n".join(lines)
    assert "https://example.test/ignored" in rendered
    assert "grep -R flag /challenge/distfiles" in rendered


def test_build_compact_lane_renderables_groups_by_challenge() -> None:
    renderables = _build_compact_lane_renderables(
        "Active Lanes",
        {
            "challenge-a": {
                "agents": {
                    "codex/gpt-5.4": {
                        "lifecycle": "busy",
                        "step_count": 7,
                        "current_tool": "bash",
                        "current_command": "python3 solve.py",
                    },
                    "gemini/gemini-2.5-flash": {
                        "lifecycle": "idle",
                        "step_count": 3,
                        "last_tool": "run_shell_command",
                        "last_command": "file /challenge/distfiles/blob.bin",
                    },
                }
            }
        },
        verbose=False,
    )

    assert renderables[0] == "[bold]Active Lanes[/bold]"
    table = renderables[1]
    assert getattr(table, "title", "").startswith("challenge-a  (steps=10, busy=1, idle=1, quota=0)")


def test_status_launches_browser_ui_by_default(monkeypatch) -> None:
    opened: dict[str, str] = {}
    monkeypatch.setattr(cli_module, "_fetch_status_data", lambda host, port: {"ok": True})
    monkeypatch.setattr(
        cli_module.webbrowser,
        "open",
        lambda url: opened.setdefault("url", url) or True,
    )

    result = CliRunner().invoke(status, ["--port", "9401"])

    assert result.exit_code == 0
    assert opened["url"] == "http://127.0.0.1:9401/ui"
    assert "Opened:" in result.output


def test_status_once_keeps_terminal_snapshot(monkeypatch) -> None:
    recorded: dict[str, object] = {}
    snapshot = {"models": [], "active_swarms": {}, "finished_swarms": {}, "results": {}}
    monkeypatch.setattr(cli_module, "_fetch_status_data", lambda host, port: snapshot)

    def _record(data, **kwargs) -> None:
        recorded["data"] = data
        recorded["kwargs"] = kwargs

    monkeypatch.setattr(cli_module, "_print_status_snapshot", _record)
    monkeypatch.setattr(
        cli_module.webbrowser,
        "open",
        lambda url: (_ for _ in ()).throw(AssertionError("browser should not open")),
    )

    result = CliRunner().invoke(status, ["--once"])

    assert result.exit_code == 0
    assert recorded["data"] == snapshot
