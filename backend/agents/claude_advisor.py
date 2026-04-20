"""Best-effort Claude advisory sidecar for strategic findings and coordinator notes."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
)

from backend.agents.advisor_base import (
    ADVISOR_MAX_RESPONSE_CHARS,
    ADVISOR_SYSTEM_PROMPT,
    CandidateReview,
    build_coordinator_annotation_prompt,
    build_finding_annotation_prompt,
    build_flag_candidate_review_prompt,
    build_lane_hint_prompt,
)
from backend.auth import AuthValidationError, validate_claude_auth
from backend.config import Settings

logger = logging.getLogger(__name__)

ADVISOR_MODEL = "claude-sonnet-4-6"


def _claude_limit_reason(text: str) -> str | None:
    normalized = " ".join(text.lower().split())
    if any(
        needle in normalized
        for needle in (
            "you've hit your limit",
            "you have hit your limit",
            "usage limit",
            "rate limit",
            "rate-limit",
            "too many requests",
            "try again at",
            "quota",
            "resets ",
        )
    ):
        return text.strip()[:500]
    return None


class _ClaudeAdvisorySession:
    def __init__(self) -> None:
        self._client: ClaudeSDKClient | None = None

    async def query(self, prompt: str, *, session_id: str) -> str:
        client = await self._ensure_client()
        parts: list[str] = []
        await client.query(prompt, session_id=session_id)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text = block.text.strip()
                        if text:
                            parts.append(text)
        return " ".join(parts).strip()

    async def stop(self) -> None:
        if self._client is None:
            return
        client = self._client
        self._client = None
        await client.__aexit__(None, None, None)

    async def _ensure_client(self) -> ClaudeSDKClient:
        if self._client is not None:
            return self._client
        options = ClaudeAgentOptions(
            model=ADVISOR_MODEL,
            system_prompt=ADVISOR_SYSTEM_PROMPT,
            env={"CLAUDECODE": ""},
            allowed_tools=[],
            permission_mode="bypassPermissions",
        )
        client = ClaudeSDKClient(options=options)
        self._client = await client.__aenter__()
        return self._client


class ClaudeAdvisor:
    """Best-effort strategic Claude reviewer."""

    def __init__(self, challenge_name: str) -> None:
        self.challenge_name = challenge_name
        self._session: _ClaudeAdvisorySession | None = None
        self._session_lock = asyncio.Lock()

    @classmethod
    def maybe_create(cls, settings: object, challenge_name: str) -> ClaudeAdvisor | None:
        try:
            validate_claude_auth(cast(Settings, settings))
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
        prompt = build_finding_annotation_prompt(
            challenge_name=self.challenge_name,
            source_model=source_model,
            challenge_brief=challenge_brief,
            finding=finding,
            sibling_insights=sibling_insights,
        )
        return await self._query(prompt, session_id="finding")

    async def annotate_coordinator_message(
        self,
        *,
        source_model: str,
        challenge_brief: str,
        message: str,
        sibling_insights: str,
    ) -> str:
        prompt = build_coordinator_annotation_prompt(
            challenge_name=self.challenge_name,
            source_model=source_model,
            challenge_brief=challenge_brief,
            message=message,
            sibling_insights=sibling_insights,
        )
        return await self._query(prompt, session_id="coordinator")

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
        prompt = build_lane_hint_prompt(
            challenge_name=self.challenge_name,
            target_model=target_model,
            challenge_brief=challenge_brief,
            lane_state=lane_state,
            sibling_findings=sibling_findings,
            manifest_excerpt=manifest_excerpt,
            artifact_previews=artifact_previews,
        )
        return await self._query(prompt, session_id="lane-hint")

    async def _query(self, prompt: str, *, session_id: str = "general") -> str:
        async with self._session_lock:
            session = self._session
            if session is None:
                session = _ClaudeAdvisorySession()
                self._session = session
            try:
                text = await session.query(prompt, session_id=session_id)
            except Exception:
                await session.stop()
                if self._session is session:
                    self._session = None
                raise
            limit_reason = _claude_limit_reason(text)
            if limit_reason:
                await session.stop()
                if self._session is session:
                    self._session = None
                raise RuntimeError(limit_reason)

        if not text or text == "NO_ADVICE":
            return ""
        if "NO_ADVICE" in text:
            return ""
        return text[:ADVISOR_MAX_RESPONSE_CHARS]

    async def review_flag_candidate(
        self,
        *,
        source_model: str,
        challenge_brief: str,
        flag: str,
        evidence: str,
        sibling_insights: str,
    ) -> CandidateReview:
        prompt = build_flag_candidate_review_prompt(
            challenge_name=self.challenge_name,
            source_model=source_model,
            challenge_brief=challenge_brief,
            flag=flag,
            evidence=evidence,
            sibling_insights=sibling_insights,
        )
        raw = await self._query(prompt, session_id="candidate-review")
        if not raw:
            return CandidateReview()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            lowered = raw.lower()
            if "unlikely" in lowered or "incorrect" in lowered:
                return CandidateReview("unlikely", raw[:ADVISOR_MAX_RESPONSE_CHARS])
            if "likely" in lowered or "plausible" in lowered:
                return CandidateReview("likely", raw[:ADVISOR_MAX_RESPONSE_CHARS])
            return CandidateReview("insufficient", raw[:ADVISOR_MAX_RESPONSE_CHARS])
        decision = str(payload.get("decision", "insufficient")).strip().lower()
        if decision not in {"likely", "unlikely", "insufficient"}:
            decision = "insufficient"
        note = str(payload.get("note", "")).strip()[:ADVISOR_MAX_RESPONSE_CHARS]
        return CandidateReview(decision, note)
