from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import yaml

from backend.import_cli import _load_cookie_header, _pick_importer
from backend.importers import (
    AutoPlatformImporter,
    DreamhackImporter,
    ImportAuth,
    SpecPlatformImporter,
)
from backend.platforms.dreamhack import DREAMHACK_API_BASE_URL

COMPETITION_URL = "https://dreamhack.io/career/competitions/2026-GMDSOFT"
COMPETITION_API_URL = f"{DREAMHACK_API_BASE_URL}/v1/career/rms/2026-GMDSOFT/"
APPLICANT_API_URL = f"{DREAMHACK_API_BASE_URL}/v1/career/recruitment-applicants/77/"

PUBLIC_HTML = """
<div class="rms-title">2026 GMDSOFT 채용연계형 CTF</div>
<div class="rms-info-actions"><button>채용 진행중</button></div>
<span class="label">CTF 개최자</span><div class="content">GMDSOFT</div>
<div class="rms-markdown"><p>대한민국 디지털 포렌식 1위 기업입니다.</p></div>
<span class="label">규칙</span><div class="content">규칙 본문</div>
<span class="label">플래그 형식</span><div class="content">플래그의 형식은 DH{...}입니다.</div>
<span class="label">플래그 제출 제한</span><div class="content">챌린지 당 30초마다 한 번씩 Flag를 제출할 수 있습니다.</div>
<script>
window.__NUXT__=(function(){return{data:[{rms:{slug:"2026-GMDSOFT",starts_at:"2025-12-29T12:00:00+09:00",ends_at:"2026-12-28T23:59:00+09:00",contact_email:"ctf@gmdsoft.com"}}]}})()
</script>
"""

AUTH_HTML = """
<div class="logged-in">authenticated</div>
"""

PUBLIC_COMPETITION_JSON = {
    "id": 2026,
    "slug": "2026-GMDSOFT",
    "title": "2026 GMDSOFT 채용연계형 CTF",
    "company": {"name": "GMDSOFT"},
    "starts_at": "2025-12-29T12:00:00+09:00",
    "ends_at": "2026-12-28T23:59:00+09:00",
    "contact_email": "ctf@gmdsoft.com",
    "is_started": True,
}

AUTH_COMPETITION_JSON = {
    **PUBLIC_COMPETITION_JSON,
    "my_applying": {"id": 77},
}


def _applicant_payload(*, port: int = 16377, solved: bool = True) -> dict[str, object]:
    return {
        "id": 77,
        "ctf_challenges": [
            {
                "id": 45,
                "order": "B",
                "title": "aeBPF",
                "description": (
                    "aeBPF: ARM64 Extended Berkeley Packet Filter\n"
                    "취약점을 찾아 공격하여 /dev/vda 파일을 읽어주세요.\n"
                    f"Host: host3.dreamhack.games\nPort: {port}\n"
                    f"nc host3.dreamhack.games {port}\n"
                    "Flag format: DH{...}"
                ),
                "tags": ["pwn"],
                "public": "https://files.example/aeBPF.zip",
                "needs_vm": False,
                "is_solved": solved,
            }
        ],
        "writeups": [
            {
                "id": "99",
                "challenge": {"id": 45},
            }
        ],
    }


def test_load_cookie_header_supports_multiline_cookie_file(tmp_path: Path) -> None:
    cookie_path = tmp_path / "dreamhack.cookie"
    cookie_path.write_text("dh_session=abc\ncsrftoken=def\n", encoding="utf-8")

    auth = _load_cookie_header(str(cookie_path))

    assert auth.mode == "cookie_file"
    assert auth.cookie_header == "dh_session=abc; csrftoken=def"


def test_pick_importer_detects_dreamhack_competitions() -> None:
    importer = _pick_importer(COMPETITION_URL)

    assert isinstance(importer, DreamhackImporter)


def test_pick_importer_falls_back_to_auto_for_unknown_platform() -> None:
    importer = _pick_importer("https://unknown-ctf.example/events/spring-2026")

    assert isinstance(importer, AutoPlatformImporter)


