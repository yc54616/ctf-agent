from __future__ import annotations

import asyncio
import tarfile
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest

from backend.agents.swarm import ChallengeSwarm
from backend.cost_tracker import CostTracker
from backend.prompts import ChallengeMeta
from backend.sandbox import (
    SHARED_ARTIFACTS_CONTAINER_ROOT,
    ExecResult,
    FilePointer,
    FileReadResult,
    _OutputSpooler,
)
from backend.tools.core import (
    do_bash,
    do_find_files,
    do_inspect_path,
    do_list_archive,
    do_peek_file,
    do_search_text,
    do_view_image,
    do_web_fetch,
)


class _FakeBashSandbox:
    def __init__(self, result: ExecResult, tmp_path: Path | None = None) -> None:
        self.result = result
        self.tmp_path = tmp_path
        self.saved: list[FilePointer] = []
        self.commands: list[str] = []

    async def exec(self, command: str, timeout_s: int = 60) -> ExecResult:
        self.commands.append(command)
        return self.result

    async def save_shared_artifact(self, prefix: str, content: str | bytes, suffix: str = ".log") -> FilePointer:
        if self.tmp_path is None:
            raise RuntimeError("tmp_path not configured")
        data = content.encode("utf-8") if isinstance(content, str) else content
        host_path = self.tmp_path / f"{prefix}{suffix}"
        host_path.write_bytes(data)
        pointer = FilePointer(
            container_path=f"{SHARED_ARTIFACTS_CONTAINER_ROOT}/{host_path.name}",
            host_path=str(host_path),
            size_bytes=len(data),
        )
        self.saved.append(pointer)
        return pointer

    def allocate_shared_artifact(self, prefix: str, suffix: str = ".log") -> FilePointer:
        if self.tmp_path is None:
            raise RuntimeError("tmp_path not configured")
        host_path = self.tmp_path / f"{prefix}{suffix}"
        pointer = FilePointer(
            container_path=f"{SHARED_ARTIFACTS_CONTAINER_ROOT}/{host_path.name}",
            host_path=str(host_path),
            size_bytes=0,
        )
        self.saved.append(pointer)
        return pointer


class _FakeReadSandbox:
    def __init__(self, result: FileReadResult) -> None:
        self.result = result

    async def read_file(self, path: str, *, inline_limit_bytes: int | None = None) -> FileReadResult:
        return self.result


class _FakeSequentialSandbox(_FakeBashSandbox):
    def __init__(self, results: list[ExecResult], tmp_path: Path | None = None) -> None:
        super().__init__(results[0] if results else ExecResult(exit_code=0, stdout="", stderr=""), tmp_path=tmp_path)
        self.results = list(results)
        self.commands: list[str] = []

    async def exec(self, command: str, timeout_s: int = 60) -> ExecResult:
        self.commands.append(command)
        if not self.results:
            raise AssertionError("No more fake exec results configured")
        return self.results.pop(0)


