"""SDK-agnostic tool logic — pure async functions, no Pydantic AI types."""

import json
import re
import shlex
from pathlib import Path
from urllib.parse import urlparse

import httpx

MAX_OUTPUT = 24_000
INLINE_EXEC_OUTPUT_LIMIT = 2_000
INLINE_EXEC_OUTPUT_LINE_LIMIT = 80
WEB_FETCH_PREVIEW_LIMIT = 8_192
INSPECT_PATH_HASH_LIMIT_BYTES = 32 * 1024 * 1024
LIST_ARCHIVE_MAX_ZIP_BYTES = 64 * 1024 * 1024

_READLIKE_COMMAND_RE = re.compile(r"\b(?:cat|sed|tail|head|rg|grep|find|nl|wc|awk)\b")
_PYTHON_FILE_READ_RE = re.compile(
    r"\bpython3?\b.*(?:\bopen\s*\(|\bPath\s*\([^)]*\)\.(?:read_text|read_bytes)\s*\()",
    re.IGNORECASE | re.DOTALL,
)
_TRACE_JSONL_RE = re.compile(r"(?:^|[\s'\"/=])(?:[^/\s'\"=]+/)*trace-[^/\s'\"=]+\.jsonl\b")
_TARGETED_SHARED_ARTIFACT_READ_RE = re.compile(
    r"\b(?:sed|tail|head|rg|grep)\b.*?/challenge/shared-artifacts/",
    re.IGNORECASE | re.DOTALL,
)
_GENERATED_EXEC_ARTIFACT_RE = re.compile(
    r"/challenge/shared-artifacts/(?:stdout|stderr)(?:-[^/\s'\"=]+)?\.log\b",
    re.IGNORECASE,
)
_FORBIDDEN_REREAD_MARKERS = (
    "/challenge/agent-repo",
    "agent-repo/",
    "/challenge/host-logs",
    "host-logs/",
    "/challenge/challenge-src/solve",
    "challenge-src/solve",
    "/challenge/challenge-src/.shared-artifacts",
    "challenge-src/.shared-artifacts",
)
_BASH_REREAD_BLOCK_MESSAGE = (
    "Blocked reread of prior traces or solve history. "
    "Use allowed roots only: /challenge/distfiles, /challenge/challenge-src "
    "(excluding solve/ and .shared-artifacts/), /challenge/workspace, "
    "/challenge/shared-artifacts, /challenge/metadata.yml"
)
_GENERATED_ARTIFACT_REREAD_BLOCK_MESSAGE = (
    "Blocked whole-file reread of generated stdout/stderr artifacts. "
    "Inspect specific ranges with sed/head/tail/rg, or rerun the original command more narrowly."
)
TARGETED_SHARED_ARTIFACT_INLINE_LIMIT = 8_000
TARGETED_SHARED_ARTIFACT_INLINE_LINE_LIMIT = 120


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


def _trim_preview_text(text: str, *, max_chars: int = INLINE_EXEC_OUTPUT_LIMIT, max_lines: int = INLINE_EXEC_OUTPUT_LINE_LIMIT) -> str:
    lines = text.splitlines()
    preview = "\n".join(lines[:max_lines])
    truncated = len(lines) > max_lines or len(preview) > max_chars
    preview = preview[:max_chars]
    if truncated:
        preview += (
            f"\n... [preview truncated — {len(text)} total chars, {len(lines)} lines]"
        )
    return preview


def _should_materialize_exec_output(text: str, *, line_threshold: int | None = None) -> bool:
    threshold = (
        INLINE_EXEC_OUTPUT_LINE_LIMIT
        if line_threshold is None
        else min(line_threshold, INLINE_EXEC_OUTPUT_LINE_LIMIT)
    )
    return len(text) > INLINE_EXEC_OUTPUT_LIMIT or text.count("\n") > threshold


def _normalize_command_text(command: str) -> str:
    return " ".join(str(command or "").split())


