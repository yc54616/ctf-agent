"""Generic browser-session-backed remote platform client."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from backend.browser_sessions import (
    ensure_playwright_import_auth,
    load_cookie_header_from_session_ref,
)
from backend.platforms.base import SubmitResult


def _dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _path_get(payload: object, dotted_path: str) -> Any:
    current: Any = payload
    for part in (segment for segment in str(dotted_path or "").split(".") if segment):
        if isinstance(current, dict):
            current = _dict(current).get(part)
            continue
        return None
    return current


def _extract_message(payload: object) -> str:
    if isinstance(payload, list):
        return "; ".join(str(item).strip() for item in payload if str(item).strip())
    payload_dict = _dict(payload)
    if payload_dict:
        for key in ("flag", "non_field_errors", "detail", "message", "error"):
            message = _extract_message(payload_dict.get(key))
            if message:
                return message
    return str(payload or "").strip()


def _render_body(template: object, substitutions: Mapping[str, object]) -> Any:
    if isinstance(template, dict):
        return {
            str(key): _render_body(value, substitutions)
            for key, value in template.items()
        }
    if isinstance(template, list):
        return [_render_body(item, substitutions) for item in template]
    if isinstance(template, str):
        rendered = template
        for key, value in substitutions.items():
            rendered = rendered.replace(f"{{{key}}}", str(value))
        return rendered
    return template


@dataclass
class BrowserPlatformClient:
    platform: str
    label: str
    competition_url: str
    session_ref: str
    profile_ref: str
    challenge_hints: list[dict[str, Any]] = field(default_factory=list)

    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)
    _cookie_header: str = field(default="", init=False, repr=False)

    async def _ensure_cookie_header(self, *, refresh: bool = False) -> str:
        if refresh:
            self._cookie_header = ""
        if self._cookie_header:
            return self._cookie_header
        cookie_header, _path = load_cookie_header_from_session_ref(self.session_ref, url=self.competition_url)
        if not cookie_header:
            auth = await ensure_playwright_import_auth(self.competition_url, session_ref=self.session_ref)
            self.session_ref = auth.session_ref or self.session_ref
            cookie_header = auth.cookie_header
        self._cookie_header = str(cookie_header or "").strip()
        return self._cookie_header

    def _profile(self) -> dict[str, Any]:
        from backend.automation_profile import load_automation_profile

        return load_automation_profile(self.profile_ref)

    def _api_base_url(self, profile: dict[str, Any]) -> str:
        api_base_url = str(profile.get("api_base_url") or "").strip()
        if api_base_url:
            return api_base_url
        parsed = urlsplit(self.competition_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    async def _ensure_client(self, *, refresh: bool = False) -> httpx.AsyncClient:
        cookie_header = await self._ensure_cookie_header(refresh=refresh)
        if not cookie_header:
            raise RuntimeError(f"{self.label} session is not authenticated; re-auth required.")
        profile = self._profile()
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; ctf-agent runtime)",
            "Cookie": cookie_header,
        }
        if refresh and self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._api_base_url(profile),
                follow_redirects=True,
                timeout=30.0,
                headers=headers,
            )
        else:
            self._client.headers.update(headers)
        return self._client

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        retry_on_auth: bool = True,
    ) -> dict[str, Any]:
        client = await self._ensure_client()
        response = await client.request(method, path, json=json_body)
        if response.status_code in {401, 403} and retry_on_auth:
            client = await self._ensure_client(refresh=True)
            response = await client.request(method, path, json=json_body)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    async def _poll_items(self) -> list[dict[str, Any]]:
        profile = self._profile()
        poll = _dict(profile.get("poll"))
        path = str(poll.get("path") or "").strip()
        items_path = str(poll.get("items_path") or "").strip()
        if not path or not items_path:
            return []
        payload = await self._request_json(str(poll.get("method") or "GET").upper(), path)
        items = _path_get(payload, items_path)
        normalized_items = [item for item in _list(items) if isinstance(item, dict)]
        name_field = str(poll.get("name_field") or "name").strip()
        id_field = str(poll.get("id_field") or "id").strip()
        for item in normalized_items:
            item_name = str(item.get(name_field) or "").strip()
            item_id = item.get(id_field)
            if not item_name:
                continue
            for hint in self.challenge_hints:
                if str(hint.get("name") or "").strip() == item_name and item_id is not None:
                    hint.setdefault("challenge_id", item_id)
        return normalized_items

    def _challenge_id_for_name(self, challenge_name: str) -> int | str | None:
        normalized_name = str(challenge_name or "").strip()
        for hint in self.challenge_hints:
            if str(hint.get("name") or "").strip() == normalized_name:
                challenge_id = hint.get("challenge_id")
                if isinstance(challenge_id, (int, str)) and str(challenge_id).strip():
                    return challenge_id
        return None

    async def fetch_challenge_stubs(self) -> list[dict[str, Any]]:
        stubs: list[dict[str, Any]] = []
        for hint in self.challenge_hints:
            name = str(hint.get("name") or "").strip()
            if not name:
                continue
            stubs.append(
                {
                    "name": name,
                    "category": str(hint.get("category") or "").strip(),
                    "value": int(hint.get("value") or 0),
                    "solves": int(hint.get("solves") or 0),
                    "source": self.platform,
                }
            )
        return stubs

    async def fetch_all_challenges(self) -> list[dict[str, Any]]:
        return await self.fetch_challenge_stubs()

    async def fetch_solved_names(self) -> set[str]:
        profile = self._profile()
        poll = _dict(profile.get("poll"))
        solved_field = str(poll.get("solved_field") or "").strip()
        name_field = str(poll.get("name_field") or "name").strip()
        if not solved_field:
            return set()
        solved: set[str] = set()
        for item in await self._poll_items():
            if item.get(solved_field):
                name = str(item.get(name_field) or "").strip()
                if name:
                    solved.add(name)
        return solved

    async def submit_flag(self, challenge_name: str, flag: str) -> SubmitResult:
        normalized_flag = str(flag or "").strip()
        if not normalized_flag:
            return SubmitResult("unknown", "", "Empty flag — nothing to submit.")

        if challenge_name in await self.fetch_solved_names():
            return SubmitResult(
                "already_solved",
                "",
                f'ALREADY SOLVED — "{challenge_name}" is already marked solved on {self.label}.',
            )

        challenge_id = self._challenge_id_for_name(challenge_name)
        if challenge_id is None:
            await self._poll_items()
            challenge_id = self._challenge_id_for_name(challenge_name)
        if challenge_id is None:
            raise RuntimeError(f'Could not resolve a remote challenge id for "{challenge_name}".')

        profile = self._profile()
        submit = _dict(profile.get("submit"))
        path_template = str(submit.get("path_template") or "").strip()
        if not path_template:
            raise RuntimeError(f"{self.label} auto-submit is not configured for this imported competition.")
        substitutions = {"challenge_id": challenge_id, "flag": normalized_flag}
        path = _render_body(path_template, substitutions)
        body = _render_body(submit.get("body"), substitutions)
        try:
            payload = await self._request_json(
                str(submit.get("method") or "POST").upper(),
                str(path),
                json_body=body,
            )
        except httpx.HTTPStatusError as exc:
            if challenge_name in await self.fetch_solved_names():
                return SubmitResult(
                    "already_solved",
                    "",
                    f'ALREADY SOLVED — "{challenge_name}" is already marked solved on {self.label}.',
                )
            detail = ""
            try:
                detail = _extract_message(exc.response.json())
            except Exception:
                detail = exc.response.text.strip()
            detail = detail or f"{self.label} rejected the submission."
            return SubmitResult(
                "incorrect",
                detail,
                f'INCORRECT — "{normalized_flag}" rejected on {self.label}. {detail}'.strip(),
            )

        success_path = str(submit.get("success_path") or "").strip()
        success = _path_get(payload, success_path) if success_path else None
        if success is True:
            return SubmitResult(
                "correct",
                "",
                f'CORRECT — "{normalized_flag}" accepted on {self.label}.',
            )

        detail = _extract_message(payload) or f"{self.label} rejected the submission."
        return SubmitResult(
            "incorrect",
            detail,
            f'INCORRECT — "{normalized_flag}" rejected on {self.label}. {detail}'.strip(),
        )

    async def pull_challenge(self, challenge: dict[str, Any], output_dir: str) -> str:
        del challenge, output_dir
        raise RuntimeError(
            f"{self.label} challenges must already exist in the imported local challenge store."
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
