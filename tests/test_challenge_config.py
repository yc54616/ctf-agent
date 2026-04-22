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


def test_effective_metadata_uses_current_instance_stage_connection(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "lab-chain"
    challenge_dir.mkdir()
    (challenge_dir / "metadata.yml").write_text(
        "\n".join(
            [
                "name: lab-chain",
                "description: stage workflow",
                "instance_stages:",
                "  - id: public_lab",
                "    title: Public Lab",
                "    description: Start the outer lab first",
                "    manual_action: deploy_from_portal",
                "    connection:",
                "      url: https://portal.example/lab",
                "  - id: internal_vm",
                "    title: Internal VM",
                "    notes: Deploy the second VM after logging in",
                "source:",
                "  platform: dreamhack",
                "  needs_vm: true",
            ]
        ),
        encoding="utf-8",
    )

    override = apply_override_patch(
        {},
        {
            "current_stage": "internal_vm",
            "stages": {
                "internal_vm": {
                    "status": "ready",
                    "connection": {
                        "scheme": "tcp",
                        "host": "10.10.10.5",
                        "port": 31337,
                        "raw_command": "nc 10.10.10.5 31337",
                    },
                }
            },
        },
    )
    write_override(challenge_dir, override)
    effective_path = refresh_effective_metadata(challenge_dir)
    effective_data = yaml.safe_load(effective_path.read_text(encoding="utf-8"))

    assert effective_data["needs_instance"] is True
    assert effective_data["current_stage"] == "internal_vm"
    assert effective_data["current_stage_title"] == "Internal VM"
    assert effective_data["current_stage_status"] == "ready"
    assert effective_data["connection"]["host"] == "10.10.10.5"
    assert effective_data["connection_info"] == "nc 10.10.10.5 31337"
    assert len(effective_data["instance_stages"]) == 2
    assert effective_data["instance_stages"][1]["is_current"] is True
    assert effective_data["instance_stages"][0]["connection_info"] == "https://portal.example/lab"


def test_effective_metadata_supports_override_stage_definitions_endpoints_and_auto_advance(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "multi-hop"
    challenge_dir.mkdir()
    (challenge_dir / "metadata.yml").write_text(
        "\n".join(
            [
                "name: multi-hop",
                "description: stage workflow",
                "instance_stages:",
                "  - id: public_lab",
                "    title: Public Lab",
                "  - id: internal_vm",
                "    title: Internal VM",
            ]
        ),
        encoding="utf-8",
    )

    override = apply_override_patch(
        {},
        {
            "instance_stages": [
                {
                    "id": "public_lab",
                    "title": "Public Lab",
                    "description": "Enter the portal first",
                    "endpoints": [
                        {
                            "id": "portal",
                            "title": "Portal",
                            "connection": {"url": "https://portal.example/lab"},
                        }
                    ],
                },
                {
                    "id": "internal_vm",
                    "title": "Internal VM",
                    "endpoints": [
                        {
                            "id": "shell",
                            "title": "Shell",
                            "connection": {
                                "scheme": "tcp",
                                "host": "10.10.10.5",
                                "port": 31337,
                            },
                        }
                    ],
                },
            ],
            "current_stage": "public_lab",
            "stages": {
                "public_lab": {
                    "status": "done",
                    "current_endpoint": "portal",
                },
                "internal_vm": {
                    "status": "ready",
                    "current_endpoint": "shell",
                    "endpoints": {
                        "shell": {
                            "connection": {
                                "scheme": "tcp",
                                "host": "10.10.10.9",
                                "port": 4444,
                                "raw_command": "nc 10.10.10.9 4444",
                            }
                        }
                    },
                },
            },
        },
    )
    write_override(challenge_dir, override)

    effective_path = refresh_effective_metadata(challenge_dir)
    effective_data = yaml.safe_load(effective_path.read_text(encoding="utf-8"))

    assert effective_data["current_stage"] == "internal_vm"
    assert effective_data["current_stage_status"] == "ready"
    assert effective_data["current_stage_endpoint"] == "shell"
    assert effective_data["current_stage_endpoint_title"] == "Shell"
    assert effective_data["connection"]["host"] == "10.10.10.9"
    assert effective_data["connection_info"] == "nc 10.10.10.9 4444"
    assert effective_data["instance_stages"][0]["status"] == "done"
    assert effective_data["instance_stages"][0]["current_endpoint"] == "portal"
    assert effective_data["instance_stages"][1]["is_current"] is True


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
