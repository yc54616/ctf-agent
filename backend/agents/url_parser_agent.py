"""URL-based CTF challenge parser.

Input:  any URL (CTFd challenge page, DreamHack, raw problem site, GitHub, …)
Output: structured dict + human-readable markdown

The agent fetches the page, strips HTML, then asks a Claude model to extract
challenge metadata into a well-defined JSON schema.  The result is useful both
for the human coordinator (markdown_summary) and for spawning solver swarms
(the structured ``challenges`` list).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM system prompt
# ---------------------------------------------------------------------------

_PARSE_SYSTEM_PROMPT = """\
You are a CTF challenge information extractor.

Given text scraped from a CTF challenge page or problem listing, extract every
challenge present into structured JSON.  Output ONLY valid JSON — no prose, no
markdown fences.

Schema (omit fields that are genuinely absent):
{
  "competition_name": "CTF event name if visible, else null",
  "challenges": [
    {
      "name": "challenge name",
      "category": "web | pwn | rev | crypto | forensics | misc | osint | ...",
      "points": 100,
      "description": "full problem description, preserve newlines",
      "files": ["attachment1.zip", "binary"],
      "connection": {
        "host": "example.com",
        "port": 31337,
        "protocol": "nc | http | https | tcp",
        "url": "http://..."
      },
      "hints": ["hint text if visible"],
      "tags": ["tag1"],
      "solve_count": 42,
      "author": "author name if shown"
    }
  ]
}

