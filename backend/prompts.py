"""Shared prompt builders for solvers, coordinators, and advisors."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from backend.tools.core import IMAGE_EXTS_FOR_VISION as IMAGE_EXTS

_BINARY_CATEGORIES = {"reverse", "reversing", "re", "pwn", "binary"}
_BINARY_EXTS = {
    ".bin",
    ".elf",
    ".so",
    ".a",
    ".o",
    ".ko",
    ".exe",
    ".dll",
    ".sys",
    ".apk",
    ".dex",
    ".jar",
    ".class",
    ".wasm",
}
_TEXTISH_EXTS = {
    ".txt",
    ".md",
    ".rst",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".log",
    ".csv",
    ".tsv",
    ".html",
    ".htm",
    ".js",
    ".mjs",
    ".cjs",
    ".css",
    ".py",
    ".rb",
    ".pl",
    ".php",
    ".java",
    ".kt",
    ".go",
    ".rs",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".sh",
    ".bash",
    ".zsh",
    ".ps1",
    ".sql",
}
_BINARY_FILENAMES = {"a.out", "chal", "challenge", "binary", "vuln", "exploitme"}
_WEB_CATEGORIES = {"web", "osint", "cloud", "api"}
_WINDOWS_CATEGORIES = {"windows", "active-directory", "ad"}
_MOBILE_CATEGORIES = {"mobile", "android"}
_FIRMWARE_CATEGORIES = {"firmware", "hardware", "iot", "embedded", "forensics"}
_BLOCKCHAIN_CATEGORIES = {"blockchain", "smart-contract", "smart contract", "solidity", "evm"}


@dataclass
class ChallengeMeta:
    name: str = "Unknown"
    category: str = ""
    value: int = 0
    description: str = ""
    tags: list[str] = field(default_factory=list)
    connection_info: str = ""
    hints: list[dict[str, Any]] = field(default_factory=list)
    solves: int = 0

    @classmethod
    def from_yaml(cls, path: str | Path) -> ChallengeMeta:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return cls(
            name=data.get("name", "Unknown"),
            category=data.get("category", ""),
            value=data.get("value", 0),
            description=data.get("description", ""),
            tags=data.get("tags", []),
            connection_info=data.get("connection_info", ""),
            hints=data.get("hints", []),
            solves=data.get("solves", 0),
        )


def list_distfiles(challenge_dir: str) -> list[str]:
    dist = Path(challenge_dir) / "distfiles"
    if not dist.exists():
        return []
    return sorted(f.name for f in dist.iterdir() if f.is_file())


def _rewrite_connection_info(conn: str) -> str:
    """Replace localhost/127.0.0.1 with host.docker.internal for bridge networking."""
    if not conn:
        return conn
    conn = re.sub(r"\blocalhost\b", "host.docker.internal", conn)
    conn = re.sub(r"\b127\.0\.0\.1\b", "host.docker.internal", conn)
    return conn


def build_named_tool_sandbox_preamble(tool_names: list[str]) -> str:
    return (
        "IMPORTANT: You are running inside a Docker sandbox. "
        "All files are under /challenge/ — distfiles at /challenge/distfiles/, "
        "workspace at /challenge/workspace/. Do NOT use any paths outside /challenge/. "
        f"Your tools: {', '.join(tool_names)}. Use these for all operations.\n\n"
    )


def build_shell_solver_preamble() -> str:
    return (
        "IMPORTANT: You are running inside a Docker sandbox. "
        "All files are under /challenge/ — distfiles at /challenge/distfiles/, "
        "workspace at /challenge/workspace/. Do NOT use any paths outside /challenge/. "
        "Use shell commands for execution, inspection, builds, and mutation. "
        "Large bash output may come back as a saved path with no inline preview, so inspect "
        "saved artifacts with targeted follow-up commands. "
        "Do not reread /challenge/agent-repo, /challenge/host-logs, prior solve/ output, "
        "or challenge-src/.shared-artifacts history. "
        "Use `report_flag_candidate 'FLAG' ['EVIDENCE'] ['CONFIDENCE']` for the guarded flag path "
        "(remote submit on CTFd, operator review when submission is disabled). "
        "Use `notify_coordinator 'MSG'` to send a note upstream.\n\n"
    )


def build_lane_bump_prompt(
    insights: str,
    *,
    operator: bool = False,
    advisory: bool = False,
) -> str:
    if operator:
        return (
            "Stop your previous line of attack. "
            "Highest priority guidance from the operator:\n\n"
            f"{insights}\n\n"
            "Do this first. Verify or refute it before returning to earlier exploration."
        )
    if advisory:
        return (
            "Prioritize this lane advisory for your next 1-2 actions:\n\n"
            f"{insights}\n\n"
            "Validate or falsify it before returning to broader search."
        )
    return (
        "Your previous attempt did not find the flag. "
        f"Additional guidance:\n\n{insights}\n\n"
        "Try a different approach. Do NOT repeat what was tried."
    )


def _looks_binary_like_distfile(name: str) -> bool:
    path = Path(name)
    suffix = path.suffix.lower()
    lower_name = path.name.lower()
    if suffix in _BINARY_EXTS or lower_name in _BINARY_FILENAMES:
        return True
    if suffix in _TEXTISH_EXTS or suffix in IMAGE_EXTS:
        return False
    return suffix == ""


def _should_include_binary_analysis(meta: ChallengeMeta, distfile_names: list[str]) -> bool:
    category = (meta.category or "").strip().lower()
    if category in _BINARY_CATEGORIES:
        return True
    return any(_looks_binary_like_distfile(name) for name in distfile_names)


def _category_tokens(meta: ChallengeMeta) -> set[str]:
    candidates = [(meta.category or "").strip().lower()] + [
        tag.strip().lower() for tag in meta.tags if tag
    ]
    return {token for token in candidates if token}


def _build_domain_hints(meta: ChallengeMeta) -> list[str]:
    tokens = _category_tokens(meta)
    hints: list[str] = []

    if tokens & _WEB_CATEGORIES:
        hints.append(
            "- Web/external surface: prefer `httpx`, `ffuf`, `feroxbuster`, `katana`, "
            "`subfinder`, or `amass` as appropriate, and use bundled wordlists under "
            "`/opt/wordlists/seclists` and `/opt/wordlists/assetnote` before downloading ad-hoc lists."
        )
    if tokens & _WINDOWS_CATEGORIES:
        hints.append(
            "- Windows/AD: start with `nxc`, `certipy`, `impacket-*`, `enum4linux-ng`, "
            "`kerbrute`, `smbclient`, and `rpcclient`; keep `evil-winrm` for when you already have credentials."
        )
    if tokens & _MOBILE_CATEGORIES:
        hints.append(
            "- Mobile/Android: use `jadx`, `apktool`, `smali`, `baksmali`, and `dex2jar` "
            "for static work before switching to `frida` or `objection`."
        )
    if tokens & _FIRMWARE_CATEGORIES:
        hints.append(
            "- Firmware/embedded: start with `binwalk`, `jefferson`, `ubireader_*`, and `unblob`; "
            "use `openocd` or `avrdude` only when the challenge actually needs hardware-style interaction."
        )
    if tokens & _BLOCKCHAIN_CATEGORIES:
        hints.append(
            "- Blockchain: prefer `forge`, `cast`, `anvil`, and `slither`; use `solc-select` "
            "or `vyper` when compiler version mismatch is part of the problem."
        )

    return hints


def build_prompt(
    meta: ChallengeMeta,
    distfile_names: list[str],
    container_arch: str = "unknown",
    has_named_tools: bool = True,
) -> str:
    """Build the system prompt.

    has_named_tools: True for solvers with discrete helper tools (`bash`,
    `report_flag_candidate`, `view_image`). False for shell-first solvers that
    use pseudo-commands routed through bash hooks.
    """
    conn_info = _rewrite_connection_info(meta.connection_info.strip())

    lines: list[str] = [
        "You are an expert CTF solver. Find the real flag for the challenge below.",
        "",
    ]

    if conn_info:
        lines += [
            "> **FIRST ACTION REQUIRED**: Your very first tool call MUST connect to the service.",
            f"> Run: `{conn_info}` (use a heredoc or pwntools script as shown below).",
            "> Do NOT explore the sandbox filesystem first. The flag is on the service, not in the container.",
            "",
        ]

    lines += [
        "## Challenge",
        f"**Name**    : {meta.name}",
        f"**Category**: {meta.category or 'Unknown'}",
        f"**Points**  : {meta.value or '?'}",
        f"**Arch**    : {container_arch}",
    ]
    if meta.tags:
        lines.append(f"**Tags**    : {', '.join(meta.tags)}")
    lines += ["", "## Description", meta.description or "_No description provided._", ""]

    if conn_info:
        if re.match(r"^https?://", conn_info):
            hint = "This is a **web service**. Use `bash` with `curl`/`python3 requests`."
        elif conn_info.startswith("nc "):
            hint = (
                "This is a **TCP service**. Each `bash` call is a fresh process — "
                "use a heredoc to send multiple lines in one shot:\n"
                "```\n"
                f"{conn_info} <<'EOF'\ncommand1\ncommand2\nEOF\n"
                "```\n"
                "Or write a Python `socket` / `pwntools` script for stateful interaction."
            )
        else:
            hint = "Connect using the details above."
        lines += ["## Service Connection", "```", conn_info, "```", hint, ""]

    if distfile_names:
        lines.append("## Attached Files")
        for name in distfile_names:
            ext = Path(name).suffix.lower()
            is_img = ext in IMAGE_EXTS
            if is_img and has_named_tools:
                suffix = "  <- **IMAGE: call `view_image` immediately** (fix magic bytes first if corrupt)"
            elif is_img:
                suffix = "  <- **IMAGE: use `exiftool`, `steghide`, `zsteg`, `strings` via bash**"
            else:
                suffix = ""
            lines.append(f"- `/challenge/distfiles/{name}`{suffix}")
        lines.append("")

    visible_hints = [h for h in meta.hints if h.get("content")]
    if visible_hints:
        lines.append("## Hints")
        for h in visible_hints:
            lines.append(f"- {h['content']}")
        lines.append("")

    if _should_include_binary_analysis(meta, distfile_names):
        lines += [
            "## Binary Analysis",
            "**Headless Ghidra** is installed as `ghidra-headless` for non-interactive analysis.",
            "Use `ghidra-headless`, `r2`, `gdb`, `angr`, and `capstone` via bash; prefer saved artifacts over dumping long disassembly into the conversation.",
            "",
        ]

    if has_named_tools:
        image_hint = "**Images: call `view_image` FIRST, before any other analysis.**"
        web_hint = "Web: check routes, params, JS source, cookies, robots.txt, and use `bash` for curl, requests, and fuzzing."
        submit_hint = (
            "**Use `report_flag_candidate` for every serious candidate**. "
            "It applies guardrails and then either submits remotely or queues operator review."
        )
        read_tool_hint = "For discovery, use focused `bash` commands and save large output under `/challenge/shared-artifacts/` before inspecting it."
    else:
        image_hint = "**Images: use `exiftool`, `steghide`, `zsteg`, `strings`, `xxd`, and `binwalk` via bash.**"
        web_hint = "Web: check routes, params, JS source, cookies, robots.txt, and use bash tools for curl, requests, and fuzzing."
        submit_hint = (
            "**Use `report_flag_candidate '<flag>'`** (pseudo-command) for every serious candidate. "
            "It applies guardrails and then either submits remotely or queues operator review."
        )
        read_tool_hint = "For discovery, use focused shell commands and save large output under `/challenge/shared-artifacts/` before inspecting it."

    lines += [
        "",
        "## Operating Rules",
        "**Use tools immediately. Do not describe — execute.**",
        "",
        "- " + ("Connect to the service now." if conn_info else "Inspect the attached files now."),
        "- Keep using tools until you have the real flag.",
        "- Try the obvious path first, then widen the search: hidden files, env vars, backups, headers, errors, timing, and encoding tricks.",
        "- Treat `/challenge/shared-artifacts/` as shared evidence. If a lane message or advisory points to a digest or artifact, inspect that evidence before repeating the same search.",
        "- If you see `Artifact path: /challenge/shared-artifacts/...`, treat it as high-priority evidence. Prefer the digest when one is available, then inspect the raw artifact.",
        "- Never reread `/challenge/agent-repo`, `/challenge/host-logs`, prior `solve/` output, or `challenge-src/.shared-artifacts/` history. Work from distfiles, challenge-src, workspace, metadata, and current shared artifacts instead.",
        "- Do not dump huge output into the conversation. If `grep -R`, `rg`, `find`, `strings`, `objdump`, `binwalk`, `ffuf`, or large HTML/JS searches may exceed about 100 lines, redirect to `/challenge/shared-artifacts/<name>.txt` first.",
        "- Large saved output may come back with only a path, not a preview. Inspect `/challenge/shared-artifacts/` with `sed -n`, `head`, `tail`, targeted `rg`, `strings`, or `xxd` instead of re-printing giant blobs.",
        "- Prefer bundled wordlists under `/opt/wordlists/seclists` and `/opt/wordlists/assetnote` before downloading ad-hoc lists.",
        f"- {read_tool_hint}",
        "- If progress requires a built artifact or running service, you may run build or compose commands early, but only after identifying the artifact or runtime state you need.",
        "- Do not run `build.sh`, `docker build`, `docker compose`, `docker-compose`, `podman-compose`, `make`, `cmake --build`, or `cargo build` just because the file exists.",
        "- For builds, compose runs, and other long commands, set an explicit larger `timeout_seconds` (for example 300 or 600) and redirect stdout/stderr to `/challenge/shared-artifacts/<name>.log`.",
        "- Inspect build or compose progress with `tail`, `sed -n`, or targeted `rg`, then verify artifacts or service state first with `ls`, `file`, or targeted checks before re-reading logs.",
        f"- {image_hint}",
        f"- {web_hint}",
        "- Crypto: identify primitives, weak keys, nonce reuse, padding oracles, and broken assumptions. For RSA, try `RsaCtfTool`, sage, or `cado-nfs` when relevant.",
        "- Pwn: use `stty raw -echo` before launching interactive binaries over nc.",
        "- Ignore placeholder flags such as `CTF{flag}` or `CTF{placeholder}`.",
        f"- {submit_hint}",
        "- After queueing a serious candidate, keep exploring unless the coordinator confirms it.",
        "- Do not guess. Do not ask. Run the next concrete check.",
    ]

    lines.extend(_build_domain_hints(meta))

    return "\n".join(lines)