def _count_text_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _should_block_reread_command(command: str) -> bool:
    normalized = _normalize_command_text(command)
    lowered = normalized.lower()
    has_forbidden_path = any(marker in lowered for marker in _FORBIDDEN_REREAD_MARKERS) or bool(
        _TRACE_JSONL_RE.search(lowered)
    )
    if not has_forbidden_path:
        return False
    return bool(_READLIKE_COMMAND_RE.search(lowered) or _PYTHON_FILE_READ_RE.search(normalized))


def _should_block_generated_artifact_wholefile_reread(command: str) -> bool:
    normalized = _normalize_command_text(command)
    lowered = normalized.lower()
    if not _GENERATED_EXEC_ARTIFACT_RE.search(lowered):
        return False
    return bool(re.search(r"\bcat\b", lowered) or _PYTHON_FILE_READ_RE.search(normalized))


def _is_targeted_shared_artifact_read(command: str) -> bool:
    normalized = _normalize_command_text(command)
    return bool(_TARGETED_SHARED_ARTIFACT_READ_RE.search(normalized))


def _saved_exec_result(label: str, pointer, *, line_count: int) -> str:
    return (
        f"[{label} saved] {pointer.container_path} "
        f"({pointer.size_bytes} bytes, {line_count} lines)"
    )


async def _materialize_exec_output(
    sandbox,
    label: str,
    text: str,
    pointer,
    *,
    suffix: str = ".log",
    line_threshold: int | None = None,
):
    if not text:
        return text, pointer
    if pointer is not None:
        return text, pointer
    if not _should_materialize_exec_output(text, line_threshold=line_threshold):
        return text, None

    saver = getattr(sandbox, "save_shared_artifact", None)
    if not callable(saver):
        return text, None

    saved_pointer = await saver(label, text, suffix=suffix)
    return _trim_preview_text(text), saved_pointer


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


def _saved_text_result(label: str, preview: str, pointer) -> str:
    return _truncate(
        "\n".join(
            [
                _preview_block(label, preview),
                f"[saved] {pointer.container_path} ({pointer.size_bytes} bytes)",
                _text_pointer_hint(pointer.container_path),
            ]
        )
    )


async def do_bash(
    sandbox,
    command: str,
    timeout_seconds: int = 60,
) -> str:
    if _should_block_reread_command(command):
        return _BASH_REREAD_BLOCK_MESSAGE
    if _should_block_generated_artifact_wholefile_reread(command):
        return _GENERATED_ARTIFACT_REREAD_BLOCK_MESSAGE

    result = await sandbox.exec(command, timeout_s=timeout_seconds)
    stdout_line_count = int(getattr(result, "stdout_lines", 0) or 0) or _count_text_lines(result.stdout)
    stderr_line_count = int(getattr(result, "stderr_lines", 0) or 0) or _count_text_lines(result.stderr)
    targeted_shared_artifact_read = _is_targeted_shared_artifact_read(command)
    if (
        targeted_shared_artifact_read
        and result.stdout
        and not result.stdout_pointer
        and len(result.stdout) <= TARGETED_SHARED_ARTIFACT_INLINE_LIMIT
        and stdout_line_count <= TARGETED_SHARED_ARTIFACT_INLINE_LINE_LIMIT
    ):
        stdout_text, stdout_pointer = (
            _trim_preview_text(
                result.stdout,
                max_chars=TARGETED_SHARED_ARTIFACT_INLINE_LIMIT,
                max_lines=TARGETED_SHARED_ARTIFACT_INLINE_LINE_LIMIT,
            ),
            None,
        )
    else:
        stdout_text, stdout_pointer = await _materialize_exec_output(
            sandbox,
            "stdout",
            result.stdout,
            result.stdout_pointer,
        )
    if (
        targeted_shared_artifact_read
        and result.stderr
        and not result.stderr_pointer
        and len(result.stderr) <= TARGETED_SHARED_ARTIFACT_INLINE_LIMIT
        and stderr_line_count <= TARGETED_SHARED_ARTIFACT_INLINE_LINE_LIMIT
    ):
        stderr_text, stderr_pointer = (
            _trim_preview_text(
                result.stderr,
                max_chars=TARGETED_SHARED_ARTIFACT_INLINE_LIMIT,
                max_lines=TARGETED_SHARED_ARTIFACT_INLINE_LINE_LIMIT,
            ),
            None,
        )
    else:
        stderr_text, stderr_pointer = await _materialize_exec_output(
            sandbox,
            "stderr",
            result.stderr,
            result.stderr_pointer,
        )
    parts: list[str] = []
    if stdout_text and not stdout_pointer:
        parts.append(stdout_text)
    if stderr_text and not stderr_pointer:
        parts.append(f"[stderr]\n{stderr_text}")
    if stdout_pointer:
        parts.append(_saved_exec_result("stdout", stdout_pointer, line_count=stdout_line_count))
    if stderr_pointer:
        parts.append(_saved_exec_result("stderr", stderr_pointer, line_count=stderr_line_count))
    pointer_paths: list[str] = []
    for pointer in (stdout_pointer, stderr_pointer):
        if pointer and pointer.container_path not in pointer_paths:
            pointer_paths.append(pointer.container_path)
    for pointer_path in pointer_paths:
        parts.append(_text_pointer_hint(pointer_path))
    if result.exit_code != 0:
        parts.append(f"[exit {result.exit_code}]")
    out = "\n".join(parts).strip() or "(no output)"
    return _truncate(out)


