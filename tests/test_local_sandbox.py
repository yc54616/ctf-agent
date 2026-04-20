from __future__ import annotations

import asyncio
from pathlib import Path

from backend.local_sandbox import LocalSandbox


def test_local_sandbox_large_read_file_only_reads_preview(monkeypatch, tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge"
    workspace_dir = tmp_path / "workspace"
    artifacts_dir = tmp_path / "artifacts"
    challenge_dir.mkdir()
    workspace_dir.mkdir()
    artifacts_dir.mkdir()
    target = workspace_dir / "big.bin"
    target.write_bytes(b"a" * 1024)

    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(self: Path) -> bytes:
        if self == target:
            raise AssertionError("large file path should not use read_bytes()")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    sandbox = LocalSandbox(
        challenge_dir=str(challenge_dir),
        workspace_dir=str(workspace_dir),
        shared_artifacts_dir=str(artifacts_dir),
        read_file_spill_threshold_bytes=32,
        artifact_preview_bytes=8,
    )

    result = asyncio.run(sandbox.read_file(str(target)))

    assert result.data == b"a" * 8
    assert result.size_bytes == 1024
    assert result.pointer is not None
    assert result.pointer.host_path == str(target)
