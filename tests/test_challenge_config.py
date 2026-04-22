from __future__ import annotations

import json
from pathlib import Path

import yaml

from backend.challenge_config import (
    apply_override_patch,
    challenge_config_snapshot,
    refresh_effective_metadata,
    write_override,
)


def test_challenge_config_snapshot_infers_connection_from_description(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "aeBPF"
    challenge_dir.mkdir()
    (challenge_dir / "metadata.yml").write_text(
        "\n".join(
            [
                "name: aeBPF",
                "category: pwn",
                "description: |-",
                "  aeBPF challenge",
                "  Host: host1.dreamhack.games",
                "  Port: 12345/tcp -> 31337/tcp",
                "  nc host1.dreamhack.games 12345",
                "connection_info: ''",
                "source:",
                "  platform: dreamhack",
                "  competition:",
                "    title: 2026 GMDSOFT",
                "    url: https://dreamhack.io/career/competitions/2026-GMDSOFT",
                "  challenge_url: https://dreamhack.io/career/competitions/2026-GMDSOFT/challenges/aebpf",
                "  needs_vm: true",
                "  status:",
                "    solved: false",
                "    writeup_submitted: true",
            ]
        ),
        encoding="utf-8",
    )

    payload = challenge_config_snapshot(challenge_dir)

    assert payload["source"]["connection"]["host"] == "host1.dreamhack.games"
    assert payload["effective"]["connection_info"] == "nc host1.dreamhack.games 12345"
    assert payload["effective"]["needs_instance"] is True
    assert payload["effective"]["source"]["status"]["writeup_submitted"] is True
    assert payload["source"]["source"]["platform_label"] == "Dreamhack"
    assert payload["source"]["source"]["capabilities"]["submit_flag"] == "confirmed"
    assert payload["runtime_mode"] == "full_remote"
    assert payload["automatic_submit"] is True


def test_apply_override_patch_and_effective_metadata_file(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "aeBPF"
    challenge_dir.mkdir()
    (challenge_dir / "metadata.yml").write_text(
        "\n".join(
            [
                "name: aeBPF",
                "description: |-",
                "  Host: host1.dreamhack.games",
                "  Port: 12345/tcp -> 31337/tcp",
                "  nc host1.dreamhack.games 12345",
                "connection_info: ''",
            ]
        ),
        encoding="utf-8",
    )

    override = apply_override_patch(
        {},
        {
            "connection": {
                "host": "host3.dreamhack.games",
                "port": 16377,
                "scheme": "tcp",
                "raw_command": "nc host3.dreamhack.games 16377",
            },
            "priority": True,
            "no_submit": True,
            "needs_instance": False,
            "notes": "operator-fixed endpoint",
        },
    )
    override_path = write_override(challenge_dir, override)
    effective_path = refresh_effective_metadata(challenge_dir)

    assert override_path is not None
    override_data = json.loads(override_path.read_text(encoding="utf-8"))
    assert override_data["connection"]["host"] == "host3.dreamhack.games"
    effective_data = yaml.safe_load(effective_path.read_text(encoding="utf-8"))
    assert effective_data["connection_info"] == "nc host3.dreamhack.games 16377"
    assert effective_data["priority"] is True
    assert effective_data["no_submit"] is True
    assert effective_data["needs_instance"] is False
    assert effective_data["notes"] == "operator-fixed endpoint"


def test_challenge_config_snapshot_forces_operator_only_on_unknown_platform(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "mystery"
    challenge_dir.mkdir()
    (challenge_dir / "metadata.yml").write_text(
        "\n".join(
            [
                "name: mystery",
                "description: mystery platform challenge",
                "source:",
                "  platform: scoreserver-x",
                "  challenge_url: https://example.com/challenges/mystery",
            ]
        ),
        encoding="utf-8",
    )

    payload = challenge_config_snapshot(challenge_dir)

    assert payload["source"]["source"]["platform"] == "scoreserver-x"
    assert payload["source"]["source"]["platform_label"] == "Scoreserver X"
    assert payload["source"]["source"]["capabilities"]["submit_flag"] == "operator_only"
    assert payload["runtime_mode"] == "operator_only"
    assert payload["automatic_submit"] is False
    assert payload["effective"]["no_submit"] is True
