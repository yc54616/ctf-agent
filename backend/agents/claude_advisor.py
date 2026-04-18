"""Best-effort Claude advisory sidecar for strategic findings and coordinator notes."""

from __future__ import annotations

import logging

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
)

from backend.auth import AuthValidationError, validate_claude_auth

logger = logging.getLogger(__name__)

ADVISOR_MODEL = "claude-opus-4-6"
ADVISOR_SYSTEM_PROMPT = """\
You are a strategic CTF advisory sidecar.

You do not solve the challenge directly. You only add high-signal directional
comments about the material you are shown.

Rules:
- Return plain text only.
- Prefer one compact paragraph or up to 6 concise sentences.
- If the material is not worth commenting on, return exactly: NO_ADVICE
- Focus on the best next direction, validation, contradiction, risk, or the
  single most useful next check.
- When advice is warranted, say what to try next, why it matters now, and what
  result would confirm or falsify the idea.
- Point to exact routes, files, artifacts, or commands when possible.
- Do not restate the whole finding/message.
"""

ADVISOR_MAX_RESPONSE_CHARS = 1200


class ClaudeAdvisor:
    """Best-effort strategic Claude reviewer."""

    def __init__(self, challenge_name: str) -> None:
        self.challenge_name = challenge_name

    @classmethod
    def maybe_create(cls, settings: object, challenge_name: str) -> ClaudeAdvisor | None:
        try:
            validate_claude_auth(settings)  # type: ignore[arg-type]
        except AuthValidationError as exc:
            logger.debug("Claude advisor disabled for %s: %s", challenge_name, exc)
            return None
        except Exception as exc:
            logger.warning("Claude advisor setup failed for %s: %s", challenge_name, exc)
            return None
        return cls(challenge_name)

    async def annotate_finding(
        self,
        *,
        source_model: str,
        challenge_brief: str,
        finding: str,
        sibling_insights: str,
    ) -> str:
        prompt = "\n".join(
            [
                f"Challenge: {self.challenge_name}",
                f"Source model: {source_model}",
                "",
                "Challenge brief:",
                challenge_brief.strip() or "No challenge brief available.",
                "",
                "Finding:",
                finding.strip() or "(empty)",
                "",
                "Sibling insights:",
                sibling_insights.strip() or "No sibling insights available yet.",
                "",
                "Add an advisory comment only if it materially improves next action quality.",
                "Prefer a concrete next move, why it matters, and what output would validate it.",
            ]
        )
        return await self._query(prompt)

    async def annotate_coordinator_message(
        self,
        *,
        source_model: str,
        challenge_brief: str,
        message: str,
        sibling_insights: str,
    ) -> str:
        prompt = "\n".join(
            [
                f"Challenge: {self.challenge_name}",
                f"Source model: {source_model}",
                "",
                "Challenge brief:",
                challenge_brief.strip() or "No challenge brief available.",
                "",
                "Coordinator message draft:",
                message.strip() or "(empty)",
                "",
                "Sibling insights:",
                sibling_insights.strip() or "No sibling insights available yet.",
                "",
                "Add an advisory comment only if it changes what the coordinator should think or do.",
                "Prefer prioritization, a concrete handoff direction, or the next check the coordinator should push.",
            ]
        )
        return await self._query(prompt)

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
        prompt = "\n".join(
            [
                f"Challenge: {self.challenge_name}",
                f"Target lane: {target_model}",
                "",
                "Challenge brief:",
                challenge_brief.strip() or "No challenge brief available.",
                "",
                "Current lane state:",
                lane_state.strip() or "(empty)",
                "",
                "Findings from other lanes:",
                sibling_findings.strip() or "No sibling findings available yet.",
                "",
                "Shared artifact manifest excerpt:",
                manifest_excerpt.strip() or "No manifest excerpt available.",
                "",
                "Artifact digests and previews:",
                artifact_previews.strip() or "No artifact digests or previews available.",
                "",
                "Add a private directional note for this lane only if it materially improves this lane's next move.",
                "Prefer a concrete next-step plan over a summary.",
                "Name exact routes, files, artifacts, or commands when possible, and say what result would confirm or refute the hypothesis.",
                "Prefer a different angle from already-covered evidence, not a team-wide summary.",
            ]
        )
        return await self._query(prompt)

    async def _query(self, prompt: str) -> str:
        options = ClaudeAgentOptions(
            model=ADVISOR_MODEL,
            system_prompt=ADVISOR_SYSTEM_PROMPT,
            env={"CLAUDECODE": ""},
            allowed_tools=[],
            permission_mode="bypassPermissions",
        )

        parts: list[str] = []
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            text = block.text.strip()
                            if text:
                                parts.append(text)

        text = " ".join(parts).strip()
        if not text or text == "NO_ADVICE":
            return ""
        if "NO_ADVICE" in text:
            return ""
        return text[:ADVISOR_MAX_RESPONSE_CHARS]
