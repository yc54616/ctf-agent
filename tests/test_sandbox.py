from __future__ import annotations

import asyncio
from pathlib import Path

from backend.sandbox import DockerSandbox, resolve_runtime_tools_host_dir


class _FakeContainer:
    def __init__(self, container_id: str, *, status: str = "exited") -> None:
        self.id = container_id
        self.status = status
        self.started = 0
        self.stopped = 0
        self.deleted = 0

    async def show(self) -> dict[str, object]:
        return {"Id": self.id, "State": {"Status": self.status}}

    async def start(self, **_kwargs) -> None:
        self.started += 1
        self.status = "running"

    async def stop(self, *, t=None, signal=None, timeout=None) -> None:  # noqa: ANN001
        self.stopped += 1
        self.status = "exited"

    async def delete(self, *, force=False, v=False, link=False, timeout=None) -> None:  # noqa: ANN001
        self.deleted += 1
        self.status = "removed"


class _FakeContainers:
    def __init__(self, container: _FakeContainer) -> None:
        self._container = container
        self.created = 0
        self.last_config: dict[str, object] | None = None

    def container(self, container_id: str, **_kwargs) -> _FakeContainer:  # noqa: ANN001
        assert container_id == self._container.id
        return self._container

    async def create(self, _config: dict[str, object]) -> _FakeContainer:
        self.created += 1
        self.last_config = _config
        return self._container


class _FakeDocker:
    def __init__(self, container: _FakeContainer) -> None:
        self.containers = _FakeContainers(container)
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


def test_docker_sandbox_stop_preserves_container_without_delete(tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge"
    workspace_dir = tmp_path / "workspace"
    challenge_dir.mkdir()
    workspace_dir.mkdir()

    container = _FakeContainer("warm-123", status="running")
    docker = _FakeDocker(container)
    sandbox = DockerSandbox(
        image="ctf-sandbox",
        challenge_dir=str(challenge_dir),
        workspace_dir=str(workspace_dir),
        preserve_stopped_container=True,
    )
    sandbox._container = container
    sandbox._docker = docker
    sandbox.owns_workspace_dir = True

    asyncio.run(sandbox.stop())

    assert container.stopped == 1
    assert container.deleted == 0
    assert sandbox.resume_container_id == "warm-123"
    assert sandbox.workspace_dir == str(workspace_dir)
    assert sandbox.owns_workspace_dir is True
    assert docker.closed == 1


def test_docker_sandbox_start_reuses_existing_container_id(monkeypatch, tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge"
    workspace_dir = tmp_path / "workspace"
    challenge_dir.mkdir()
    workspace_dir.mkdir()

    container = _FakeContainer("warm-456", status="exited")
    docker = _FakeDocker(container)
    monkeypatch.setattr("backend.sandbox.aiodocker.Docker", lambda: docker)

    sandbox = DockerSandbox(
        image="ctf-sandbox",
        challenge_dir=str(challenge_dir),
        workspace_dir=str(workspace_dir),
        existing_container_id="warm-456",
        preserve_stopped_container=True,
    )

    asyncio.run(sandbox.start())

    assert sandbox.resume_container_id == "warm-456"
    assert container.started == 1
    assert docker.containers.created == 0


def test_runtime_tools_default_dir_is_repo_local_cache() -> None:
    resolved = resolve_runtime_tools_host_dir()

    assert resolved.name == "runtime-tools"
    assert resolved.parent.name == ".cache"
    assert resolved.parent.parent.name == "ctf-agent"


def test_docker_sandbox_mounts_runtime_tool_cache_and_env(monkeypatch, tmp_path: Path) -> None:
    challenge_dir = tmp_path / "challenge"
    workspace_dir = tmp_path / "workspace"
    runtime_tools_dir = tmp_path / "runtime-tools"
    challenge_dir.mkdir()
    workspace_dir.mkdir()

    container = _FakeContainer("fresh-789", status="created")
    docker = _FakeDocker(container)
    monkeypatch.setattr("backend.sandbox.aiodocker.Docker", lambda: docker)

    sandbox = DockerSandbox(
        image="ctf-sandbox",
        challenge_dir=str(challenge_dir),
        workspace_dir=str(workspace_dir),
        runtime_tools_dir=str(runtime_tools_dir),
        runtime_tools_auto_update=False,
        runtime_tools_refresh_interval_seconds=123,
    )

    asyncio.run(sandbox.start())

    config = docker.containers.last_config or {}
    binds = list(((config.get("HostConfig") or {}).get("Binds") or []))  # type: ignore[union-attr]
    env = list(config.get("Env") or [])

    assert f"{runtime_tools_dir}:/challenge/runtime-tools:rw" in binds
    assert "CTF_AGENT_RUNTIME_TOOLS_AUTO_UPDATE=0" in env
    assert "CTF_AGENT_RUNTIME_TOOLS_REFRESH_INTERVAL_SECONDS=123" in env
    assert "PATH=/challenge/runtime-tools/npm/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" in env