def test_pick_importer_supports_declarative_platform_specs(tmp_path: Path) -> None:
    spec_dir = tmp_path / "platform-specs"
    spec_dir.mkdir()
    (spec_dir / "acme.yml").write_text(
        "\n".join(
            [
                "platform: acmeboard",
                "label: Acme Board",
                "match:",
                "  domains: [ctf.example.com]",
                "  url_patterns: [/events/]",
                "capabilities:",
                "  import: confirmed",
                "  poll_solved: unsupported",
                "  submit_flag: operator_only",
                "  pull_files: unsupported",
                "import:",
                "  competition_slug_regex: '/events/(?P<slug>[A-Za-z0-9_-]+)'",
                "  competition_title_regex: '<h1[^>]*>(?P<title>.*?)</h1>'",
                "  challenge_regex: '<li class=\"challenge\" data-category=\"(?P<category>[^\"]+)\">\\s*<a href=\"(?P<challenge_url>[^\"]+)\">(?P<name>[^<]+)</a>\\s*<div class=\"desc\">(?P<description>.*?)</div>\\s*</li>'",
            ]
        ),
        encoding="utf-8",
    )

    importer = _pick_importer(
        "https://ctf.example.com/events/spring-2026",
        (str(spec_dir),),
    )

    assert isinstance(importer, SpecPlatformImporter)
    assert importer.platform == "acmeboard"


def test_dreamhack_import_writes_competition_manifest_and_challenge_metadata(
    monkeypatch,
    tmp_path: Path,
) -> None:
    importer = DreamhackImporter()
    cookie_path = tmp_path / "dreamhack.cookie"
    cookie_path.write_text("dh_session=abc\n", encoding="utf-8")

    async def fake_fetch_text(_client, url: str, *, headers: dict[str, str]) -> str:
        assert url == COMPETITION_URL
        return AUTH_HTML if headers.get("Cookie") else PUBLIC_HTML

    async def fake_fetch_json(_client, url: str, *, headers: dict[str, str]) -> dict[str, Any]:
        if url == COMPETITION_API_URL:
            return AUTH_COMPETITION_JSON if headers.get("Cookie") else PUBLIC_COMPETITION_JSON
        if url == APPLICANT_API_URL:
            assert headers["Cookie"] == "dh_session=abc"
            return _applicant_payload()
        raise AssertionError(f"unexpected fetch json: {url}")

    async def fake_download_files(_client, file_urls, dist_dir: Path, *, headers: dict[str, str]) -> None:
        assert headers["Cookie"] == "dh_session=abc"
        assert file_urls == ["https://files.example/aeBPF.zip"]
        dist_dir.mkdir(parents=True, exist_ok=True)
        (dist_dir / "aeBPF.zip").write_text("payload", encoding="utf-8")

    monkeypatch.setattr(importer, "_fetch_text", fake_fetch_text)
    monkeypatch.setattr(importer, "_fetch_json", fake_fetch_json)
    monkeypatch.setattr(importer, "_download_files", fake_download_files)

    result = asyncio.run(
        importer.import_competition(
            COMPETITION_URL,
            ImportAuth(
                mode="cookie_file",
                cookie_header="dh_session=abc",
                cookie_file=str(cookie_path),
            ),
            tmp_path,
        )
    )

    competition_dir = result.competition_dir
    manifest = yaml.safe_load((competition_dir / "competition.yml").read_text(encoding="utf-8"))
    metadata = yaml.safe_load((competition_dir / "aeBPF" / "metadata.yml").read_text(encoding="utf-8"))

    assert manifest["platform"] == "dreamhack"
    assert manifest["platform_label"] == "Dreamhack"
    assert manifest["capabilities"]["submit_flag"] == "confirmed"
    assert manifest["runtime_mode"] == "full_remote"
    assert manifest["title"] == "2026 GMDSOFT 채용연계형 CTF"
    assert manifest["challenge_entries"][0]["name"] == "aeBPF"
    assert manifest["challenge_entries"][0]["challenge_id"] == 45
    assert manifest["challenge_entries"][0]["solved"] is True
    assert metadata["name"] == "aeBPF"
    assert metadata["category"] == "pwn"
    assert metadata["connection"]["host"] == "host3.dreamhack.games"
    assert metadata["connection"]["port"] == 16377
    assert metadata["source"]["applicant_id"] == "77"
    assert metadata["source"]["platform_label"] == "Dreamhack"
    assert metadata["source"]["capabilities"]["poll_solved"] == "confirmed"
    assert metadata["source"]["runtime_mode"] == "full_remote"
    assert metadata["source"]["auth"]["cookie_file"] == str(cookie_path.resolve())
    assert "session_ref" not in metadata["source"]["auth"]
    assert metadata["source"]["remote"]["profile_ref"] == manifest["remote"]["profile_ref"]
    assert metadata["source"]["challenge_id"] == 45
    assert metadata["source"]["status"]["solved"] is True
    assert metadata["source"]["status"]["writeup_submitted"] is True
    assert manifest["auth"]["cookie_file"] == str(cookie_path.resolve())
    assert Path(manifest["remote"]["profile_ref"]).exists()
    assert (competition_dir / "aeBPF" / "distfiles" / "aeBPF.zip").exists()
    assert (competition_dir / ".source-cache" / "competition-public.html").exists()
    assert (competition_dir / ".source-cache" / "competition-public.json").exists()
    assert (competition_dir / ".source-cache" / "competition-authenticated.html").exists()
    assert (competition_dir / ".source-cache" / "competition-authenticated.json").exists()
    assert (competition_dir / ".source-cache" / "applicant-authenticated.json").exists()
    challenge_cache = competition_dir / ".source-cache" / "challenge-aeBPF-45.json"
    assert challenge_cache.exists()
    cached_payload = json.loads(challenge_cache.read_text(encoding="utf-8"))
    assert cached_payload["title"] == "aeBPF"
    automation_profile = json.loads(
        Path(manifest["remote"]["profile_ref"]).read_text(encoding="utf-8")
    )
    assert automation_profile["mode"] == "http_json_api"
    assert automation_profile["poll"]["path"] == "/v1/career/recruitment-applicants/77/"


