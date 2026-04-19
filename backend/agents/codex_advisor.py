"""Best-effort Codex advisory sidecar for strategic finding/message review."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any, cast

from backend.agents.advisor_base import (
    ADVISOR_MAX_RESPONSE_CHARS,
    ADVISOR_SYSTEM_PROMPT,
    CandidateReview,
    build_coordinator_annotation_prompt,
    build_finding_annotation_prompt,
    build_flag_candidate_review_prompt,
    build_lane_hint_prompt,
)
from backend.auth import AuthValidationError, validate_codex_auth
from backend.config import Settings

logger = logging.getLogger(__name__)

ADVISOR_MODEL = "gpt-5.4-mini"
ADVISOR_TIMEOUT_SECONDS = 45.0

_rpc_counter = itertools.count(1)


class _CodexAdvisorySession:
    def __init__(self, model: str) -> None:
        self.model = model
        self._proc: asyncio.subprocess.Process | None = None
        self._thread_id: str | None = None
        self._pending_responses: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task | None = None
        self._turn_done = asyncio.Event()
        self._turn_error: str | None = None
        self._parts: list[str] = []

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            "codex",
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        await self._rpc(
            "initialize",
            {
                "clientInfo": {"name": "ctf-advisor", "version": "1.0.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self._send_notification("initialized", {})
        response = await self._rpc(
            "thread/start",
            {
                "model": self.model,
                "personality": "pragmatic",
                "baseInstructions": ADVISOR_SYSTEM_PROMPT,
                "cwd": ".",
                "approvalPolicy": "on-request",
                "sandbox": "read-only",
                "dynamicTools": [],
            },
        )
        self._thread_id = response.get("result", {}).get("thread", {}).get("id", "")

    async def query(self, prompt: str) -> str:
        if not self._proc or not self._thread_id:
            await self.start()

        self._turn_done.clear()
        self._turn_error = None
        self._parts = []

        await self._rpc(
            "turn/start",
            {
                "threadId": self._thread_id,
                "input": [{"type": "text", "text": prompt}],
            },
        )
        await asyncio.wait_for(self._turn_done.wait(), timeout=ADVISOR_TIMEOUT_SECONDS)
        if self._turn_error:
            raise RuntimeError(self._turn_error)
        return " ".join(self._parts).strip()

    async def stop(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:
                self._proc.kill()
            self._proc = None

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self._proc and self._proc.stdin
        msg_id = next(_rpc_counter)
        msg: dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            msg["params"] = params
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_responses[msg_id] = future
        self._proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self._proc.stdin.drain()
        try:
            return await asyncio.wait_for(future, timeout=ADVISOR_TIMEOUT_SECONDS)
        finally:
            self._pending_responses.pop(msg_id, None)

    async def _respond_to_request(self, request_id: int, result: Any) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write((json.dumps({"id": request_id, "result": result}) + "\n").encode())
        await self._proc.stdin.drain()

    async def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        assert self._proc and self._proc.stdin
        payload: dict[str, Any] = {"method": method}
        if params:
            payload["params"] = params
        self._proc.stdin.write((json.dumps(payload) + "\n").encode())
        await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                self._turn_done.set()
                return
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_id = msg.get("id")
            if msg_id is not None and ("result" in msg or "error" in msg):
                future = self._pending_responses.pop(msg_id, None)
                if future and not future.done():
                    if "error" in msg:
                        future.set_exception(RuntimeError(f"Codex RPC error: {msg['error']}"))
                    else:
                        future.set_result(msg)
                continue

            method = msg.get("method", "")
            params = msg.get("params", {})

            if method == "item/tool/call" and msg_id is not None:
                await self._respond_to_request(
                    msg_id,
                    {
                        "contentItems": [{"type": "inputText", "text": "Advisory sidecar cannot use tools."}],
                        "success": False,
                    },
                )
                continue

            if method == "item/completed":
                item = params.get("item", params)
                if item.get("type") == "agentMessage":
                    text = str(item.get("text", "")).strip()
                    if text:
                        self._parts.append(text)
                continue

            if method == "turn/completed":
                turn = params.get("turn", {})
                if turn.get("status") == "failed":
                    self._turn_error = str(turn.get("error", "unknown"))
                self._turn_done.set()
                continue


class CodexAdvisor:
    """Best-effort strategic Codex reviewer."""

    def __init__(self, challenge_name: str, model: str = ADVISOR_MODEL) -> None:
        self.challenge_name = challenge_name
        self.model = model

    @classmethod
    def maybe_create(cls, settings: object, challenge_name: str) -> CodexAdvisor | None:
        try:
            validate_codex_auth(cast(Settings, settings))
        except AuthValidationError as exc:
            logger.debug("Codex advisor disabled for %s: %s", challenge_name, exc)
            return None
        except Exception as exc:
            logger.warning("Codex advisor setup failed for %s: %s", challenge_name, exc)
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
        return await self._query(prompt)

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
        prompt = build_lane_hint_prompt(
            challenge_name=self.challenge_name,
            target_model=target_model,
            challenge_brief=challenge_brief,
            lane_state=lane_state,
            sibling_findings=sibling_findings,
            manifest_excerpt=manifest_excerpt,
            artifact_previews=artifact_previews,
        )
        return await self._query(prompt)

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
        raw = await self._query(prompt)
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

    async def _query(self, prompt: str) -> str:
        session = _CodexAdvisorySession(self.model)
        try:
            text = await session.query(prompt)
        finally:
            await session.stop()

        text = " ".join(text.split()).strip()
        if not text or text == "NO_ADVICE" or "NO_ADVICE" in text:
            return ""
        return text[:ADVISOR_MAX_RESPONSE_CHARS]
