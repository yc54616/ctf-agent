from __future__ import annotations

from pathlib import Path

import pytest

from backend.agents.swarm import ChallengeSwarm
from backend.sandbox import (
    SHARED_ARTIFACTS_CONTAINER_ROOT,
    ExecResult,
    FilePointer,
    FileReadResult,
    _OutputSpooler,
)
from backend.tools.core import do_bash, do_read_file, do_view_image


class _FakeBashSandbox:
    def __init__(self, result: ExecResult) -> None:
        self.result = result

    async def exec(self, command: str, timeout_s: int = 60) -> ExecResult:
        return self.result


class _FakeReadSandbox:
    def __init__(self, result: FileReadResult) -> None:
        self.result = result

    async def read_file(self, path: str, *, inline_limit_bytes: int | None = None) -> FileReadResult:
        return self.result


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
    preview, pointer, total_bytes = spooler.finalize()

    assert preview == "hello"
    assert total_bytes == 11
    assert pointer is not None
    assert pointer.size_bytes == 11
    assert pointer.host_path is not None
    assert Path(pointer.host_path).read_bytes() == b"hello-world"


@pytest.mark.asyncio
async def test_do_bash_returns_preview_and_saved_path_for_spilled_output() -> None:
    sandbox = _FakeBashSandbox(
        ExecResult(
            exit_code=0,
            stdout="line-1\nline-2\n",
            stderr="",
            stdout_bytes=120_000,
            stdout_pointer=FilePointer(
                container_path=f"{SHARED_ARTIFACTS_CONTAINER_ROOT}/stdout.log",
                size_bytes=120_000,
            ),
        )
    )

    out = await do_bash(sandbox, "yes | head")

    assert "[stdout preview]" in out
    assert f"{SHARED_ARTIFACTS_CONTAINER_ROOT}/stdout.log" in out
    assert "sed -n '1,120p'" in out


@pytest.mark.asyncio
async def test_do_read_file_returns_pointer_for_large_text() -> None:
    sandbox = _FakeReadSandbox(
        FileReadResult(
            path="/challenge/distfiles/huge.txt",
            data=b"header\nvalue=1\n",
            size_bytes=900_000,
            pointer=FilePointer(
                container_path="/challenge/distfiles/huge.txt",
                size_bytes=900_000,
            ),
        )
    )

    out = await do_read_file(sandbox, "/challenge/distfiles/huge.txt")

    assert "Large text file kept at /challenge/distfiles/huge.txt" in out
    assert "[text preview]" in out
    assert "rg -n 'pattern'" in out


@pytest.mark.asyncio
async def test_do_read_file_returns_binary_pointer_hint_for_large_binary() -> None:
    sandbox = _FakeReadSandbox(
        FileReadResult(
            path="/challenge/distfiles/dump.bin",
            data=b"\x00\x01\x02ABC\x00",
            size_bytes=4_000_000,
            pointer=FilePointer(
                container_path="/challenge/distfiles/dump.bin",
                size_bytes=4_000_000,
            ),
        )
    )

    out = await do_read_file(sandbox, "/challenge/distfiles/dump.bin")

    assert "Large binary file at /challenge/distfiles/dump.bin" in out
    assert "xxd /challenge/distfiles/dump.bin | head -40" in out
    assert "binwalk /challenge/distfiles/dump.bin" in out


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
        meta=object(),  # type: ignore[arg-type]
        ctfd=object(),  # type: ignore[arg-type]
        cost_tracker=object(),  # type: ignore[arg-type]
        settings=object(),  # type: ignore[arg-type]
        model_specs=[],
    )

    summary = swarm._shareable_text(
        "finding-test",
        "A" * 800,
        threshold=500,
    )

    assert "[artifact]" in summary
    assert SHARED_ARTIFACTS_CONTAINER_ROOT in summary
    shared_dir = challenge_dir / ".shared-artifacts"
    written = list(shared_dir.iterdir())
    assert len(written) == 1
    assert written[0].read_text(encoding="utf-8") == "A" * 800