class _LocalExecSandbox(_FakeBashSandbox):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(ExecResult(exit_code=0, stdout="", stderr=""), tmp_path=tmp_path)

    async def exec(self, command: str, timeout_s: int = 60) -> ExecResult:
        proc = await asyncio.create_subprocess_shell(
            command,
            executable="/bin/bash",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            raise
        return ExecResult(
            exit_code=proc.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )


class _FakeStreamResponse:
    def __init__(self, chunks: list[bytes], status_code: int = 200, reason_phrase: str = "OK", encoding: str = "utf-8") -> None:
        self._chunks = chunks
        self.status_code = status_code
        self.reason_phrase = reason_phrase
        self.encoding = encoding

    async def __aenter__(self) -> _FakeStreamResponse:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeHttpClient:
    def __init__(self, response: _FakeStreamResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeHttpClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    def stream(self, method: str, url: str, content=None, headers=None) -> _FakeStreamResponse:
        return self._response


def test_output_spooler_spills_to_file(tmp_path: Path) -> None:
    def pointer_factory(prefix: str, suffix: str) -> FilePointer:
        host_path = tmp_path / f"{prefix}{suffix}"
        return FilePointer(
            container_path=f"{SHARED_ARTIFACTS_CONTAINER_ROOT}/{host_path.name}",
            host_path=str(host_path),
            size_bytes=0,
        )

    spooler = _OutputSpooler(
        label="stdout",
        spill_threshold_bytes=8,
        preview_bytes=5,
        pointer_factory=pointer_factory,
    )

    spooler.feed(b"hello")
    spooler.feed(b"-world")
    preview, pointer, total_bytes, total_lines = spooler.finalize()

    assert preview == "hello"
    assert total_bytes == 11
    assert total_lines == 1
    assert pointer is not None
    assert pointer.size_bytes == 11
    assert pointer.host_path is not None
    assert Path(pointer.host_path).read_bytes() == b"hello-world"


@pytest.mark.asyncio
async def test_do_bash_returns_saved_path_without_preview_for_spilled_output() -> None:
    sandbox = _FakeBashSandbox(
        ExecResult(
            exit_code=0,
            stdout="line-1\nline-2\n",
            stderr="",
            stdout_bytes=120_000,
            stdout_lines=2,
            stdout_pointer=FilePointer(
                container_path=f"{SHARED_ARTIFACTS_CONTAINER_ROOT}/stdout.log",
                size_bytes=120_000,
            ),
        )
    )

    out = await do_bash(sandbox, "yes | head")

    assert "[stdout preview]" not in out
    assert f"[stdout saved] {SHARED_ARTIFACTS_CONTAINER_ROOT}/stdout.log (120000 bytes, 2 lines)" in out
    assert "sed -n '1,120p'" in out


@pytest.mark.asyncio
async def test_do_bash_materializes_medium_output_even_without_sandbox_pointer(tmp_path: Path) -> None:
    sandbox = _FakeBashSandbox(
        ExecResult(
            exit_code=0,
            stdout=("line\n" * 200).strip(),
            stderr="",
        ),
        tmp_path=tmp_path,
    )

    out = await do_bash(sandbox, "cat lots.txt")

    assert "[stdout preview]" not in out
    assert "[stdout saved] /challenge/shared-artifacts/stdout.log (999 bytes, 200 lines)" in out
    assert "sed -n '1,120p' /challenge/shared-artifacts/stdout.log" in out
    assert sandbox.saved
    assert Path(sandbox.saved[0].host_path or "").read_text(encoding="utf-8").startswith("line\nline\n")


@pytest.mark.asyncio
async def test_do_bash_keeps_small_output_inline() -> None:
    sandbox = _FakeBashSandbox(
        ExecResult(
            exit_code=0,
            stdout="flag-ish candidate\n",
            stderr="",
        )
    )

    out = await do_bash(sandbox, "printf 'flag-ish candidate\\n'")

    assert out == "flag-ish candidate"


@pytest.mark.asyncio
async def test_do_bash_blocks_forbidden_reread_patterns() -> None:
    sandbox = _FakeBashSandbox(ExecResult(exit_code=0, stdout="should not run", stderr=""))

    out = await do_bash(sandbox, "sed -n '1,80p' /challenge/host-logs/trace-test.jsonl")

    assert "Blocked reread of prior traces or solve history" in out
    assert "/challenge/distfiles" in out
    assert not sandbox.commands


@pytest.mark.asyncio
async def test_do_bash_blocks_relative_python_reread_patterns() -> None:
    sandbox = _FakeBashSandbox(ExecResult(exit_code=0, stdout="should not run", stderr=""))

    out = await do_bash(
        sandbox,
        "python3 - <<'PY'\nfrom pathlib import Path\nprint(Path('challenge-src/solve/result.json').read_text())\nPY",
    )

    assert "Blocked reread of prior traces or solve history" in out
    assert not sandbox.commands


@pytest.mark.asyncio
async def test_do_web_fetch_materializes_medium_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = _FakeBashSandbox(
        ExecResult(exit_code=0, stdout="", stderr=""),
        tmp_path=tmp_path,
    )
    body = ("<html>\n" + ("A" * 2000) + "\n") * 3
    response = _FakeStreamResponse([body.encode("utf-8")])

    monkeypatch.setattr("backend.tools.core.httpx.AsyncClient", lambda **kwargs: _FakeHttpClient(response))

    out = await do_web_fetch("https://example.test", sandbox=sandbox)

    assert "HTTP 200 OK" in out
    assert "[body preview]" in out
    assert "[body saved] /challenge/shared-artifacts/web-fetch.http" in out
    assert "rg -n 'pattern' /challenge/shared-artifacts/web-fetch.http" in out
    assert sandbox.saved
    assert Path(sandbox.saved[-1].host_path or "").read_text(encoding="utf-8").startswith("<html>\nAAAA")


@pytest.mark.asyncio
async def test_do_web_fetch_preserves_large_single_chunk_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = _FakeBashSandbox(
        ExecResult(exit_code=0, stdout="", stderr=""),
        tmp_path=tmp_path,
    )
    body = b"A" * 10_000
    response = _FakeStreamResponse([body])

    monkeypatch.setattr("backend.tools.core.httpx.AsyncClient", lambda **kwargs: _FakeHttpClient(response))

    out = await do_web_fetch("https://example.test", sandbox=sandbox)

    assert "[body saved] /challenge/shared-artifacts/web-fetch.http" in out
    saved = sandbox.saved[-1]
    assert saved.size_bytes == len(body)
    assert Path(saved.host_path or "").read_bytes() == body


@pytest.mark.asyncio
async def test_do_web_fetch_rejects_local_file_urls() -> None:
    out = await do_web_fetch("file:///challenge/distfiles/flag.txt")

    assert "web_fetch only supports http:// or https:// URLs" in out
    assert "`bash`" in out
    assert "/challenge/shared-artifacts/" in out


@pytest.mark.asyncio
async def test_do_web_fetch_rejects_local_container_paths() -> None:
    out = await do_web_fetch("/challenge/distfiles/flag.txt")

    assert "web_fetch only supports http:// or https:// URLs" in out
    assert "`bash`" in out
    assert "/challenge/shared-artifacts/" in out


@pytest.mark.asyncio
async def test_do_find_files_stops_at_limit_without_full_listing(tmp_path: Path) -> None:
    root = tmp_path / "distfiles"
    root.mkdir()
    for idx in range(12):
        (root / f"file-{idx:03d}.txt").write_text("x", encoding="utf-8")

    sandbox = _LocalExecSandbox(tmp_path)
    out = await do_find_files(sandbox, str(root), maxdepth=3, kind="files", limit=5)

    assert str(root / "file-000.txt") in out
    assert str(root / "file-004.txt") in out
    assert str(root / "file-005.txt") not in out
    assert "... [stopped after 5 entries]" in out
    assert "[saved]" not in out


@pytest.mark.asyncio
async def test_do_peek_file_text_uses_numbered_preview() -> None:
    sandbox = _FakeBashSandbox(
        ExecResult(
            exit_code=0,
            stdout="     5: alpha\n     6: beta\n",
            stderr="",
        )
    )

    out = await do_peek_file(sandbox, "/challenge/distfiles/readme.txt", mode="text", start_line=5, line_count=2)

    assert "5: alpha" in out
    assert "6: beta" in out


@pytest.mark.asyncio
async def test_do_peek_file_hex_renders_offsets() -> None:
    sandbox = _FakeBashSandbox(
        ExecResult(
            exit_code=0,
            stdout="00000010: 41 42 43 44                                      ABCD\n",
            stderr="",
        )
    )

    out = await do_peek_file(sandbox, "/challenge/distfiles/blob.bin", mode="hex", byte_offset=16, byte_count=4)

    assert "00000010:" in out
    assert "41 42 43 44" in out


@pytest.mark.asyncio
async def test_do_search_text_stops_at_limit_without_scanning_full_results(tmp_path: Path) -> None:
    root = tmp_path / "distfiles"
    root.mkdir()
    for idx in range(12):
        (root / f"config-{idx:03d}.txt").write_text(
            f"before\nCONFIG_BPF=y {idx}\nafter\n",
            encoding="utf-8",
        )

    sandbox = _LocalExecSandbox(tmp_path)
    out = await do_search_text(sandbox, str(root), "CONFIG_BPF", limit=5)

    assert "config-000.txt:2:CONFIG_BPF=y 0" in out
    assert "config-004.txt:2:CONFIG_BPF=y 4" in out
    assert "config-005.txt:2:CONFIG_BPF=y 5" not in out
    assert "... [stopped after 5 matches]" in out
    assert "[saved]" not in out


@pytest.mark.asyncio
async def test_do_search_text_preserves_overlapping_matches_with_context(tmp_path: Path) -> None:
    root = tmp_path / "distfiles"
    root.mkdir()
    target = root / "config.txt"
    target.write_text(
        "\n".join(
            [
                "before-1",
                "CONFIG_BPF first",
                "bridge",
                "CONFIG_BPF second",
                "after-1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    sandbox = _LocalExecSandbox(tmp_path)
    out = await do_search_text(sandbox, str(root), "CONFIG_BPF", context_lines=1, limit=5)

    assert f"{target}:2:CONFIG_BPF first" in out
    assert f"{target}:4:CONFIG_BPF second" in out
    assert f"{target}-3:bridge" in out
    assert out.count("--") == 0


@pytest.mark.asyncio
async def test_do_search_text_handles_utf16_text_files(tmp_path: Path) -> None:
    root = tmp_path / "distfiles"
    root.mkdir()
    target = root / "unicode.txt"
    target.write_text("before\nCONFIG_BPF=y\nafter\n", encoding="utf-16")

    sandbox = _LocalExecSandbox(tmp_path)
    out = await do_search_text(sandbox, str(root), "CONFIG_BPF", limit=5)

    assert f"{target}:2:CONFIG_BPF=y" in out


@pytest.mark.asyncio
async def test_do_inspect_path_includes_metadata_and_preview() -> None:
    sandbox = _FakeSequentialSandbox(
        [
            ExecResult(
                exit_code=0,
                stdout='{"path": "/challenge/distfiles/run.sh", "kind": "file", "mode": "0o755", "size": 42, "executable": true, "symlink_target": null, "binary": false, "sha256": "abc123"}',
                stderr="",
            ),
            ExecResult(
                exit_code=0,
                stdout="     1: #!/bin/sh\n     2: echo hi\n",
                stderr="",
            ),
        ]
    )

    out = await do_inspect_path(sandbox, "/challenge/distfiles/run.sh")

    assert "path: /challenge/distfiles/run.sh" in out
    assert "kind: file" in out
    assert "mode: 0o755" in out
    assert "sha256: abc123" in out
    assert "echo hi" in out


@pytest.mark.asyncio
async def test_do_list_archive_stops_at_limit_without_full_member_dump(tmp_path: Path) -> None:
    archive_root = tmp_path / "src"
    archive_root.mkdir()
    for idx in range(12):
        (archive_root / f"file-{idx:03d}.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    archive_path = tmp_path / "src.tar"
    with tarfile.open(archive_path, "w") as tf:
        for child in sorted(archive_root.iterdir()):
            tf.add(child, arcname=f"src/{child.name}")

    sandbox = _LocalExecSandbox(tmp_path)
    out = await do_list_archive(sandbox, str(archive_path), limit=5)

    assert "src/file-000.c" in out
    assert "src/file-004.c" in out
    assert "src/file-005.c" not in out
    assert "... [stopped after 5 entries]" in out
    assert "[saved]" not in out


@pytest.mark.asyncio
async def test_do_list_archive_skips_large_zip_member_enumeration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive_root = tmp_path / "src"
    archive_root.mkdir()
    for idx in range(3):
        (archive_root / f"file-{idx:03d}.txt").write_text("payload\n", encoding="utf-8")
    archive_path = tmp_path / "src.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        for child in sorted(archive_root.iterdir()):
            zf.write(child, arcname=f"src/{child.name}")

    monkeypatch.setattr("backend.tools.core.LIST_ARCHIVE_MAX_ZIP_BYTES", 1)

    sandbox = _LocalExecSandbox(tmp_path)
    out = await do_list_archive(sandbox, str(archive_path), limit=5)

    assert f"{archive_path}\t(zip archive," in out
    assert (
        "sampled first" in out
        or "src/file-000.txt" in out
        or "skipped member enumeration for large zip" in out
    )


@pytest.mark.asyncio
async def test_do_view_image_uses_read_file_inline_limit() -> None:
    png = bytes([0x89, 0x50, 0x4E, 0x47]) + b"rest"
    sandbox = _FakeReadSandbox(
        FileReadResult(
            path="/challenge/distfiles/pic.png",
            data=png,
            size_bytes=len(png),
        )
    )

    out = await do_view_image(sandbox, "/challenge/distfiles/pic.png", use_vision=True)

    assert out == (png, "image/png")


def test_shared_artifact_summary_is_written_to_challenge_store(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge"
    challenge_dir.mkdir()

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_dir),
        meta=ChallengeMeta(name="artifact-test"),
        ctfd=cast(Any, object()),
        cost_tracker=CostTracker(),
        settings=cast(Any, object()),
        model_specs=[],
    )

    summary = swarm._shareable_text(
        "finding-test",
        "A" * 800,
        threshold=500,
    )

    assert "Pointer:" in summary
    assert SHARED_ARTIFACTS_CONTAINER_ROOT in summary
    shared_dir = challenge_dir / ".shared-artifacts"
    written = list(shared_dir.iterdir())
    assert len(written) == 1
    assert written[0].read_text(encoding="utf-8") == "A" * 800