def test_spec_importer_writes_competition_manifest_and_basic_challenge_metadata(
    monkeypatch,
    tmp_path: Path,
) -> None:
    spec_dir = tmp_path / "platform-specs"
    spec_dir.mkdir()
    (spec_dir / "acme.yml").write_text(
        "\n".join(
            [
                "platform: acmeboard",
                "label: Acme Board",
                "match:",
                "  domains: [ctf.example.com]",
                "  url_patterns: [/events/]",
                "capabilities:",
                "  import: confirmed",
                "  poll_solved: unsupported",
                "  submit_flag: operator_only",
                "  pull_files: unsupported",
                "import:",
                "  competition_slug_regex: '/events/(?P<slug>[A-Za-z0-9_-]+)'",
                "  competition_title_regex: '<h1[^>]*>(?P<title>.*?)</h1>'",
                "  challenge_regex: '<li class=\"challenge\" data-category=\"(?P<category>[^\"]+)\">\\s*<a href=\"(?P<challenge_url>[^\"]+)\">(?P<name>[^<]+)</a>\\s*<div class=\"desc\">(?P<description>.*?)</div>\\s*</li>'",
            ]
        ),
        encoding="utf-8",
    )
    importer = _pick_importer(
        "https://ctf.example.com/events/spring-2026",
        (str(spec_dir),),
    )
    assert isinstance(importer, SpecPlatformImporter)

    html = """
    <html>
      <body>
        <h1>Acme Spring 2026</h1>
        <ul>
          <li class="challenge" data-category="web">
            <a href="/events/spring-2026/challenges/portal">Portal</a>
            <div class="desc">Visit https://portal.example and recover the flag.</div>
          </li>
        </ul>
      </body>
    </html>
    """

    async def fake_fetch_text(_client, url: str, *, headers: dict[str, str]) -> str:
        assert url == "https://ctf.example.com/events/spring-2026"
        assert headers["User-Agent"]
        return html

    monkeypatch.setattr(importer, "_fetch_text", fake_fetch_text)

    result = asyncio.run(
        importer.import_competition(
            "https://ctf.example.com/events/spring-2026",
            ImportAuth(),
            tmp_path / "challenges",
        )
    )

    manifest = yaml.safe_load((result.competition_dir / "competition.yml").read_text(encoding="utf-8"))
    metadata = yaml.safe_load((result.competition_dir / "Portal" / "metadata.yml").read_text(encoding="utf-8"))

    assert manifest["platform"] == "acmeboard"
    assert manifest["platform_label"] == "Acme Board"
    assert manifest["runtime_mode"] == "operator_only"
    assert metadata["name"] == "Portal"
    assert metadata["category"] == "web"
    assert metadata["source"]["platform_label"] == "Acme Board"
    assert metadata["source"]["capabilities"]["submit_flag"] == "operator_only"
    assert metadata["source"]["challenge_url"] == "https://ctf.example.com/events/spring-2026/challenges/portal"
    assert metadata["connection"]["url"] == "https://portal.example"


