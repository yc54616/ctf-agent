from __future__ import annotations

import asyncio
from pathlib import Path

from backend.sandbox import DockerSandbox


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

    def container(self, container_id: str, **_kwargs) -> _FakeContainer:  # noqa: ANN001
        assert container_id == self._container.id
        return self._container

    async def create(self, _config: dict[str, object]) -> _FakeContainer:
        self.created += 1
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
