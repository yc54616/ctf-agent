from __future__ import annotations

import os
from pathlib import Path

from pydantic_ai.usage import RunUsage

from backend.agents.lane_runtime import LaneRuntime, RuntimeCostTrackerProxy
from backend.runtime_control import append_jsonl, lane_control_paths, write_json_atomic


def _write_config(control_dir: Path) -> None:
    control = lane_control_paths(control_dir)
    write_json_atomic(
        control.config_path,
        {
            "model_spec": "codex/gpt-5.4-mini",
            "provider": "codex",
            "control_dir": str(control_dir),
            "meta": {
                "name": "demo",
                "category": "",
                "value": 0,
                "description": "",
                "tags": [],
                "connection_info": "",
                "hints": [],
                "solves": 0,
            },
            "settings": {},
        },
    )


def test_lane_runtime_ignores_stale_commands_on_start(tmp_path: Path) -> None:
    control_dir = tmp_path / "control"
    control_dir.mkdir(parents=True)
    _write_config(control_dir)
    control = lane_control_paths(control_dir)
    append_jsonl(control.commands_path, {"type": "shutdown", "ts": 1})
    stale_size = control.commands_path.stat().st_size

    runtime = LaneRuntime(control)

    assert runtime._commands_offset == stale_size


def test_write_json_atomic_can_publish_host_readable_state(tmp_path: Path) -> None:
    target = tmp_path / "state" / "heartbeat.json"

    write_json_atomic(target, {"ok": True}, mode=0o644)

    assert target.read_text(encoding="utf-8").strip().startswith("{")
    assert target.stat().st_mode & 0o777 == 0o644


def test_lane_runtime_sets_provider_home_for_codex(monkeypatch, tmp_path: Path) -> None:
    control_dir = tmp_path / "control"
    control_dir.mkdir(parents=True)
    _write_config(control_dir)
    control = lane_control_paths(control_dir)
    runtime = LaneRuntime(control)

    monkeypatch.delenv("CTF_AGENT_PROVIDER_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)

    runtime._apply_provider_runtime_env()

    provider_home = "/challenge/provider-home"
    assert os.environ["CTF_AGENT_PROVIDER_HOME"] == provider_home
    assert os.environ["HOME"] == provider_home
    assert os.environ["CODEX_HOME"] == f"{provider_home}/.codex"


def test_lane_runtime_sets_gemini_ipc_dir_under_control(monkeypatch, tmp_path: Path) -> None:
    control_dir = tmp_path / "control"
    control_dir.mkdir(parents=True)
    _write_config(control_dir)
    control = lane_control_paths(control_dir)
    runtime = LaneRuntime(control)

    monkeypatch.delenv("CTF_AGENT_GEMINI_IPC_DIR", raising=False)

    runtime._apply_provider_runtime_env()

    ipc_dir = Path(os.environ["CTF_AGENT_GEMINI_IPC_DIR"])
    assert ipc_dir == control_dir / "gemini-ipc"
    assert (ipc_dir / "requests").is_dir()
    assert (ipc_dir / "responses").is_dir()


def test_runtime_cost_tracker_proxy_exposes_by_agent_usage(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    proxy = RuntimeCostTrackerProxy(events_path, provider_spec="gemini")

    proxy.record(
        "demo/gemini",
        RunUsage(input_tokens=100, output_tokens=25, cache_read_tokens=40),
        "gemini-2.5-flash",
        duration_seconds=2.5,
    )

    agent = proxy.by_agent.get("demo/gemini")
    assert agent is not None
    assert agent.usage.input_tokens == 100
    assert agent.usage.output_tokens == 25
    assert agent.usage.cache_read_tokens == 40
    assert agent.duration_seconds == 2.5
    assert proxy.total_cost_usd >= 0.0
    assert proxy.total_tokens == agent.usage.total_tokens
    assert "cached" in proxy.format_usage("demo/gemini")