async def do_write_file(sandbox, path: str, content: str) -> str:
    try:
        await sandbox.write_file(path, content)
        return f"Written {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error writing file: {e}"


async def do_find_files(
    sandbox,
    path: str,
    maxdepth: int = 3,
    kind: str = "files",
    pattern: str = "",
    limit: int = 200,
) -> str:
    if maxdepth < 0:
        return "Error: maxdepth must be >= 0."
    if limit <= 0:
        return "Error: limit must be > 0."
    if kind not in {"files", "dirs", "all"}:
        return "Error: kind must be one of files, dirs, or all."

    command = (
        "python3 - <<'PY'\n"
        "import fnmatch, os, sys\n"
        f"root = {path!r}\n"
        f"maxdepth = {maxdepth}\n"
        f"kind = {kind!r}\n"
        f"pattern = {pattern!r}\n"
        f"limit = {limit}\n"
        "if not os.path.exists(root):\n"
        "    sys.stderr.write(f'Path not found: {root}')\n"
        "    raise SystemExit(2)\n"
        "count = 0\n"
        "stopped = False\n"
        "def matches(name):\n"
        "    return not pattern or fnmatch.fnmatch(name, pattern)\n"
        "def emit(entry):\n"
        "    global count, stopped\n"
        "    print(entry)\n"
        "    count += 1\n"
        "    if count >= limit:\n"
        "        stopped = True\n"
        "        return True\n"
        "    return False\n"
        "if os.path.isfile(root):\n"
        "    name = os.path.basename(root)\n"
        "    if kind in {'files', 'all'} and maxdepth >= 0 and matches(name):\n"
        "        emit(root)\n"
        "else:\n"
        "    root_norm = root.rstrip(os.sep) or root\n"
        "    base_depth = root_norm.count(os.sep)\n"
        "    for current, dirnames, filenames in os.walk(root, topdown=True):\n"
        "        dirnames.sort()\n"
        "        filenames.sort()\n"
        "        current_norm = current.rstrip(os.sep) or current\n"
        "        depth = current_norm.count(os.sep) - base_depth\n"
        "        if depth > maxdepth:\n"
        "            dirnames[:] = []\n"
        "            continue\n"
        "        current_name = os.path.basename(current_norm) or current_norm\n"
        "        if kind in {'dirs', 'all'} and matches(current_name):\n"
        "            if emit(current):\n"
        "                break\n"
        "        if depth >= maxdepth:\n"
        "            dirnames[:] = []\n"
        "            continue\n"
        "        if kind in {'files', 'all'}:\n"
        "            for filename in filenames:\n"
        "                if not matches(filename):\n"
        "                    continue\n"
        "                if emit(os.path.join(current, filename)):\n"
        "                    break\n"
        "        if stopped:\n"
        "            break\n"
        "if stopped:\n"
        "    print(f'... [stopped after {limit} entries]')\n"
        "PY"
    )
    result = await sandbox.exec(command, timeout_s=60)
    if result.exit_code != 0:
        return result.stderr.strip() or f"Error finding files under {path}"

    stdout_text, stdout_pointer = await _materialize_exec_output(
        sandbox,
        "find-files",
        result.stdout.strip(),
        result.stdout_pointer,
        suffix=".txt",
    )
    if stdout_pointer:
        body = _saved_text_result("find", stdout_text, stdout_pointer)
    else:
        body = stdout_text or f"No matching entries found under {path}."
    return body


