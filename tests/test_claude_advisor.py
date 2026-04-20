from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from backend.agents.advisor_base import ADVISOR_SYSTEM_PROMPT, CandidateReview
from backend.agents.claude_advisor import ClaudeAdvisor
from backend.agents.codex_advisor import CodexAdvisor
from backend.agents.swarm import (
    ADVISOR_ARTIFACT_FOCUSED_SIBLING_MAX_CHARS,
    ADVISOR_LANE_STATE_MAX_CHARS,
    ADVISOR_MANIFEST_EXCERPT_MAX_CHARS,
    ADVISOR_SIBLING_INSIGHTS_MAX_CHARS,
    ChallengeSwarm,
)
from backend.cli import _validate_runtime_auth
from backend.cost_tracker import CostTracker
from backend.message_bus import SharedFindingRef
from backend.prompts import ChallengeMeta
from backend.solver_base import GAVE_UP, SolverResult


class _FakeAdvisor:
    def __init__(self, finding_reply: str = "", coordinator_reply: str = "", lane_reply: str = "") -> None:
        self.finding_reply = finding_reply
        self.coordinator_reply = coordinator_reply
        self.lane_reply = lane_reply
        self.flag_review = CandidateReview()
        self.finding_calls: list[dict[str, str]] = []
        self.coordinator_calls: list[dict[str, str]] = []
        self.lane_calls: list[dict[str, str]] = []
        self.flag_calls: list[dict[str, str]] = []

    async def annotate_finding(
        self,
        *,
        source_model: str,
        challenge_brief: str,
        finding: str,
        sibling_insights: str,
    ) -> str:
        self.finding_calls.append(
            {
                "source_model": source_model,
                "challenge_brief": challenge_brief,
                "finding": finding,
                "sibling_insights": sibling_insights,
            }
        )
        return self.finding_reply

    async def annotate_coordinator_message(
        self,
        *,
        source_model: str,
        challenge_brief: str,
        message: str,
        sibling_insights: str,
    ) -> str:
        self.coordinator_calls.append(
            {
                "source_model": source_model,
                "challenge_brief": challenge_brief,
                "message": message,
                "sibling_insights": sibling_insights,
            }
        )
        return self.coordinator_reply

    async def suggest_lane_hint(
        self,
        *,
        target_model: str,
        challenge_brief: str,
        lane_state: str,
        sibling_findings: str,
        manifest_excerpt: str,
        artifact_previews: str,
    ) -> str:
        self.lane_calls.append(
            {
                "target_model": target_model,
                "challenge_brief": challenge_brief,
                "lane_state": lane_state,
                "sibling_findings": sibling_findings,
                "manifest_excerpt": manifest_excerpt,
                "artifact_previews": artifact_previews,
            }
        )
        return self.lane_reply

    async def review_flag_candidate(
        self,
        *,
        source_model: str,
        challenge_brief: str,
        flag: str,
        evidence: str,
        sibling_insights: str,
    ) -> CandidateReview:
        self.flag_calls.append(
            {
                "source_model": source_model,
                "challenge_brief": challenge_brief,
                "flag": flag,
                "evidence": evidence,
                "sibling_insights": sibling_insights,
            }
        )
        return self.flag_review


class _FakeLaneSolver:
    def __init__(self, lifecycle: str = "idle") -> None:
        self._runtime = {
            "lifecycle": lifecycle,
            "step_count": 4,
            "last_tool": "bash",
            "last_command": "grep -R auth /challenge/shared-artifacts",
            "last_exit_hint": "login form found",
        }
        self.bumped: list[str] = []
        self.advisory_bumped: list[str] = []

    def get_runtime_status(self) -> dict[str, object]:
        return dict(self._runtime)

    def bump(self, insights: str) -> None:
        self.bumped.append(insights)

    def bump_advisory(self, insights: str) -> None:
        self.advisory_bumped.append(insights)


def _make_swarm(tmp_path) -> ChallengeSwarm:
    return ChallengeSwarm(
        challenge_dir=str(tmp_path / "challenge"),
        meta=ChallengeMeta(
            name="advisor-test",
            category="web",
            value=300,
            description="Investigate the shared login surface and recover the real flag.",
            connection_info="https://ctf.example/challenge",
            hints=[{"content": "The admin route is not linked from the main page."}],
        ),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        model_specs=["codex/gpt-5.4"],
        coordinator_inbox=asyncio.Queue(),
    )


def test_advisor_system_prompt_stays_compact() -> None:
    assert "NO_ADVICE" in ADVISOR_SYSTEM_PROMPT
    assert "Do not call tools." in ADVISOR_SYSTEM_PROMPT
    assert len(ADVISOR_SYSTEM_PROMPT) < 900


def test_validate_runtime_auth_rejects_claude_solver_specs() -> None:
    with pytest.raises(ValueError, match="Claude solver lanes are disabled"):
        _validate_runtime_auth(
            cast(Any, SimpleNamespace(use_home_auth=False)),
            ["claude-sdk/claude-opus-4-7/medium"],
            "codex",
        )


