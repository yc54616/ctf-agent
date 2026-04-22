from __future__ import annotations

from pathlib import Path

from backend.browser_sessions import browser_session_root, resolve_session_ref


def test_browser_session_root_defaults_to_repo_cache() -> None:
    root = browser_session_root()

    assert root.name == "browser-sessions"
    assert root.parent.name == ".cache"
    assert root.parent.parent.name == "ctf-agent"


def test_resolve_session_ref_uses_repo_cache_for_relative_paths() -> None:
    resolved = resolve_session_ref("dreamhack/session.json")

    assert resolved.name == "session.json"
    assert resolved.parent.name == "dreamhack"
    assert resolved.parent.parent == browser_session_root()


def test_resolve_session_ref_keeps_absolute_paths(tmp_path: Path) -> None:
    target = tmp_path / "session.json"

    resolved = resolve_session_ref(str(target))

    assert resolved == target.resolve()
