from __future__ import annotations

import pytest

from backend.message_bus import (
    CandidateRef,
    ChallengeMessageBus,
    CoordinatorNoteRef,
    SharedFindingRef,
)


@pytest.mark.asyncio
async def test_message_bus_formats_structured_pointer_events() -> None:
    bus = ChallengeMessageBus()
    await bus.post(
        "codex/gpt-5.4",
        SharedFindingRef(
            model="codex/gpt-5.4",
            kind="finding_ref",
            summary="Potential admin API at /api/v1/k8s/get",
            pointer_path="/challenge/shared-artifacts/finding.txt",
            digest_path="/challenge/shared-artifacts/.advisor/finding.digest.md",
        ),
    )
    await bus.post(
        "gemini/gemini-2.5-flash",
        SharedFindingRef(
            model="gemini/gemini-2.5-flash",
            kind="artifact_ref",
            content="Artifact path: /challenge/shared-artifacts/app.js",
            summary="JS bundle references hidden auth route",
            artifact_path="/challenge/shared-artifacts/app.js",
            pointer_path="/challenge/shared-artifacts/artifact-ref-challenge-a-gemini.md",
            digest_path="/challenge/shared-artifacts/.advisor/app.js-123.digest.md",
        ),
    )

    unread = await bus.check("claude/observer")

    rendered = bus.format_unread(unread)
    assert "Digest: /challenge/shared-artifacts/.advisor/finding.digest.md" in rendered
    assert "Pointer: /challenge/shared-artifacts/finding.txt" in rendered
    assert "Hint: Potential admin API at /api/v1/k8s/get" in rendered
    assert "Pointer: /challenge/shared-artifacts/artifact-ref-challenge-a-gemini.md" in rendered
    assert "Artifact: /challenge/shared-artifacts/app.js" not in rendered
    assert "Digest: /challenge/shared-artifacts/.advisor/app.js-123.digest.md" in rendered
    assert "Hint: JS bundle references hidden auth route" not in rendered


@pytest.mark.asyncio
async def test_message_bus_still_accepts_legacy_string_posts() -> None:
    bus = ChallengeMessageBus()
    await bus.post("codex/gpt-5.4", "Artifact path: /challenge/shared-artifacts/test.txt")

    unread = await bus.check("gemini/gemini-2.5-flash")

    assert unread[0].content == "Artifact path: /challenge/shared-artifacts/test.txt"
    assert bus.format_unread(unread).startswith("**Findings from other agents:**")


@pytest.mark.asyncio
async def test_message_bus_broadcast_wraps_structured_coordinator_event() -> None:
    bus = ChallengeMessageBus()

    await bus.broadcast("Focus on the shared login artifact first.")

    findings = await bus.snapshot_findings()

    assert len(findings) == 1
    assert isinstance(findings[0], SharedFindingRef)
    assert findings[0].kind == "coordinator_note"
    assert findings[0].summary == "Focus on the shared login artifact first."


def test_candidate_ref_and_coordinator_note_render_compact_pointer_messages() -> None:
    candidate = CandidateRef(
        challenge_name="challenge-a",
        flag="flag{candidate}",
        source_models=["codex/gpt-5.4"],
        advisor_decision="likely",
        advisor_note="route and evidence look plausible",
        summary="matched hidden admin route",
        evidence_digest_paths={
            "codex/gpt-5.4": "/challenge/shared-artifacts/.advisor/candidate.digest.md"
        },
        evidence_pointer_paths={"codex/gpt-5.4": "/challenge/shared-artifacts/candidate.txt"},
        trace_paths={"codex/gpt-5.4": "/tmp/trace.jsonl"},
    )
    note = CoordinatorNoteRef(
        challenge_name="challenge-a",
        source_model="codex/gpt-5.4",
        summary="Focus on the admin route evidence first.",
        pointer_path="/challenge/shared-artifacts/coordinator.txt",
    )

    assert "FLAG CANDIDATE: flag{candidate}" in candidate.rendered_text()
    assert "Evidence digest: /challenge/shared-artifacts/.advisor/candidate.digest.md" in candidate.rendered_text()
    assert "Evidence pointer: /challenge/shared-artifacts/candidate.txt" in candidate.rendered_text()
    assert "ADVISOR MESSAGE: [challenge-a/codex/gpt-5.4] Focus on the admin route evidence first." in note.rendered_text()


def test_shared_finding_ref_migrates_legacy_artifact_pointer_snapshot() -> None:
    finding = SharedFindingRef.from_snapshot(
        {
            "model": "codex/gpt-5.4",
            "kind": "artifact_ref",
            "content": "Artifact path: /challenge/shared-artifacts/app.js",
            "pointer_path": "/challenge/shared-artifacts/app.js",
            "digest_path": "/challenge/shared-artifacts/.advisor/app.js-123.digest.md",
        }
    )

    assert finding is not None
    assert finding.artifact_path == "/challenge/shared-artifacts/app.js"
    assert finding.pointer_path == ""
