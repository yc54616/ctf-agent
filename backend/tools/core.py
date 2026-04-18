"""SDK-agnostic tool logic — pure async functions, no Pydantic AI types."""

import json
import shlex
from pathlib import Path

import httpx

MAX_OUTPUT = 24_000


def _truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    lines = text.split("\n")
    head = "\n".join(lines[:200])
    return head[:limit] + f"\n... [truncated — {len(text)} total chars, {len(lines)} lines]"


def _looks_binary(data: bytes) -> bool:
    if not data:
        return False
    non_text = sum(
        1
        for b in data
        if b == 0 or (b < 7) or (13 < b < 32 and b != 27)
    )
    return non_text / len(data) > 0.05


def _preview_block(label: str, preview: str) -> str:
    cleaned = preview.strip("\n")
    return f"[{label} preview]\n{cleaned or '(empty preview)'}"


def _text_pointer_hint(path: str) -> str:
    quoted = shlex.quote(path)
    return (
        "Use bash to inspect specific ranges:\n"
        f"  sed -n '1,120p' {quoted}\n"
        f"  tail -n 120 {quoted}\n"
        f"  rg -n 'pattern' {quoted}"
    )


def _binary_pointer_hint(path: str) -> str:
    quoted = shlex.quote(path)
    return (
        "Use bash to inspect it:\n"
        f"  file {quoted}\n"
        f"  xxd {quoted} | head -40\n"
        f"  strings {quoted} | head -200\n"
        f"  exiftool {quoted}\n"
        f"  binwalk {quoted}"
    )


async def do_bash(sandbox, command: str, timeout_seconds: int = 60) -> str:
    result = await sandbox.exec(command, timeout_s=timeout_seconds)
    parts: list[str] = []
    if result.stdout:
        if result.stdout_pointer:
            parts.append(_preview_block("stdout", result.stdout))
        else:
            parts.append(result.stdout)
    if result.stderr:
        if result.stderr_pointer:
            parts.append(_preview_block("stderr", result.stderr))
        else:
            parts.append(f"[stderr]\n{result.stderr}")
    if result.stdout_pointer:
        parts.append(
            f"[stdout saved] {result.stdout_pointer.container_path} ({result.stdout_pointer.size_bytes} bytes)"
        )
    if result.stderr_pointer:
        parts.append(
            f"[stderr saved] {result.stderr_pointer.container_path} ({result.stderr_pointer.size_bytes} bytes)"
        )
    if result.stdout_pointer or result.stderr_pointer:
        pointer_path = (result.stdout_pointer or result.stderr_pointer).container_path
        parts.append(_text_pointer_hint(pointer_path))
    if result.exit_code != 0:
        parts.append(f"[exit {result.exit_code}]")
    out = "\n".join(parts).strip() or "(no output)"
    return _truncate(out)


async def do_read_file(sandbox, path: str) -> str:
    try:
        result = await sandbox.read_file(path)
    except Exception as e:
        return f"Error reading file: {e}"

    data = result.data
    pointer_path = result.pointer.container_path if result.pointer else path

    if _looks_binary(data):
        label = "Large binary file" if result.pointer else "Binary file"
        return (
            f"{label} at {pointer_path} ({result.size_bytes} bytes).\n"
            f"{_binary_pointer_hint(pointer_path)}"
        )

    text = data.decode("utf-8", errors="replace")
    if result.pointer:
        return _truncate(
            "\n".join(
                [
                    f"Large text file kept at {pointer_path} ({result.size_bytes} bytes).",
                    _preview_block("text", text),
                    _text_pointer_hint(pointer_path),
                ]
            )
        )

    return _truncate(text)


async def do_write_file(sandbox, path: str, content: str) -> str:
    try:
        await sandbox.write_file(path, content)
        return f"Written {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error writing file: {e}"


async def do_list_files(sandbox, path: str = "/challenge/distfiles") -> str:
    result = await sandbox.exec(f"ls -la {shlex.quote(path)}")
    out = result.stdout.strip()
    if result.exit_code != 0:
        return result.stderr.strip() or f"Error listing {path}"
    return out or f"{path} is empty."


async def do_submit_flag(ctfd, challenge_name: str, flag: str) -> tuple[str, bool]:
    """Submit a flag. Returns (display_message, is_confirmed)."""
    flag = flag.strip()
    if not flag:
        return "Empty flag — nothing to submit.", False

    try:
        result = await ctfd.submit_flag(challenge_name, flag)
        is_confirmed = result.status in ("correct", "already_solved")
        return result.display, is_confirmed
    except Exception as e:
        return f"submit_flag error: {e}", False


def _is_internal_url(url: str) -> bool:
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    if host.startswith("169.254.") or host.startswith("10.") or host.startswith("192.168."):
        return True
    if host.startswith("172."):
        try:
            second_octet = int(host.split(".")[1])
            if 16 <= second_octet <= 31:
                return True
        except (ValueError, IndexError):
            pass
    return False