async def do_peek_file(
    sandbox,
    path: str,
    mode: str = "text",
    start_line: int = 1,
    line_count: int = 120,
    byte_offset: int = 0,
    byte_count: int = 256,
) -> str:
    if mode not in {"text", "hex"}:
        return "Error: mode must be text or hex."
    if start_line < 1:
        return "Error: start_line must be >= 1."
    if line_count < 1:
        return "Error: line_count must be >= 1."
    if byte_offset < 0:
        return "Error: byte_offset must be >= 0."
    if byte_count < 1:
        return "Error: byte_count must be >= 1."

    if mode == "text":
        script = (
            "python3 - <<'PY'\n"
            "import sys\n"
            f"path = {path!r}\n"
            f"start = {start_line}\n"
            f"count = {line_count}\n"
            "end = start + count - 1\n"
            "try:\n"
            "    with open(path, 'r', encoding='utf-8', errors='replace') as fh:\n"
            "        for idx, line in enumerate(fh, start=1):\n"
            "            if idx < start:\n"
            "                continue\n"
            "            if idx > end:\n"
            "                break\n"
            "            sys.stdout.write(f'{idx:>6}: {line}')\n"
            "except FileNotFoundError:\n"
            "    sys.stderr.write(f'No such file: {path}')\n"
            "    raise SystemExit(2)\n"
            "PY"
        )
    else:
        script = (
            "python3 - <<'PY'\n"
            "import sys\n"
            f"path = {path!r}\n"
            f"offset = {byte_offset}\n"
            f"count = {byte_count}\n"
            "try:\n"
            "    with open(path, 'rb') as fh:\n"
            "        fh.seek(offset)\n"
            "        data = fh.read(count)\n"
            "except FileNotFoundError:\n"
            "    sys.stderr.write(f'No such file: {path}')\n"
            "    raise SystemExit(2)\n"
            "for row_offset in range(0, len(data), 16):\n"
            "    chunk = data[row_offset:row_offset + 16]\n"
            "    hex_part = ' '.join(f'{b:02x}' for b in chunk)\n"
            "    ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)\n"
            "    print(f'{offset + row_offset:08x}: {hex_part:<47}  {ascii_part}')\n"
            "PY"
        )

    result = await sandbox.exec(script, timeout_s=60)
    if result.exit_code != 0:
        return result.stderr.strip() or f"Error peeking {path}"
    stdout_text, stdout_pointer = await _materialize_exec_output(
        sandbox,
        "peek-file",
        result.stdout.strip(),
        result.stdout_pointer,
        suffix=".txt",
    )
    if stdout_pointer:
        body = _saved_text_result("peek", stdout_text, stdout_pointer)
    else:
        body = stdout_text or f"{path} produced no output."
    return body