@pytest.mark.asyncio
async def test_build_advised_coordinator_message_appends_advisor_comment(tmp_path) -> None:
    swarm = _make_swarm(tmp_path)
    advisor = _FakeAdvisor(coordinator_reply="Check whether the API route is auth-gated.")
    swarm._advisors["codex"] = advisor
    swarm.findings["gemini/gemini-2.5-flash"] = "Potential admin API at /api/v1/k8s/get"

    message = await swarm._build_advised_coordinator_message(
        "codex/gpt-5.4",
        "Found an admin-looking API route worth checking.",
    )

    assert message.endswith("[Advisor] Check whether the API route is auth-gated.")
    assert swarm.last_coordinator_advisor_note == "Check whether the API route is auth-gated."
    assert advisor.coordinator_calls
    assert "Potential admin API" in advisor.coordinator_calls[0]["sibling_insights"]
    assert "Investigate the shared login surface" in advisor.coordinator_calls[0]["challenge_brief"]


@pytest.mark.asyncio
async def test_lane_listener_adds_private_hint_without_reposting_to_bus(tmp_path) -> None:
    swarm = _make_swarm(tmp_path)
    advisor = _FakeAdvisor(lane_reply="Pivot from broad grep to the shared login artifact first.")
    swarm._advisors["codex"] = advisor
    swarm.solvers["codex/gpt-5.4"] = cast(Any, _FakeLaneSolver())
    swarm.solvers["gemini/gemini-2.5-flash"] = cast(Any, _FakeLaneSolver())
    manifest_path = swarm.shared_artifacts_dir / "manifest.md"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        "# Shared Artifact Manifest\n\n- fact: login page artifact\n- path: /challenge/shared-artifacts/login.html\n",
        encoding="utf-8",
    )
    (swarm.shared_artifacts_dir / "login.html").write_text(
        "<html><form><input name='csrf' value='token-123'></form></html>\n",
        encoding="utf-8",
    )
    await swarm.message_bus.post(
        "gemini/gemini-2.5-flash",
        SharedFindingRef(
            model="gemini/gemini-2.5-flash",
            content="Artifact path: /challenge/shared-artifacts/login.html",
        ),
    )
    await swarm.message_bus.post(
        "codex/gpt-5.4-mini",
        SharedFindingRef(
            model="codex/gpt-5.4-mini",
            content="Potential CSRF token at /challenge/shared-artifacts/login.html",
        ),
    )

    await swarm._maybe_issue_lane_advisories()

    unread = await swarm.message_bus.check("codex/gpt-5.4")
    assert [finding.content for finding in unread] == [
        "Artifact path: /challenge/shared-artifacts/login.html",
        "Potential CSRF token at /challenge/shared-artifacts/login.html",
    ]
    assert swarm.last_advisor_note == "Pivot from broad grep to the shared login artifact first."
    assert swarm.lane_advisor_notes["codex/gpt-5.4"] == swarm.last_advisor_note
    assert swarm.advisor_lane_hint_count == 1
    assert swarm.coordinator_inbox is not None
    lane_solver = cast(_FakeLaneSolver, swarm.solvers["codex/gpt-5.4"])
    assert not swarm.coordinator_inbox.qsize()
    assert not lane_solver.bumped
    assert lane_solver.advisory_bumped
    assert (
        "Private advisor note for this lane"
        in lane_solver.advisory_bumped[0]
    )
    assert advisor.lane_calls
    assert "The admin route is not linked" in advisor.lane_calls[0]["challenge_brief"]
    assert "/challenge/shared-artifacts/.advisor/login.html-" in advisor.lane_calls[0]["artifact_previews"]
    assert "# Artifact Digest" in advisor.lane_calls[0]["artifact_previews"]
    assert "token-123" in advisor.lane_calls[0]["artifact_previews"]
    assert advisor.lane_calls[0]["manifest_excerpt"] == ""
    assert "login.html |" in advisor.lane_calls[0]["sibling_findings"]
    assert "Digest:" not in advisor.lane_calls[0]["sibling_findings"]
    assert "Pointer:" not in advisor.lane_calls[0]["sibling_findings"]
    assert len(advisor.lane_calls[0]["sibling_findings"]) <= ADVISOR_ARTIFACT_FOCUSED_SIBLING_MAX_CHARS


