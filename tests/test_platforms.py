from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from backend.config import Settings
from backend.platforms import (
    BrowserPlatformClient,
    CompositePlatformClient,
    build_platform_client,
    resolve_platform_descriptor,
)
from backend.platforms.dreamhack import DreamhackClient
from backend.platforms.specs import PlatformSpec
from backend.prompts import ChallengeMeta


def _dreamhack_applicant_payload(*, solved: bool = False) -> dict[str, object]:
    return {
        "id": 77,
        "ctf_challenges": [
            {
                "id": 45,
                "order": "B",
                "title": "aeBPF",
                "description": "Host: host3.dreamhack.games\nPort: 16377\nnc host3.dreamhack.games 16377\nFlag format: DH{...}",
                "tags": ["pwn"],
                "public": "https://files.example/aeBPF.zip",
                "needs_vm": False,
                "is_solved": solved,
            }
        ],
        "writeups": [{"id": "99", "challenge": {"id": 45}}],
    }


@pytest.mark.asyncio
async def test_dreamhack_client_fetches_and_normalizes_challenges(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DreamhackClient(
        competition_slug="2026-GMDSOFT",
        applicant_id="77",
        cookie_header="dh_session=abc",
        competition_title="2026 GMDSOFT 채용연계형 CTF",
        competition_url="https://dreamhack.io/career/competitions/2026-GMDSOFT",
    )

    async def fake_get_json(_path: str) -> dict[str, object]:
        return _dreamhack_applicant_payload(solved=True)

    monkeypatch.setattr(client, "_get_json", fake_get_json)

    challenges = await client.fetch_all_challenges()
    solved = await client.fetch_solved_names()

    assert challenges[0]["name"] == "aeBPF"
    assert challenges[0]["connection"]["host"] == "host3.dreamhack.games"
    assert challenges[0]["connection"]["port"] == 16377
    assert challenges[0]["source"]["challenge_id"] == 45
    assert challenges[0]["source"]["status"]["writeup_submitted"] is True
    assert solved == {"aeBPF"}


@pytest.mark.asyncio
async def test_dreamhack_client_submit_flag_accepts_and_caches_solution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DreamhackClient(
        competition_slug="2026-GMDSOFT",
        applicant_id="77",
        cookie_header="dh_session=abc",
    )

    async def fake_fetch_applicant(*, force: bool = False) -> dict[str, object]:
        del force
        return _dreamhack_applicant_payload(solved=False)

    async def fake_post_json(_path: str, body: dict[str, Any]) -> dict[str, object]:
        assert body == {"flag": "DH{real_flag}"}
        return {"is_correct": True}

    monkeypatch.setattr(client, "_fetch_applicant", fake_fetch_applicant)
    monkeypatch.setattr(client, "_post_json", fake_post_json)

    result = await client.submit_flag("aeBPF", "DH{real_flag}")

    assert result.status == "correct"
    assert result.display == 'CORRECT — "DH{real_flag}" accepted on Dreamhack.'
    assert await client.fetch_solved_names() == {"aeBPF"}


@pytest.mark.asyncio
async def test_dreamhack_client_submit_flag_maps_incorrect_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DreamhackClient(
        competition_slug="2026-GMDSOFT",
        applicant_id="77",
        cookie_header="dh_session=abc",
    )

    async def fake_fetch_applicant(*, force: bool = False) -> dict[str, object]:
        del force
        return _dreamhack_applicant_payload(solved=False)

    async def fake_post_json(_path: str, _body: dict[str, Any]) -> dict[str, object]:
        request = httpx.Request("POST", "https://dreamhack.io/api/v1/career/recruitment-ctf-challenges/45/submit/")
        response = httpx.Response(400, json={"flag": ["wrong flag"]}, request=request)
        raise httpx.HTTPStatusError("bad request", request=request, response=response)

    monkeypatch.setattr(client, "_fetch_applicant", fake_fetch_applicant)
    monkeypatch.setattr(client, "_post_json", fake_post_json)

    result = await client.submit_flag("aeBPF", "DH{wrong}")

    assert result.status == "incorrect"
    assert "wrong flag" in result.message
    assert 'INCORRECT — "DH{wrong}" rejected on Dreamhack.' in result.display


def test_build_platform_client_routes_imported_dreamhack_challenges() -> None:
    settings = Settings(remote_cookie_header="dh_session=abc")
    meta = ChallengeMeta(
        name="aeBPF",
        source={
            "platform": "dreamhack",
            "competition": {
                "slug": "2026-GMDSOFT",
                "title": "2026 GMDSOFT 채용연계형 CTF",
                "url": "https://dreamhack.io/career/competitions/2026-GMDSOFT",
            },
            "applicant_id": "77",
        },
    )

    client = build_platform_client(
        settings,
        {"aeBPF": meta},
        cookie_header="dh_session=abc",
    )

    assert isinstance(client, CompositePlatformClient)
    dreamhack_client = cast(DreamhackClient, client.clients["default"])
    assert dreamhack_client.platform == "dreamhack"
    assert client.challenge_routes["aeBPF"] == "default"


def test_build_platform_client_reloads_cookie_file_from_imported_metadata(tmp_path: Path) -> None:
    cookie_path = tmp_path / "dreamhack.cookie"
    cookie_path.write_text("dh_session=stored\ncsrftoken=def\n", encoding="utf-8")
    settings = Settings()
    meta = ChallengeMeta(
        name="aeBPF",
        source={
            "platform": "dreamhack",
            "competition": {
                "slug": "2026-GMDSOFT",
                "title": "2026 GMDSOFT 채용연계형 CTF",
                "url": "https://dreamhack.io/career/competitions/2026-GMDSOFT",
            },
            "applicant_id": "77",
            "auth": {
                "mode": "cookie_file",
                "cookie_file": str(cookie_path.resolve()),
            },
        },
    )

    client = build_platform_client(
        settings,
        {"aeBPF": meta},
        cookie_header="",
    )

    assert isinstance(client, CompositePlatformClient)
    dreamhack_client = cast(DreamhackClient, client.clients["default"])
    assert dreamhack_client.cookie_header == "dh_session=stored; csrftoken=def"


def test_build_platform_client_prefers_saved_browser_profile(tmp_path: Path) -> None:
    session_path = tmp_path / "dreamhack-session.json"
    session_path.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "dh_session",
                        "value": "stored",
                        "domain": "dreamhack.io",
                        "path": "/",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    profile_path = tmp_path / "automation-profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "version": 1,
                "platform": "dreamhack",
                "platform_label": "Dreamhack",
                "competition_url": "https://dreamhack.io/career/competitions/2026-GMDSOFT",
                "api_base_url": "https://dreamhack.io/api",
                "runtime_mode": "full_remote",
                "mode": "http_json_api",
                "challenge_hints": [{"name": "aeBPF", "challenge_id": 45}],
                "poll": {
                    "method": "GET",
                    "path": "/v1/career/recruitment-applicants/77/",
                    "items_path": "ctf_challenges",
                    "name_field": "title",
                    "id_field": "id",
                    "solved_field": "is_solved",
                },
                "submit": {
                    "method": "POST",
                    "path_template": "/v1/career/recruitment-ctf-challenges/{challenge_id}/submit/",
                    "body": {"flag": "{flag}"},
                    "success_path": "is_correct",
                },
            }
        ),
        encoding="utf-8",
    )
    settings = Settings()
    meta = ChallengeMeta(
        name="aeBPF",
        source={
            "platform": "dreamhack",
            "competition": {
                "slug": "2026-GMDSOFT",
                "title": "2026 GMDSOFT 채용연계형 CTF",
                "url": "https://dreamhack.io/career/competitions/2026-GMDSOFT",
            },
            "challenge_id": 45,
            "auth": {
                "mode": "playwright_storage_state",
                "session_ref": str(session_path.resolve()),
            },
            "remote": {
                "runtime_mode": "full_remote",
                "profile_ref": str(profile_path.resolve()),
            },
        },
    )

    client = build_platform_client(settings, {"aeBPF": meta}, cookie_header="")

    assert isinstance(client, CompositePlatformClient)
    browser_client = cast(BrowserPlatformClient, client.clients["default"])
    assert browser_client.session_ref == str(session_path.resolve())
    assert browser_client.profile_ref == str(profile_path.resolve())