async def do_search_text(
    sandbox,
    path: str,
    query: str,
    glob: str = "",
    ignore_case: bool = True,
    context_lines: int = 2,
    limit: int = 80,
) -> str:
    if not query:
        return "Error: query must be non-empty."
    if context_lines < 0:
        return "Error: context_lines must be >= 0."
    if limit < 1:
        return "Error: limit must be >= 1."

    script = (
        "python3 - <<'PY'\n"
        "import codecs, collections, fnmatch, os, sys\n"
        f"root = {path!r}\n"
        f"query = {query!r}\n"
        f"glob_pattern = {glob!r}\n"
        f"ignore_case = {ignore_case!r}\n"
        f"context = {context_lines}\n"
        f"limit = {limit}\n"
        "target = query.lower() if ignore_case else query\n"
        "match_count = 0\n"
        "first_match = True\n"
        "def detect_encoding(pathname):\n"
        "    try:\n"
        "        with open(pathname, 'rb') as fh:\n"
        "            sample = fh.read(4096)\n"
        "    except OSError:\n"
        "        return None, True\n"
        "    if sample.startswith(codecs.BOM_UTF8):\n"
        "        return 'utf-8-sig', False\n"
        "    if sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):\n"
        "        return 'utf-16', False\n"
        "    if sample.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):\n"
        "        return 'utf-32', False\n"
        "    if sample and b'\\x00' in sample:\n"
        "        even = sample[::2]\n"
        "        odd = sample[1::2]\n"
        "        even_nuls = even.count(0)\n"
        "        odd_nuls = odd.count(0)\n"
        "        if odd and odd_nuls * 2 >= len(odd) and even_nuls * 4 < max(len(even), 1):\n"
        "            return 'utf-16-le', False\n"
        "        if even and even_nuls * 2 >= len(even) and odd_nuls * 4 < max(len(odd), 1):\n"
        "            return 'utf-16-be', False\n"
        "        return None, True\n"
        "    return 'utf-8', False\n"
        "def iter_files(root_path):\n"
        "    if os.path.isfile(root_path):\n"
        "        yield root_path\n"
        "        return\n"
        "    for current, _, files in os.walk(root_path):\n"
        "        files.sort()\n"
        "        for name in files:\n"
        "            yield os.path.join(current, name)\n"
        "if not os.path.exists(root):\n"
        "    sys.stderr.write(f'Path not found: {root}')\n"
        "    raise SystemExit(2)\n"
        "for candidate in iter_files(root):\n"
        "    if glob_pattern and not fnmatch.fnmatch(os.path.basename(candidate), glob_pattern):\n"
        "        continue\n"
        "    encoding, skip = detect_encoding(candidate)\n"
        "    if skip:\n"
        "        continue\n"
        "    try:\n"
        "        with open(candidate, 'r', encoding=encoding, errors='replace') as fh:\n"
        "            previous = collections.deque(maxlen=context)\n"
        "            last_emitted = 0\n"
        "            line_no = 0\n"
        "            pending_after = 0\n"
        "            while True:\n"
        "                raw = fh.readline()\n"
        "                if not raw:\n"
        "                    break\n"
        "                line_no += 1\n"
        "                line = raw.rstrip('\\n')\n"
        "                hay = line.lower() if ignore_case else line\n"
        "                matched = target in hay\n"
        "                if matched:\n"
        "                    block_start = previous[0][0] if previous else line_no\n"
        "                    if not first_match and (last_emitted == 0 or block_start > last_emitted + 1):\n"
        "                        print('--')\n"
        "                    first_match = False\n"
        "                    for prev_no, prev_line in previous:\n"
        "                        if prev_no > last_emitted:\n"
        "                            print(f'{candidate}-{prev_no}:{prev_line}')\n"
        "                            last_emitted = prev_no\n"
        "                    if line_no > last_emitted:\n"
        "                        print(f'{candidate}:{line_no}:{line}')\n"
        "                        last_emitted = line_no\n"
        "                    match_count += 1\n"
        "                    pending_after = max(pending_after, context)\n"
        "                    if match_count >= limit:\n"
        "                        print(f'... [stopped after {limit} matches]')\n"
        "                        raise SystemExit(0)\n"
        "                elif pending_after > 0 and line_no > last_emitted:\n"
        "                    print(f'{candidate}-{line_no}:{line}')\n"
        "                    last_emitted = line_no\n"
        "                    pending_after -= 1\n"
        "                previous.append((line_no, line))\n"
        "    except OSError:\n"
        "        continue\n"
        "PY"
    )
    result = await sandbox.exec(script, timeout_s=90)
    if result.exit_code != 0:
        return result.stderr.strip() or f"Error searching {path}"
    stdout_text, stdout_pointer = await _materialize_exec_output(
        sandbox,
        "search-text",
        result.stdout.strip(),
        result.stdout_pointer,
        suffix=".txt",
    )
    if stdout_pointer:
        body = _saved_text_result("search", stdout_text, stdout_pointer)
    else:
        body = stdout_text or f"No matches for {query!r} under {path}."
    return body


