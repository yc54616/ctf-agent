from __future__ import annotations

import json

from backend.operator_ui import collect_advisory_history


def test_collect_advisory_history_prefers_structured_events(monkeypatch, tmp_path) -> None:
    repo_root = tmp_path / "repo"
    logs_dir = repo_root / "logs"
    trace_dir = logs_dir
    trace_dir.mkdir(parents=True)

    challenge_name = "aeBPF"
    lane_control = repo_root / "logs" / "2026_GMDSOFT" / challenge_name / ".lane-state" / "codex-gpt-5.4" / "control"
    lane_control.mkdir(parents=True)
    (lane_control / "config.json").write_text(
        json.dumps({"model_spec": "codex/gpt-5.4"}, indent=2),
        encoding="utf-8",
    )
    (lane_control / "events.jsonl").write_text(
        json.dumps(
            {
                "type": "advisory_applied",
                "ts": 100.0,
                "source": "advisor",
                "insights": "Check the verifier patch hunk before rereading the manifest.",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    trace_path = trace_dir / "trace-aeBPF-gpt-5.4-0001-000000000000.jsonl"
    trace_path.write_text(
        json.dumps(
            {
                "type": "bump",
                "ts": 100.0,
                "source": "auto",
                "insights": "Private advisor note for this lane:\nCheck the verifier patch hunk before rereading the manifest.",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("backend.operator_ui._repo_root", lambda: repo_root)

    payload = collect_advisory_history(challenge_name, log_dir=logs_dir)

    assert payload["challenge_name"] == challenge_name
    assert len(payload["entries"]) == 1
    entry = payload["entries"][0]
    assert entry["source"] == "advisor"
    assert entry["model_spec"] == "codex/gpt-5.4"
    assert "verifier patch hunk" in entry["preview"]