@pytest.mark.asyncio
async def test_browser_platform_client_polls_and_submits_from_saved_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_path = tmp_path / "session.json"
    session_path.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "session", "value": "abc", "domain": "dreamhack.io", "path": "/"}
                ]
            }
        ),
        encoding="utf-8",
    )
    profile_path = tmp_path / "automation-profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "version": 1,
                "platform": "dreamhack",
                "platform_label": "Dreamhack",
                "competition_url": "https://dreamhack.io/career/competitions/2026-GMDSOFT",
                "api_base_url": "https://dreamhack.io/api",
                "runtime_mode": "full_remote",
                "mode": "http_json_api",
                "challenge_hints": [{"name": "aeBPF", "challenge_id": 45}],
                "poll": {
                    "method": "GET",
                    "path": "/v1/career/recruitment-applicants/77/",
                    "items_path": "ctf_challenges",
                    "name_field": "title",
                    "id_field": "id",
                    "solved_field": "is_solved",
                },
                "submit": {
                    "method": "POST",
                    "path_template": "/v1/career/recruitment-ctf-challenges/{challenge_id}/submit/",
                    "body": {"flag": "{flag}"},
                    "success_path": "is_correct",
                },
            }
        ),
        encoding="utf-8",
    )
    client = BrowserPlatformClient(
        platform="dreamhack",
        label="Dreamhack",
        competition_url="https://dreamhack.io/career/competitions/2026-GMDSOFT",
        session_ref=str(session_path.resolve()),
        profile_ref=str(profile_path.resolve()),
        challenge_hints=[{"name": "aeBPF", "challenge_id": 45}],
    )

    phase = {"solved": False}

    async def fake_request_json(method: str, path: str, *, json_body: Any = None, retry_on_auth: bool = True) -> dict[str, Any]:
        del retry_on_auth
        if method == "GET":
            assert path == "/v1/career/recruitment-applicants/77/"
            return {"ctf_challenges": [{"id": 45, "title": "aeBPF", "is_solved": phase["solved"]}]}
        assert method == "POST"
        assert path == "/v1/career/recruitment-ctf-challenges/45/submit/"
        assert json_body == {"flag": "DH{real_flag}"}
        phase["solved"] = True
        return {"is_correct": True}

    monkeypatch.setattr(client, "_request_json", fake_request_json)
    assert await client.fetch_solved_names() == set()
    result = await client.submit_flag("aeBPF", "DH{real_flag}")
    assert result.status == "correct"


def test_resolve_platform_descriptor_uses_operator_only_fallback_for_unknown_platform() -> None:
    descriptor = resolve_platform_descriptor("score-server-x")

    assert descriptor is not None
    assert descriptor.label == "Score Server X"
    assert descriptor.capabilities["import"] == "confirmed"
    assert descriptor.capabilities["submit_flag"] == "operator_only"


def test_resolve_platform_descriptor_loads_declarative_specs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = PlatformSpec.from_dict(
        {
            "platform": "acmeboard",
            "label": "Acme Board",
            "capabilities": {
                "import": "confirmed",
                "poll_solved": "unsupported",
                "submit_flag": "operator_only",
                "pull_files": "unsupported",
            },
        }
    )
    assert spec is not None
    monkeypatch.setattr(
        "backend.platforms.catalog.find_platform_spec",
        lambda platform: spec if platform == "acmeboard" else None,
    )

    descriptor = resolve_platform_descriptor("acmeboard")

    assert descriptor is not None
    assert descriptor.label == "Acme Board"
    assert descriptor.capabilities["submit_flag"] == "operator_only"