async def do_inspect_path(sandbox, path: str) -> str:
    script = (
        "python3 - <<'PY'\n"
        "import hashlib, json, os, pathlib, stat, sys\n"
        f"path = {path!r}\n"
        f"hash_limit = {INSPECT_PATH_HASH_LIMIT_BYTES}\n"
        "target = pathlib.Path(path)\n"
        "if not target.exists() and not target.is_symlink():\n"
        "    sys.stderr.write(f'Path not found: {path}')\n"
        "    raise SystemExit(2)\n"
        "info = {\n"
        "    'path': str(target),\n"
        "    'kind': 'symlink' if target.is_symlink() else ('dir' if target.is_dir() else 'file'),\n"
        "    'mode': oct(target.lstat().st_mode & 0o777),\n"
        "    'size': target.stat().st_size if target.exists() and not target.is_dir() else None,\n"
        "    'executable': os.access(target, os.X_OK),\n"
        "    'symlink_target': os.readlink(target) if target.is_symlink() else None,\n"
        "}\n"
        "if target.is_file():\n"
        "    with open(target, 'rb') as fh:\n"
        "        sample = fh.read(4096)\n"
        "    info['binary'] = b'\\x00' in sample\n"
        "    if info['size'] is not None and info['size'] <= hash_limit:\n"
        "        digest = hashlib.sha256()\n"
        "        with open(target, 'rb') as fh:\n"
        "            for chunk in iter(lambda: fh.read(1024 * 1024), b''):\n"
        "                digest.update(chunk)\n"
        "        info['sha256'] = digest.hexdigest()\n"
        "    else:\n"
        "        info['sha256'] = None\n"
        "print(json.dumps(info))\n"
        "PY"
    )
    result = await sandbox.exec(script, timeout_s=90)
    if result.exit_code != 0:
        return result.stderr.strip() or f"Error inspecting {path}"
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return f"Error: invalid inspect payload for {path}"

    lines = [
        f"path: {info['path']}",
        f"kind: {info['kind']}",
        f"mode: {info['mode']}",
        f"executable: {'yes' if info['executable'] else 'no'}",
    ]
    if info.get("size") is not None:
        lines.append(f"size: {info['size']} bytes")
    if info.get("symlink_target"):
        lines.append(f"symlink_target: {info['symlink_target']}")
    if info.get("sha256"):
        lines.append(f"sha256: {info['sha256']}")
    elif info["kind"] == "file":
        lines.append("sha256: omitted for large file")

    if info["kind"] == "file":
        preview = await do_peek_file(
            sandbox,
            path,
            mode="hex" if info.get("binary") else "text",
            line_count=40,
            byte_count=256,
        )
        lines.extend(["", preview])
    return "\n".join(lines)


