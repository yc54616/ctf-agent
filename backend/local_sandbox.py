"""Container-local sandbox adapter used by the in-sandbox lane runtime."""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

from backend.sandbox import (
    DEFAULT_ARTIFACT_PREVIEW_BYTES,
    DEFAULT_EXEC_OUTPUT_SPILL_THRESHOLD_BYTES,
    DEFAULT_READ_FILE_SPILL_THRESHOLD_BYTES,
    ExecResult,
    FilePointer,
    FileReadResult,
    _OutputSpooler,
    allocate_artifact_pointer,
)


class LocalSandbox:
    """Local filesystem/subprocess view matching the DockerSandbox shape."""

    def __init__(
        self,
        *,
        challenge_dir: str,
        workspace_dir: str,
        shared_artifacts_dir: str,
        exec_output_spill_threshold_bytes: int = DEFAULT_EXEC_OUTPUT_SPILL_THRESHOLD_BYTES,
        read_file_spill_threshold_bytes: int = DEFAULT_READ_FILE_SPILL_THRESHOLD_BYTES,
        artifact_preview_bytes: int = DEFAULT_ARTIFACT_PREVIEW_BYTES,
    ) -> None:
        self.challenge_dir = challenge_dir
        self.workspace_dir = workspace_dir
        self.shared_artifacts_dir = shared_artifacts_dir
        self.exec_output_spill_threshold_bytes = exec_output_spill_threshold_bytes
        self.read_file_spill_threshold_bytes = read_file_spill_threshold_bytes
        self.artifact_preview_bytes = artifact_preview_bytes
        self.container_id = "local-runtime"
        self._lock = asyncio.Lock()

    @property
    def is_started(self) -> bool:
        return True

    async def start(self) -> None:
        Path(self.workspace_dir).mkdir(parents=True, exist_ok=True)
        Path(self.shared_artifacts_dir).mkdir(parents=True, exist_ok=True)

    async def stop(self) -> None:
        return

    def _make_pointer(self, prefix: str, suffix: str) -> FilePointer:
        safe_prefix = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in prefix).strip("-") or "artifact"
        return allocate_artifact_pointer(
            self.shared_artifacts_dir,
            "/challenge/shared-artifacts",
            safe_prefix,
            suffix,
        )

    def allocate_shared_artifact(self, prefix: str, suffix: str = ".log") -> FilePointer:
        return self._make_pointer(prefix, suffix)

    async def save_shared_artifact(self, prefix: str, content: str | bytes, suffix: str = ".log") -> FilePointer:
        data = content.encode("utf-8") if isinstance(content, str) else content
        pointer = self.allocate_shared_artifact(prefix, suffix)
        if not pointer.host_path:
            raise RuntimeError("Artifact pointer missing host path")
        host_path = Path(pointer.host_path)
        host_path.parent.mkdir(parents=True, exist_ok=True)
        host_path.write_bytes(data)
        pointer.size_bytes = len(data)
        return pointer

    async def exec(self, command: str, timeout_s: int = 300) -> ExecResult:
        async with self._lock:
            wrapped = f"timeout --signal=KILL --kill-after=5 {timeout_s} bash -lc {shlex.quote(command)}"
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-lc",
                wrapped,
                cwd="/challenge",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert proc.stdout is not None
            assert proc.stderr is not None

            stdout_spool = _OutputSpooler(
                label="stdout",
                spill_threshold_bytes=self.exec_output_spill_threshold_bytes,
                preview_bytes=self.artifact_preview_bytes,
                pointer_factory=self._make_pointer,
            )
            stderr_spool = _OutputSpooler(
                label="stderr",
                spill_threshold_bytes=self.exec_output_spill_threshold_bytes,
                preview_bytes=self.artifact_preview_bytes,
                pointer_factory=self._make_pointer,
            )

            async def _pump(stream: asyncio.StreamReader, spool: _OutputSpooler) -> None:
                while True:
                    chunk = await stream.read(8192)
                    if not chunk:
                        return
                    spool.feed(chunk)

            stdout_task = asyncio.create_task(_pump(proc.stdout, stdout_spool))
            stderr_task = asyncio.create_task(_pump(proc.stderr, stderr_spool))
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout_s + 30)
            except TimeoutError:
                proc.kill()
                await proc.wait()
            finally:
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

            stdout_text, stdout_pointer, stdout_bytes, stdout_lines = stdout_spool.finalize()
            stderr_text, stderr_pointer, stderr_bytes, stderr_lines = stderr_spool.finalize()
            exit_code = proc.returncode if proc.returncode is not None else -1
            if exit_code == -9 and "Command timed out" not in stderr_text:
                stderr_text = f"{stderr_text}\nCommand timed out".strip()
            return ExecResult(
                exit_code=exit_code,
                stdout=stdout_text,
                stderr=stderr_text,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
                stdout_lines=stdout_lines,
                stderr_lines=stderr_lines,
                stdout_pointer=stdout_pointer,
                stderr_pointer=stderr_pointer,
            )

    async def read_file(self, path: str, *, inline_limit_bytes: int | None = None) -> FileReadResult:
        target = Path(path)
        if not target.is_file():
            raise FileNotFoundError(path)
        size_bytes = target.stat().st_size
        limit = self.read_file_spill_threshold_bytes if inline_limit_bytes is None else inline_limit_bytes
        if size_bytes <= limit:
            data = target.read_bytes()
            return FileReadResult(path=path, data=data, size_bytes=len(data))

        with target.open("rb") as fh:
            data = fh.read(self.artifact_preview_bytes)
        return FileReadResult(
            path=path,
            data=data,
            size_bytes=size_bytes,
            pointer=FilePointer(container_path=path, size_bytes=size_bytes, host_path=str(target)),
        )

    async def write_file(self, path: str, content: str | bytes) -> None:
        data = content.encode("utf-8") if isinstance(content, str) else content
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def copy_from(self, container_path: str, host_path: str) -> None:
        source = Path(container_path)
        target = Path(host_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
