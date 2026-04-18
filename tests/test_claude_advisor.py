from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.agents.codex_advisor import CodexAdvisor
from backend.agents.swarm import ChallengeSwarm
from backend.cli import _validate_runtime_auth
from backend.cost_tracker import CostTracker


class _FakeAdvisor:
    def __init__(self, finding_reply: str = "", coordinator_reply: str = "", lane_reply: str = "") -> None:
        self.finding_reply = finding_reply
        self.coordinator_reply = coordinator_reply
        self.lane_reply = lane_reply
        self.finding_calls: list[dict[str, str]] = []
        self.coordinator_calls: list[dict[str, str]] = []
        self.lane_calls: list[dict[str, str]] = []

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
        meta=SimpleNamespace(
            name="advisor-test",
            category="web",
            value=300,
            description="Investigate the shared login surface and recover the real flag.",
            connection_info="https://ctf.example/challenge",
            hints=[{"content": "The admin route is not linked from the main page."}],
        ),
        ctfd=object(),  # type: ignore[arg-type]
        cost_tracker=CostTracker(),
        settings=object(),  # type: ignore[arg-type]
        model_specs=["codex/gpt-5.4"],
        coordinator_inbox=asyncio.Queue(),
    )


def test_validate_runtime_auth_rejects_claude_solver_specs() -> None:
    with pytest.raises(ValueError, match="Claude solver lanes are disabled"):
        _validate_runtime_auth(
            SimpleNamespace(use_home_auth=False),
            ["claude-sdk/claude-opus-4-6/medium"],
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
    swarm.solvers["codex/gpt-5.4"] = _FakeLaneSolver()
    swarm.solvers["gemini/gemini-2.5-flash"] = _FakeLaneSolver()
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
    await swarm.message_bus.post("gemini/gemini-2.5-flash", "Artifact path: /challenge/shared-artifacts/login.html")
    await swarm.message_bus.post("codex/gpt-5.4-mini", "Potential CSRF token at /challenge/shared-artifacts/login.html")

    await swarm._maybe_issue_lane_advisories()

    unread = await swarm.message_bus.check("codex/gpt-5.4")
    assert [finding.content for finding in unread] == [
        "Artifact path: /challenge/shared-artifacts/login.html",
        "Potential CSRF token at /challenge/shared-artifacts/login.html",
    ]
    assert swarm.last_advisor_note == "Pivot from broad grep to the shared login artifact first."
    assert swarm.lane_advisor_notes["codex/gpt-5.4"] == swarm.last_advisor_note
    assert swarm.advisor_lane_hint_count == 1
    assert not swarm.coordinator_inbox.qsize()
    assert not swarm.solvers["codex/gpt-5.4"].bumped
    assert swarm.solvers["codex/gpt-5.4"].advisory_bumped
    assert (
        "Private advisor note for this lane"
        in swarm.solvers["codex/gpt-5.4"].advisory_bumped[0]
    )
    assert advisor.lane_calls
    assert "The admin route is not linked" in advisor.lane_calls[0]["challenge_brief"]
    assert "/challenge/shared-artifacts/.advisor/login.html-" in advisor.lane_calls[0]["artifact_previews"]
    assert "# Artifact Digest" in advisor.lane_calls[0]["artifact_previews"]
    assert "token-123" in advisor.lane_calls[0]["artifact_previews"]


@pytest.mark.asyncio
async def test_lane_listener_deduplicates_same_context(tmp_path) -> None:
    swarm = _make_swarm(tmp_path)
    advisor = _FakeAdvisor(lane_reply="Read the shared bundle before another broad search.")
    swarm._advisors["codex"] = advisor
    swarm.solvers["codex/gpt-5.4"] = _FakeLaneSolver()
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
    await swarm.message_bus.post("gemini/gemini-2.5-flash", "Artifact path: /challenge/shared-artifacts/app.js")
    await swarm.message_bus.post("codex/gpt-5.4-mini", "Artifact path: /challenge/shared-artifacts/app.js")

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
    swarm.solvers["codex/gpt-5.4"] = _FakeLaneSolver()
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
    await swarm.message_bus.post("gemini/gemini-2.5-flash", "Potential CSRF token in shared login page")
    await swarm.message_bus.post("codex/gpt-5.4-mini", "Repeated auth failure after broad grep")

    await swarm._maybe_issue_lane_advisories()

    assert advisor.lane_calls
    assert advisor.lane_calls[0]["artifact_previews"] == ""
    assert advisor.lane_calls[0]["manifest_excerpt"]
    assert swarm.solvers["codex/gpt-5.4"].advisory_bumped


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


def test_get_advisor_routes_codex_and_gemini_to_separate_backends(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
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

    assert swarm._get_advisor("codex/gpt-5.4") is codex_advisor
    assert swarm._get_advisor("gemini/gemini-2.5-flash") is claude_advisor


@pytest.mark.asyncio
async def test_codex_advisor_builds_finding_prompt_via_query() -> None:
    advisor = CodexAdvisor("midnight-roulette")

    async def fake_query(prompt: str) -> str:
        assert "Challenge: midnight-roulette" in prompt
        assert "Source model: codex/gpt-5.4" in prompt
        assert "Challenge brief:" in prompt
        assert "Investigate shared login surface" in prompt
        assert "Sibling insights:" in prompt
        return "Check whether the route is auth-gated."

    advisor._query = fake_query  # type: ignore[method-assign]

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

    async def fake_query(prompt: str) -> str:
        assert "Coordinator message draft:" in prompt
        assert "Source model: codex/gpt-5.4" in prompt
        assert "Challenge brief:" in prompt
        return "Keep the bump focused on auth gates."

    advisor._query = fake_query  # type: ignore[method-assign]

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

    async def fake_query(prompt: str) -> str:
        assert "Target lane: codex/gpt-5.4" in prompt
        assert "Challenge brief:" in prompt
        assert "Current lane state:" in prompt
        assert "Findings from other lanes:" in prompt
        assert "Shared artifact manifest excerpt:" in prompt
        assert "Artifact digests and previews:" in prompt
        assert "csrf token here" in prompt
        return "Read the shared login artifact before another broad grep."

    advisor._query = fake_query  # type: ignore[method-assign]

    result = await advisor.suggest_lane_hint(
        target_model="codex/gpt-5.4",
        challenge_brief="Investigate shared login surface",
        lane_state="Lifecycle: idle\nLast command: grep -R auth /challenge/shared-artifacts",
        sibling_findings="Artifact path: /challenge/shared-artifacts/login.html",
        manifest_excerpt="- fact: login page artifact",
        artifact_previews="/challenge/shared-artifacts/login.html\n<html>csrf token here</html>",
    )

    assert result == "Read the shared login artifact before another broad grep."
