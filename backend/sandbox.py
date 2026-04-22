"""Docker sandbox for CTF challenge solving — native async via aiodocker."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
import shlex
import tarfile
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiodocker

from backend.challenge_config import refresh_effective_metadata

logger = logging.getLogger(__name__)

CONTAINER_LABEL = "ctf-agent"

# Concurrency control
_start_semaphore: asyncio.Semaphore | None = None
_active_count: int = 0
_count_lock = asyncio.Lock()

_WARN_THRESHOLDS = {100, 200, 500}
SHARED_ARTIFACTS_HOST_DIRNAME = ".shared-artifacts"
SHARED_ARTIFACTS_CONTAINER_ROOT = "/challenge/shared-artifacts"
CONTROL_CONTAINER_ROOT = "/challenge/control"
PROVIDER_HOME_CONTAINER_ROOT = "/challenge/provider-home"
CHALLENGE_SRC_CONTAINER_ROOT = "/challenge/challenge-src"
REPO_CONTAINER_ROOT = "/challenge/agent-repo"
TRACE_CONTAINER_ROOT = "/challenge/host-logs"
AUTH_SEED_CONTAINER_ROOT = "/challenge/auth-seeds"
DEFAULT_EXEC_OUTPUT_SPILL_THRESHOLD_BYTES = 64 * 1024
DEFAULT_READ_FILE_SPILL_THRESHOLD_BYTES = 256 * 1024
DEFAULT_ARTIFACT_PREVIEW_BYTES = 8 * 1024


def configure_semaphore(max_concurrent: int = 50) -> None:
    """Set the max concurrent container starts. Call once at startup."""
    global _start_semaphore
    _start_semaphore = asyncio.Semaphore(max_concurrent)


async def _track_start() -> None:
    global _active_count
    async with _count_lock:
        _active_count += 1
        if _active_count in _WARN_THRESHOLDS:
            logger.warning("Active containers: %d", _active_count)


async def _track_stop() -> None:
    global _active_count
    async with _count_lock:
        _active_count = max(0, _active_count - 1)


async def cleanup_orphan_containers() -> None:
    """Kill any leftover ctf-agent containers from a previous run."""
    try:
        docker = aiodocker.Docker()
        try:
            containers = await docker.containers.list(
                all=True,
                filters={"label": [CONTAINER_LABEL]},
            )
            for c in containers:
                try:
                    await c.delete(force=True)
                except Exception:
                    pass
            if containers:
                logger.info("Cleaned up %d orphan container(s)", len(containers))
        finally:
            await docker.close()
    except Exception as e:
        logger.warning("Orphan cleanup failed: %s", e)


@dataclass
class FilePointer:
    container_path: str
    size_bytes: int
    host_path: str | None = None


def resolve_shared_artifacts_dir(challenge_dir: str | Path) -> Path:
    root = Path(challenge_dir).resolve() / SHARED_ARTIFACTS_HOST_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def allocate_artifact_pointer(
    host_root: str | Path,
    container_root: str,
    prefix: str,
    suffix: str,
) -> FilePointer:
    safe_prefix = re.sub(r"[^A-Za-z0-9._-]+", "-", prefix).strip("-") or "artifact"
    token = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    filename = f"{safe_prefix}-{token}{suffix}"
    root = Path(host_root)
    root.mkdir(parents=True, exist_ok=True)
    host_path = root / filename
    container_path = f"{container_root.rstrip('/')}/{filename}"
    return FilePointer(container_path=container_path, host_path=str(host_path), size_bytes=0)


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_lines: int = 0
    stderr_lines: int = 0
    stdout_pointer: FilePointer | None = None
    stderr_pointer: FilePointer | None = None


@dataclass
class FileReadResult:
    path: str
    data: bytes
    size_bytes: int
    pointer: FilePointer | None = None


class _OutputSpooler:
    def __init__(
        self,
        *,
        label: str,
        spill_threshold_bytes: int,
        preview_bytes: int,
        pointer_factory,
    ) -> None:
        self.label = label
        self.spill_threshold_bytes = spill_threshold_bytes
        self.preview_bytes = preview_bytes
        self.pointer_factory = pointer_factory
        self.total_bytes = 0
        self.total_newlines = 0
        self._preview = bytearray()
        self._buffer = bytearray()
        self._fp = None
        self._pointer: FilePointer | None = None
        self._finalized = False
        self._saw_bytes = False
        self._ended_with_newline = False

    def feed(self, chunk: bytes) -> None:
        if self._finalized:
            raise RuntimeError("Cannot feed finalized spooler")

        if chunk:
            self._saw_bytes = True
            self._ended_with_newline = chunk.endswith(b"\n")
        self.total_bytes += len(chunk)
        self.total_newlines += chunk.count(b"\n")
        remaining_preview = self.preview_bytes - len(self._preview)
        if remaining_preview > 0:
            self._preview.extend(chunk[:remaining_preview])

        if self._fp is not None:
            self._fp.write(chunk)
            return

        self._buffer.extend(chunk)
        if len(self._buffer) > self.spill_threshold_bytes:
            self._pointer = self.pointer_factory(self.label, ".log")
            if not self._pointer.host_path:
                raise RuntimeError("Spill pointer missing host path")
            host_path = Path(self._pointer.host_path)
            host_path.parent.mkdir(parents=True, exist_ok=True)
            self._fp = host_path.open("wb")
            self._fp.write(self._buffer)
            self._buffer.clear()

    def _line_count(self) -> int:
        if not self._saw_bytes:
            return 0
        return self.total_newlines if self._ended_with_newline else self.total_newlines + 1

    def finalize(self) -> tuple[str, FilePointer | None, int, int]:
        line_count = self._line_count()
        if self._finalized:
            preview = self._preview.decode("utf-8", errors="replace")
            return preview, self._pointer, self.total_bytes, line_count

        self._finalized = True
        if self._fp is not None:
            self._fp.close()
            self._fp = None

        if self._pointer is not None:
            self._pointer.size_bytes = self.total_bytes
            preview = self._preview.decode("utf-8", errors="replace")
            self._buffer.clear()
            return preview, self._pointer, self.total_bytes, line_count

        text = self._buffer.decode("utf-8", errors="replace")
        self._buffer.clear()
        return text, None, self.total_bytes, line_count


@dataclass
class DockerSandbox:
    """Isolated Docker container for a single solver agent."""

    image: str
    challenge_dir: str
    memory_limit: str = "4g"
    exec_output_spill_threshold_bytes: int = DEFAULT_EXEC_OUTPUT_SPILL_THRESHOLD_BYTES
    read_file_spill_threshold_bytes: int = DEFAULT_READ_FILE_SPILL_THRESHOLD_BYTES
    artifact_preview_bytes: int = DEFAULT_ARTIFACT_PREVIEW_BYTES
    shared_artifacts_dir: str = ""
    workspace_dir: str = ""
    control_dir: str = ""
    provider_home_dir: str = ""
    trace_dir: str = ""
    repo_root_dir: str = ""
    challenge_src_dir: str = ""
    auth_seed_mounts: dict[str, str] = field(default_factory=dict)
    existing_container_id: str = ""
    preserve_stopped_container: bool = False
    owns_workspace_dir: bool = False
    _container: Any = field(default=None, repr=False)
    _docker: Any = field(default=None, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def container_id(self) -> str:
        """The Docker container ID, available after start()."""
        if not self._container:
            raise RuntimeError("Sandbox not started")
        return self._container.id

    @property
    def is_started(self) -> bool:
        return self._container is not None

    @property
    def resume_container_id(self) -> str:
        if self._container is not None:
            return str(self._container.id)
        return str(self.existing_container_id or "")

    def _parse_memory_limit(self) -> int:
        s = self.memory_limit.strip().lower()
        try:
            if s.endswith("g"):
                return int(s[:-1]) * 1024 * 1024 * 1024
            if s.endswith("m"):
                return int(s[:-1]) * 1024 * 1024
            return int(s)
        except (ValueError, IndexError):
            logger.warning("Invalid memory_limit %r, defaulting to 4GB", self.memory_limit)
            return 4 * 1024 * 1024 * 1024

    def _artifact_root_host(self) -> Path:
        if self.shared_artifacts_dir:
            root = Path(self.shared_artifacts_dir)
            root.mkdir(parents=True, exist_ok=True)
            return root
        return resolve_shared_artifacts_dir(self.challenge_dir)

    def _artifact_root_container(self) -> str:
        return SHARED_ARTIFACTS_CONTAINER_ROOT

    def _make_pointer(self, prefix: str, suffix: str) -> FilePointer:
        return allocate_artifact_pointer(
            self._artifact_root_host(),
            self._artifact_root_container(),
            prefix,
            suffix,
        )

    def allocate_shared_artifact(self, prefix: str, suffix: str = ".log") -> FilePointer:
        """Reserve a shared-artifact path for streaming writes from higher-level tools."""
        return self._make_pointer(prefix, suffix)

    async def save_shared_artifact(self, prefix: str, content: str | bytes, suffix: str = ".log") -> FilePointer:
        """Persist generated text/blob output under the shared-artifacts mount."""
        data = content.encode("utf-8") if isinstance(content, str) else content

        pointer = self.allocate_shared_artifact(prefix, suffix)
        if not pointer.host_path:
            raise RuntimeError("Artifact pointer missing host path")
        host_path = Path(pointer.host_path)
        host_path.parent.mkdir(parents=True, exist_ok=True)
        host_path.write_bytes(data)
        pointer.size_bytes = len(data)
        return pointer

    async def start(self) -> None:
        if self._container is not None:
            return
        sem = _start_semaphore or asyncio.Semaphore(50)
        async with sem:
            self._docker = aiodocker.Docker()
            self.shared_artifacts_dir = str(self._artifact_root_host())

            if self.workspace_dir:
                Path(self.workspace_dir).mkdir(parents=True, exist_ok=True)
                self.owns_workspace_dir = False
            else:
                self.workspace_dir = tempfile.mkdtemp(prefix="ctf-workspace-")
                self.owns_workspace_dir = True

            existing_container_id = str(self.existing_container_id or "").strip()
            if existing_container_id:
                container = self._docker.containers.container(existing_container_id)
                try:
                    info = await container.show()
                except Exception:
                    logger.warning(
                        "Preserved sandbox %s is unavailable; creating a fresh container",
                        existing_container_id[:12],
                    )
                    self.existing_container_id = ""
                else:
                    state = info.get("State", {}) if isinstance(info, dict) else {}
                    status = str(state.get("Status") or "").strip().lower()
                    self._container = container
                    if status != "running":
                        await self._container.start()
                    await _track_start()
                    logger.info("Sandbox resumed: %s", existing_container_id[:12])
                    return

            challenge_root = Path(self.challenge_dir).resolve()
            distfiles = str(challenge_root / "distfiles")
            meta_yml = str(refresh_effective_metadata(challenge_root))

            binds: list[str] = [f"{self.workspace_dir}:/challenge/workspace:rw"]
            binds.append(f"{self.shared_artifacts_dir}:{SHARED_ARTIFACTS_CONTAINER_ROOT}:rw")
            challenge_src_host = self.challenge_src_dir or str(challenge_root)
            if Path(challenge_src_host).exists():
                binds.append(f"{challenge_src_host}:{CHALLENGE_SRC_CONTAINER_ROOT}:ro")
            if self.control_dir:
                Path(self.control_dir).mkdir(parents=True, exist_ok=True)
                binds.append(f"{self.control_dir}:{CONTROL_CONTAINER_ROOT}:rw")
            if self.provider_home_dir:
                provider_home = Path(self.provider_home_dir)
                provider_home.mkdir(parents=True, exist_ok=True)
                (provider_home / ".codex").mkdir(parents=True, exist_ok=True)
                (provider_home / ".gemini").mkdir(parents=True, exist_ok=True)
                binds.append(f"{self.provider_home_dir}:{PROVIDER_HOME_CONTAINER_ROOT}:rw")
            if self.repo_root_dir and Path(self.repo_root_dir).exists():
                binds.append(f"{self.repo_root_dir}:{REPO_CONTAINER_ROOT}:ro")
            if self.trace_dir:
                Path(self.trace_dir).mkdir(parents=True, exist_ok=True)
                binds.append(f"{self.trace_dir}:{TRACE_CONTAINER_ROOT}:rw")
            if self.auth_seed_mounts:
                for name, source in sorted(self.auth_seed_mounts.items()):
                    src_path = Path(source).expanduser()
                    if not src_path.exists():
                        continue
                    if self.provider_home_dir and name == "codex-auth.json":
                        binds.append(
                            f"{src_path}:{PROVIDER_HOME_CONTAINER_ROOT}/.codex/auth.json:rw"
                        )
                        continue
                    if self.provider_home_dir and name == "gemini-oauth.json":
                        binds.append(
                            f"{src_path}:{PROVIDER_HOME_CONTAINER_ROOT}/.gemini/oauth_creds.json:rw"
                        )
                        continue
                    binds.append(f"{src_path}:{AUTH_SEED_CONTAINER_ROOT}/{name}:ro")
            if Path(distfiles).exists():
                binds.append(f"{distfiles}:/challenge/distfiles:ro")
            if Path(meta_yml).exists():
                binds.append(f"{meta_yml}:/challenge/metadata.yml:ro")

            config = {
                "Image": self.image,
                "Cmd": ["sleep", "infinity"],
                "WorkingDir": "/challenge",
                "Tty": False,
                "Labels": {CONTAINER_LABEL: "true"},
                "HostConfig": {
                    "Binds": binds,
                    "ExtraHosts": ["host.docker.internal:host-gateway"],
                    "CapAdd": ["SYS_ADMIN", "SYS_PTRACE"],
                    "SecurityOpt": ["seccomp=unconfined"],
                    "Devices": [{"PathOnHost": "/dev/loop-control", "PathInContainer": "/dev/loop-control", "CgroupPermissions": "rwm"}],
                    "Memory": self._parse_memory_limit(),
                    "NanoCpus": int(2 * 1e9),
                },
            }

            self._container = await self._docker.containers.create(config)
            await self._container.start()
            await _track_start()

            info = await self._container.show()
            short_id = info["Id"][:12]
            self.existing_container_id = str(info["Id"])
            logger.info("Sandbox started: %s", short_id)

    async def exec_detached(
        self,
        command: str,
        *,
        cwd: str = "/challenge",
        env: dict[str, str] | None = None,
    ) -> None:
        if not self._container:
            raise RuntimeError("Sandbox not started")

        args = ["docker", "exec", "-d", "-w", cwd]
        for key, value in sorted((env or {}).items()):
            args.extend(["-e", f"{key}={value}"])
        args.extend([self.container_id, "bash", "-lc", command])
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip() or stdout.decode(
                "utf-8", errors="replace"
            ).strip()
            raise RuntimeError(detail or f"docker exec -d failed for {self.container_id}")

    async def exec(self, command: str, timeout_s: int = 300) -> ExecResult:
        if not self._container:
            raise RuntimeError("Sandbox not started")

        async with self._lock:
            try:
                return await self._exec_inner(command, timeout_s)
            except aiodocker.exceptions.DockerError as e:
                # Container was deleted (e.g., sibling solver found the flag)
                return ExecResult(exit_code=-1, stdout="", stderr=f"Container gone: {e}")

    async def _exec_inner(self, command: str, timeout_s: int) -> ExecResult:
        # Wrap command with `timeout` so the container kills the process on expiry.
        # --signal=KILL ensures hard kill; --kill-after=5 is a safety net.
        wrapped = f"timeout --signal=KILL --kill-after=5 {timeout_s} bash -c {shlex.quote(command)}"
        exec_instance = await self._container.exec(
            cmd=["bash", "-c", wrapped],
            stdout=True,
            stderr=True,
            tty=False,
        )

        stream = exec_instance.start(detach=False)
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

        async def _collect() -> None:
            while True:
                msg = await stream.read_out()
                if msg is None:
                    break
                if msg.stream == 1:
                    stdout_spool.feed(msg.data)
                else:
                    stderr_spool.feed(msg.data)

        try:
            # Give extra margin beyond the container-side timeout
            await asyncio.wait_for(_collect(), timeout=timeout_s + 30)
        except TimeoutError:
            try:
                await stream.close()
            except Exception:
                pass
            stdout_text, stdout_pointer, stdout_bytes, stdout_lines = stdout_spool.finalize()
            stderr_text, stderr_pointer, stderr_bytes, stderr_lines = stderr_spool.finalize()
            timeout_stderr = "Command timed out"
            if stderr_text:
                timeout_stderr = f"{stderr_text}\n{timeout_stderr}"
            return ExecResult(
                exit_code=-1,
                stdout=stdout_text,
                stderr=timeout_stderr,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
                stdout_lines=stdout_lines,
                stderr_lines=stderr_lines,
                stdout_pointer=stdout_pointer,
                stderr_pointer=stderr_pointer,
            )

        inspect = await exec_instance.inspect()
        exit_code = inspect.get("ExitCode", 0)
        stdout_text, stdout_pointer, stdout_bytes, stdout_lines = stdout_spool.finalize()
        stderr_text, stderr_pointer, stderr_bytes, stderr_lines = stderr_spool.finalize()

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

    async def _read_file_raw(self, path: str) -> bytes:
        if not self._container:
            raise RuntimeError("Sandbox not started")

        try:
            tar = await asyncio.wait_for(
                self._container.get_archive(path),
                timeout=30,
            )
        except TimeoutError as e:
            raise TimeoutError(f"Timed out reading {path}") from e

        # aiodocker 0.26.0 returns tarfile.TarFile directly
        with tar:
            for member in tar:
                if member.isfile():
                    f = tar.extractfile(member)
                    if f:
                        return f.read()
        raise FileNotFoundError(f"No file found at {path}")

    async def _stat_file_size(self, path: str) -> int:
        quoted = shlex.quote(path)
        result = await self._exec_inner(
            f"test -f -- {quoted} && stat -c '%s' -- {quoted}",
            timeout_s=30,
        )
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise FileNotFoundError(detail or f"No file found at {path}")
        try:
            return int(result.stdout.strip())
        except ValueError as e:
            raise RuntimeError(f"Unable to determine file size for {path}: {result.stdout!r}") from e

    async def _read_file_sample(self, path: str, limit: int) -> bytes:
        script = (
            "python3 - <<'PY'\n"
            "import base64\n"
            f"path = {path!r}\n"
            f"limit = {limit}\n"
            "with open(path, 'rb') as fh:\n"
            "    data = fh.read(limit)\n"
            "print(base64.b64encode(data).decode())\n"
            "PY"
        )
        result = await self._exec_inner(script, timeout_s=30)
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(detail or f"Failed to sample {path}")
        encoded = result.stdout.strip()
        return base64.b64decode(encoded) if encoded else b""

    async def read_file(self, path: str, *, inline_limit_bytes: int | None = None) -> FileReadResult:
        """Read a file from the container without loading large files fully into memory."""
        if not self._container:
            raise RuntimeError("Sandbox not started")

        async with self._lock:
            size_bytes = await self._stat_file_size(path)
            limit = (
                self.read_file_spill_threshold_bytes
                if inline_limit_bytes is None
                else inline_limit_bytes
            )
            if size_bytes <= limit:
                data = await self._read_file_raw(path)
                return FileReadResult(path=path, data=data, size_bytes=len(data))

            sample = await self._read_file_sample(path, self.artifact_preview_bytes)
            return FileReadResult(
                path=path,
                data=sample,
                size_bytes=size_bytes,
                pointer=FilePointer(container_path=path, size_bytes=size_bytes),
            )

    async def write_file(self, path: str, content: str | bytes) -> None:
        """Write a file into the container via tar archive."""
        if not self._container:
            raise RuntimeError("Sandbox not started")

        if isinstance(content, str):
            content = content.encode("utf-8")

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=Path(path).name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        buf.seek(0)

        try:
            await asyncio.wait_for(
                self._container.put_archive(str(Path(path).parent), buf.getvalue()),
                timeout=30,
            )
        except TimeoutError as e:
            raise TimeoutError(f"Timed out writing {path}") from e

    async def copy_from(self, container_path: str, host_path: str) -> None:
        Path(host_path).parent.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "cp",
            f"{self.container_id}:{container_path}",
            host_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip() or stdout.decode(
                "utf-8", errors="replace"
            ).strip()
            raise RuntimeError(detail or f"docker cp failed for {container_path}")

    async def stop(self, *, delete: bool | None = None) -> None:
        remove_container = not self.preserve_stopped_container if delete is None else bool(delete)
        if self._container:
            container_id = str(self._container.id)
            try:
                if remove_container:
                    await self._container.delete(force=True)
                    self.existing_container_id = ""
                else:
                    await self._container.stop(t=5)
                    self.existing_container_id = container_id
            except Exception:
                pass
            self._container = None
            await _track_stop()

        if self._docker:
            try:
                await self._docker.close()
            except Exception:
                pass
            self._docker = None

        if remove_container and self.workspace_dir and self.owns_workspace_dir:
            import shutil
            try:
                shutil.rmtree(self.workspace_dir, ignore_errors=True)
            except Exception:
                pass
        if remove_container:
            self.workspace_dir = ""
            self.owns_workspace_dir = False
        logger.info("Sandbox stopped")