def test_artifact_preview_block_avoids_full_read_bytes(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    swarm = _make_swarm(tmp_path)
    artifact = swarm.shared_artifacts_dir / "large.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("A" * 8192, encoding="utf-8")
    host_path = artifact.resolve()
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(self: Path) -> bytes:
        if self.resolve() == host_path:
            raise AssertionError("artifact preview should not call read_bytes()")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    rendered = swarm._artifact_preview_block("/challenge/shared-artifacts/large.txt")

    assert rendered.startswith("/challenge/shared-artifacts/large.txt")


def test_ensure_artifact_digest_uses_in_memory_cache(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    swarm = _make_swarm(tmp_path)
    artifact = swarm.shared_artifacts_dir / "routes.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("hidden admin route /api/v1/internal\n", encoding="utf-8")

    digest_path, revision, first_text = swarm._ensure_artifact_digest("/challenge/shared-artifacts/routes.txt")

    digest_host_path = swarm._shared_artifact_host_path(digest_path)
    assert digest_host_path is not None
    original_read_text = Path.read_text

    def guarded_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self.resolve() == digest_host_path.resolve():
            raise AssertionError("cached digest lookup should not read the digest file again")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    cached_digest_path, cached_revision, cached_text = swarm._ensure_artifact_digest(
        "/challenge/shared-artifacts/routes.txt"
    )

    assert cached_digest_path == digest_path
    assert cached_revision == revision
    assert cached_text == first_text


def test_generated_artifact_paths_are_not_shareable(tmp_path) -> None:
    swarm = _make_swarm(tmp_path)

    assert swarm._is_shareable_artifact_path("/challenge/shared-artifacts/routes.txt")
    assert not swarm._is_shareable_artifact_path(
        "/challenge/shared-artifacts/.advisor/routes.txt-123.digest.md"
    )
    assert not swarm._is_shareable_artifact_path(
        "/challenge/shared-artifacts/artifact-ref-advisor-test-codex-gpt-5.4.md"
    )
    assert not swarm._is_shareable_artifact_path(
        "/challenge/shared-artifacts/candidate-advisor-test-codex-gpt-5.4.txt"
    )
    assert not swarm._is_shareable_artifact_path(
        "/challenge/shared-artifacts/finding-advisor-test-codex-gpt-5.4.txt"
    )


def test_swarm_advisor_context_is_capped(tmp_path) -> None:
    swarm = _make_swarm(tmp_path)
    long_finding = "Potential route " + ("A" * 1200)
    for idx in range(6):
        swarm.findings[f"lane-{idx}"] = long_finding + str(idx)

    sibling = swarm._gather_sibling_insights("lane-0")

    manifest_path = swarm.shared_artifacts_dir / "manifest.md"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text("\n".join(f"line {idx} {'B'*120}" for idx in range(32)), encoding="utf-8")
    manifest = swarm._manifest_excerpt()

    lane_state = swarm._clip_text_block("X" * 5000, limit=ADVISOR_LANE_STATE_MAX_CHARS)

    assert len(sibling) <= ADVISOR_SIBLING_INSIGHTS_MAX_CHARS
    assert len(manifest) <= ADVISOR_MANIFEST_EXCERPT_MAX_CHARS
    assert len(lane_state) <= ADVISOR_LANE_STATE_MAX_CHARS


def test_focused_manifest_excerpt_prefers_relevant_artifacts(tmp_path) -> None:
    swarm = _make_swarm(tmp_path)
    manifest_path = swarm.shared_artifacts_dir / "manifest.md"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        "\n".join(
            [
                "# Shared Artifact Manifest",
                "",
                "- 2026-04-20T00:00:00Z | codex/gpt-5.4",
                "  - fact: login page",
                "  - path: /challenge/shared-artifacts/login.html",
                "- 2026-04-20T00:01:00Z | gemini/gemini-2.5-flash",
                "  - fact: admin route map",
                "  - path: /challenge/shared-artifacts/routes.txt",
                "  - digest: /challenge/shared-artifacts/.advisor/routes.txt.digest.md",
            ]
        ),
        encoding="utf-8",
    )

    excerpt = swarm._focused_manifest_excerpt(["/challenge/shared-artifacts/routes.txt"])

    assert excerpt == ""


def test_artifact_digest_includes_signal_context_around_late_hits(tmp_path) -> None:
    swarm = _make_swarm(tmp_path)
    artifact = swarm.shared_artifacts_dir / "routes.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        "\n".join(
            [f"line {idx}" for idx in range(20)]
            + [
                "before late signal",
                "hidden admin route /api/v1/internal",
                "after late signal",
            ]
            + [f"tail {idx}" for idx in range(20)]
        ),
        encoding="utf-8",
    )

    digest = swarm._build_artifact_digest(
        "/challenge/shared-artifacts/routes.txt",
        artifact,
    )

    assert "## Signal contexts" in digest
    assert "before late signal" in digest
    assert "hidden admin route /api/v1/internal" in digest
    assert "after late signal" in digest


def test_artifact_signal_context_preview_reads_around_signal_hits(tmp_path) -> None:
    swarm = _make_swarm(tmp_path)
    artifact = swarm.shared_artifacts_dir / "routes.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        "\n".join(
            [
                "before",
                "hidden admin route /api/v1/internal",
                "after",
                "tail",
            ]
        ),
        encoding="utf-8",
    )

    preview = swarm._artifact_signal_context_preview_block("/challenge/shared-artifacts/routes.txt")

    assert preview.startswith("/challenge/shared-artifacts/routes.txt")
    assert "[signal-contexts]" in preview
    assert "before" in preview
    assert "hidden admin route /api/v1/internal" in preview
    assert "after" in preview


def test_swarm_status_preserves_lane_advisor_note_over_runtime_placeholder(tmp_path) -> None:
    swarm = _make_swarm(tmp_path)
    swarm.solvers["codex/gpt-5.4"] = cast(Any, _FakeLaneSolver())
    swarm.lane_advisor_notes["codex/gpt-5.4"] = "Check the extracted login artifact before grepping again."

    status = swarm.get_status()

    assert (
        status["agents"]["codex/gpt-5.4"]["advisor_note"]
        == "Check the extracted login artifact before grepping again."
    )


