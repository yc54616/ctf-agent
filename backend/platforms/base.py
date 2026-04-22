"""Shared runtime platform client interfaces."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

CAPABILITY_CONFIRMED = "confirmed"
CAPABILITY_OPERATOR_ONLY = "operator_only"
CAPABILITY_UNSUPPORTED = "unsupported"
PLATFORM_CAPABILITY_KEYS = ("import", "poll_solved", "submit_flag", "pull_files")
RUNTIME_MODE_FULL_REMOTE = "full_remote"
RUNTIME_MODE_OPERATOR_ONLY = "operator_only"
RUNTIME_MODE_IMPORT_ONLY = "import_only"


def normalize_capability_state(value: object, *, default: str = CAPABILITY_UNSUPPORTED) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in {
        CAPABILITY_CONFIRMED,
        CAPABILITY_OPERATOR_ONLY,
        CAPABILITY_UNSUPPORTED,
    }:
        return normalized
    return default


def normalize_platform_capabilities(
    value: object,
    *,
    defaults: Mapping[str, object] | None = None,
) -> dict[str, str]:
    raw: dict[str, object] = (
        {str(key): item for key, item in value.items()}
        if isinstance(value, dict)
        else {}
    )
    normalized: dict[str, str] = {}
    for key in PLATFORM_CAPABILITY_KEYS:
        if key in raw:
            normalized[key] = normalize_capability_state(raw.get(key))
            continue
        if defaults and key in defaults:
            normalized[key] = normalize_capability_state(defaults.get(key))
            continue
        normalized[key] = CAPABILITY_UNSUPPORTED
    return normalized


def runtime_mode_from_capabilities(value: object) -> str:
    capabilities = normalize_platform_capabilities(value)
    submit_mode = capabilities.get("submit_flag", CAPABILITY_UNSUPPORTED)
    if submit_mode == CAPABILITY_CONFIRMED:
        return RUNTIME_MODE_FULL_REMOTE
    if submit_mode == CAPABILITY_OPERATOR_ONLY:
        return RUNTIME_MODE_OPERATOR_ONLY
    for key in ("poll_solved", "pull_files"):
        if capabilities.get(key) != CAPABILITY_UNSUPPORTED:
            return RUNTIME_MODE_OPERATOR_ONLY
    return RUNTIME_MODE_IMPORT_ONLY


@dataclass(frozen=True)
class SubmitResult:
    status: str  # "correct" | "already_solved" | "incorrect" | "unknown"
    message: str
    display: str


@runtime_checkable
class PlatformClient(Protocol):
    platform: str
    label: str

    async def fetch_challenge_stubs(self) -> list[dict[str, Any]]:
        """Fetch a lightweight challenge list for ordering and polling."""

    async def fetch_all_challenges(self) -> list[dict[str, Any]]:
        """Fetch the full challenge list with enough detail for the coordinator."""

    async def fetch_solved_names(self) -> set[str]:
        """Fetch the set of solved challenge names."""

    async def submit_flag(self, challenge_name: str, flag: str) -> SubmitResult:
        """Submit a flag for a challenge."""

    async def pull_challenge(self, challenge: dict[str, Any], output_dir: str) -> str:
        """Pull a challenge into a local directory when the platform supports it."""

    async def close(self) -> None:
        """Release any held resources."""


@dataclass
class NullPlatformClient:
    """No-op remote client used when only local imported challenges are available."""

    platform: str = "local"
    label: str = "local challenge store"

    async def fetch_challenge_stubs(self) -> list[dict[str, Any]]:
        return []

    async def fetch_all_challenges(self) -> list[dict[str, Any]]:
        return []

    async def fetch_solved_names(self) -> set[str]:
        return set()

    async def submit_flag(self, challenge_name: str, flag: str) -> SubmitResult:
        raise RuntimeError(
            "Remote platform submission is not configured for this run. "
            "Provide the required platform auth or use operator approval."
        )

    async def pull_challenge(self, challenge: dict[str, Any], output_dir: str) -> str:
        raise RuntimeError("Remote challenge pull is not configured for this run.")

    async def close(self) -> None:
        return None


@dataclass
class CompositePlatformClient:
    """Aggregates multiple platform clients behind a single runtime interface."""

    clients: dict[str, PlatformClient]
    challenge_routes: dict[str, str] = field(default_factory=dict)
    platform: str = "multi"
    label: str = "multiple remote platforms"

    def __post_init__(self) -> None:
        self.clients = {str(key): value for key, value in self.clients.items()}
        self.challenge_routes = {
            str(challenge_name): str(client_key)
            for challenge_name, client_key in self.challenge_routes.items()
            if str(client_key) in self.clients
        }

    def register_challenge_route(self, challenge_name: str, client_key: str) -> None:
        normalized_name = str(challenge_name or "").strip()
        normalized_key = str(client_key or "").strip()
        if normalized_name and normalized_key in self.clients:
            self.challenge_routes.setdefault(normalized_name, normalized_key)

    def _record_client_routes(self, client_key: str, challenges: list[dict[str, Any]]) -> None:
        for challenge in challenges:
            if not isinstance(challenge, dict):
                continue
            name = str(challenge.get("name") or "").strip()
            if name:
                self.challenge_routes.setdefault(name, client_key)

    def _resolve_client(self, challenge_name: str) -> PlatformClient:
        normalized_name = str(challenge_name or "").strip()
        client_key = self.challenge_routes.get(normalized_name, "")
        if client_key:
            client = self.clients.get(client_key)
            if client is not None:
                return client
        if len(self.clients) == 1:
            return next(iter(self.clients.values()))
        raise RuntimeError(
            f'Could not determine which remote platform owns "{normalized_name}".'
        )

    async def fetch_challenge_stubs(self) -> list[dict[str, Any]]:
        aggregated: list[dict[str, Any]] = []
        for client_key, client in self.clients.items():
            stubs = await client.fetch_challenge_stubs()
            self._record_client_routes(client_key, stubs)
            for stub in stubs:
                payload = dict(stub)
                payload.setdefault("_platform_client_key", client_key)
                payload.setdefault("source", getattr(client, "platform", "remote"))
                aggregated.append(payload)
        return aggregated

    async def fetch_all_challenges(self) -> list[dict[str, Any]]:
        aggregated: list[dict[str, Any]] = []
        for client_key, client in self.clients.items():
            challenges = await client.fetch_all_challenges()
            self._record_client_routes(client_key, challenges)
            for challenge in challenges:
                payload = dict(challenge)
                payload.setdefault("_platform_client_key", client_key)
                payload.setdefault("source", getattr(client, "platform", "remote"))
                aggregated.append(payload)
        return aggregated

    async def fetch_solved_names(self) -> set[str]:
        solved: set[str] = set()
        for client in self.clients.values():
            solved |= await client.fetch_solved_names()
        return solved

    async def submit_flag(self, challenge_name: str, flag: str) -> SubmitResult:
        client = self._resolve_client(challenge_name)
        return await client.submit_flag(challenge_name, flag)

    async def pull_challenge(self, challenge: dict[str, Any], output_dir: str) -> str:
        client_key = str(challenge.get("_platform_client_key") or "").strip()
        client = self.clients.get(client_key) if client_key else None
        if client is None and len(self.clients) == 1:
            client = next(iter(self.clients.values()))
        if client is None:
            raise RuntimeError(
                f'Could not determine which remote platform should pull "{challenge.get("name", "?")}".'
            )
        return await client.pull_challenge(challenge, output_dir)

    async def close(self) -> None:
        for client in self.clients.values():
            try:
                await client.close()
            except Exception:
                continue


def platform_label(client: object) -> str:
    return str(getattr(client, "label", "") or "remote platform").strip()