Rules:
- Extract ALL challenges visible.
- For connection: look for nc/netcat commands, host:port patterns, and URLs in the description.
- For files: list attachment filenames mentioned.
- description: include the complete problem statement, not a truncation.
- If only one challenge is on the page, still wrap it in the "challenges" array.
- Do not invent information. If a field is missing, omit it.
"""

# ---------------------------------------------------------------------------
# HTTP fetch (no external deps)
# ---------------------------------------------------------------------------

_FETCH_TIMEOUT = 15
_MAX_TEXT_FOR_LLM = 14_000  # chars sent to LLM — keeps prompt cost low


def _fetch_url(url: str, *, cookie_header: str = "", auth_token: str = "") -> tuple[str, str]:
    """Return (content_type, body_text).  Raises RuntimeError on failure.

    Args:
        url: Any http(s) URL.
        cookie_header: Raw Cookie header value to attach for authenticated
            requests (e.g. a CTFd session cookie so the challenges page
            returns actual challenges instead of a login redirect).
        auth_token: Optional CTFd API token — attached as ``Authorization:
            Token <value>`` when set.  Ignored for non-CTFd hosts, but sending
            an unrelated Authorization header is generally harmless.
    """
    headers: dict[str, str] = {
        "User-Agent": "Mozilla/5.0 (CTF-Agent/1.0; challenge-parser)",
        "Accept": "text/html,application/xhtml+xml,text/plain,*/*",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    if auth_token:
        headers["Authorization"] = f"Token {auth_token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            content_type: str = resp.headers.get_content_type() or "text/plain"
            charset: str = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read()
            try:
                return content_type, raw.decode(charset, errors="replace")
            except LookupError:
                return content_type, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} {exc.reason} — {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"URL error fetching {url}: {exc.reason}") from exc


def _html_to_text(html: str) -> str:
    """Minimal HTML → plain-text (no external deps required)."""
    # Drop <script> and <style> blocks entirely
    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    # Replace block-level tags with newlines
    html = re.sub(r"<(?:br|p|div|li|tr|h[1-6])[^>]*>", "\n", html, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Decode common HTML entities
    for entity, char in [
        ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
        ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " "),
    ]:
        text = text.replace(entity, char)
    # Collapse blank lines while keeping paragraph structure
    lines = [ln.strip() for ln in text.splitlines()]
    # Remove consecutive blank lines
    cleaned: list[str] = []
    prev_blank = False
    for ln in lines:
        is_blank = not ln
        if is_blank and prev_blank:
            continue
        cleaned.append(ln)
        prev_blank = is_blank
    return "\n".join(cleaned).strip()


# ---------------------------------------------------------------------------
# LLM parsing
# ---------------------------------------------------------------------------

async def _llm_parse(url: str, text: str) -> dict[str, Any]:
    """Ask a Claude model to extract challenge data from page text."""
    try:
        from claude_agent_sdk import (  # type: ignore[import]
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            TextBlock,
        )
    except ImportError as exc:
        raise RuntimeError("claude_agent_sdk is required for URL parsing") from exc

    prompt = f"Source URL: {url}\n\nPage content:\n{text}"

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        system_prompt=_PARSE_SYSTEM_PROMPT,
        max_turns=1,
        permission_mode="bypassPermissions",
    )

    result_text = ""
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        result_text += block.text

    # Extract the JSON object from the response (model may emit surrounding prose)
    json_match = re.search(r"\{[\s\S]*\}", result_text)
    if not json_match:
        raise RuntimeError(
            f"No JSON found in LLM response (first 300 chars): {result_text[:300]}"
        )
    return json.loads(json_match.group())


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------

def _render_markdown(parsed: dict[str, Any]) -> str:
    """Produce a human-readable markdown summary of the parsed result."""
    lines: list[str] = []

    comp = parsed.get("competition_name")
    url = parsed.get("source_url", "")
    if comp:
        lines.append(f"# {comp}")
    if url:
        lines.append(f"**Source:** {url}")
    lines.append("")

    challenges: list[dict[str, Any]] = parsed.get("challenges") or []
    if not challenges:
        lines.append("_No challenges could be parsed from this URL._")
        return "\n".join(lines)

    for ch in challenges:
        name = ch.get("name") or "Unnamed"
        category = ch.get("category") or "?"
        points = ch.get("points")
        solves = ch.get("solve_count")
        author = ch.get("author")

        lines.append(f"## {name}")

        meta_parts = [f"Category: **{category}**"]
        if points is not None:
            meta_parts.append(f"Points: **{points}**")
        if solves is not None:
            meta_parts.append(f"Solves: **{solves}**")
        if author:
            meta_parts.append(f"Author: *{author}*")
        lines.append(" | ".join(meta_parts))
        lines.append("")

        desc = (ch.get("description") or "").strip()
        if desc:
            lines.append(desc)
            lines.append("")

        conn = ch.get("connection")
        if conn:
            if conn.get("url"):
                lines.append(f"**URL:** `{conn['url']}`")
            elif conn.get("host") and conn.get("port"):
                proto = conn.get("protocol") or "nc"
                lines.append(
                    f"**Connection:** `{proto} {conn['host']} {conn['port']}`"
                )
            lines.append("")

        files: list[str] = ch.get("files") or []
        if files:
            lines.append(f"**Files:** {', '.join(f'`{f}`' for f in files)}")
            lines.append("")

        tags: list[str] = ch.get("tags") or []
        if tags:
            lines.append(f"**Tags:** {', '.join(tags)}")
            lines.append("")

        hints: list[str] = ch.get("hints") or []
        if hints:
            lines.append("**Hints:**")
            for hint in hints:
                lines.append(f"- {hint}")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def parse_challenge_url(
    url: str,
    *,
    cookie_header: str = "",
    auth_token: str = "",
) -> dict[str, Any]:
    """Fetch a URL and return structured CTF challenge data.

    Args:
        url: Any URL pointing to CTF challenge(s).  Works for CTFd challenge
             pages, DreamHack problem pages, GitHub repos with a README, raw
             text pages, etc.
        cookie_header: Optional raw Cookie header for authenticated fetches
             (e.g. a CTFd session cookie so a private challenges page
             returns real content instead of a login redirect).
        auth_token: Optional CTFd API token.

    Returns:
        A dict with:
          - ``challenges``        list of parsed challenge dicts
          - ``competition_name``  CTF event name (or None)
          - ``source_url``        the original URL
          - ``raw_text_preview``  first 500 chars of the scraped text
          - ``markdown_summary``  human-readable markdown (for the operator UI)
          - ``auth_used``         dict describing which auth inputs were used
          - ``error``             only present on fetch / parse failure
    """
    auth_used = {
        "cookie": bool(cookie_header),
        "token": bool(auth_token),
    }
    # Step 1: fetch
    try:
        content_type, raw_body = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: _fetch_url(url, cookie_header=cookie_header, auth_token=auth_token),
        )
    except RuntimeError as exc:
        logger.warning("url_parser: fetch failed for %s: %s", url, exc)
        return {
            "error": str(exc),
            "source_url": url,
            "challenges": [],
            "markdown_summary": f"Failed to fetch URL: {exc}",
            "raw_text_preview": "",
            "auth_used": auth_used,
        }

    # Step 2: normalise to plain text
    if "html" in content_type:
        text = _html_to_text(raw_body)
    else:
        text = raw_body

    raw_text_preview = text[:500]
    text_for_llm = text[:_MAX_TEXT_FOR_LLM]

    # Step 3: LLM extraction
    try:
        parsed = await _llm_parse(url, text_for_llm)
    except Exception as exc:
        logger.warning("url_parser: LLM parse failed for %s: %s", url, exc)
        parsed = {"challenges": [], "competition_name": None}

    # Step 4: annotate and render
    parsed["source_url"] = url
    parsed["raw_text_preview"] = raw_text_preview
    parsed["markdown_summary"] = _render_markdown(parsed)
    parsed["auth_used"] = auth_used
    return parsed


# ---------------------------------------------------------------------------
# Local-directory parsing (for local_mode: no network, read description +
# binary files from disk and extract metadata via LLM)
# ---------------------------------------------------------------------------

_DESCRIPTION_GLOB_EXTS = {".txt", ".md", ".markdown", ".rst", ".json", ".yml", ".yaml"}
_DESCRIPTION_FILENAMES = {
    "readme", "description", "desc", "problem",
    "challenge", "instructions", "info", "prompt",
}
_BINARY_EXTS_LOCAL = {
    ".bin", ".exe", ".elf", ".so", ".dll", ".dylib",
    ".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".xz",
    ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".wav", ".mp3", ".mp4",
    ".pyc", ".class", ".jar", ".apk",
}


def _read_local_directory(directory: str) -> tuple[list[str], list[str], str]:
    """Walk a directory and return (text_contents, binary_filenames, tree).

    Text files (description / readme / config) are read into the first list.
    Binary files (binaries / archives / media) are listed by name in the second.
    ``tree`` is a compact tree view of the directory for the LLM prompt.
    """
    import os as _os
    from pathlib import Path

    root = Path(directory).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise RuntimeError(f"Directory does not exist or is not a directory: {root}")

    text_blocks: list[str] = []
    binary_files: list[str] = []
    tree_lines: list[str] = []

    # Cap what we send to LLM so a repo full of sample files doesn't blow up.
    max_text_per_file = 8_000
    max_total_text = 24_000
    total_text = 0

    for dirpath, dirnames, filenames in _os.walk(root):
        # Skip common noise dirs
        dirnames[:] = [
            d for d in sorted(dirnames)
            if d not in {".git", "__pycache__", ".venv", "node_modules", ".cache"}
        ]
        rel_dir = Path(dirpath).relative_to(root)
        for name in sorted(filenames):
            full_path = Path(dirpath) / name
            rel = (rel_dir / name) if str(rel_dir) != "." else Path(name)
            suffix = full_path.suffix.lower()
            name_lower = name.lower()
            stem_lower = full_path.stem.lower()

            # Classify: text vs binary
            is_text = (
                suffix in _DESCRIPTION_GLOB_EXTS
                or stem_lower in _DESCRIPTION_FILENAMES
                or name_lower in _DESCRIPTION_FILENAMES
            )
            is_binary = suffix in _BINARY_EXTS_LOCAL

            size = 0
            try:
                size = full_path.stat().st_size
            except OSError:
                pass
            tree_lines.append(f"  {rel}  ({size} bytes)")

            if is_text and total_text < max_total_text:
                try:
                    text = full_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                text = text[:max_text_per_file].strip()
                if not text:
                    continue
                total_text += len(text)
                text_blocks.append(f"=== FILE: {rel} ===\n{text}")
            elif is_binary or not is_text:
                binary_files.append(str(rel))

    tree = "\n".join(tree_lines[:200])
    return text_blocks, binary_files, tree


def _build_local_prompt(directory: str, text_blocks: list[str], binaries: list[str], tree: str) -> str:
    """Synthesise a compact prompt for the LLM from local-directory contents."""
    body_parts = [
        f"Source directory: {directory}",
        "",
        f"Directory tree (first 200 entries):\n{tree or '(empty)'}",
        "",
    ]
    if binaries:
        body_parts.append(
            "Binary / attachment files (these are the challenge distfiles — "
            "list them by filename in the 'files' field):\n"
            + "\n".join(f"  - {b}" for b in binaries[:40])
        )
        body_parts.append("")
    if text_blocks:
        body_parts.append("Text files (descriptions / readmes):")
        body_parts.extend(text_blocks)
    else:
        body_parts.append("(no readable text files found)")
    return "\n".join(body_parts)


async def parse_local_directory(directory: str) -> dict[str, Any]:
    """Parse a local challenge directory into structured metadata.

    Designed for ``--local`` mode where the operator has a folder containing
    challenge binaries + a description file and wants the swarm to solve it
    without any CTFd-style remote platform.  Workflow:

    1. Walk the directory, classifying files into text (descriptions/readmes)
       and binary (executables/archives/media).
    2. Hand the text + file list to the LLM with the same extractor prompt
       used by ``parse_challenge_url``.
    3. Return the structured result with ``source_path`` instead of
       ``source_url``.  The caller can then create the ``challenges/<slug>/``
       layout on disk from this result.

    No network.  No Claude auth dependency beyond the normal LLM call.
    """
    try:
        text_blocks, binaries, tree = _read_local_directory(directory)
    except RuntimeError as exc:
        logger.warning("url_parser: local dir scan failed for %s: %s", directory, exc)
        return {
            "error": str(exc),
            "source_path": directory,
            "challenges": [],
            "markdown_summary": f"Failed to read directory: {exc}",
            "raw_text_preview": "",
            "binary_files": [],
        }

    prompt_text = _build_local_prompt(directory, text_blocks, binaries, tree)
    raw_text_preview = prompt_text[:500]

    try:
        parsed = await _llm_parse(directory, prompt_text[:_MAX_TEXT_FOR_LLM])
    except Exception as exc:  # noqa: BLE001
        logger.warning("url_parser: LLM parse failed for %s: %s", directory, exc)
        parsed = {"challenges": [], "competition_name": None}

    # Inject the binary file list into each parsed challenge if LLM missed them.
    challenges_list = parsed.get("challenges") or []
    if isinstance(challenges_list, list) and binaries:
        for ch in challenges_list:
            if not isinstance(ch, dict):
                continue
            if not ch.get("files"):
                ch["files"] = list(binaries)

    parsed["source_path"] = directory
    parsed["source_url"] = ""  # compat with UI that expects source_url
    parsed["raw_text_preview"] = raw_text_preview
    parsed["binary_files"] = binaries
    parsed["markdown_summary"] = _render_markdown(parsed)
    return parsed
