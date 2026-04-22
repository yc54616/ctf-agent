from __future__ import annotations

from backend.prompts import ChallengeMeta, build_prompt


def test_challenge_meta_infers_flag_guard_from_description_and_hints() -> None:
    meta = ChallengeMeta(
        name="chal",
        description="Solve it.\nFlag format: DH{...}",
        hints=[{"content": r"Flag regex: `^DH\{[A-Za-z0-9_]+\}$`"}],
    )

    assert meta.flag_format == "DH{...}"
    assert meta.flag_regex == r"^DH\{[A-Za-z0-9_]+\}$"


def test_build_prompt_includes_flag_format_guidance_when_available() -> None:
    prompt = build_prompt(
        ChallengeMeta(name="chal", description="Flag format: DH{...}"),
        distfile_names=[],
    )

    assert "## Flag Format" in prompt
    assert "- Expected format: `DH{...}`" in prompt


def test_build_prompt_includes_ctf_skills_reference_paths() -> None:
    prompt = build_prompt(
        ChallengeMeta(name="webby", category="web"),
        distfile_names=["index.html"],
    )

    assert "## Local CTF Skills" in prompt
    assert "/challenge/agent-repo/ctf-skills/ctf-web/SKILL.md" in prompt
    assert "/challenge/agent-repo/ctf-skills/solve-challenge/SKILL.md" in prompt