async def do_list_archive(sandbox, path: str, limit: int = 200) -> str:
    if limit < 1:
        return "Error: limit must be >= 1."

    script = (
        "python3 - <<'PY'\n"
        "import pathlib, shutil, subprocess, sys, tarfile, zipfile\n"
        f"path = {path!r}\n"
        f"limit = {limit}\n"
        f"zip_skip_threshold = {LIST_ARCHIVE_MAX_ZIP_BYTES}\n"
        "archive = pathlib.Path(path)\n"
        "if not archive.exists():\n"
        "    sys.stderr.write(f'Path not found: {path}')\n"
        "    raise SystemExit(2)\n"
        "count = 0\n"
        "def emit(entry):\n"
        "    global count\n"
        "    print(entry)\n"
        "    count += 1\n"
        "    return count >= limit\n"
        "if zipfile.is_zipfile(archive):\n"
        "    archive_size = archive.stat().st_size\n"
        "    if archive_size > zip_skip_threshold:\n"
        "        print(f'{archive}\\t(zip archive, {archive_size} bytes)')\n"
        "        helper = shutil.which('zipinfo') or shutil.which('unzip')\n"
        "        if helper:\n"
        "            helper_name = pathlib.Path(helper).name\n"
        "            cmd = [helper, '-1', str(archive)] if helper_name == 'zipinfo' else [helper, '-Z1', str(archive)]\n"
        "            try:\n"
        "                proc = subprocess.Popen(\n"
        "                    cmd,\n"
        "                    stdout=subprocess.PIPE,\n"
        "                    stderr=subprocess.DEVNULL,\n"
        "                    text=True,\n"
        "                    errors='replace',\n"
        "                )\n"
        "                sampled = 0\n"
        "                assert proc.stdout is not None\n"
        "                for raw in proc.stdout:\n"
        "                    line = raw.rstrip('\\n')\n"
        "                    if not line:\n"
        "                        continue\n"
        "                    print(line)\n"
        "                    sampled += 1\n"
        "                    if sampled >= limit:\n"
        "                        print(f'... [sampled first {limit} entries from large zip via {helper_name}]')\n"
        "                        break\n"
        "                try:\n"
        "                    proc.kill()\n"
        "                except OSError:\n"
        "                    pass\n"
        "                try:\n"
        "                    proc.wait(timeout=1)\n"
        "                except Exception:\n"
        "                    pass\n"
        "                if sampled > 0:\n"
        "                    raise SystemExit(0)\n"
        "            except Exception:\n"
        "                pass\n"
        "        print(f'... [skipped member enumeration for large zip > {zip_skip_threshold} bytes]')\n"
        "        raise SystemExit(0)\n"
        "    with zipfile.ZipFile(archive) as zf:\n"
        "        for info in zf.infolist():\n"
        "            if emit(f'{info.filename}\\t{info.file_size}'):\n"
        "                print(f'... [stopped after {limit} entries]')\n"
        "                break\n"
        "elif tarfile.is_tarfile(archive):\n"
        "    with tarfile.open(archive, 'r:*') as tf:\n"
        "        for member in tf:\n"
        "            if emit(f'{member.name}\\t{member.size}'):\n"
        "                print(f'... [stopped after {limit} entries]')\n"
        "                break\n"
        "elif archive.suffix == '.gz':\n"
        "    print(f'{archive.stem}\\t(gzip stream)')\n"
        "else:\n"
        "    sys.stderr.write(f'Unsupported archive format: {path}')\n"
        "    raise SystemExit(2)\n"
        "PY"
    )
    result = await sandbox.exec(script, timeout_s=90)
    if result.exit_code != 0:
        return result.stderr.strip() or f"Error listing archive {path}"
    stdout_text, stdout_pointer = await _materialize_exec_output(
        sandbox,
        "list-archive",
        result.stdout.strip(),
        result.stdout_pointer,
        suffix=".txt",
    )
    if stdout_pointer:
        body = _saved_text_result("archive", stdout_text, stdout_pointer)
    else:
        body = stdout_text or f"No archive entries found in {path}."
    return body