def test_auto_importer_writes_manifest_and_heuristic_challenge_metadata(
    monkeypatch,
    tmp_path: Path,
) -> None:
    importer = _pick_importer("https://unknown-ctf.example/events/spring-2026")
    assert isinstance(importer, AutoPlatformImporter)

    html = """
    <html>
      <head><title>Unknown Spring 2026</title></head>
      <body>
        <ul>
          <li class="challenge-card" data-category="crypto">
            <a href="/events/spring-2026/challenges/vault">Vault</a>
            <div class="description">nc vault.example 31337</div>
          </li>
        </ul>
      </body>
    </html>
    """

    async def fake_fetch_text(_client, url: str, *, headers: dict[str, str]) -> str:
        assert url == "https://unknown-ctf.example/events/spring-2026"
        assert headers["User-Agent"]
        return html

    monkeypatch.setattr(importer, "_fetch_text", fake_fetch_text)

    result = asyncio.run(
        importer.import_competition(
            "https://unknown-ctf.example/events/spring-2026",
            ImportAuth(),
            tmp_path / "challenges",
        )
    )

    manifest = yaml.safe_load((result.competition_dir / "competition.yml").read_text(encoding="utf-8"))
    metadata = yaml.safe_load((result.competition_dir / "Vault" / "metadata.yml").read_text(encoding="utf-8"))

    assert manifest["platform"] == "unknown-ctf-example"
    assert manifest["runtime_mode"] == "operator_only"
    assert manifest["import_mode"] == "heuristic_auto"
    assert metadata["name"] == "Vault"
    assert metadata["category"] == "crypto"
    assert metadata["source"]["import_mode"] == "heuristic_auto"
    assert metadata["source"]["capabilities"]["submit_flag"] == "operator_only"
    assert metadata["connection_info"] == "nc vault.example 31337"


def test_dreamhack_refresh_preserves_runtime_override_and_results(monkeypatch, tmp_path: Path) -> None:
    importer = DreamhackImporter()
    phase = {"count": 0}

    async def fake_fetch_text(_client, url: str, *, headers: dict[str, str]) -> str:
        assert url == COMPETITION_URL
        return AUTH_HTML if headers.get("Cookie") else PUBLIC_HTML

    async def fake_fetch_json(_client, url: str, *, headers: dict[str, str]) -> dict[str, Any]:
        if url == COMPETITION_API_URL:
            return AUTH_COMPETITION_JSON if headers.get("Cookie") else PUBLIC_COMPETITION_JSON
        if url == APPLICANT_API_URL:
            return _applicant_payload(port=16377 if phase["count"] == 0 else 31337, solved=phase["count"] == 1)
        raise AssertionError(f"unexpected fetch json: {url}")

    async def fake_download_files(_client, file_urls, dist_dir: Path, *, headers: dict[str, str]) -> None:
        dist_dir.mkdir(parents=True, exist_ok=True)
        (dist_dir / "aeBPF.zip").write_text("payload", encoding="utf-8")

    monkeypatch.setattr(importer, "_fetch_text", fake_fetch_text)
    monkeypatch.setattr(importer, "_fetch_json", fake_fetch_json)
    monkeypatch.setattr(importer, "_download_files", fake_download_files)

    result = asyncio.run(
        importer.import_competition(
            COMPETITION_URL,
            ImportAuth(mode="cookie_file", cookie_header="dh_session=abc"),
            tmp_path,
        )
    )
    competition_dir = result.competition_dir
    challenge_dir = competition_dir / "aeBPF"
    override_dir = challenge_dir / ".runtime"
    override_dir.mkdir(parents=True, exist_ok=True)
    (override_dir / "override.json").write_text(
        json.dumps({"connection": {"host": "override.example", "port": 31337}}, indent=2),
        encoding="utf-8",
    )
    solve_dir = challenge_dir / "solve"
    solve_dir.mkdir(parents=True, exist_ok=True)
    (solve_dir / "result.json").write_text('{"status":"flag_found"}', encoding="utf-8")

    phase["count"] = 1
    asyncio.run(
        importer.import_competition(
            COMPETITION_URL,
            ImportAuth(mode="cookie_file", cookie_header="dh_session=abc"),
            tmp_path,
            refresh=True,
        )
    )

    metadata = yaml.safe_load((challenge_dir / "metadata.yml").read_text(encoding="utf-8"))
    assert metadata["connection"]["port"] == 31337
    assert metadata["source"]["status"]["solved"] is True
    assert (override_dir / "override.json").exists()
    assert (solve_dir / "result.json").exists()
