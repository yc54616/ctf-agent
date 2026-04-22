"""Dreamhack recruitment CTF runtime client."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from backend.challenge_config import infer_connection, render_connection_info
from backend.platforms.base import SubmitResult
from backend.platforms.catalog import platform_source_defaults
from backend.prompts import infer_flag_guard_from_texts

DREAMHACK_API_BASE_URL = "https://dreamhack.io/api"
_USER_AGENT = "Mozilla/5.0 (compatible; ctf-agent runtime)"


def _dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _extract_message(payload: object) -> str:
    if isinstance(payload, list):
        return "; ".join(str(item).strip() for item in payload if str(item).strip())
    payload_dict = _dict(payload)
    if payload_dict:
        for key in ("flag", "non_field_errors", "detail", "message"):
            value = payload_dict.get(key)
            message = _extract_message(value)
            if message:
                return message
    text = str(payload or "").strip()
    return text


@dataclass
class DreamhackClient:
    competition_slug: str
    applicant_id: str
    cookie_header: str
    competition_title: str = ""
    competition_url: str = ""

    platform: str = "dreamhack"
    label: str = "Dreamhack"

    _client: httpx.AsyncClient | None = field(default=None, repr=False)
    _applicant_cache: dict[str, Any] | None = field(default=None, repr=False)
    _challenge_ids: dict[str, int] = field(default_factory=dict, repr=False)
    _solved_names_cache: set[str] = field(default_factory=set, repr=False)

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=DREAMHACK_API_BASE_URL,
                follow_redirects=True,
                timeout=30.0,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Cookie": self.cookie_header,
                },
            )
        return self._client

    async def _get_json(self, path: str) -> dict[str, Any]:
        client = await self._ensure_client()
        response = await client.get(path)
        response.raise_for_status()
        payload = response.json()
        return _dict(payload)

    async def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        client = await self._ensure_client()
        response = await client.post(path, json=body)
        response.raise_for_status()
        payload = response.json()
        return _dict(payload)

    async def _fetch_applicant(self, *, force: bool = False) -> dict[str, Any]:
        if self._applicant_cache is not None and not force:
            return dict(self._applicant_cache)
        payload = await self._get_json(
            f"/v1/career/recruitment-applicants/{self.applicant_id}/"
        )
        self._applicant_cache = payload
        self._challenge_ids = {}
        self._solved_names_cache = set()
        for challenge in payload.get("ctf_challenges", []) or []:
            if not isinstance(challenge, dict):
                continue
            title = str(challenge.get("title") or "").strip()
            if not title:
                continue
            challenge_id = challenge.get("id")
            if isinstance(challenge_id, int):
                self._challenge_ids[title] = challenge_id
            if challenge.get("is_solved"):
                self._solved_names_cache.add(title)
        return dict(payload)

    def _writeup_ids_by_challenge(self, applicant: dict[str, Any]) -> dict[int, str]:
        writeups: dict[int, str] = {}
        for writeup in applicant.get("writeups", []) or []:
            if not isinstance(writeup, dict):
                continue
            challenge = _dict(writeup.get("challenge"))
            challenge_id = challenge.get("id")
            if isinstance(challenge_id, int):
                writeups[challenge_id] = str(writeup.get("id") or "").strip()
        return writeups

    def _challenge_metadata(self, challenge: dict[str, Any], applicant: dict[str, Any]) -> dict[str, Any]:
        platform_defaults = platform_source_defaults(self.platform)
        title = str(challenge.get("title") or "").strip() or f"challenge-{challenge.get('id', '?')}"
        description = str(challenge.get("description") or "").strip()
        tags = [
            str(tag).strip()
            for tag in (challenge.get("tags") or [])
            if str(tag).strip()
        ]
        category = tags[0] if tags else ""
        connection = infer_connection(description)
        flag_format, flag_regex = infer_flag_guard_from_texts(description)
        challenge_id = challenge.get("id")
        challenge_id_value = challenge_id if isinstance(challenge_id, int) else None
        writeups = self._writeup_ids_by_challenge(applicant)
        source_payload: dict[str, Any] = {
            **platform_defaults,
            "competition": {
                "slug": self.competition_slug,
                "title": self.competition_title,
                "url": self.competition_url,
            },
            "applicant_id": self.applicant_id,
            "challenge_id": challenge_id_value,
            "challenge_url": (
                f"{self.competition_url}#challenge-{challenge_id_value}"
                if self.competition_url and challenge_id_value is not None
                else self.competition_url
            ),
            "order": str(challenge.get("order") or "").strip(),
            "public_url": str(challenge.get("public") or "").strip(),
            "needs_vm": bool(challenge.get("needs_vm")),
            "status": {
                "solved": bool(challenge.get("is_solved")),
                "writeup_submitted": challenge_id_value in writeups if challenge_id_value is not None else False,
            },
        }
        if challenge_id_value is not None and challenge_id_value in writeups:
            source_payload["writeup_id"] = writeups[challenge_id_value]

        metadata: dict[str, Any] = {
            "name": title,
            "category": category,
            "description": description,
            "value": 0,
            "tags": tags,
            "connection_info": render_connection_info(connection),
            "solves": 0,
            "flag_format": flag_format,
            "flag_regex": flag_regex,
            "source": source_payload,
        }
        if connection:
            metadata["connection"] = connection
        return metadata

    async def fetch_challenge_stubs(self) -> list[dict[str, Any]]:
        challenges = await self.fetch_all_challenges()
        stubs: list[dict[str, Any]] = []
        for challenge in challenges:
            stubs.append(
                {
                    "name": challenge.get("name", ""),
                    "category": challenge.get("category", ""),
                    "value": challenge.get("value", 0),
                    "solves": challenge.get("solves", 0),
                    "source": self.platform,
                }
            )
        return stubs

    async def fetch_all_challenges(self) -> list[dict[str, Any]]:
        applicant = await self._fetch_applicant()
        challenges: list[dict[str, Any]] = []
        for challenge in applicant.get("ctf_challenges", []) or []:
            if isinstance(challenge, dict):
                challenges.append(self._challenge_metadata(challenge, applicant))
        return challenges

    async def fetch_solved_names(self) -> set[str]:
        try:
            await self._fetch_applicant(force=True)
        except Exception:
            return set(self._solved_names_cache)
        return set(self._solved_names_cache)

    async def submit_flag(self, challenge_name: str, flag: str) -> SubmitResult:
        normalized_flag = str(flag or "").strip()
        if not normalized_flag:
            return SubmitResult("unknown", "", "Empty flag — nothing to submit.")

        applicant = await self._fetch_applicant()
        if challenge_name in self._solved_names_cache:
            return SubmitResult(
                "already_solved",
                "",
                f'ALREADY SOLVED — "{challenge_name}" is already marked solved on Dreamhack.',
            )

        challenge_id = self._challenge_ids.get(challenge_name)
        if challenge_id is None:
            for challenge in applicant.get("ctf_challenges", []) or []:
                if not isinstance(challenge, dict):
                    continue
                if str(challenge.get("title") or "").strip() == str(challenge_name or "").strip():
                    raw_id = challenge.get("id")
                    if isinstance(raw_id, int):
                        challenge_id = raw_id
                        self._challenge_ids[challenge_name] = raw_id
                    break
        if challenge_id is None:
            raise RuntimeError(f'Dreamhack challenge "{challenge_name}" is not available for this applicant.')

        try:
            payload = await self._post_json(
                f"/v1/career/recruitment-ctf-challenges/{challenge_id}/submit/",
                {"flag": normalized_flag},
            )
        except httpx.HTTPStatusError as exc:
            message = ""
            try:
                message = _extract_message(exc.response.json())
            except Exception:
                message = exc.response.text.strip() if exc.response is not None else ""
            if challenge_name in await self.fetch_solved_names():
                return SubmitResult(
                    "already_solved",
                    message,
                    f'ALREADY SOLVED — "{challenge_name}" is already marked solved on Dreamhack.',
                )
            detail = message or "Dreamhack rejected the submission."
            return SubmitResult(
                "incorrect",
                detail,
                f'INCORRECT — "{normalized_flag}" rejected on Dreamhack. {detail}'.strip(),
            )

        if bool(payload.get("is_correct")):
            self._solved_names_cache.add(challenge_name)
            return SubmitResult(
                "correct",
                "",
                f'CORRECT — "{normalized_flag}" accepted on Dreamhack.',
            )

        detail = _extract_message(payload) or "Dreamhack rejected the submission."
        return SubmitResult(
            "incorrect",
            detail,
            f'INCORRECT — "{normalized_flag}" rejected on Dreamhack. {detail}'.strip(),
        )

    async def pull_challenge(self, challenge: dict[str, Any], output_dir: str) -> str:
        raise RuntimeError(
            "Dreamhack challenges must be imported locally with `ctf-import` before solving."
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