async def do_fs_query(
    sandbox,
    *,
    action: str,
    path: str,
    maxdepth: int = 3,
    kind: str = "files",
    pattern: str = "",
    limit: int = 200,
    mode: str = "text",
    start_line: int = 1,
    line_count: int = 120,
    byte_offset: int = 0,
    byte_count: int = 256,
    query: str = "",
    glob: str = "",
    ignore_case: bool = True,
    context_lines: int = 2,
) -> str:
    """Dispatch bounded filesystem inspection operations through one tool surface."""
    if action == "find":
        return await do_find_files(
            sandbox,
            path,
            maxdepth=maxdepth,
            kind=kind,
            pattern=pattern,
            limit=limit,
        )
    if action == "peek":
        return await do_peek_file(
            sandbox,
            path,
            mode=mode,
            start_line=start_line,
            line_count=line_count,
            byte_offset=byte_offset,
            byte_count=byte_count,
        )
    if action == "search":
        return await do_search_text(
            sandbox,
            path,
            query=query,
            glob=glob,
            ignore_case=ignore_case,
            context_lines=context_lines,
            limit=limit,
        )
    if action == "inspect":
        return await do_inspect_path(sandbox, path)
    if action == "archive_list":
        return await do_list_archive(sandbox, path, limit=limit)
    return (
        "Error: action must be one of "
        "find, peek, search, inspect, or archive_list."
    )


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


def _web_fetch_scheme_error(url: str) -> str | None:
    stripped = url.strip()
    if not stripped:
        return "Fetch error: URL is empty. web_fetch only supports full http:// or https:// URLs."

    local_hint = (
        "Fetch error: web_fetch only supports http:// or https:// URLs.\n"
        "For local files use `bash` with a short preview or save the output under `/challenge/shared-artifacts/`."
    )
    if stripped.startswith(("file://", "/", "./", "../", "~")):
        return local_hint

    parsed = urlparse(stripped)
    if parsed.scheme == "file":
        return local_hint
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        return (
            f"Fetch error: unsupported URL scheme `{parsed.scheme}`. "
            "web_fetch only supports http:// or https:// URLs."
        )
    if not parsed.scheme:
        return "Fetch error: web_fetch only supports full http:// or https:// URLs."
    return None


async def do_web_fetch(
    url: str,
    method: str = "GET",
    body: str = "",
    sandbox=None,
) -> str:
    if scheme_error := _web_fetch_scheme_error(url):
        return scheme_error
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
            preview = bytearray()
            total_bytes = 0
            saved_pointer = None
            artifact_file = None
            try:
                async for chunk in resp.aiter_bytes():
                    total_bytes += len(chunk)
                    remaining = WEB_FETCH_PREVIEW_LIMIT - len(preview)
                    if remaining > 0:
                        preview.extend(chunk[:remaining])
                    if artifact_file is not None:
                        artifact_file.write(chunk)
                        continue
                    if total_bytes > INLINE_EXEC_OUTPUT_LIMIT:
                        allocator = getattr(sandbox, "allocate_shared_artifact", None)
                        if callable(allocator):
                            saved_pointer = allocator("web-fetch", ".http")
                            if saved_pointer.host_path:
                                artifact_file = Path(saved_pointer.host_path).open("wb")
                                artifact_file.write(preview)
                                missing_from_chunk = total_bytes - len(preview)
                                if missing_from_chunk > 0:
                                    artifact_file.write(chunk[-missing_from_chunk:])
            finally:
                if artifact_file is not None:
                    artifact_file.close()
                    artifact_file = None
            if saved_pointer is not None:
                saved_pointer.size_bytes = total_bytes

            text = preview.decode(resp.encoding or "utf-8", errors="replace")
            prefix = f"HTTP {resp.status_code} {resp.reason_phrase}\n{'─' * 40}\n"
            if saved_pointer is not None:
                return _truncate(
                    "\n".join(
                        [
                            prefix.rstrip("\n"),
                            _preview_block("body", text),
                            f"[body saved] {saved_pointer.container_path} ({saved_pointer.size_bytes} bytes)",
                            _text_pointer_hint(saved_pointer.container_path),
                        ]
                    )
                )
            if total_bytes > len(preview):
                text += f"\n... [truncated, total {total_bytes} bytes]"
            return _truncate(prefix + text)
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
