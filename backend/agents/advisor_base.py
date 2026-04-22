"""Common advisory sidecar protocol and no-op fallback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

ADVISOR_SYSTEM_PROMPT = """\
You are a strategic CTF advisory sidecar.

You do not solve the challenge directly. You only add high-signal directional
comments about the material you are shown.

Rules:
- Return plain text only.
- Prefer one compact paragraph or up to 6 concise sentences.
- If the material is not worth commenting on, return exactly: NO_ADVICE
- Focus on the single best next direction, validation, contradiction, or risk.
- When advice is warranted, say what to try next, why it matters now, and what
  result would confirm or falsify the idea.
- Point to exact routes, files, artifacts, or commands when possible.
- Do not restate the whole finding/message.
- Do not call tools.
"""

ADVISOR_MAX_RESPONSE_CHARS = 1200


@dataclass
class CandidateReview:
    decision: str = "insufficient"
    note: str = ""


def _advisor_sections(*pairs: tuple[str, str]) -> str:
    parts: list[str] = []
    for title, body in pairs:
        parts.extend([title, body, ""])
    return "\n".join(parts[:-1])


def build_finding_annotation_prompt(
    *,
    challenge_name: str,
    source_model: str,
    challenge_brief: str,
    finding: str,
    sibling_insights: str,
) -> str:
    return "\n".join(
        [
            f"Challenge: {challenge_name}",
            f"Source model: {source_model}",
            "",
            _advisor_sections(
                ("Challenge brief:", challenge_brief.strip() or "No challenge brief available."),
                ("Finding:", finding.strip() or "(empty)"),
                ("Sibling insights:", sibling_insights.strip() or "No sibling insights available yet."),
            ),
            "Add an advisory comment only if it materially improves next action quality.",
            "Prefer a concrete next move, why it matters now, and what output would validate it.",
        ]
    )


def build_coordinator_annotation_prompt(
    *,
    challenge_name: str,
    source_model: str,
    challenge_brief: str,
    message: str,
    sibling_insights: str,
) -> str:
    return "\n".join(
        [
            f"Challenge: {challenge_name}",
            f"Source model: {source_model}",
            "",
            _advisor_sections(
                ("Challenge brief:", challenge_brief.strip() or "No challenge brief available."),
                ("Coordinator message draft:", message.strip() or "(empty)"),
                ("Sibling insights:", sibling_insights.strip() or "No sibling insights available yet."),
            ),
            "Add an advisory comment only if it changes what the coordinator should think or do.",
            "Prefer prioritization, a concrete handoff direction, or the next check the coordinator should push.",
        ]
    )


def build_lane_hint_prompt(
    *,
    challenge_name: str,
    target_model: str,
    challenge_brief: str,
    lane_state: str,
    sibling_findings: str,
    manifest_excerpt: str,
    artifact_previews: str,
) -> str:
    return "\n".join(
        [
            f"Challenge: {challenge_name}",
            f"Target lane: {target_model}",
            "",
            _advisor_sections(
                ("Challenge brief:", challenge_brief.strip() or "No challenge brief available."),
                ("Current lane state:", lane_state.strip() or "(empty)"),
                ("Findings from other lanes:", sibling_findings.strip() or "No sibling findings available yet."),
                ("Shared artifact manifest excerpt:", manifest_excerpt.strip() or "No manifest excerpt available."),
                ("Artifact digests and previews:", artifact_previews.strip() or "No artifact digests or previews available."),
            ),
            "Add a private directional note for this lane only if it materially improves this lane's next move.",
            "Prefer a concrete next-step plan over a summary.",
            "Name exact routes, files, artifacts, or commands when possible, and say what result would confirm or refute the hypothesis.",
            "Prefer a different angle from already-covered evidence, not a team-wide summary.",
        ]
    )


def build_flag_candidate_review_prompt(
    *,
    challenge_name: str,
    source_model: str,
    challenge_brief: str,
    flag: str,
    evidence: str,
    sibling_insights: str,
) -> str:
    return "\n".join(
        [
            f"Challenge: {challenge_name}",
            f"Source model: {source_model}",
            "",
            _advisor_sections(
                ("Challenge brief:", challenge_brief.strip() or "No challenge brief available."),
                ("Candidate flag:", flag.strip() or "(empty)"),
                ("Evidence:", evidence.strip() or "No evidence provided."),
                ("Sibling insights:", sibling_insights.strip() or "No sibling insights available yet."),
            ),
            "Return strict JSON only:",
            '{"decision":"likely|unlikely|insufficient","note":"short reason"}',
        ]
    )


class AdvisorProtocol(Protocol):
    """Provider-specific advisory sidecars implement this interface."""

    async def annotate_finding(
        self,
        *,
        source_model: str,
        challenge_brief: str,
        finding: str,
        sibling_insights: str,
    ) -> str: ...

    async def annotate_coordinator_message(
        self,
        *,
        source_model: str,
        challenge_brief: str,
        message: str,
        sibling_insights: str,
    ) -> str: ...

    async def suggest_lane_hint(
        self,
        *,
        target_model: str,
        challenge_brief: str,
        lane_state: str,
        sibling_findings: str,
        manifest_excerpt: str,
        artifact_previews: str,
    ) -> str: ...

    async def review_flag_candidate(
        self,
        *,
        source_model: str,
        challenge_brief: str,
        flag: str,
        evidence: str,
        sibling_insights: str,
    ) -> CandidateReview: ...


class NoopAdvisor:
    """Safe fallback when no provider-specific advisor is available."""

    async def annotate_finding(
        self,
        *,
        source_model: str,
        challenge_brief: str,
        finding: str,
        sibling_insights: str,
    ) -> str:
        return ""

    async def annotate_coordinator_message(
        self,
        *,
        source_model: str,
        challenge_brief: str,
        message: str,
        sibling_insights: str,
    ) -> str:
        return ""

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
        return ""

    async def review_flag_candidate(
        self,
        *,
        source_model: str,
        challenge_brief: str,
        flag: str,
        evidence: str,
        sibling_insights: str,
    ) -> CandidateReview:
        return CandidateReview()