@pytest.mark.asyncio
async def test_share_artifact_finding_uses_recent_trace_paths(tmp_path) -> None:
    swarm = _make_swarm(tmp_path)
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(
        json.dumps(
            {
                "type": "tool_call",
                "tool": "bash",
                "args": "{\"command\": \"sed -n '1,120p' /challenge/shared-artifacts/kernel_symbols.txt\", \"timeout_seconds\": 30}",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    artifact = swarm.shared_artifacts_dir / "kernel_symbols.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        "commit_creds\nprepare_kernel_cred\ninit_task\n",
        encoding="utf-8",
    )
    solver = _FakeLaneSolver()
    solver._runtime["last_exit_hint"] = "commit_creds and prepare_kernel_cred are present in the extracted image"

    result = SolverResult(
        flag=None,
        status=GAVE_UP,
        findings_summary="",
        step_count=8,
        cost_usd=0.02,
        log_path=str(trace_path),
    )

    await swarm._maybe_share_artifact_finding("codex/gpt-5.4", cast(Any, solver), result)

    finding = swarm.shared_finding_events["codex/gpt-5.4"]
    assert finding.artifact_path == "/challenge/shared-artifacts/kernel_symbols.txt"
    assert finding.pointer_path.startswith("/challenge/shared-artifacts/artifact-ref-")
    assert "commit_creds" in finding.summary


@pytest.mark.asyncio
async def test_share_artifact_finding_falls_back_to_digest_summary(tmp_path) -> None:
    swarm = _make_swarm(tmp_path)
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(
        json.dumps(
            {
                "type": "tool_call",
                "tool": "bash",
                "args": "{\"command\": \"sed -n '1,120p' /challenge/shared-artifacts/interesting.txt\", \"timeout_seconds\": 30}",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    artifact = swarm.shared_artifacts_dir / "interesting.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        "line 1\nhidden admin route /api/v1/internal\nline 3\n",
        encoding="utf-8",
    )
    solver = _FakeLaneSolver()
    solver._runtime["last_exit_hint"] = "cands 0"

    result = SolverResult(
        flag=None,
        status=GAVE_UP,
        findings_summary="",
        step_count=4,
        cost_usd=0.01,
        log_path=str(trace_path),
    )

    await swarm._maybe_share_artifact_finding("codex/gpt-5.3-codex", cast(Any, solver), result)

    finding = swarm.shared_finding_events["codex/gpt-5.3-codex"]
    assert finding.artifact_path == "/challenge/shared-artifacts/interesting.txt"
    assert finding.pointer_path.startswith("/challenge/shared-artifacts/artifact-ref-")
    assert "hidden admin route" in finding.summary


@pytest.mark.asyncio
async def test_lane_listener_deduplicates_same_context(tmp_path) -> None:
    swarm = _make_swarm(tmp_path)
    advisor = _FakeAdvisor(lane_reply="Read the shared bundle before another broad search.")
    swarm._advisors["codex"] = advisor
    swarm.solvers["codex/gpt-5.4"] = cast(Any, _FakeLaneSolver())
    manifest_path = swarm.shared_artifacts_dir / "manifest.md"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        "# Shared Artifact Manifest\n\n- fact: js bundle\n- path: /challenge/shared-artifacts/app.js\n",
        encoding="utf-8",
    )
    (swarm.shared_artifacts_dir / "app.js").write_text(
        ("fetch('/api/v1/app')\nconst csrf = 'abc123';\n" * 600),
        encoding="utf-8",
    )
    await swarm.message_bus.post(
        "gemini/gemini-2.5-flash",
        SharedFindingRef(
            model="gemini/gemini-2.5-flash",
            content="Artifact path: /challenge/shared-artifacts/app.js",
        ),
    )
    await swarm.message_bus.post(
        "codex/gpt-5.4-mini",
        SharedFindingRef(
            model="codex/gpt-5.4-mini",
            content="Artifact path: /challenge/shared-artifacts/app.js",
        ),
    )

    await swarm._maybe_issue_lane_advisories()
    await swarm._maybe_issue_lane_advisories()

    assert len(advisor.lane_calls) == 1
    assert "/challenge/shared-artifacts/.advisor/app.js-" in advisor.lane_calls[0]["artifact_previews"]
    assert "fetch('/api/v1/app')" in advisor.lane_calls[0]["artifact_previews"]


@pytest.mark.asyncio
async def test_lane_listener_skips_artifact_previews_without_explicit_artifact_paths(tmp_path) -> None:
    swarm = _make_swarm(tmp_path)
    advisor = _FakeAdvisor(lane_reply="Validate the sibling hypothesis before another broad grep.")
    swarm._advisors["codex"] = advisor
    swarm.solvers["codex/gpt-5.4"] = cast(Any, _FakeLaneSolver())
    manifest_path = swarm.shared_artifacts_dir / "manifest.md"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        "# Shared Artifact Manifest\n\n- fact: login page artifact\n- path: /challenge/shared-artifacts/login.html\n",
        encoding="utf-8",
    )
    (swarm.shared_artifacts_dir / "login.html").write_text(
        "<html><form><input name='csrf' value='token-123'></form></html>\n",
        encoding="utf-8",
    )
    await swarm.message_bus.post(
        "gemini/gemini-2.5-flash",
        SharedFindingRef(
            model="gemini/gemini-2.5-flash",
            content="Potential CSRF token in shared login page",
        ),
    )
    await swarm.message_bus.post(
        "codex/gpt-5.4-mini",
        SharedFindingRef(
            model="codex/gpt-5.4-mini",
            content="Repeated auth failure after broad grep",
        ),
    )

    await swarm._maybe_issue_lane_advisories()

    assert advisor.lane_calls
    assert advisor.lane_calls[0]["artifact_previews"] == ""
    assert advisor.lane_calls[0]["manifest_excerpt"]
    lane_solver = cast(_FakeLaneSolver, swarm.solvers["codex/gpt-5.4"])
    assert lane_solver.advisory_bumped


@pytest.mark.asyncio
async def test_lane_advisory_monitor_skips_digest_auto_bumps(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    swarm = _make_swarm(tmp_path)
    calls = {"digest": 0, "advisory": 0}

    async def fake_digest() -> None:
        calls["digest"] += 1

    async def fake_advisory() -> None:
        calls["advisory"] += 1
        swarm.cancel_event.set()

    monkeypatch.setattr(swarm, "_maybe_issue_lane_digest_updates", fake_digest)
    monkeypatch.setattr(swarm, "_maybe_issue_lane_advisories", fake_advisory)
    monkeypatch.setattr("backend.agents.swarm.ADVISOR_LISTENER_INTERVAL_SECONDS", 0)

    await swarm._monitor_lane_advisories()

    assert calls == {"digest": 0, "advisory": 1}


@pytest.mark.asyncio
async def test_lane_advisory_monitor_retriggers_when_lane_returns_idle(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm = _make_swarm(tmp_path)
    lane_solver = _FakeLaneSolver(lifecycle="busy")
    swarm.solvers["codex/gpt-5.4"] = cast(Any, lane_solver)
    calls = {"advisory": 0}

    async def fake_advisory() -> None:
        calls["advisory"] += 1
        if calls["advisory"] == 1:
            lane_solver._runtime["lifecycle"] = "idle"
            lane_solver._runtime["step_count"] = 5
            return
        swarm.cancel_event.set()

    monkeypatch.setattr(swarm, "_maybe_issue_lane_advisories", fake_advisory)
    monkeypatch.setattr("backend.agents.swarm.ADVISOR_LISTENER_INTERVAL_SECONDS", 0)

    await swarm._monitor_lane_advisories()

    assert calls["advisory"] == 2


@pytest.mark.asyncio
async def test_lane_advisory_monitor_retriggers_when_artifact_digest_changes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm = _make_swarm(tmp_path)
    swarm.solvers["codex/gpt-5.4"] = cast(Any, _FakeLaneSolver())
    artifact = swarm.shared_artifacts_dir / "routes.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("line 1\nhidden admin route\n", encoding="utf-8")
    await swarm.message_bus.post(
        "gemini/gemini-2.5-flash",
        SharedFindingRef(
            model="gemini/gemini-2.5-flash",
            content="Artifact path: /challenge/shared-artifacts/routes.txt",
        ),
    )
    calls = {"advisory": 0}

    async def fake_advisory() -> None:
        calls["advisory"] += 1
        if calls["advisory"] == 1:
            artifact.write_text(
                "line 1\nhidden admin route\nfresh digest signal\n",
                encoding="utf-8",
            )
            return
        swarm.cancel_event.set()

    monkeypatch.setattr(swarm, "_maybe_issue_lane_advisories", fake_advisory)
    monkeypatch.setattr("backend.agents.swarm.ADVISOR_LISTENER_INTERVAL_SECONDS", 0)

    await swarm._monitor_lane_advisories()

    assert calls["advisory"] == 2


@pytest.mark.asyncio
async def test_lane_advisory_trigger_signature_changes_when_manifest_changes(tmp_path) -> None:
    swarm = _make_swarm(tmp_path)
    swarm.solvers["codex/gpt-5.4"] = cast(Any, _FakeLaneSolver())
    manifest_path = swarm.shared_artifacts_dir / "manifest.md"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        "# Shared Artifact Manifest\n\n- fact: login page\n  - path: /challenge/shared-artifacts/login.html\n",
        encoding="utf-8",
    )

    first = await swarm._lane_advisory_trigger_signature()
    manifest_path.write_text(
        "# Shared Artifact Manifest\n\n- fact: login page\n  - path: /challenge/shared-artifacts/login.html\n- fact: admin route map\n  - path: /challenge/shared-artifacts/routes.txt\n",
        encoding="utf-8",
    )
    second = await swarm._lane_advisory_trigger_signature()

    assert first != second


def test_get_advisor_prefers_claude_until_sticky_fallback(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    swarm = _make_swarm(tmp_path)
    codex_advisor = _FakeAdvisor()
    claude_advisor = _FakeAdvisor()

    monkeypatch.setattr(
        "backend.agents.codex_advisor.CodexAdvisor.maybe_create",
        classmethod(lambda cls, settings, challenge_name: codex_advisor),
    )
    monkeypatch.setattr(
        "backend.agents.claude_advisor.ClaudeAdvisor.maybe_create",
        classmethod(lambda cls, settings, challenge_name: claude_advisor),
    )

    assert swarm._get_advisor("codex/gpt-5.4") is claude_advisor
    assert swarm._get_advisor("gemini/gemini-2.5-flash") is claude_advisor

    swarm._set_sticky_advisor_backend("codex", "quota exhausted")

    assert swarm._get_advisor("codex/gpt-5.4") is codex_advisor
    assert swarm._get_advisor("gemini/gemini-2.5-flash") is codex_advisor


@pytest.mark.asyncio
async def test_claude_limit_text_triggers_sticky_codex_fallback(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    swarm = _make_swarm(tmp_path)
    claude_advisor = _FakeAdvisor(
        lane_reply="You've hit your limit · resets Apr 24, 4am (Asia/Seoul)"
    )
    codex_advisor = _FakeAdvisor(
        lane_reply="Check the extracted buildroot patch before more broad reads."
    )
    swarm._advisors["claude"] = claude_advisor
    swarm._advisors["codex"] = codex_advisor

    persisted: list[bool] = []

    async def fake_persist() -> None:
        persisted.append(True)

    monkeypatch.setattr(swarm, "_persist_runtime_state", fake_persist)

    result = await swarm._run_advisor_call(
        "codex/gpt-5.4",
        timeout_seconds=1.0,
        operation_label="lane hint",
        call=lambda advisor: advisor.suggest_lane_hint(
            target_model="codex/gpt-5.4",
            challenge_brief="Investigate the shared login surface",
            lane_state="step 4",
            sibling_findings="artifact path: /challenge/shared-artifacts/login.html",
            manifest_excerpt="# Shared Artifact Manifest",
            artifact_previews="preview",
        ),
    )

    assert result == "Check the extracted buildroot patch before more broad reads."
    assert swarm._sticky_advisor_backend == "codex"
    assert persisted == [True]
    assert len(claude_advisor.lane_calls) == 1
    assert len(codex_advisor.lane_calls) == 1


def test_limit_exception_requests_sticky_codex_fallback(tmp_path) -> None:
    swarm = _make_swarm(tmp_path)

    assert swarm._should_sticky_fallback_to_codex(
        RuntimeError("You've hit your limit · resets Apr 24, 4am (Asia/Seoul)")
    )


def test_format_exception_text_uses_class_name_when_message_is_empty(tmp_path) -> None:
    swarm = _make_swarm(tmp_path)

    assert swarm._format_exception_text(TimeoutError()) == "TimeoutError"


@pytest.mark.asyncio
async def test_advisor_timeout_backoff_skips_repeated_lane_hint_calls(tmp_path) -> None:
    swarm = _make_swarm(tmp_path)

    class _TimeoutAdvisor:
        def __init__(self) -> None:
            self.calls = 0

        async def suggest_lane_hint(self, **_: object) -> str:
            self.calls += 1
            raise TimeoutError

    advisor = _TimeoutAdvisor()
    swarm._advisors["codex"] = cast(Any, advisor)
    swarm._sticky_advisor_backend = "codex"

    kwargs = {
        "target_model": "codex/gpt-5.4",
        "challenge_brief": "Investigate shared login surface",
        "lane_state": "step 4",
        "sibling_findings": "artifact path: /challenge/shared-artifacts/login.html",
        "manifest_excerpt": "# Shared Artifact Manifest",
        "artifact_previews": "preview",
    }

    first = await swarm._run_advisor_call(
        "codex/gpt-5.4",
        timeout_seconds=0.1,
        operation_label="lane hint",
        call=lambda backend: backend.suggest_lane_hint(**kwargs),
    )
    second = await swarm._run_advisor_call(
        "codex/gpt-5.4",
        timeout_seconds=0.1,
        operation_label="lane hint",
        call=lambda backend: backend.suggest_lane_hint(**kwargs),
    )
    third = await swarm._run_advisor_call(
        "codex/gpt-5.4",
        timeout_seconds=0.1,
        operation_label="lane hint",
        call=lambda backend: backend.suggest_lane_hint(**kwargs),
    )

    assert first is None
    assert second is None
    assert third is None
    assert advisor.calls == 2


@pytest.mark.asyncio
async def test_advisor_timeout_backoff_logs_once_per_bucket(tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    swarm = _make_swarm(tmp_path)

    class _TimeoutAdvisor:
        async def suggest_lane_hint(self, **_: object) -> str:
            raise TimeoutError

    swarm._advisors["codex"] = cast(Any, _TimeoutAdvisor())
    swarm._sticky_advisor_backend = "codex"

    kwargs = {
        "target_model": "codex/gpt-5.4",
        "challenge_brief": "Investigate shared login surface",
        "lane_state": "step 4",
        "sibling_findings": "artifact path: /challenge/shared-artifacts/login.html",
        "manifest_excerpt": "# Shared Artifact Manifest",
        "artifact_previews": "preview",
    }

    with caplog.at_level(logging.DEBUG):
        await swarm._run_advisor_call(
            "codex/gpt-5.4",
            timeout_seconds=0.1,
            operation_label="lane hint",
            call=lambda backend: backend.suggest_lane_hint(**kwargs),
        )
        await swarm._run_advisor_call(
            "codex/gpt-5.4",
            timeout_seconds=0.1,
            operation_label="lane hint",
            call=lambda backend: backend.suggest_lane_hint(**kwargs),
        )
        await swarm._run_advisor_call(
            "codex/gpt-5.4",
            timeout_seconds=0.1,
            operation_label="lane hint",
            call=lambda backend: backend.suggest_lane_hint(**kwargs),
        )
        await swarm._run_advisor_call(
            "codex/gpt-5.4",
            timeout_seconds=0.1,
            operation_label="lane hint",
            call=lambda backend: backend.suggest_lane_hint(**kwargs),
        )

    backoff_messages = [
        record.message for record in caplog.records if "timeout backoff" in record.message
    ]
    assert len(backoff_messages) == 1


@pytest.mark.asyncio
async def test_codex_advisor_builds_finding_prompt_via_query() -> None:
    advisor = CodexAdvisor("midnight-roulette")

    async def fake_query(prompt: str, *, session_key: str = "general") -> str:
        assert session_key == "finding"
        assert "Challenge: midnight-roulette" in prompt
        assert "Source model: codex/gpt-5.4" in prompt
        assert "Challenge brief:" in prompt
        assert "Investigate shared login surface" in prompt
        assert "Sibling insights:" in prompt
        return "Check whether the route is auth-gated."

    advisor._query = cast(Any, fake_query)

    result = await advisor.annotate_finding(
        source_model="codex/gpt-5.4",
        challenge_brief="Investigate shared login surface",
        finding="Potential admin API at /api/v1/k8s/get",
        sibling_insights="Potential login bypass in JS bundle",
    )

    assert result == "Check whether the route is auth-gated."


@pytest.mark.asyncio
async def test_codex_advisor_builds_coordinator_prompt_via_query() -> None:
    advisor = CodexAdvisor("midnight-roulette")

    async def fake_query(prompt: str, *, session_key: str = "general") -> str:
        assert session_key == "coordinator"
        assert "Coordinator message draft:" in prompt
        assert "Source model: codex/gpt-5.4" in prompt
        assert "Challenge brief:" in prompt
        return "Keep the bump focused on auth gates."

    advisor._query = cast(Any, fake_query)

    result = await advisor.annotate_coordinator_message(
        source_model="codex/gpt-5.4",
        challenge_brief="Investigate shared login surface",
        message="Potential admin API route worth checking.",
        sibling_insights="Potential login bypass in JS bundle",
    )

    assert result == "Keep the bump focused on auth gates."


@pytest.mark.asyncio
async def test_codex_advisor_builds_lane_hint_prompt_via_query() -> None:
    advisor = CodexAdvisor("midnight-roulette")

    async def fake_query(prompt: str, *, session_key: str = "general") -> str:
        assert session_key == "lane-hint"
        assert "Target lane: codex/gpt-5.4" in prompt
        assert "Challenge brief:" in prompt
        assert "Current lane state:" in prompt
        assert "Findings from other lanes:" in prompt
        assert "Shared artifact manifest excerpt:" in prompt
        assert "Artifact digests and previews:" in prompt
        assert "csrf token here" in prompt
        return "Read the shared login artifact before another broad grep."

    advisor._query = cast(Any, fake_query)

    result = await advisor.suggest_lane_hint(
        target_model="codex/gpt-5.4",
        challenge_brief="Investigate shared login surface",
        lane_state="Lifecycle: idle\nLast command: grep -R auth /challenge/shared-artifacts",
        sibling_findings="Artifact path: /challenge/shared-artifacts/login.html",
        manifest_excerpt="- fact: login page artifact",
        artifact_previews="/challenge/shared-artifacts/login.html\n<html>csrf token here</html>",
    )

    assert result == "Read the shared login artifact before another broad grep."


@pytest.mark.asyncio
async def test_codex_advisor_reuses_session_per_task_family(monkeypatch: pytest.MonkeyPatch) -> None:
    lifecycle = {"created": [], "queries": [], "stopped": []}

    class FakeSession:
        def __init__(self, model: str) -> None:
            lifecycle["created"].append(model)

        async def query(self, prompt: str) -> str:
            if "Finding:" in prompt:
                lifecycle["queries"].append("finding")
                return "check auth"
            if "Current lane state:" in prompt:
                lifecycle["queries"].append("lane-hint")
                return "check artifact"
            lifecycle["queries"].append("other")
            return "noop"

        async def stop(self) -> None:
            lifecycle["stopped"].append("stop")

    monkeypatch.setattr("backend.agents.codex_advisor._CodexAdvisorySession", FakeSession)

    advisor = CodexAdvisor("midnight-roulette")
    first = await advisor.annotate_finding(
        source_model="codex/gpt-5.4",
        challenge_brief="Investigate shared login surface",
        finding="Potential admin API at /api/v1/k8s/get",
        sibling_insights="Potential login bypass in JS bundle",
    )
    second = await advisor.annotate_finding(
        source_model="codex/gpt-5.4",
        challenge_brief="Investigate shared login surface",
        finding="Potential admin API at /api/v1/k8s/get",
        sibling_insights="Potential login bypass in JS bundle",
    )
    third = await advisor.suggest_lane_hint(
        target_model="codex/gpt-5.4",
        challenge_brief="Investigate shared login surface",
        lane_state="Lifecycle: idle",
        sibling_findings="Artifact path: /challenge/shared-artifacts/login.html",
        manifest_excerpt="- fact: login page artifact",
        artifact_previews="/challenge/shared-artifacts/login.html\n<html>csrf token here</html>",
    )

    assert first == "check auth"
    assert second == "check auth"
    assert third == "check artifact"
    assert lifecycle["created"] == ["gpt-5.4-mini", "gpt-5.4-mini"]
    assert lifecycle["queries"] == ["finding", "finding", "lane-hint"]


@pytest.mark.asyncio
async def test_claude_advisor_reuses_persistent_client(monkeypatch: pytest.MonkeyPatch) -> None:
    lifecycle = {"enter": 0, "exit": 0, "queries": []}

    class FakeTextBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeAssistantMessage:
        def __init__(self, text: str) -> None:
            self.content = [FakeTextBlock(text)]

    class FakeClaudeSDKClient:
        def __init__(self, options=None, transport=None) -> None:
            self.options = options
            self.transport = transport
            self._next_text = ""

        async def __aenter__(self):
            lifecycle["enter"] += 1
            return self

        async def __aexit__(self, exc_type, exc, tb):
            lifecycle["exit"] += 1

        async def query(self, prompt: str, session_id: str = "default") -> None:
            lifecycle["queries"].append((prompt, session_id))
            self._next_text = f"reply:{session_id}"

        async def receive_response(self):
            yield FakeAssistantMessage(self._next_text)

    monkeypatch.setattr("backend.agents.claude_advisor.ClaudeSDKClient", FakeClaudeSDKClient)
    monkeypatch.setattr("backend.agents.claude_advisor.AssistantMessage", FakeAssistantMessage)
    monkeypatch.setattr("backend.agents.claude_advisor.TextBlock", FakeTextBlock)

    advisor = ClaudeAdvisor("midnight-roulette")

    first = await advisor.annotate_finding(
        source_model="codex/gpt-5.4",
        challenge_brief="Investigate shared login surface",
        finding="Potential admin API at /api/v1/k8s/get",
        sibling_insights="Potential login bypass in JS bundle",
    )
    second = await advisor.suggest_lane_hint(
        target_model="codex/gpt-5.4",
        challenge_brief="Investigate shared login surface",
        lane_state="Lifecycle: idle",
        sibling_findings="Artifact path: /challenge/shared-artifacts/login.html",
        manifest_excerpt="- fact: login page artifact",
        artifact_previews="/challenge/shared-artifacts/login.html",
    )

    assert first == "reply:finding"
    assert second == "reply:lane-hint"
    assert lifecycle["enter"] == 1
    assert lifecycle["exit"] == 0
    assert [session_id for _prompt, session_id in lifecycle["queries"]] == ["finding", "lane-hint"]


@pytest.mark.asyncio
async def test_claude_advisor_raises_on_limit_text(monkeypatch: pytest.MonkeyPatch) -> None:
    lifecycle = {"enter": 0, "exit": 0}

    class FakeTextBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeAssistantMessage:
        def __init__(self, text: str) -> None:
            self.content = [FakeTextBlock(text)]

    class FakeClaudeSDKClient:
        def __init__(self, options=None, transport=None) -> None:
            self.options = options
            self.transport = transport

        async def __aenter__(self):
            lifecycle["enter"] += 1
            return self

        async def __aexit__(self, exc_type, exc, tb):
            lifecycle["exit"] += 1

        async def query(self, prompt: str, session_id: str = "default") -> None:
            return None

        async def receive_response(self):
            yield FakeAssistantMessage("You've hit your limit · resets Apr 24, 4am (Asia/Seoul)")

    monkeypatch.setattr("backend.agents.claude_advisor.ClaudeSDKClient", FakeClaudeSDKClient)
    monkeypatch.setattr("backend.agents.claude_advisor.AssistantMessage", FakeAssistantMessage)
    monkeypatch.setattr("backend.agents.claude_advisor.TextBlock", FakeTextBlock)

    advisor = ClaudeAdvisor("midnight-roulette")

    with pytest.raises(RuntimeError, match="hit your limit"):
        await advisor.annotate_finding(
            source_model="codex/gpt-5.4",
            challenge_brief="Investigate shared login surface",
            finding="Potential admin API at /api/v1/k8s/get",
            sibling_insights="Potential login bypass in JS bundle",
        )

    assert lifecycle["enter"] == 1
    assert lifecycle["exit"] == 1
    assert advisor._session is None