async def do_web_fetch(url: str, method: str = "GET", body: str = "") -> str:
    if _is_internal_url(url):
        return "Fetch error: access to internal/private networks is blocked."
    try:
        # verify=False: CTF challenge services often use self-signed certs
        async with (
            httpx.AsyncClient(verify=False, timeout=30.0) as client,
            client.stream(
                method,
                url,
                content=body or None,
                headers={"User-Agent": "Mozilla/5.0"},
            ) as resp,
        ):
            preview_limit = 20_000
            preview = bytearray()
            total_bytes = 0
            async for chunk in resp.aiter_bytes():
                total_bytes += len(chunk)
                remaining = preview_limit - len(preview)
                if remaining > 0:
                    preview.extend(chunk[:remaining])

            text = preview.decode(resp.encoding or "utf-8", errors="replace")
            prefix = f"HTTP {resp.status_code} {resp.reason_phrase}\n{'─' * 40}\n"
            if total_bytes > len(preview):
                text += f"\n... [truncated, total {total_bytes} bytes]"
            return prefix + text
    except Exception as e:
        return f"Fetch error: {e}"


async def do_webhook_create() -> str:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post("https://webhook.site/token")
            if resp.status_code != 200:
                return f"webhook.site error: HTTP {resp.status_code}"
            data = resp.json()
            return json.dumps({"uuid": data["uuid"], "url": f"https://webhook.site/{data['uuid']}"})
    except Exception as e:
        return f"webhook_create error: {e}"


async def do_webhook_get_requests(uuid: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"https://webhook.site/token/{uuid}/requests")
            if resp.status_code != 200:
                return f"webhook.site error: HTTP {resp.status_code}"
            data = resp.json()
            if not data.get("data"):
                return "No requests received yet."
            out = json.dumps(data["data"], indent=2)
            return out[:8000] if len(out) > 8000 else out
    except Exception as e:
        return f"webhook_get_requests error: {e}"


async def do_check_findings(message_bus, model_spec: str) -> str:
    """Get unread findings from sibling solvers."""
    if not message_bus:
        return "No message bus available."
    findings = await message_bus.check(model_spec)
    if not findings:
        return "No new findings from other agents."
    return message_bus.format_unread(findings)


# Image constants (shared with vision wrapper)
IMAGE_EXTS_FOR_VISION: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".webp": "image/webp",
}

IMAGE_MAGIC: dict[str, list[int]] = {
    "image/png": [0x89, 0x50, 0x4E, 0x47],
    "image/jpeg": [0xFF, 0xD8, 0xFF],
    "image/gif": [0x47, 0x49, 0x46],
    "image/bmp": [0x42, 0x4D],
    "image/webp": [0x52, 0x49, 0x46, 0x46],
}

MAX_IMAGE_BYTES = 4 * 1024 * 1024  # 4 MB


def _has_valid_magic(data: bytes, mime_type: str) -> bool:
    magic = IMAGE_MAGIC.get(mime_type)
    if not magic:
        return True
    return all(i < len(data) and data[i] == b for i, b in enumerate(magic))


async def do_view_image(sandbox, filename: str, use_vision: bool) -> tuple[bytes, str] | str:
    """Returns (image_bytes, media_type) on success, or error string."""
    # Strip leading path if model passes full container path
    basename = Path(filename).name
    ext = Path(basename).suffix.lower()
    mime_type = IMAGE_EXTS_FOR_VISION.get(ext)
    if not mime_type:
        return f"Not a supported image type: {filename}"

    if not use_vision:
        return "Vision not available for this model. Use bash tools (steghide, zsteg, exiftool, strings) instead."

    # Try the filename as-is first (if it's an absolute path), then search standard dirs
    search_paths = []
    if filename.startswith("/"):
        search_paths.append(filename)
    search_paths.extend([f"/challenge/distfiles/{basename}", f"/challenge/workspace/{basename}"])

    for path in search_paths:
        try:
            result = await sandbox.read_file(path, inline_limit_bytes=MAX_IMAGE_BYTES)
            if result.pointer:
                return (
                    f"Image too large for vision ({result.size_bytes / 1024 / 1024:.1f} MB > 4 MB limit). "
                    "Use bash tools (steghide, zsteg, binwalk, exiftool, strings, xxd) instead."
                )
            data = result.data
            if not _has_valid_magic(data, mime_type):
                return (
                    "Cannot load image: file appears invalid or corrupted. "
                    "Fix the magic bytes in the sandbox first, save to /challenge/workspace/, "
                    "then call view_image again."
                )
            return (data, mime_type)
        except Exception:
            continue

    return f"File not found: {filename} (searched: {', '.join(search_paths)})"
