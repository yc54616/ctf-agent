"""ChallengeSwarm — Parallel solvers racing on one challenge."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, TypeVar

from backend.agents.advisor_base import AdvisorProtocol, CandidateReview, NoopAdvisor
from backend.auth import AuthValidationError
from backend.cost_tracker import CostTracker
from backend.ctfd import CTFdClient
from backend.message_bus import (
    CandidateRef,
    ChallengeMessageBus,
    CoordinatorNoteRef,
    SharedFindingRef,
)
from backend.models import DEFAULT_MODELS, provider_from_spec
from backend.prompts import ChallengeMeta, list_distfiles
from backend.sandbox import (
    SHARED_ARTIFACTS_CONTAINER_ROOT,
    allocate_artifact_pointer,
    resolve_shared_artifacts_dir,
)
from backend.solver_base import (
    CANCELLED,
    ERROR,
    FLAG_CANDIDATE,
    FLAG_FOUND,
    GAVE_UP,
    QUOTA_ERROR,
    SolverProtocol,
    SolverResult,
)

if TYPE_CHECKING:
    from backend.config import Settings

logger = logging.getLogger(__name__)
TAdvisorResult = TypeVar("TAdvisorResult")


FINDING_ARTIFACT_THRESHOLD_CHARS = 500
COORDINATOR_ARTIFACT_THRESHOLD_CHARS = 500
ARTIFACT_PREVIEW_CHARS = 500
MAX_LOCAL_RESTARTS = 5
RESTART_BUDGET_RESET_STEP_DELTA = 10
MANIFEST_ENTRY_LIMIT = 8
ADVISOR_LISTENER_INTERVAL_SECONDS = 2.0
ADVISOR_COORDINATOR_TIMEOUT_SECONDS = 8.0
ADVISOR_LANE_HINT_TIMEOUT_SECONDS = 30.0
ADVISOR_TIMEOUT_BACKOFF_AFTER_CONSECUTIVE_TIMEOUTS = 2
ADVISOR_TIMEOUT_BACKOFF_SECONDS = 120.0
ADVISOR_TIMEOUT_BACKOFF_LOG_BUCKET_SECONDS = 30.0
ADVISOR_ARTIFACT_PREVIEW_MAX_FILES = 3
ADVISOR_ARTIFACT_PREVIEW_BYTES = 2048
ADVISOR_ARTIFACT_ESCALATED_MAX_FILES = 1
ADVISOR_ARTIFACT_ESCALATED_HEAD_BYTES = 8192
ADVISOR_ARTIFACT_ESCALATED_TAIL_BYTES = 4096
ADVISOR_ARTIFACT_SIGNAL_CONTEXT_MAX_FILES = 1
ADVISOR_ARTIFACT_SIGNAL_CONTEXT_MAX_HITS = 3
ADVISOR_ARTIFACT_FINDING_LIMIT = 4
ADVISOR_ARTIFACT_PREVIEW_MAX_CHARS = 2200
ADVISOR_DIGEST_DIRNAME = ".advisor"
ADVISOR_DIGEST_SAMPLE_BYTES = 2048
ADVISOR_DIGEST_EXPANDED_HEAD_BYTES = 8192
ADVISOR_DIGEST_EXPANDED_TAIL_BYTES = 4096
ADVISOR_DIGEST_MAX_HITS = 10
ADVISOR_DIGEST_MAX_ITEMS = 8
ADVISOR_DIGEST_SIGNAL_CONTEXTS = 3
ADVISOR_DIGEST_CONTEXT_RADIUS = 1
ADVISOR_MANIFEST_EXCERPT_MAX_CHARS = 1200
ADVISOR_SIBLING_INSIGHTS_MAX_ITEMS = 4
ADVISOR_SIBLING_INSIGHTS_MAX_CHARS = 1600
ADVISOR_ARTIFACT_FOCUSED_SIBLING_MAX_ITEMS = 3
ADVISOR_ARTIFACT_FOCUSED_SIBLING_MAX_CHARS = 900
ADVISOR_LANE_STATE_MAX_CHARS = 600
PROACTIVE_CONTEXT_REFRESH_MIN_STEPS = 180
FLAG_CANDIDATE_SENTINEL_COMPACTS = frozenset(
    {
        "noflagseen",
        "none",
        "null",
        "unknown",
        "na",
        "reflect",
        "reflection",
    }
)
FLAG_CANDIDATE_PLACEHOLDER_COMPACTS = frozenset(
    {
        "flag",
        "fakeflag",
        "placeholder",
        "dummy",
        "exampleflag",
        "sampleflag",
        "noflagseen",
        "insertflaghere",
        "putflaghere",
        "yourflaghere",
        "notaflag",
        "notrealflag",
        "reflect",
        "reflection",
    }
)
FLAG_CANDIDATE_PLACEHOLDER_TOKENS = frozenset(
    {
        "fake",
        "flag",
        "placeholder",
        "dummy",
        "example",
        "sample",
        "no",
        "seen",
        "insert",
        "put",
        "your",
        "here",
        "not",
        "real",
        "reflect",
        "reflection",
    }
)
PROACTIVE_CONTEXT_REFRESH_STEP_INTERVAL = 180
SHARED_ARTIFACT_PATH_RE = re.compile(r"/challenge/shared-artifacts/[^\s)\]>\"']+")
ADVISOR_ROUTE_RE = re.compile(r"(?<![A-Za-z0-9_])/(?:[A-Za-z0-9_.:-]+/)*[A-Za-z0-9_.:-]+")
ADVISOR_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
ADVISOR_JSON_KEY_RE = re.compile(r'"([A-Za-z0-9_.-]{2,64})"\s*:')
ADVISOR_FORM_FIELD_RE = re.compile(r"""name\s*=\s*['"]([^'"]+)['"]""")
ADVISOR_TEXTLIKE_SUFFIXES = {
    ".html",
    ".htm",
    ".js",
    ".json",
    ".txt",
    ".log",
    ".md",
    ".xml",
    ".yml",
    ".yaml",
    ".csv",
}
ADVISOR_HEAD_ONLY_SUFFIXES = {
    ".html",
    ".htm",
    ".js",
    ".json",
    ".md",
    ".xml",
    ".yml",
    ".yaml",
}
ADVISOR_SIGNAL_TERMS = (
    "api",
    "auth",
    "token",
    "csrf",
    "flag",
    "admin",
    "login",
    "endpoint",
    "route",
    "k8s",
    "dashboard",
    "<html",
    "fetch(",
    "{",
    "[",
)
NON_FACTUAL_PREFIXES = (
    "try ",
    "use ",
    "check ",
    "run ",
    "continue ",
    "do not ",
    "first,",
    "next ",
)
NON_FACTUAL_SUBSTRINGS = (
    " should ",
    " try ",
    " use ",
    " check ",
    " repeat ",
    " follow up ",
)
IGNORED_ARTIFACT_BASENAMES = ("manifest.md",)
IGNORED_ARTIFACT_PREFIXES = (
    "stdout-",
    "stderr-",
    "lane-resume-",
    "artifact-ref-",
    "candidate-",
    "finding-",
)


def _int_from_object(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _float_from_object(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _trace_tail_lines(value: object, *, limit: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(line) for line in value[:limit]]


@dataclass
class LaneRestartState:
    last_total_steps: int = -1
    last_dead_end_fingerprint: str = ""
    last_trace_fingerprint: str = ""
    restart_count: int = 0
    last_context_refresh_step: int = 0
    restart_budget_baseline_step: int = 0


@dataclass
class FlagCandidateRecord:
    normalized_flag: str
    raw_flag: str
    first_seen_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
    status: str = "pending"
    advisor_decision: str = ""
    advisor_note: str = ""
    submit_display: str = ""
    coordinator_notified_at: float | None = None
    source_models: set[str] = field(default_factory=set)
    evidence_snippets: list[str] = field(default_factory=list)
    evidence_digest_paths: dict[str, str] = field(default_factory=dict)
    evidence_pointer_paths: dict[str, str] = field(default_factory=dict)
    confidences: dict[str, str] = field(default_factory=dict)
    step_counts: dict[str, int] = field(default_factory=dict)
    trace_paths: dict[str, str] = field(default_factory=dict)
    confirmation_source: str = ""
    _review_started: bool = False

    def snapshot(self) -> dict[str, object]:
        return {
            "flag": self.raw_flag,
            "status": self.status,
            "advisor_decision": self.advisor_decision,
            "advisor_note": self.advisor_note,
            "submit_display": self.submit_display,
            "source_models": sorted(self.source_models),
            "evidence_snippets": list(self.evidence_snippets),
            "evidence_digest_paths": dict(self.evidence_digest_paths),
            "evidence_pointer_paths": dict(self.evidence_pointer_paths),
            "confidences": dict(self.confidences),
            "step_counts": dict(self.step_counts),
            "trace_paths": dict(self.trace_paths),
            "confirmation_source": self.confirmation_source,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "coordinator_notified_at": self.coordinator_notified_at,
        }

    @classmethod
    def from_snapshot(
        cls,
        normalized_flag: str,
        payload: object,
    ) -> FlagCandidateRecord | None:
        if not isinstance(payload, dict):
            return None
        raw_payload = {str(key): value for key, value in payload.items()}
        raw_source_models = raw_payload.get("source_models", [])
        source_model_items = raw_source_models if isinstance(raw_source_models, list) else []
        raw_flag = str(raw_payload.get("flag") or normalized_flag).strip() or normalized_flag
        source_models = {
            str(model).strip()
            for model in source_model_items
            if str(model).strip()
        }
        raw_evidence_snippets = raw_payload.get("evidence_snippets", [])
        evidence_items = raw_evidence_snippets if isinstance(raw_evidence_snippets, list) else []
        evidence_snippets = [
            str(snippet)[:500]
            for snippet in evidence_items
            if str(snippet).strip()
        ]
        raw_evidence_digests = raw_payload.get("evidence_digest_paths", {})
        evidence_digest_paths = (
            {
                str(model): str(digest_path)
                for model, digest_path in raw_evidence_digests.items()
                if str(model).strip() and str(digest_path).strip()
            }
            if isinstance(raw_evidence_digests, dict)
            else {}
        )
        raw_evidence_pointers = raw_payload.get("evidence_pointer_paths", {})
        evidence_pointer_paths = (
            {
                str(model): str(pointer_path)
                for model, pointer_path in raw_evidence_pointers.items()
                if str(model).strip() and str(pointer_path).strip()
            }
            if isinstance(raw_evidence_pointers, dict)
            else {}
        )
        raw_confidences = raw_payload.get("confidences", {})
        confidences = (
            {
                str(model): str(confidence)
                for model, confidence in raw_confidences.items()
                if str(model).strip()
            }
            if isinstance(raw_confidences, dict)
            else {}
        )
        raw_step_counts = raw_payload.get("step_counts", {})
        step_counts = (
            {
                str(model): _int_from_object(step_count)
                for model, step_count in raw_step_counts.items()
                if str(model).strip()
            }
            if isinstance(raw_step_counts, dict)
            else {}
        )
        raw_trace_paths = raw_payload.get("trace_paths", {})
        trace_paths = (
            {
                str(model): str(trace_path)
                for model, trace_path in raw_trace_paths.items()
                if str(model).strip() and str(trace_path).strip()
            }
            if isinstance(raw_trace_paths, dict)
            else {}
        )
        return cls(
            normalized_flag=normalized_flag,
            raw_flag=raw_flag,
            first_seen_at=_float_from_object(raw_payload.get("first_seen_at")) or time.time(),
            last_seen_at=_float_from_object(raw_payload.get("last_seen_at")) or time.time(),
            status=str(raw_payload.get("status") or "pending"),
            advisor_decision=str(raw_payload.get("advisor_decision") or ""),
            advisor_note=str(raw_payload.get("advisor_note") or "")[:500],
            submit_display=str(raw_payload.get("submit_display") or "")[:500],
            coordinator_notified_at=_float_from_object(raw_payload.get("coordinator_notified_at")),
            source_models=source_models,
            evidence_snippets=evidence_snippets,
            evidence_digest_paths=evidence_digest_paths,
            evidence_pointer_paths=evidence_pointer_paths,
            confidences=confidences,
            step_counts=step_counts,
            trace_paths=trace_paths,
            confirmation_source=str(raw_payload.get("confirmation_source") or "")[:120],
        )


@dataclass
class ChallengeSwarm:
    """Parallel solvers racing on one challenge."""

    challenge_dir: str
    meta: ChallengeMeta
    ctfd: CTFdClient
    cost_tracker: CostTracker
    settings: Settings
    result_store: dict[str, dict[str, object]] | None = None
    model_specs: list[str] = field(default_factory=lambda: list(DEFAULT_MODELS))
    no_submit: bool = False
    local_mode: bool = False
    coordinator_inbox: asyncio.Queue | None = None

    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    solvers: dict[str, SolverProtocol] = field(default_factory=dict)
    agent_results: dict[str, SolverResult] = field(default_factory=dict)
    findings: dict[str, str] = field(default_factory=dict)
    shared_finding_events: dict[str, SharedFindingRef] = field(default_factory=dict, init=False, repr=False)
    winner: SolverResult | None = None
    confirmed_flag: str | None = None
    _flag_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    flag_candidates: dict[str, FlagCandidateRecord] = field(default_factory=dict)
    _submit_count: dict[str, int] = field(default_factory=dict)  # per-model wrong submission count
    _submitted_flags: set[str] = field(default_factory=set)  # dedup exact flags
    _last_submit_time: dict[str, float] = field(default_factory=dict)  # per-model last submit timestamp
    message_bus: ChallengeMessageBus = field(default_factory=ChallengeMessageBus)
    shared_artifacts_dir: Path = field(init=False)
    winner_model_spec: str | None = None
    winner_confirmation_source: str = ""
    saved_solve_artifacts: dict[str, str] = field(default_factory=dict)
    last_advisor_note: str = ""
    last_coordinator_advisor_note: str = ""
    last_shared_finding: str = ""
    lane_advisor_notes: dict[str, str] = field(default_factory=dict)
    coordinator_message_count: int = 0
    advisor_lane_hint_count: int = 0
    advisor_coordinator_count: int = 0
    _save_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _advisors: dict[str, AdvisorProtocol] = field(default_factory=dict, init=False, repr=False)
    _background_tasks: set[asyncio.Task] = field(default_factory=set, init=False, repr=False)
    _lane_restart_state: dict[str, LaneRestartState] = field(default_factory=dict, init=False, repr=False)
    _lane_restart_notes: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _lane_advisory_fingerprints: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _shared_artifact_fingerprints: set[str] = field(default_factory=set, init=False, repr=False)
    _artifact_manifest_entries: list[dict[str, str]] = field(default_factory=list, init=False, repr=False)
    _artifact_digest_cache: dict[str, tuple[str, str, str]] = field(default_factory=dict, init=False, repr=False)
    _lane_seen_digest_revisions: dict[str, dict[str, str]] = field(default_factory=dict, init=False, repr=False)
    _manifest_cache_signature: str = field(default="", init=False, repr=False)
    _manifest_cache_lines: tuple[str, ...] = field(default_factory=tuple, init=False, repr=False)
    _sticky_advisor_backend: str | None = field(default=None, init=False, repr=False)
    _sticky_advisor_reason: str = field(default="", init=False, repr=False)
    _advisor_timeout_streaks: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _advisor_timeout_backoff_until: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _advisor_timeout_backoff_buckets: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.shared_artifacts_dir = resolve_shared_artifacts_dir(self.challenge_dir)
        self._restore_runtime_state()

    def _restore_runtime_state(self) -> None:
        if not self.result_store:
            return
        persisted = self.result_store.get(self.meta.name)
        if not isinstance(persisted, dict):
            return
        if persisted.get("status") == FLAG_FOUND and persisted.get("flag"):
            self.confirmed_flag = str(persisted.get("flag") or "").strip() or None
            self.winner_model_spec = str(persisted.get("winner_model") or "").strip() or None
            self.winner_confirmation_source = str(persisted.get("confirmation_source") or "").strip()
        self.last_advisor_note = str(persisted.get("advisor_note") or "")
        self.last_coordinator_advisor_note = str(persisted.get("coordinator_advisor_note") or "")
        self.last_shared_finding = str(persisted.get("shared_finding") or "")
        sticky_backend = str(persisted.get("advisor_backend") or "").strip()
        if sticky_backend == "codex":
            self._sticky_advisor_backend = "codex"
            self._sticky_advisor_reason = str(persisted.get("advisor_backend_reason") or "")
        raw_shared_findings = persisted.get("shared_findings", {})
        if isinstance(raw_shared_findings, dict):
            for model_spec, payload in raw_shared_findings.items():
                finding = SharedFindingRef.from_snapshot(payload)
                if finding is None:
                    continue
                self.shared_finding_events[str(model_spec)] = finding
                self.findings[str(model_spec)] = finding.rendered_text()
        flag_candidates = persisted.get("flag_candidates", {})
        if isinstance(flag_candidates, dict):
            for normalized_flag, payload in flag_candidates.items():
                restored = FlagCandidateRecord.from_snapshot(str(normalized_flag), payload)
                if restored is not None:
                    self.flag_candidates[restored.normalized_flag] = restored

    def _runtime_step_count(self) -> int:
        total = 0
        for result in self.agent_results.values():
            total = max(total, result.step_count)
        for candidate in self.flag_candidates.values():
            total = max(total, max(candidate.step_counts.values(), default=0))
        return total

    def _runtime_result_payload(self) -> dict[str, object]:
        status = FLAG_FOUND if self.confirmed_flag else (
            "candidate_pending" if self.flag_candidates else "pending"
        )
        payload: dict[str, object] = {
            "challenge_name": self.meta.name,
            "status": status,
            "step_count": self._runtime_step_count(),
            "advisor_note": self.last_advisor_note,
            "coordinator_advisor_note": self.last_coordinator_advisor_note,
            "shared_finding": self.last_shared_finding,
            "advisor_backend": self._sticky_advisor_backend or "claude",
            "advisor_backend_reason": self._sticky_advisor_reason,
            "shared_findings": {
                model_spec: finding.snapshot()
                for model_spec, finding in sorted(self.shared_finding_events.items())
            },
            "shared_artifacts_path": str(self.shared_artifacts_dir.resolve()),
            "flag_candidates": {
                flag: record.snapshot()
                for flag, record in sorted(self.flag_candidates.items())
            },
            "saved_at": datetime.now(UTC).isoformat(),
        }
        if self.confirmed_flag:
            payload["flag"] = self.confirmed_flag
            payload["winner_model"] = self.winner_model_spec or ""
            payload["confirmation_source"] = self.winner_confirmation_source or "ctfd"
            payload["findings_summary"] = (
                self.winner.findings_summary if self.winner else "confirmed by coordinator"
            )
        return payload

    async def _persist_runtime_state(self) -> None:
        if self.result_store is None:
            return

        async with self._save_lock:
            payload = self._runtime_result_payload()
            self.result_store[self.meta.name] = payload

            challenge_root = Path(self.challenge_dir).resolve()
            if not challenge_root.exists():
                return

            solve_dir = challenge_root / "solve"
            solve_dir.mkdir(parents=True, exist_ok=True)

            if payload.get("status") == FLAG_FOUND and not self.saved_solve_artifacts:
                flag_path = solve_dir / "flag.txt"
                flag_path.write_text(str(payload.get("flag") or "") + "\n", encoding="utf-8")

            if payload.get("status") == FLAG_FOUND and self.saved_solve_artifacts:
                return

            result_path = solve_dir / "result.json"
            result_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _persist_shared_text_pointer(
        self,
        prefix: str,
        content: str,
        suffix: str = ".txt",
    ) -> tuple[str, int]:
        pointer = allocate_artifact_pointer(
            self.shared_artifacts_dir,
            SHARED_ARTIFACTS_CONTAINER_ROOT,
            prefix,
            suffix,
        )
        assert pointer.host_path is not None
        Path(pointer.host_path).write_text(content, encoding="utf-8")
        pointer.size_bytes = len(content.encode("utf-8"))
        return pointer.container_path or "", pointer.size_bytes

    def _persist_artifact_note_pointer(
        self,
        *,
        model_spec: str,
        artifact_path: str,
        fact_summary: str,
        digest_path: str = "",
    ) -> str:
        note_lines = [
            "# Shared Artifact Reference",
            f"- source_model: {model_spec}",
            f"- artifact: {artifact_path}",
        ]
        if fact_summary.strip():
            note_lines.append(f"- summary: {fact_summary.strip()}")
        if digest_path.strip():
            note_lines.append(f"- digest: {digest_path.strip()}")
        note_lines.append("")
        pointer_path, _size_bytes = self._persist_shared_text_pointer(
            f"artifact-ref-{self.meta.name}-{model_spec}",
            "\n".join(note_lines),
            suffix=".md",
        )
        return pointer_path

    def _compact_summary(self, content: str, *, limit: int = 160) -> str:
        text = self._normalize_text_line(content)
        if not text:
            return ""
        return text[: limit - 1] + "..." if len(text) > limit else text

    @staticmethod
    def _clip_text_block(text: str, *, limit: int) -> str:
        cleaned = str(text or "").strip()
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: limit - 3].rstrip() + "..."

    def _record_shared_finding(self, model_spec: str, finding: SharedFindingRef) -> None:
        rendered = finding.rendered_text()
        self.shared_finding_events[model_spec] = finding
        self.findings[model_spec] = rendered
        self.last_shared_finding = rendered

    def _manifest_file_path(self) -> Path:
        return self.shared_artifacts_dir / "manifest.md"

    def _manifest_lines(self) -> tuple[str, ...]:
        path = self._manifest_file_path()
        if not path.exists():
            self._manifest_cache_signature = ""
            self._manifest_cache_lines = ()
            return ()
        signature = self._manifest_signature()
        if signature and signature == self._manifest_cache_signature:
            return self._manifest_cache_lines
        try:
            lines = tuple(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            self._manifest_cache_signature = ""
            self._manifest_cache_lines = ()
            return ()
        self._manifest_cache_signature = signature
        self._manifest_cache_lines = lines
        return lines

    def _advisor_digest_dir(self) -> Path:
        path = self.shared_artifacts_dir / ADVISOR_DIGEST_DIRNAME
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _generic_finding_digest_name(self, pointer_path: str) -> str:
        base = Path(pointer_path).name or "finding"
        safe_base = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in base)
        suffix = hashlib.sha1(pointer_path.encode("utf-8", errors="replace")).hexdigest()[:10]
        return f"{safe_base}-{suffix}.digest.md"

    def _generic_finding_digest_paths(self, pointer_path: str) -> tuple[Path, str]:
        name = self._generic_finding_digest_name(pointer_path)
        host_path = self._advisor_digest_dir() / name
        container_path = f"{SHARED_ARTIFACTS_CONTAINER_ROOT}/{ADVISOR_DIGEST_DIRNAME}/{name}"
        return host_path, container_path

    def _build_generic_finding_digest(
        self,
        *,
        model_spec: str,
        pointer_path: str,
        text: str,
    ) -> str:
        normalized_lines: list[str] = []
        for raw_line in text.splitlines():
            cleaned = self._normalize_text_line(raw_line)
            if not cleaned or cleaned in normalized_lines:
                continue
            normalized_lines.append(self._truncate_text(cleaned, 180))
            if len(normalized_lines) >= ADVISOR_DIGEST_MAX_ITEMS:
                break
        summary = self._compact_summary(text)
        lines = [
            "# Finding Digest",
            f"- source_model: {model_spec}",
            f"- pointer: {pointer_path}",
            f"- summary: {summary or '(empty)'}",
            "",
        ]
        if normalized_lines:
            lines.append("## Key Lines")
            lines.extend(f"- {line}" for line in normalized_lines)
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    def _persist_generic_finding_digest(
        self,
        *,
        model_spec: str,
        pointer_path: str,
        text: str,
    ) -> tuple[str, str, str]:
        digest_host_path, digest_container_path = self._generic_finding_digest_paths(pointer_path)
        digest_text = self._build_generic_finding_digest(
            model_spec=model_spec,
            pointer_path=pointer_path,
            text=text,
        )
        revision = hashlib.sha256(digest_text.encode("utf-8", errors="replace")).hexdigest()
        digest_host_path.write_text(digest_text, encoding="utf-8")
        return digest_container_path, revision, digest_text

    def _shared_artifact_host_path(self, container_path: str) -> Path | None:
        prefix = f"{SHARED_ARTIFACTS_CONTAINER_ROOT}/"
        if not container_path.startswith(prefix):
            return None
        relative_path = container_path.removeprefix(prefix)
        if not relative_path.strip():
            return None
        return self.shared_artifacts_dir / relative_path

    def _read_shared_pointer_text(self, pointer_path: str) -> str:
        host_path = self._shared_artifact_host_path(pointer_path)
        if host_path is None or not host_path.exists():
            return ""
        try:
            return host_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _candidate_evidence_digest_name(self, pointer_path: str) -> str:
        base = Path(pointer_path).name or "candidate"
        safe_base = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in base)
        suffix = hashlib.sha1(f"candidate\0{pointer_path}".encode("utf-8", errors="replace")).hexdigest()[:10]
        return f"{safe_base}-{suffix}.candidate.digest.md"

    def _candidate_evidence_digest_paths(self, pointer_path: str) -> tuple[Path, str]:
        name = self._candidate_evidence_digest_name(pointer_path)
        host_path = self._advisor_digest_dir() / name
        container_path = f"{SHARED_ARTIFACTS_CONTAINER_ROOT}/{ADVISOR_DIGEST_DIRNAME}/{name}"
        return host_path, container_path

    def _build_candidate_evidence_digest(
        self,
        *,
        model_spec: str,
        flag: str,
        pointer_path: str,
        text: str,
        advisor_decision: str = "",
        advisor_note: str = "",
    ) -> str:
        normalized_lines: list[str] = []
        for raw_line in text.splitlines():
            cleaned = self._normalize_text_line(raw_line)
            if not cleaned or cleaned in normalized_lines:
                continue
            normalized_lines.append(self._truncate_text(cleaned, 180))
            if len(normalized_lines) >= ADVISOR_DIGEST_MAX_ITEMS:
                break
        summary = self._compact_summary(text)
        lines = [
            "# Candidate Evidence Digest",
            f"- source_model: {model_spec}",
            f"- flag: {flag.strip() or '(empty)'}",
            f"- pointer: {pointer_path}",
            f"- advisor_decision: {advisor_decision or 'insufficient'}",
            f"- summary: {summary or '(empty)'}",
        ]
        note = self._normalize_text_line(advisor_note)
        if note:
            lines.append(f"- advisor_note: {self._truncate_text(note, 180)}")
        lines.append("")
        if normalized_lines:
            lines.append("## Key Lines")
            lines.extend(f"- {line}" for line in normalized_lines)
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    def _persist_candidate_evidence_digest(
        self,
        *,
        model_spec: str,
        flag: str,
        pointer_path: str,
        text: str,
        advisor_decision: str = "",
        advisor_note: str = "",
    ) -> tuple[str, str, str]:
        digest_host_path, digest_container_path = self._candidate_evidence_digest_paths(pointer_path)
        digest_text = self._build_candidate_evidence_digest(
            model_spec=model_spec,
            flag=flag,
            pointer_path=pointer_path,
            text=text,
            advisor_decision=advisor_decision,
            advisor_note=advisor_note,
        )
        revision = hashlib.sha256(digest_text.encode("utf-8", errors="replace")).hexdigest()
        digest_host_path.write_text(digest_text, encoding="utf-8")
        return digest_container_path, revision, digest_text

    def _shareable_text(self, prefix: str, content: str, *, threshold: int) -> str:
        text = content.strip()
        if not text:
            return text
        summary = self._compact_summary(text)
        if len(text) <= threshold:
            return summary or text
        pointer_path, size_bytes = self._persist_shared_text_pointer(prefix, text)
        size_suffix = f" ({size_bytes} bytes)" if size_bytes else ""
        return f"{summary}\nPointer: {pointer_path}{size_suffix}".strip()

    def _make_finding_event(
        self,
        *,
        model_spec: str,
        prefix: str,
        content: str,
    ) -> SharedFindingRef:
        text = content.strip()
        pointer_path, _size_bytes = self._persist_shared_text_pointer(prefix, text)
        digest_path, revision, _digest_text = self._persist_generic_finding_digest(
            model_spec=model_spec,
            pointer_path=pointer_path,
            text=text,
        )
        return SharedFindingRef(
            model=model_spec,
            kind="finding_ref",
            content="",
            summary=self._compact_summary(text),
            pointer_path=pointer_path,
            digest_path=digest_path,
            revision=revision,
        )

    @staticmethod
    def _normalize_text_line(value: str) -> str:
        return " ".join(value.strip().split())

    def _finding_fingerprint(self, kind: str, content: str) -> str:
        normalized = self._normalize_text_line(content)
        payload = f"{kind}\0{normalized}"
        return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()

    def _extract_shared_artifact_paths(self, *texts: str) -> list[str]:
        seen: set[str] = set()
        paths: list[str] = []
        for text in texts:
            if not text:
                continue
            for match in SHARED_ARTIFACT_PATH_RE.findall(text):
                candidate = match.rstrip(".,:;)]}>")
                if candidate not in seen:
                    seen.add(candidate)
                    paths.append(candidate)
        return paths

    def _is_shareable_artifact_path(self, artifact_path: str) -> bool:
        prefix = f"{SHARED_ARTIFACTS_CONTAINER_ROOT}/"
        if not artifact_path.startswith(prefix):
            return False
        relative = artifact_path.removeprefix(prefix)
        if not relative or relative.startswith(f"{ADVISOR_DIGEST_DIRNAME}/"):
            return False
        name = Path(relative).name
        if name in IGNORED_ARTIFACT_BASENAMES:
            return False
        return not any(name.startswith(prefix) for prefix in IGNORED_ARTIFACT_PREFIXES)

    def _should_log_advisor_backoff(self, cooldown_key: str, remaining_backoff: float) -> bool:
        bucket_size = max(1.0, ADVISOR_TIMEOUT_BACKOFF_LOG_BUCKET_SECONDS)
        bucket = max(0, int(remaining_backoff // bucket_size))
        previous = self._advisor_timeout_backoff_buckets.get(cooldown_key)
        if previous == bucket:
            return False
        self._advisor_timeout_backoff_buckets[cooldown_key] = bucket
        return True

    def _sanitize_fact_summary(self, candidate: str, artifact_path: str) -> str:
        text = str(candidate or "")
        if not text:
            return ""

        cleaned = text.replace(artifact_path, " ")
        cleaned = self._normalize_text_line(cleaned)
        if not cleaned:
            return ""

        segments = re.split(r"(?:\n| \| |\s{2,})", cleaned)
        for segment in segments:
            fact = self._normalize_text_line(segment)
            if not fact:
                continue
            lower = fact.lower()
            if fact.startswith("[") and "]" in fact[:20]:
                continue
            if lower.startswith("{") and '"command"' in lower:
                continue
            if lower.startswith(NON_FACTUAL_PREFIXES):
                continue
            if any(token in lower for token in NON_FACTUAL_SUBSTRINGS):
                continue
            if any(
                lower.startswith(prefix)
                for prefix in ("message sent", "no new findings", "yolo mode", "tool failed:")
            ):
                continue
            if re.fullmatch(r"cands?\s+\d+", lower):
                continue
            if "usage limit" in lower or lower.startswith(("turn failed:", "error:", "fatal:")):
                continue
            if lower.startswith(
                ("grep ", "sed ", "rg ", "find ", "strings ", "xxd ", "objdump ", "binwalk ", "ffuf ", "curl ", "python3 ")
            ):
                continue
            return fact[:160]
        return ""

    def _artifact_fact_summary(self, artifact_path: str, *candidates: str) -> str:
        for candidate in candidates:
            fact = self._sanitize_fact_summary(candidate, artifact_path)
            if not fact:
                continue
            return fact
        return ""

    def _fallback_artifact_fact_summary(self, artifact_path: str, digest_text: str) -> str:
        section_priority = (
            "signal contexts",
            "signal hits",
            "routes",
            "urls",
            "json keys",
            "form fields",
            "head sample",
            "middle sample",
            "tail sample",
        )
        sections: dict[str, list[str]] = {}
        current_section = ""
        for raw_line in digest_text.splitlines():
            line = raw_line.strip()
            if line.startswith("## "):
                current_section = line[3:].strip().lower()
                sections.setdefault(current_section, [])
                continue
            if not line.startswith("- "):
                continue
            body = line[2:]
            lowered = body.lower()
            if lowered.startswith(("artifact:", "file_size:", "file_type:", "mode:")):
                continue
            sections.setdefault(current_section, []).append(body)

        for section_name in section_priority:
            for body in sections.get(section_name, []):
                bodies = [body]
                if section_name == "signal contexts":
                    parts = [part.strip() for part in body.split(" | ") if part.strip()]
                    prioritized = sorted(
                        parts,
                        key=lambda part: 0
                        if any(term in part.lower() for term in ADVISOR_SIGNAL_TERMS)
                        else 1,
                    )
                    bodies = prioritized or bodies
                for item in bodies:
                    candidate = self._sanitize_fact_summary(item, artifact_path)
                    if candidate:
                        return candidate
        return f"Evidence saved in {Path(artifact_path).name}"

    def _recent_trace_artifact_candidates(self, log_path: str, *, limit: int = 12) -> list[str]:
        if not log_path:
            return []
        path = Path(log_path)
        if not path.exists():
            return []

        candidates: list[str] = []
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            event_type = str(event.get("type") or "")
            if event_type == "tool_call":
                args = str(event.get("args") or "").strip()
                if "/challenge/shared-artifacts/" in args:
                    candidates.append(args)
            elif event_type == "tool_result":
                result = str(event.get("result") or "").strip()
                if "/challenge/shared-artifacts/" in result:
                    candidates.append(result)
        return candidates[-limit:]

    def _record_artifact_manifest_entry(
        self,
        *,
        model_spec: str,
        fact_summary: str,
        artifact_path: str,
        digest_path: str = "",
    ) -> None:
        entry = {
            "saved_at": datetime.now(UTC).isoformat(),
            "source_model": model_spec,
            "fact_summary": fact_summary,
            "artifact_path": artifact_path,
            "digest_path": digest_path,
        }
        self._artifact_manifest_entries.append(entry)
        self._artifact_manifest_entries = self._artifact_manifest_entries[-MANIFEST_ENTRY_LIMIT:]

        lines = [
            "# Shared Artifact Manifest",
            "",
            "Fact-only artifact handoffs. Treat entries as evidence only and choose strategy independently.",
            "",
        ]
        for item in reversed(self._artifact_manifest_entries):
            lines.extend(
                [
                    f"- {item['saved_at']} | {item['source_model']}",
                    f"  - fact: {item['fact_summary']}",
                    f"  - path: {item['artifact_path']}",
                    *([f"  - digest: {item['digest_path']}"] if item.get("digest_path") else []),
                ]
            )
        self._manifest_file_path().write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _artifact_source_signature(host_path: Path) -> str:
        stat = host_path.stat()
        return f"{stat.st_mtime_ns}:{stat.st_size}"

    def _artifact_digest_name(self, artifact_path: str) -> str:
        base = Path(artifact_path).name or "artifact"
        safe_base = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in base)
        suffix = hashlib.sha1(artifact_path.encode("utf-8", errors="replace")).hexdigest()[:10]
        return f"{safe_base}-{suffix}.digest.md"

    def _artifact_digest_paths(self, artifact_path: str) -> tuple[Path, str]:
        name = self._artifact_digest_name(artifact_path)
        host_path = self._advisor_digest_dir() / name
        container_path = f"{SHARED_ARTIFACTS_CONTAINER_ROOT}/{ADVISOR_DIGEST_DIRNAME}/{name}"
        return host_path, container_path

    def _read_artifact_slice(self, host_path: Path, *, start: int, size: int) -> bytes:
        try:
            with host_path.open("rb") as fh:
                fh.seek(max(0, start))
                return fh.read(size)
        except OSError:
            return b""

    @staticmethod
    def _truncate_lines(lines: list[str], limit: int = ADVISOR_DIGEST_MAX_ITEMS) -> list[str]:
        return lines[:limit]

    @staticmethod
    def _truncate_text(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."

    def _text_digest_sections(self, host_path: Path) -> dict[str, list[str] | str]:
        stat = host_path.stat()
        file_size = stat.st_size
        head = self._read_artifact_slice(host_path, start=0, size=ADVISOR_DIGEST_SAMPLE_BYTES)
        middle = b""
        tail = b""
        if file_size > ADVISOR_DIGEST_EXPANDED_HEAD_BYTES:
            middle = self._read_artifact_slice(
                host_path,
                start=max(0, (file_size // 2) - (ADVISOR_DIGEST_SAMPLE_BYTES // 2)),
                size=ADVISOR_DIGEST_SAMPLE_BYTES,
            )
        if file_size > ADVISOR_DIGEST_EXPANDED_TAIL_BYTES:
            tail = self._read_artifact_slice(
                host_path,
                start=max(0, file_size - ADVISOR_DIGEST_SAMPLE_BYTES),
                size=ADVISOR_DIGEST_SAMPLE_BYTES,
            )

        signal_hits: list[str] = []
        urls: list[str] = []
        routes: list[str] = []
        json_keys: list[str] = []
        form_fields: list[str] = []
        seen_urls: set[str] = set()
        seen_routes: set[str] = set()
        seen_json_keys: set[str] = set()
        seen_form_fields: set[str] = set()
        signal_contexts: list[list[str]] = []
        recent_lines: deque[tuple[int, str]] = deque(maxlen=ADVISOR_DIGEST_CONTEXT_RADIUS)
        open_contexts: list[dict[str, object]] = []

        try:
            with host_path.open("r", encoding="utf-8", errors="replace") as fh:
                for lineno, raw_line in enumerate(fh, start=1):
                    raw_clean = raw_line.rstrip("\n")
                    line = raw_clean.strip()
                    line_for_context = self._truncate_text(line or raw_clean, 180)

                    remaining_contexts: list[dict[str, object]] = []
                    for ctx in open_contexts:
                        trigger_lineno = int(ctx["trigger_lineno"])
                        remaining_after = int(ctx["remaining_after"])
                        if lineno > trigger_lineno and remaining_after > 0 and line_for_context:
                            lines_ref = ctx.get("lines")
                            if isinstance(lines_ref, list):
                                lines_ref.append(f"L{lineno}: {line_for_context}")
                            remaining_after -= 1
                            ctx["remaining_after"] = remaining_after
                        if remaining_after > 0:
                            remaining_contexts.append(ctx)
                    open_contexts = remaining_contexts

                    if not line:
                        continue
                    lowered = line.lower()
                    has_signal = any(term in lowered for term in ADVISOR_SIGNAL_TERMS)
                    if len(signal_hits) < ADVISOR_DIGEST_MAX_HITS and has_signal:
                        signal_hits.append(f"L{lineno}: {self._truncate_text(line, 180)}")
                    if has_signal and len(signal_contexts) < ADVISOR_DIGEST_SIGNAL_CONTEXTS:
                        context_lines = [f"L{ctx_lineno}: {ctx_text}" for ctx_lineno, ctx_text in recent_lines]
                        context_lines.append(f"L{lineno}: {self._truncate_text(line, 180)}")
                        signal_contexts.append(context_lines)
                        open_contexts.append(
                            {
                                "trigger_lineno": lineno,
                                "remaining_after": ADVISOR_DIGEST_CONTEXT_RADIUS,
                                "lines": context_lines,
                            }
                        )
                    for match in ADVISOR_URL_RE.findall(line):
                        if match not in seen_urls:
                            seen_urls.add(match)
                            urls.append(match)
                            if len(urls) >= ADVISOR_DIGEST_MAX_ITEMS:
                                break
                    for match in ADVISOR_ROUTE_RE.findall(line):
                        if len(match) < 4 or match in seen_routes or match == "/":
                            continue
                        seen_routes.add(match)
                        routes.append(match)
                        if len(routes) >= ADVISOR_DIGEST_MAX_ITEMS:
                            break
                    for match in ADVISOR_JSON_KEY_RE.findall(line):
                        if match in seen_json_keys:
                            continue
                        seen_json_keys.add(match)
                        json_keys.append(match)
                        if len(json_keys) >= ADVISOR_DIGEST_MAX_ITEMS:
                            break
                    for match in ADVISOR_FORM_FIELD_RE.findall(line):
                        if match in seen_form_fields:
                            continue
                        seen_form_fields.add(match)
                        form_fields.append(match)
                        if len(form_fields) >= ADVISOR_DIGEST_MAX_ITEMS:
                            break
                    recent_lines.append((lineno, line_for_context))
        except OSError:
            pass

        return {
            "mode": ["text-scan-v1"],
            "head": [self._truncate_text(self._decode_artifact_preview(head), 900)] if head else [],
            "middle": [self._truncate_text(self._decode_artifact_preview(middle), 500)] if middle else [],
            "tail": [self._truncate_text(self._decode_artifact_preview(tail), 500)] if tail else [],
            "signal_hits": self._truncate_lines(signal_hits),
            "signal_contexts": [
                " | ".join(context_lines)
                for context_lines in signal_contexts
                if context_lines
            ],
            "urls": self._truncate_lines(urls),
            "routes": self._truncate_lines(routes),
            "json_keys": self._truncate_lines(json_keys),
            "form_fields": self._truncate_lines(form_fields),
        }

    def _binary_digest_sections(self, host_path: Path) -> dict[str, list[str] | str]:
        head = self._read_artifact_slice(host_path, start=0, size=ADVISOR_DIGEST_EXPANDED_TAIL_BYTES)
        strings_hits: list[str] = []
        if head:
            for raw_match in re.findall(rb"[ -~]{6,}", head):
                text = raw_match.decode("utf-8", errors="replace").strip()
                lowered = text.lower()
                if not text or not any(term in lowered for term in ADVISOR_SIGNAL_TERMS):
                    continue
                strings_hits.append(self._truncate_text(text, 120))
                if len(strings_hits) >= ADVISOR_DIGEST_MAX_ITEMS:
                    break
        return {
            "mode": ["binary-scan-v1"],
            "head": [head[:96].hex()] if head else [],
            "signal_hits": self._truncate_lines(strings_hits),
        }

    def _build_artifact_digest(self, artifact_path: str, host_path: Path) -> str:
        stat = host_path.stat()
        initial = self._read_artifact_slice(host_path, start=0, size=ADVISOR_ARTIFACT_PREVIEW_BYTES)
        text_like = self._is_text_like_artifact(host_path, initial)
        sections = (
            self._text_digest_sections(host_path)
            if text_like
            else self._binary_digest_sections(host_path)
        )
        lines = [
            "# Artifact Digest",
            f"- artifact: {artifact_path}",
            f"- file_size: {stat.st_size}",
            f"- file_type: {'text-like' if text_like else 'binary-like'}",
            f"- mode: {', '.join(sections.get('mode', []) or ['unknown'])}",
            "",
        ]
        section_specs = (
            ("Head sample", "head"),
            ("Middle sample", "middle"),
            ("Tail sample", "tail"),
            ("Signal hits", "signal_hits"),
            ("Signal contexts", "signal_contexts"),
            ("URLs", "urls"),
            ("Routes", "routes"),
            ("JSON keys", "json_keys"),
            ("Form fields", "form_fields"),
        )
        for title, key in section_specs:
            items = [str(item).strip() for item in sections.get(key, []) if str(item).strip()]
            if not items:
                continue
            lines.append(f"## {title}")
            lines.extend(f"- {item}" for item in items)
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    def _ensure_artifact_digest(self, artifact_path: str) -> tuple[str, str, str]:
        host_path = self._shared_artifact_host_path(artifact_path)
        if host_path is None or not host_path.exists() or not host_path.is_file():
            return "", "", ""

        signature = self._artifact_source_signature(host_path)
        digest_host_path, digest_container_path = self._artifact_digest_paths(artifact_path)
        cached = self._artifact_digest_cache.get(artifact_path)
        if cached and cached[0] == signature and digest_host_path.exists():
            return digest_container_path, cached[1], cached[2]

        digest_text = self._build_artifact_digest(artifact_path, host_path)
        revision = hashlib.sha256(digest_text.encode("utf-8", errors="replace")).hexdigest()
        digest_host_path.write_text(digest_text, encoding="utf-8")
        self._artifact_digest_cache[artifact_path] = (signature, revision, digest_text)
        return digest_container_path, revision, digest_text

    async def _post_artifact_fact(
        self,
        *,
        model_spec: str,
        artifact_path: str,
        fact_summary: str,
    ) -> bool:
        fingerprint = self._finding_fingerprint(
            "artifact",
            f"{artifact_path}\0{fact_summary}",
        )
        if fingerprint in self._shared_artifact_fingerprints:
            return False

        self._shared_artifact_fingerprints.add(fingerprint)
        digest_path, _revision, digest_text = self._ensure_artifact_digest(artifact_path)
        fact_summary = fact_summary or self._fallback_artifact_fact_summary(artifact_path, digest_text)
        pointer_path = self._persist_artifact_note_pointer(
            model_spec=model_spec,
            artifact_path=artifact_path,
            fact_summary=fact_summary,
            digest_path=digest_path,
        )
        finding = SharedFindingRef(
            model=model_spec,
            kind="artifact_ref",
            content=f"Artifact path: {artifact_path}",
            summary=fact_summary,
            artifact_path=artifact_path,
            pointer_path=pointer_path,
            digest_path=digest_path,
        )
        self._record_shared_finding(model_spec, finding)
        self._record_artifact_manifest_entry(
            model_spec=model_spec,
            fact_summary=fact_summary,
            artifact_path=artifact_path,
            digest_path=digest_path,
        )
        await self.message_bus.post(model_spec, finding)
        return True

    async def _maybe_share_artifact_finding(
        self,
        model_spec: str,
        solver: SolverProtocol,
        result: SolverResult,
    ) -> None:
        if result.status in (ERROR, QUOTA_ERROR, CANCELLED):
            return

        runtime_getter = getattr(solver, "get_runtime_status", None)
        runtime = runtime_getter() if callable(runtime_getter) else {}
        if not isinstance(runtime, dict):
            runtime = {}

        candidates = [
            result.findings_summary,
            str(runtime.get("last_exit_hint") or ""),
        ]
        trace_candidates = self._recent_trace_artifact_candidates(result.log_path)
        artifact_paths = self._extract_shared_artifact_paths(*candidates, *trace_candidates)
        for artifact_path in artifact_paths:
            if not self._is_shareable_artifact_path(artifact_path):
                continue
            fact_summary = self._artifact_fact_summary(artifact_path, *candidates, *trace_candidates)
            await self._post_artifact_fact(
                model_spec=model_spec,
                artifact_path=artifact_path,
                fact_summary=fact_summary,
            )
            return

    async def _monitor_live_artifact_sharing(self) -> None:
        while not self.cancel_event.is_set():
            for model_spec, solver in list(self.solvers.items()):
                runtime_getter = getattr(solver, "get_runtime_status", None)
                runtime = runtime_getter() if callable(runtime_getter) else {}
                if not isinstance(runtime, dict):
                    continue
                lifecycle = str(runtime.get("lifecycle") or "")
                if lifecycle in {"starting", "busy", "won", "quota_error", "cancelled"}:
                    continue

                candidates = [
                    str(runtime.get("last_exit_hint") or ""),
                    str(runtime.get("last_command") or ""),
                ]
                artifact_paths = self._extract_shared_artifact_paths(*candidates)
                for artifact_path in artifact_paths:
                    if not self._is_shareable_artifact_path(artifact_path):
                        continue
                    fact_summary = self._artifact_fact_summary(artifact_path, *candidates)
                    if not fact_summary:
                        continue
                    await self._post_artifact_fact(
                        model_spec=model_spec,
                        artifact_path=artifact_path,
                        fact_summary=fact_summary,
                    )
                    break
            try:
                await asyncio.wait_for(self.cancel_event.wait(), timeout=2.0)
            except TimeoutError:
                continue

    def _recent_trace_commands(self, log_path: str, limit: int = 8) -> list[str]:
        if not log_path:
            return []
        path = Path(log_path)
        if not path.exists():
            return []

        recent: list[str] = []
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "tool_call":
                continue
            step = event.get("step", "?")
            tool = event.get("tool", "?")
            args = str(event.get("args", "")).replace("\n", " ")
            recent.append(f"- step {step}: {tool} {args[:160]}")
        return recent[-limit:]

    def _build_writeup_draft(
        self,
        *,
        model_spec: str,
        result: SolverResult,
        trace_path: str,
        workspace_path: str,
        shared_artifacts_path: str,
    ) -> str:
        commands = self._recent_trace_commands(trace_path)
        command_block = "\n".join(commands) if commands else "- No trace commands captured."
        findings = result.findings_summary.strip() or "No findings summary captured."
        return "\n".join(
            [
                f"# {self.meta.name}",
                "",
                "## Metadata",
                f"- Category: {self.meta.category or 'Unknown'}",
                f"- Points: {self.meta.value or '?'}",
                f"- Winner model: {model_spec}",
                f"- Flag: {result.flag or '-'}",
                "",
                "## Overview",
                findings,
                "",
                "## Recon",
                f"- Trace: {trace_path or '-'}",
                f"- Shared artifacts: {shared_artifacts_path}",
                "",
                "## Exploit Path",
                findings,
                "",
                "## Files / Commands",
                f"- Workspace snapshot: {workspace_path or '-'}",
                command_block,
                "",
                "## Flag",
                result.flag or "-",
                "",
            ]
        )

    async def _persist_solved_artifacts(
        self,
        *,
        model_spec: str,
        solver: SolverProtocol,
        result: SolverResult,
    ) -> None:
        if result.status != FLAG_FOUND or self.saved_solve_artifacts:
            return

        async with self._save_lock:
            if result.status != FLAG_FOUND or self.saved_solve_artifacts:
                return

            challenge_root = Path(self.challenge_dir).resolve()
            solve_dir = challenge_root / "solve"
            solve_dir.mkdir(parents=True, exist_ok=True)

            workspace_path = ""
            sandbox = getattr(solver, "sandbox", None)
            workspace_dir_raw = str(getattr(sandbox, "workspace_dir", "") or "")
            workspace_dir = Path(workspace_dir_raw) if workspace_dir_raw else None
            if workspace_dir and workspace_dir.exists():
                workspace_dst = solve_dir / "workspace"
                shutil.rmtree(workspace_dst, ignore_errors=True)
                shutil.copytree(workspace_dir, workspace_dst)
                workspace_path = str(workspace_dst)

            trace_path = ""
            if result.log_path and Path(result.log_path).exists():
                trace_dst = solve_dir / "trace.jsonl"
                shutil.copy2(result.log_path, trace_dst)
                trace_path = str(trace_dst)

            flag_path = solve_dir / "flag.txt"
            flag_path.write_text((result.flag or "") + "\n", encoding="utf-8")

            saved_at = datetime.now(UTC).isoformat()
            result_payload: dict[str, object] = {
                "challenge_name": self.meta.name,
                "status": result.status,
                "flag": result.flag,
                "step_count": result.step_count,
                "winner_model": model_spec,
                "confirmation_source": self.winner_confirmation_source or "ctfd",
                "findings_summary": result.findings_summary,
                "advisor_note": self.last_advisor_note,
                "coordinator_advisor_note": self.last_coordinator_advisor_note,
                "shared_finding": self.last_shared_finding,
                "shared_findings": {
                    model_spec: finding.snapshot()
                    for model_spec, finding in sorted(self.shared_finding_events.items())
                },
                "trace_path": trace_path,
                "workspace_path": workspace_path,
                "shared_artifacts_path": str(self.shared_artifacts_dir.resolve()),
                "flag_candidates": {
                    flag: record.snapshot()
                    for flag, record in sorted(self.flag_candidates.items())
                },
                "saved_at": saved_at,
            }

            result_path = solve_dir / "result.json"
            result_path.write_text(json.dumps(result_payload, indent=2), encoding="utf-8")
            if self.result_store is not None:
                self.result_store[self.meta.name] = result_payload

            writeup_path = solve_dir / "writeup.md"
            writeup_path.write_text(
                self._build_writeup_draft(
                    model_spec=model_spec,
                    result=result,
                    trace_path=trace_path,
                    workspace_path=workspace_path,
                    shared_artifacts_path=str(self.shared_artifacts_dir.resolve()),
                ),
                encoding="utf-8",
            )

            self.saved_solve_artifacts = {
                "flag_path": str(flag_path),
                "writeup_path": str(writeup_path),
                "result_path": str(result_path),
                "trace_path": trace_path,
                "workspace_path": workspace_path,
                "shared_artifacts_path": str(self.shared_artifacts_dir.resolve()),
                "saved_at": saved_at,
            }

    def _create_solver(
        self,
        model_spec: str,
        *,
        sandbox=None,
        initial_step_count: int = 0,
    ):
        """Create the right solver type based on provider.

        - codex/* → CodexSolver (Codex App Server, subscription-first)
        - gemini/*, google/* → GeminiSolver (Gemini CLI, home-auth first)
        """
        provider = provider_from_spec(model_spec)

        def _report_flag_candidate(flag, evidence="", confidence="medium", step_count=0, trace_path=""):
            return self.report_flag_candidate(
                flag,
                model_spec,
                evidence=evidence,
                confidence=confidence,
                step_count=step_count,
                trace_path=trace_path,
            )
        _notify = self._make_notify_fn(model_spec)

        if provider == "claude-sdk":
            raise ValueError(
                f"Claude solver lanes are disabled for {model_spec}. "
                "Use Claude as coordinator/advisor only."
            )

        if provider == "codex":
            from backend.agents.runtime_solver import InSandboxRuntimeSolver

            return InSandboxRuntimeSolver(
                model_spec=model_spec,
                challenge_dir=self.challenge_dir,
                meta=self.meta,
                cost_tracker=self.cost_tracker,
                settings=self.settings,
                cancel_event=self.cancel_event,
                no_submit=self.no_submit,
                local_mode=self.local_mode,
                report_flag_candidate_fn=_report_flag_candidate,
                notify_coordinator=_notify,
                initial_step_count=initial_step_count,
                sandbox=sandbox,
            )

        if provider in ("gemini", "google"):
            from backend.agents.runtime_solver import InSandboxRuntimeSolver

            return InSandboxRuntimeSolver(
                model_spec=model_spec,
                challenge_dir=self.challenge_dir,
                meta=self.meta,
                cost_tracker=self.cost_tracker,
                settings=self.settings,
                cancel_event=self.cancel_event,
                no_submit=self.no_submit,
                local_mode=self.local_mode,
                report_flag_candidate_fn=_report_flag_candidate,
                notify_coordinator=_notify,
                initial_step_count=initial_step_count,
                sandbox=sandbox,
            )

        raise ValueError(f"Unsupported solver provider in model spec: {model_spec}")

    def _make_notify_fn(self, model_spec: str):
        """Create a callback that pushes solver messages to the coordinator inbox."""
        async def _notify(message: str) -> None:
            if self.coordinator_inbox:
                advised_message = await self._build_advised_coordinator_message(model_spec, message)
                pointer_path, _size_bytes = self._persist_shared_text_pointer(
                    f"coordinator-{self.meta.name}-{model_spec}",
                    advised_message,
                )
                self.coordinator_inbox.put_nowait(
                    CoordinatorNoteRef(
                        challenge_name=self.meta.name,
                        source_model=model_spec,
                        summary=self._compact_summary(advised_message),
                        pointer_path=pointer_path,
                    )
                )
                self.coordinator_message_count += 1
        return _notify

    @staticmethod
    def _normalize_candidate_flag(flag: str) -> str:
        return flag.strip()

    @staticmethod
    def _compact_candidate_marker(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.lower())

    @staticmethod
    def _candidate_marker_tokens(value: str) -> tuple[str, ...]:
        return tuple(token for token in re.findall(r"[a-z0-9]+", value.lower()) if token)

    @staticmethod
    def _extract_candidate_flag_body(flag: str) -> str:
        normalized = flag.strip()
        if normalized.count("{") != 1 or normalized.count("}") != 1 or not normalized.endswith("}"):
            return ""
        open_brace = normalized.find("{")
        if open_brace <= 0:
            return ""
        body = normalized[open_brace + 1 : -1].strip()
        return body if body and "{" not in body and "}" not in body else ""

    @classmethod
    def _looks_like_placeholder_marker(cls, value: str) -> bool:
        compact = cls._compact_candidate_marker(value)
        if not compact:
            return False
        if compact in FLAG_CANDIDATE_PLACEHOLDER_COMPACTS:
            return True
        tokens = cls._candidate_marker_tokens(value)
        return bool(tokens) and all(token in FLAG_CANDIDATE_PLACEHOLDER_TOKENS for token in tokens)

    @classmethod
    def _reject_candidate_reason(cls, flag: str) -> str:
        normalized = " ".join(flag.strip().split())
        if not normalized:
            return "empty flag"
        compact = cls._compact_candidate_marker(normalized)
        if compact in FLAG_CANDIDATE_SENTINEL_COMPACTS:
            return "placeholder sentinel"
        if cls._looks_like_placeholder_marker(normalized):
            return "placeholder sentinel"
        body = cls._extract_candidate_flag_body(normalized)
        if body and cls._looks_like_placeholder_marker(body):
            return "placeholder sentinel"
        return ""

    async def report_flag_candidate(
        self,
        flag: str,
        model_spec: str,
        *,
        evidence: str = "",
        confidence: str = "medium",
        step_count: int = 0,
        trace_path: str = "",
    ) -> str:
        normalized = self._normalize_candidate_flag(flag)
        if not normalized:
            return "Flag candidate rejected: empty flag."
        reject_reason = self._reject_candidate_reason(normalized)
        if reject_reason:
            return f"Flag candidate rejected: {reject_reason}."

        async with self._flag_lock:
            if self.confirmed_flag:
                return f"ALREADY SOLVED — flag already confirmed: {self.confirmed_flag}"

            candidate = self.flag_candidates.get(normalized)
            is_new = candidate is None
            if candidate is None:
                candidate = FlagCandidateRecord(
                    normalized_flag=normalized,
                    raw_flag=flag.strip() or normalized,
                )
                self.flag_candidates[normalized] = candidate

            candidate.last_seen_at = time.time()
            candidate.source_models.add(model_spec)
            candidate.confidences[model_spec] = confidence.strip() or "medium"
            candidate.step_counts[model_spec] = step_count
            if trace_path:
                candidate.trace_paths[model_spec] = trace_path
            cleaned_evidence = evidence.strip()
            if cleaned_evidence and cleaned_evidence not in candidate.evidence_snippets:
                candidate.evidence_snippets.append(cleaned_evidence[:500])
            candidate.advisor_decision = ""
            candidate.advisor_note = ""
            if cleaned_evidence:
                pointer_path, _size_bytes = self._persist_shared_text_pointer(
                    f"candidate-{self.meta.name}-{model_spec}",
                    cleaned_evidence,
                )
                if pointer_path:
                    candidate.evidence_pointer_paths[model_spec] = pointer_path
                    digest_path, _revision, _digest_text = self._persist_candidate_evidence_digest(
                        model_spec=model_spec,
                        flag=candidate.raw_flag,
                        pointer_path=pointer_path,
                        text=cleaned_evidence,
                        advisor_decision=candidate.advisor_decision,
                        advisor_note=candidate.advisor_note,
                    )
                    if digest_path:
                        candidate.evidence_digest_paths[model_spec] = digest_path
            candidate._review_started = False
            candidate.coordinator_notified_at = None
            candidate.status = "pending"

        await self._persist_runtime_state()

        if self.no_submit:
            if is_new:
                return (
                    f'Queued flag candidate "{normalized}" for operator review. '
                    "Keep exploring and do not submit it yourself."
                )
            return (
                f'Flag candidate "{normalized}" is already queued for operator review. '
                "Keep exploring and do not re-submit the same candidate."
            )

        display, is_confirmed = await self.try_submit_flag(normalized, model_spec)
        normalized_display = str(display or "").strip()
        status = "unknown"
        if normalized_display.startswith("CORRECT"):
            status = "correct"
        elif normalized_display.startswith("ALREADY SOLVED"):
            status = "already_solved"
        elif normalized_display.startswith("INCORRECT"):
            status = "incorrect"

        if status in {"correct", "already_solved", "incorrect"}:
            await self.note_coordinator_submission(normalized, normalized_display, status)
        else:
            async with self._flag_lock:
                candidate = self.flag_candidates.get(normalized)
                if candidate is not None:
                    candidate.submit_display = normalized_display
                    candidate.last_seen_at = time.time()
                    candidate.status = "pending"
            await self._persist_runtime_state()

        if is_confirmed:
            return normalized_display
        if status == "incorrect":
            return (
                f'Candidate "{normalized}" auto-submitted to CTFd: {normalized_display} '
                "Operator review can still confirm it manually if you have independent evidence."
            )
        return (
            f'Candidate "{normalized}" was not confirmed automatically. '
            f"{normalized_display or 'Keep investigating.'}"
        )

    async def reject_flag_candidate(self, flag: str, *, rejected_by: str = "operator_local") -> str:
        normalized = self._normalize_candidate_flag(flag)
        if not normalized:
            return "Candidate rejection rejected: empty flag."

        async with self._flag_lock:
            if self.confirmed_flag == normalized:
                return f'Cannot reject "{normalized}" because it is already confirmed.'

            candidate = self.flag_candidates.get(normalized)
            if candidate is None:
                return f'No candidate "{normalized}" is queued for {self.meta.name}.'

            candidate.status = "rejected"
            candidate.confirmation_source = rejected_by
            if self.local_mode:
                candidate.submit_display = f'USER REJECTED — "{candidate.raw_flag}" dismissed in local mode.'
            else:
                candidate.submit_display = f'USER REJECTED — "{candidate.raw_flag}" dismissed by operator review.'
            candidate.last_seen_at = time.time()
            rejection_scope = "locally" if self.local_mode else "manually by operator review"
            advisory = (
                f'Candidate rejected {rejection_scope}: "{candidate.raw_flag}". '
                "Treat it as a dead end unless you get materially different evidence."
            )
            for model_spec in sorted(candidate.source_models):
                solver = self.solvers.get(model_spec)
                if solver and hasattr(solver, "bump_advisory"):
                    solver.bump_advisory(advisory)

        await self._persist_runtime_state()
        return candidate.submit_display

    async def _review_flag_candidate(self, normalized_flag: str, source_model: str) -> None:
        candidate = self.flag_candidates.get(normalized_flag)
        if candidate is None:
            return

        evidence = "\n".join(candidate.evidence_snippets[-3:])
        review = await self._run_advisor_call(
            source_model,
            timeout_seconds=ADVISOR_COORDINATOR_TIMEOUT_SECONDS,
            operation_label="candidate review",
            call=lambda advisor: advisor.review_flag_candidate(
                source_model=source_model,
                challenge_brief=self._advisor_challenge_brief(),
                flag=candidate.raw_flag,
                evidence=evidence,
                sibling_insights=self._gather_sibling_insights(source_model),
            ),
        )

        candidate = self.flag_candidates.get(normalized_flag)
        if candidate is None:
            return

        candidate.advisor_decision = (
            review.decision if review and review.decision in {"likely", "unlikely", "insufficient"} else "insufficient"
        )
        candidate.advisor_note = (review.note if review else "").strip()[:500]
        candidate.status = "pending_coordinator"

        for model_spec, pointer_path in list(candidate.evidence_pointer_paths.items()):
            evidence_text = self._read_shared_pointer_text(pointer_path)
            if not evidence_text.strip():
                evidence_text = "\n".join(candidate.evidence_snippets[-3:]).strip()
            if not evidence_text:
                continue
            digest_path, _revision, _digest_text = self._persist_candidate_evidence_digest(
                model_spec=model_spec,
                flag=candidate.raw_flag,
                pointer_path=pointer_path,
                text=evidence_text,
                advisor_decision=candidate.advisor_decision,
                advisor_note=candidate.advisor_note,
            )
            if digest_path:
                candidate.evidence_digest_paths[model_spec] = digest_path

        if not self.coordinator_inbox:
            await self._persist_runtime_state()
            return

        self.coordinator_inbox.put_nowait(
            CandidateRef(
                challenge_name=self.meta.name,
                flag=candidate.raw_flag,
                source_models=sorted(candidate.source_models) or [source_model],
                advisor_decision=candidate.advisor_decision,
                advisor_note=candidate.advisor_note,
                summary=self._compact_summary(evidence or candidate.raw_flag),
                evidence_digest_paths=dict(candidate.evidence_digest_paths),
                evidence_pointer_paths=dict(candidate.evidence_pointer_paths),
                trace_paths=dict(candidate.trace_paths),
            )
        )
        candidate.coordinator_notified_at = time.time()
        self.coordinator_message_count += 1
        await self._persist_runtime_state()

    async def note_coordinator_submission(self, flag: str, display: str, status: str) -> None:
        normalized = self._normalize_candidate_flag(flag)
        candidate = self.flag_candidates.get(normalized)
        now = time.time()
        solver = None
        if candidate is None:
            candidate = FlagCandidateRecord(
                normalized_flag=normalized,
                raw_flag=flag.strip() or normalized,
            )
            self.flag_candidates[normalized] = candidate

        candidate.submit_display = display
        candidate.last_seen_at = now
        if status in {"correct", "already_solved"}:
            candidate.status = "confirmed"
            candidate.confirmation_source = "ctfd"
            self.confirmed_flag = normalized
            self.winner_confirmation_source = "ctfd"
            self.winner = SolverResult(
                flag=normalized,
                status=FLAG_FOUND,
                findings_summary=display,
                step_count=max(candidate.step_counts.values(), default=0),
                cost_usd=0.0,
                log_path=next(iter(candidate.trace_paths.values()), ""),
            )
            if not self.winner_model_spec and candidate.source_models:
                self.winner_model_spec = sorted(candidate.source_models)[0]
            solver = (
                (self.solvers.get(self.winner_model_spec or "") if self.winner_model_spec else None)
                or next(iter(self.solvers.values()), None)
            )
            await self._persist_runtime_state()
        if status in {"correct", "already_solved"}:
            if solver is None:
                solver = SimpleNamespace(sandbox=None)
            await self._persist_solved_artifacts(
                model_spec=self.winner_model_spec or "ctfd/confirm",
                solver=solver,
                result=self.winner,
            )
            self._set_all_solver_stop_reasons(
                f"flag confirmed by CTFd for {self.meta.name}",
            )
            await self._stop_solver_processes()
            self.cancel_event.set()
            await self._persist_runtime_state()
            return

        if status == "incorrect":
            candidate.status = "incorrect"
            candidate.confirmation_source = ""
            advisory = (
                f'Candidate rejected by coordinator: "{candidate.raw_flag}". '
                "Do not retry the same flag automatically. Keep exploring other hypotheses, "
                "but operator review may still confirm it manually if external evidence is stronger than the CTFd response."
            )
            for model_spec in sorted(candidate.source_models):
                solver = self.solvers.get(model_spec)
                if solver:
                    solver.bump_advisory(advisory)
        await self._persist_runtime_state()

    async def approve_flag_candidate(self, flag: str, *, approved_by: str = "operator_local") -> str:
        normalized = self._normalize_candidate_flag(flag)
        if not normalized:
            return "Candidate approval rejected: empty flag."

        async with self._flag_lock:
            if self.confirmed_flag:
                if self.confirmed_flag == normalized:
                    return f'Already solved with "{normalized}".'
                return (
                    f'Cannot approve "{normalized}" because '
                    f'"{self.confirmed_flag}" is already confirmed.'
                )

            candidate = self.flag_candidates.get(normalized)
            if candidate is None:
                return f'No candidate "{normalized}" is queued for {self.meta.name}.'

            source_model = (
                sorted(candidate.source_models)[0]
                if candidate.source_models
                else (self.winner_model_spec or (self.model_specs[0] if self.model_specs else ""))
            )
            if self.local_mode:
                display = f'USER CONFIRMED LOCALLY — "{candidate.raw_flag}" marked solved in local mode.'
            else:
                display = (
                    f'USER CONFIRMED MANUALLY — "{candidate.raw_flag}" marked solved '
                    "without CTFd confirmation."
                )
            candidate.status = "confirmed"
            candidate.confirmation_source = approved_by
            candidate.submit_display = display
            candidate.last_seen_at = time.time()
            self.confirmed_flag = normalized
            self.winner_model_spec = source_model or self.winner_model_spec
            self.winner_confirmation_source = approved_by
            self.winner = SolverResult(
                flag=normalized,
                status=FLAG_FOUND,
                findings_summary=display,
                step_count=max(candidate.step_counts.values(), default=0),
                cost_usd=0.0,
                log_path=next(iter(candidate.trace_paths.values()), ""),
            )
            solver = (
                (self.solvers.get(self.winner_model_spec or "") if self.winner_model_spec else None)
                or next(iter(self.solvers.values()), None)
            )

        await self._persist_runtime_state()

        if solver is None:
            solver = SimpleNamespace(sandbox=None)
        await self._persist_solved_artifacts(
            model_spec=self.winner_model_spec or "operator/local",
            solver=solver,
            result=self.winner,
        )
        self._set_all_solver_stop_reasons(
            f"flag approved locally for {self.meta.name}",
        )
        await self._stop_solver_processes()
        self.cancel_event.set()
        await self._persist_runtime_state()
        return display

    async def mark_solved_externally(
        self,
        flag: str,
        *,
        note: str = "",
        approved_by: str = "operator_external",
    ) -> str:
        normalized = self._normalize_candidate_flag(flag)
        if not normalized:
            return "External solve rejected: empty flag."

        note_text = " ".join(str(note or "").split()).strip()[:500]

        async with self._flag_lock:
            if self.confirmed_flag:
                if self.confirmed_flag == normalized:
                    return f'Already solved with "{normalized}".'
                return (
                    f'Cannot mark "{normalized}" solved because '
                    f'"{self.confirmed_flag}" is already confirmed.'
                )

            candidate = self.flag_candidates.get(normalized)
            if candidate is None:
                candidate = FlagCandidateRecord(
                    normalized_flag=normalized,
                    raw_flag=flag.strip() or normalized,
                )
                self.flag_candidates[normalized] = candidate

            source_model = (
                sorted(candidate.source_models)[0]
                if candidate.source_models
                else (self.winner_model_spec or (self.model_specs[0] if self.model_specs else ""))
            )
            if note_text and note_text not in candidate.evidence_snippets:
                candidate.evidence_snippets.append(note_text)
            display = (
                f'USER REPORTED EXTERNAL SOLVE — "{candidate.raw_flag}" marked solved from operator input.'
            )
            if note_text:
                display = f"{display} Note: {note_text[:200]}"
            candidate.status = "confirmed"
            candidate.confirmation_source = approved_by
            candidate.submit_display = display
            candidate.last_seen_at = time.time()
            self.confirmed_flag = normalized
            self.winner_model_spec = source_model or self.winner_model_spec
            self.winner_confirmation_source = approved_by
            self.winner = SolverResult(
                flag=normalized,
                status=FLAG_FOUND,
                findings_summary=display,
                step_count=max(candidate.step_counts.values(), default=0),
                cost_usd=0.0,
                log_path=next(iter(candidate.trace_paths.values()), ""),
            )
            solver = (
                (self.solvers.get(self.winner_model_spec or "") if self.winner_model_spec else None)
                or next(iter(self.solvers.values()), None)
            )

        await self._persist_runtime_state()

        if solver is None:
            solver = SimpleNamespace(sandbox=None)
        await self._persist_solved_artifacts(
            model_spec=self.winner_model_spec or "operator/external",
            solver=solver,
            result=self.winner,
        )
        self._set_all_solver_stop_reasons(
            f"external solve reported for {self.meta.name}",
        )
        await self._stop_solver_processes()
        self.cancel_event.set()
        await self._persist_runtime_state()
        return display

    def _advisor_backend_for_source(self, model_spec: str) -> str:
        del model_spec
        return self._sticky_advisor_backend or "claude"

    def _set_sticky_advisor_backend(self, backend: str, reason: str) -> None:
        if backend != "codex":
            return
        if self._sticky_advisor_backend == "codex":
            return
        self._sticky_advisor_backend = "codex"
        self._sticky_advisor_reason = reason.strip()[:500]
        logger.warning("[%s] Switching advisor backend to Codex: %s", self.meta.name, self._sticky_advisor_reason or "fallback requested")

    @staticmethod
    def _should_sticky_fallback_to_codex(exc: Exception) -> bool:
        if isinstance(exc, AuthValidationError):
            return True
        text = str(exc)
        if ChallengeSwarm._advisor_limit_reason_text(text):
            return True
        normalized = text.lower()
        return any(
            needle in normalized
            for needle in (
                "quota",
                "rate limit",
                "rate-limit",
                "too many requests",
                "unauthorized",
                "forbidden",
                "permission",
                "authentication",
                "auth",
                "401",
                "403",
            )
        )

    @staticmethod
    def _advisor_limit_reason_text(text: str) -> str | None:
        normalized = " ".join(text.lower().split())
        if any(
            needle in normalized
            for needle in (
                "you've hit your limit",
                "you have hit your limit",
                "usage limit",
                "rate limit",
                "rate-limit",
                "too many requests",
                "try again at",
                "quota",
                "resets ",
            )
        ):
            return text.strip()[:500]
        return None

    @staticmethod
    def _format_exception_text(exc: BaseException) -> str:
        text = " ".join(str(exc).split()).strip()
        if text:
            return text[:500]
        return exc.__class__.__name__

    @classmethod
    def _advisor_reply_requests_codex_fallback(cls, result: object) -> str | None:
        if isinstance(result, str):
            return cls._advisor_limit_reason_text(result)
        if isinstance(result, CandidateReview):
            return cls._advisor_limit_reason_text(result.note)
        return None

    def _get_advisor(self, model_spec: str) -> AdvisorProtocol:
        backend = self._advisor_backend_for_source(model_spec)
        cached = self._advisors.get(backend)
        if cached is not None:
            return cached

        try:
            if backend == "codex":
                from backend.agents.codex_advisor import CodexAdvisor

                advisor = CodexAdvisor.maybe_create(self.settings, self.meta.name)
            else:
                from backend.agents.claude_advisor import ClaudeAdvisor

                advisor = ClaudeAdvisor.maybe_create(self.settings, self.meta.name)
        except Exception as exc:
            logger.warning("[%s] %s advisor unavailable: %s", self.meta.name, backend, exc)
            advisor = None

        if backend == "claude" and advisor is None:
            self._set_sticky_advisor_backend("codex", "Claude advisor unavailable")
            backend = "codex"
            cached = self._advisors.get(backend)
            if cached is not None:
                return cached
            try:
                from backend.agents.codex_advisor import CodexAdvisor

                advisor = CodexAdvisor.maybe_create(self.settings, self.meta.name)
            except Exception as exc:
                logger.warning("[%s] codex advisor unavailable after Claude fallback: %s", self.meta.name, exc)
                advisor = None

        resolved: AdvisorProtocol = advisor or NoopAdvisor()
        self._advisors[backend] = resolved
        return resolved

    async def _run_advisor_call(
        self,
        model_spec: str,
        *,
        timeout_seconds: float,
        operation_label: str,
        call: Callable[[AdvisorProtocol], Awaitable[TAdvisorResult]],
    ) -> TAdvisorResult | None:
        backend = self._advisor_backend_for_source(model_spec)
        cooldown_key = f"{backend}:{operation_label}"
        backoff_until = self._advisor_timeout_backoff_until.get(cooldown_key, 0.0)
        remaining_backoff = backoff_until - time.monotonic()
        if remaining_backoff > 0:
            if self._should_log_advisor_backoff(cooldown_key, remaining_backoff):
                logger.debug(
                    "[%s/%s] %s advisor skipped (%s): timeout backoff %.0fs remaining",
                    self.meta.name,
                    model_spec,
                    backend,
                    operation_label,
                    remaining_backoff,
                )
            return None
        self._advisor_timeout_backoff_buckets.pop(cooldown_key, None)
        advisor = self._get_advisor(model_spec)
        try:
            result = await asyncio.wait_for(call(advisor), timeout=timeout_seconds)
        except Exception as exc:
            exc_text = self._format_exception_text(exc)
            self._record_advisor_timeout(cooldown_key, exc)
            if backend == "claude" and self._should_sticky_fallback_to_codex(exc):
                self._set_sticky_advisor_backend("codex", f"{operation_label}: {exc_text}")
                await self._persist_runtime_state()
                retry_advisor = self._get_advisor(model_spec)
                retry_backend = self._advisor_backend_for_source(model_spec)
                retry_key = f"{retry_backend}:{operation_label}"
                try:
                    return await asyncio.wait_for(call(retry_advisor), timeout=timeout_seconds)
                except Exception as retry_exc:
                    retry_text = self._format_exception_text(retry_exc)
                    self._record_advisor_timeout(retry_key, retry_exc)
                    logger.debug(
                        "[%s/%s] codex advisor retry skipped after Claude failure (%s): %s",
                        self.meta.name,
                        model_spec,
                        operation_label,
                        retry_text,
                    )
                    return None

            logger.debug(
                "[%s/%s] %s advisor skipped (%s): %s",
                self.meta.name,
                model_spec,
                backend,
                operation_label,
                exc_text,
            )
            return None
        if backend == "claude":
            fallback_reason = self._advisor_reply_requests_codex_fallback(result)
            if fallback_reason:
                self._set_sticky_advisor_backend("codex", f"{operation_label}: {fallback_reason}")
                await self._persist_runtime_state()
                retry_advisor = self._get_advisor(model_spec)
                retry_backend = self._advisor_backend_for_source(model_spec)
                retry_key = f"{retry_backend}:{operation_label}"
                try:
                    return await asyncio.wait_for(call(retry_advisor), timeout=timeout_seconds)
                except Exception as retry_exc:
                    retry_text = self._format_exception_text(retry_exc)
                    self._record_advisor_timeout(retry_key, retry_exc)
                    logger.debug(
                        "[%s/%s] codex advisor retry skipped after Claude reply fallback (%s): %s",
                        self.meta.name,
                        model_spec,
                        operation_label,
                        retry_text,
                    )
                    return None
        self._advisor_timeout_streaks.pop(cooldown_key, None)
        self._advisor_timeout_backoff_until.pop(cooldown_key, None)
        self._advisor_timeout_backoff_buckets.pop(cooldown_key, None)
        return result

    def _record_advisor_timeout(self, cooldown_key: str, exc: BaseException) -> None:
        if not isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return
        streak = self._advisor_timeout_streaks.get(cooldown_key, 0) + 1
        self._advisor_timeout_streaks[cooldown_key] = streak
        if streak < ADVISOR_TIMEOUT_BACKOFF_AFTER_CONSECUTIVE_TIMEOUTS:
            return
        self._advisor_timeout_backoff_until[cooldown_key] = (
            time.monotonic() + ADVISOR_TIMEOUT_BACKOFF_SECONDS
        )
        self._advisor_timeout_backoff_buckets.pop(cooldown_key, None)

    def _schedule_background(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)

        def _done(done_task: asyncio.Task) -> None:
            self._background_tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("[%s] Background task failed: %s", self.meta.name, exc)

        task.add_done_callback(_done)

    async def _resume_pending_candidate_reviews(self) -> None:
        if self.flag_candidates:
            await self._persist_runtime_state()

    async def _build_advised_coordinator_message(self, model_spec: str, message: str) -> str:
        advice = await self._run_advisor_call(
            model_spec,
            timeout_seconds=ADVISOR_COORDINATOR_TIMEOUT_SECONDS,
            operation_label="coordinator annotation",
            call=lambda advisor: advisor.annotate_coordinator_message(
                source_model=model_spec,
                challenge_brief=self._advisor_challenge_brief(),
                message=message,
                sibling_insights=self._gather_sibling_insights(model_spec),
            ),
        )
        advice = (advice or "").strip()
        if not advice:
            return message
        self.last_advisor_note = advice
        self.last_coordinator_advisor_note = advice
        self.advisor_coordinator_count += 1
        return f"{message}\n\n[Advisor] {advice}"

    def _gather_sibling_insights(self, exclude_model: str) -> str:
        parts: list[str] = []
        for model, finding in self.findings.items():
            if model != exclude_model and finding:
                parts.append(f"[{model}]: {finding}")
        if not parts:
            return "No sibling insights available yet."
        return self._clip_text_block(
            "\n\n".join(parts[-ADVISOR_SIBLING_INSIGHTS_MAX_ITEMS:]),
            limit=ADVISOR_SIBLING_INSIGHTS_MAX_CHARS,
        )

    def _advisor_challenge_brief(self) -> str:
        name = str(getattr(self.meta, "name", "Unknown") or "Unknown")
        category = str(getattr(self.meta, "category", "") or "Unknown")
        value = getattr(self.meta, "value", 0) or "?"
        description = self._normalize_text_line(str(getattr(self.meta, "description", "") or ""))
        connection_info = self._normalize_text_line(str(getattr(self.meta, "connection_info", "") or ""))

        hints: list[str] = []
        for hint in getattr(self.meta, "hints", []) or []:
            if isinstance(hint, dict):
                content = self._normalize_text_line(str(hint.get("content", "") or ""))
                if content:
                    hints.append(content)

        distfiles = list_distfiles(self.challenge_dir)
        lines = [
            f"Name: {name}",
            f"Category: {category}",
            f"Points: {value}",
        ]
        if description:
            lines.extend(["Description:", description[:600]])
        if connection_info:
            lines.extend(["Connection:", connection_info[:200]])
        if hints:
            lines.extend(["Hints:"] + [f"- {content[:200]}" for content in hints[:3]])
        if distfiles:
            lines.extend(["Distfiles:", ", ".join(distfiles[:10])[:400]])
        return "\n".join(lines)

    def _manifest_excerpt(self, max_lines: int = 16) -> str:
        lines = self._manifest_lines()
        if not lines:
            return ""
        excerpt = "\n".join(lines if len(lines) <= max_lines else lines[-max_lines:]).strip()
        return self._clip_text_block(excerpt, limit=ADVISOR_MANIFEST_EXCERPT_MAX_CHARS)

    def _manifest_signature(self) -> str:
        path = self._manifest_file_path()
        if not path.exists():
            return ""
        try:
            stat = path.stat()
        except OSError:
            return ""
        return f"{stat.st_mtime_ns}:{stat.st_size}"

    def _focused_manifest_excerpt(self, focus_paths: list[str], max_lines: int = 12) -> str:
        lines = self._manifest_lines()
        if not lines:
            return ""

        normalized_focus = [item.strip() for item in focus_paths if item.strip()]
        if not normalized_focus:
            return self._manifest_excerpt(max_lines=max_lines)
        digested_focus = {
            artifact_path
            for artifact_path in normalized_focus
            if self._ensure_artifact_digest(artifact_path)[1]
        }

        focused_blocks: list[list[str]] = []
        current_block: list[str] = []
        for raw_line in lines:
            line = raw_line.rstrip()
            if line.startswith("- ") and current_block:
                focused_blocks.append(current_block)
                current_block = [line]
                continue
            if line.startswith("- "):
                current_block = [line]
                continue
            if current_block:
                current_block.append(line)
        if current_block:
            focused_blocks.append(current_block)

        matched_lines: list[str] = []
        matched_focus_block = False
        for block in reversed(focused_blocks):
            block_text = "\n".join(block)
            matched_paths = [path for path in normalized_focus if path in block_text]
            if not matched_paths:
                continue
            matched_focus_block = True
            if "digest:" in block_text.lower() or any(path in digested_focus for path in matched_paths):
                continue
            matched_lines.extend(block)
            if len(matched_lines) >= max_lines:
                break

        excerpt = "\n".join(reversed(matched_lines[-max_lines:])).strip()
        if not excerpt:
            if matched_focus_block:
                return ""
            return self._manifest_excerpt(max_lines=max_lines)
        return self._clip_text_block(excerpt, limit=ADVISOR_MANIFEST_EXCERPT_MAX_CHARS)

    def _shared_artifact_host_path(self, artifact_path: str) -> Path | None:
        prefix = f"{SHARED_ARTIFACTS_CONTAINER_ROOT}/"
        if not artifact_path.startswith(prefix):
            return None
        relative = artifact_path.removeprefix(prefix)
        if not relative:
            return None
        return (self.shared_artifacts_dir / relative).resolve()

    def _decode_artifact_preview(self, raw: bytes) -> str:
        if not raw:
            return ""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
        cleaned = "\n".join(line.rstrip() for line in text.splitlines()[:24]).strip()
        return cleaned or raw[:128].hex()

    def _is_text_like_artifact(self, host_path: Path, raw: bytes) -> bool:
        if host_path.suffix.lower() in ADVISOR_TEXTLIKE_SUFFIXES:
            return True
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return True

    def _artifact_preview_has_signal(self, text: str) -> bool:
        lowered = text.lower()
        return any(term in lowered for term in ADVISOR_SIGNAL_TERMS)

    def _artifact_preview_block(self, artifact_path: str) -> str:
        host_path = self._shared_artifact_host_path(artifact_path)
        if host_path is None or not host_path.exists() or not host_path.is_file():
            return ""

        raw = self._read_artifact_slice(host_path, start=0, size=ADVISOR_ARTIFACT_PREVIEW_BYTES)
        if not raw:
            return f"{artifact_path}\n[head-2k]\n(empty file)"

        cleaned = self._decode_artifact_preview(raw)
        return f"{artifact_path}\n[head-2k]\n{cleaned[:800]}"

    def _artifact_preview_block_expanded(self, artifact_path: str) -> str:
        host_path = self._shared_artifact_host_path(artifact_path)
        if host_path is None or not host_path.exists() or not host_path.is_file():
            return ""

        try:
            file_size = host_path.stat().st_size
        except OSError:
            return ""
        head = self._read_artifact_slice(host_path, start=0, size=ADVISOR_ARTIFACT_ESCALATED_HEAD_BYTES)
        if not head or not self._is_text_like_artifact(host_path, head):
            return ""

        suffix = host_path.suffix.lower()
        if suffix in ADVISOR_HEAD_ONLY_SUFFIXES or file_size <= ADVISOR_ARTIFACT_ESCALATED_HEAD_BYTES:
            body = self._decode_artifact_preview(head)
            return f"{artifact_path}\n[head-8k]\n{body[:2000]}"

        try:
            with host_path.open("rb") as fp:
                fp.seek(max(0, file_size - ADVISOR_ARTIFACT_ESCALATED_TAIL_BYTES))
                tail = fp.read(ADVISOR_ARTIFACT_ESCALATED_TAIL_BYTES)
        except OSError:
            tail = b""

        head_text = self._decode_artifact_preview(head)
        tail_text = self._decode_artifact_preview(tail)
        if not tail_text:
            return f"{artifact_path}\n[head-8k]\n{head_text[:2000]}"
        return (
            f"{artifact_path}\n[head-tail-4k]\n"
            f"{head_text[:1200]}\n\n--- tail ---\n{tail_text[:1200]}"
        )

    def _artifact_signal_context_preview_block(self, artifact_path: str) -> str:
        host_path = self._shared_artifact_host_path(artifact_path)
        if host_path is None or not host_path.exists() or not host_path.is_file():
            return ""

        raw = self._read_artifact_slice(host_path, start=0, size=ADVISOR_ARTIFACT_PREVIEW_BYTES)
        if not raw or not self._is_text_like_artifact(host_path, raw):
            return ""

        contexts: list[list[str]] = []
        recent_lines: deque[tuple[int, str]] = deque(maxlen=ADVISOR_DIGEST_CONTEXT_RADIUS)
        open_contexts: list[dict[str, object]] = []
        try:
            with host_path.open("r", encoding="utf-8", errors="replace") as fh:
                for lineno, raw_line in enumerate(fh, start=1):
                    raw_clean = raw_line.rstrip("\n")
                    line = raw_clean.strip()
                    line_for_context = self._truncate_text(line or raw_clean, 180)

                    remaining_contexts: list[dict[str, object]] = []
                    for ctx in open_contexts:
                        trigger_lineno = int(ctx["trigger_lineno"])
                        remaining_after = int(ctx["remaining_after"])
                        if lineno > trigger_lineno and remaining_after > 0 and line_for_context:
                            lines_ref = ctx.get("lines")
                            if isinstance(lines_ref, list):
                                lines_ref.append(f"L{lineno}: {line_for_context}")
                            remaining_after -= 1
                            ctx["remaining_after"] = remaining_after
                        if remaining_after > 0:
                            remaining_contexts.append(ctx)
                    open_contexts = remaining_contexts

                    lowered = line.lower()
                    if line and any(term in lowered for term in ADVISOR_SIGNAL_TERMS):
                        context_lines = [f"L{ctx_lineno}: {ctx_text}" for ctx_lineno, ctx_text in recent_lines]
                        context_lines.append(f"L{lineno}: {self._truncate_text(line, 180)}")
                        contexts.append(context_lines)
                        open_contexts.append(
                            {
                                "trigger_lineno": lineno,
                                "remaining_after": ADVISOR_DIGEST_CONTEXT_RADIUS,
                                "lines": context_lines,
                            }
                        )
                        if len(contexts) >= ADVISOR_ARTIFACT_SIGNAL_CONTEXT_MAX_HITS:
                            break
                    if line_for_context:
                        recent_lines.append((lineno, line_for_context))
        except OSError:
            return ""

        if not contexts:
            return ""
        blocks = [" | ".join(lines) for lines in contexts if lines]
        if not blocks:
            return ""
        return f"{artifact_path}\n[signal-contexts]\n" + "\n".join(blocks)

    def _artifact_digest_block(self, artifact_path: str) -> str:
        digest_path, revision, digest_text = self._ensure_artifact_digest(artifact_path)
        if not digest_text:
            return ""
        compact = digest_text.strip()
        if len(compact) > 1600:
            compact = compact[:1597].rstrip() + "..."
        return f"{digest_path}\n[digest-{revision[:12]}]\n{compact}"

    def _advisor_artifact_previews(self, *texts: str) -> str:
        raw_paths = self._extract_shared_artifact_paths(*texts)
        paths = [
            artifact_path
            for artifact_path in raw_paths
            if self._is_shareable_artifact_path(artifact_path)
        ]
        if not paths:
            return ""

        previews: list[str] = []
        seen: set[str] = set()
        path_counts: dict[str, int] = {}
        signal_context_count = 0
        for artifact_path in paths:
            path_counts[artifact_path] = path_counts.get(artifact_path, 0) + 1

        escalated_count = 0
        for artifact_path in paths:
            if artifact_path in seen:
                continue
            seen.add(artifact_path)
            block = self._artifact_digest_block(artifact_path)
            if not block:
                block = self._artifact_preview_block(artifact_path)
                if not block:
                    continue
                preview_block = block
                if escalated_count < ADVISOR_ARTIFACT_ESCALATED_MAX_FILES:
                    base_body = block.split("\n", 2)[-1]
                    host_path = self._shared_artifact_host_path(artifact_path)
                    text_like = False
                    truncated = False
                    if host_path is not None and host_path.exists() and host_path.is_file():
                        try:
                            head = self._read_artifact_slice(
                                host_path,
                                start=0,
                                size=ADVISOR_ARTIFACT_PREVIEW_BYTES,
                            )
                            truncated = host_path.stat().st_size > ADVISOR_ARTIFACT_PREVIEW_BYTES
                            text_like = self._is_text_like_artifact(host_path, head)
                        except OSError:
                            text_like = False
                            truncated = False
                    repeated = path_counts.get(artifact_path, 0) >= 2
                    if text_like and (repeated or (truncated and self._artifact_preview_has_signal(base_body))):
                        if signal_context_count < ADVISOR_ARTIFACT_SIGNAL_CONTEXT_MAX_FILES:
                            signal_context = self._artifact_signal_context_preview_block(artifact_path)
                            if signal_context:
                                block = signal_context
                                signal_context_count += 1
                        if block == preview_block and escalated_count < ADVISOR_ARTIFACT_ESCALATED_MAX_FILES:
                            expanded = self._artifact_preview_block_expanded(artifact_path)
                            if expanded:
                                block = expanded
                                escalated_count += 1
            previews.append(block)
            if len(previews) >= ADVISOR_ARTIFACT_PREVIEW_MAX_FILES:
                break
        return self._clip_text_block(
            "\n\n---\n\n".join(previews),
            limit=ADVISOR_ARTIFACT_PREVIEW_MAX_CHARS,
        )

    def _artifact_finding_excerpt_from_paths(self, artifact_paths: list[str]) -> str:
        seen: set[str] = set()
        lines: list[str] = []
        for artifact_path in artifact_paths:
            normalized = artifact_path.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            lines.append(f"Artifact path: {normalized}")
            if len(lines) >= ADVISOR_ARTIFACT_FINDING_LIMIT:
                break
        return "\n".join(lines)

    async def _maybe_issue_lane_digest_updates(self) -> None:
        findings = await self.message_bus.snapshot_findings()
        artifact_paths = [
            artifact_path
            for artifact_path in self._extract_shared_artifact_paths(
                "\n".join(finding.content for finding in findings)
            )
            if self._is_shareable_artifact_path(artifact_path)
        ]
        if not artifact_paths:
            return

        digest_updates: list[tuple[str, str, str]] = []
        for artifact_path in artifact_paths[:ADVISOR_ARTIFACT_PREVIEW_MAX_FILES]:
            digest_path, revision, _digest_text = self._ensure_artifact_digest(artifact_path)
            if not digest_path or not revision:
                continue
            digest_updates.append((artifact_path, digest_path, revision))
        if not digest_updates:
            return

        for model_spec, solver in self.solvers.items():
            if model_spec in self.agent_results:
                continue
            seen_revisions = self._lane_seen_digest_revisions.setdefault(model_spec, {})
            pending_paths: list[str] = []
            updated_pairs: list[tuple[str, str]] = []
            for artifact_path, digest_path, revision in digest_updates:
                if seen_revisions.get(artifact_path) == revision:
                    continue
                pending_paths.append(digest_path)
                updated_pairs.append((artifact_path, revision))
            if not pending_paths:
                continue
            bullet_lines = "\n".join(f"- {path}" for path in pending_paths[:3])
            solver.bump(
                "Updated shared artifact digest available:\n"
                f"{bullet_lines}\n"
                "Read the relevant digest before another broad search. Prefer digest, then manifest, then the raw artifact."
            )
            for artifact_path, revision in updated_pairs:
                seen_revisions[artifact_path] = revision

    def _lane_advisory_state(self, model_spec: str, runtime: dict[str, object]) -> str:
        lifecycle = str(runtime.get("lifecycle") or "unknown")
        current_command = str(runtime.get("current_command") or "").strip()
        last_command = str(runtime.get("last_command") or "").strip()
        last_exit_hint = str(runtime.get("last_exit_hint") or "").strip()
        parts = [
            f"Lane: {model_spec}",
            f"Lifecycle: {lifecycle}",
            f"Current command: {current_command or '-'}",
            f"Last command: {last_command or '-'}",
            f"Last note: {last_exit_hint or '-'}",
        ]
        return "\n".join(parts)

    def _lane_advisory_runtime_marker(
        self,
        model_spec: str,
        runtime: dict[str, object],
    ) -> tuple[str, str, int, int, str]:
        lifecycle = str(runtime.get("lifecycle") or "unknown")
        step_count = _int_from_object(runtime.get("step_count", 0))
        last_completed_at = int(float(runtime.get("last_completed_at") or 0) or 0)
        last_exit_hint = self._truncate_text(str(runtime.get("last_exit_hint") or "").strip(), 120)
        return (model_spec, lifecycle, step_count, last_completed_at, last_exit_hint)

    def _advisory_digest_signature(
        self,
        findings: list[SharedFindingRef],
    ) -> tuple[tuple[str, str], ...]:
        artifact_paths = [
            artifact_path
            for artifact_path in self._extract_shared_artifact_paths(
                "\n".join(finding.content for finding in findings)
            )
            if self._is_shareable_artifact_path(artifact_path)
        ]
        signature: list[tuple[str, str]] = []
        for artifact_path in artifact_paths[:ADVISOR_ARTIFACT_PREVIEW_MAX_FILES]:
            _digest_path, revision, _digest_text = self._ensure_artifact_digest(artifact_path)
            if revision:
                signature.append((artifact_path, revision))
        return tuple(signature)

    def _lane_sibling_context(
        self,
        model_spec: str,
        findings: list[SharedFindingRef],
    ) -> tuple[str, list[str], int]:
        focus_paths: list[str] = []
        for finding in findings:
            if finding.model == model_spec:
                continue
            artifact_paths = [
                artifact_path
                for artifact_path in self._extract_shared_artifact_paths(
                    finding.prompt_text().strip(),
                    str(finding.summary or "").strip(),
                )
                if self._is_shareable_artifact_path(artifact_path)
            ]
            for artifact_path in artifact_paths:
                if artifact_path not in focus_paths:
                    focus_paths.append(artifact_path)

        artifact_focused = bool(focus_paths)
        sibling_lines: list[str] = []
        sibling_count = 0
        for finding in findings:
            if finding.model == model_spec:
                continue
            prompt = finding.prompt_text().strip()
            summary = str(finding.summary or "").strip()
            if not prompt and not summary:
                continue
            sibling_count += 1

            artifact_paths = [
                artifact_path
                for artifact_path in self._extract_shared_artifact_paths(prompt, summary)
                if self._is_shareable_artifact_path(artifact_path)
            ]
            if artifact_focused:
                compact_summary = self._compact_summary(summary or prompt, limit=96)
                if artifact_paths:
                    artifact_name = Path(artifact_paths[0]).name or artifact_paths[0]
                    line = f"[{finding.model}] {artifact_name}"
                    if compact_summary:
                        line = f"{line} | {compact_summary}"
                else:
                    line = f"[{finding.model}] {compact_summary or 'related sibling note'}"
                sibling_lines.append(self._truncate_text(line, 180))
                continue

            sibling_lines.append(f"[{finding.model}] {prompt or summary}")

        if focus_paths:
            sibling_lines = sibling_lines[-ADVISOR_ARTIFACT_FOCUSED_SIBLING_MAX_ITEMS:]
            limit = ADVISOR_ARTIFACT_FOCUSED_SIBLING_MAX_CHARS
        else:
            sibling_lines = sibling_lines[-ADVISOR_SIBLING_INSIGHTS_MAX_ITEMS:]
            limit = ADVISOR_SIBLING_INSIGHTS_MAX_CHARS

        sibling_text = self._clip_text_block("\n".join(sibling_lines), limit=limit)
        return sibling_text, focus_paths, sibling_count

    async def _lane_advisory_trigger_signature(self) -> tuple[object, ...]:
        findings = await self.message_bus.snapshot_findings()
        posts = _int_from_object(self.message_bus.stats_snapshot().get("total_posts", 0))
        runtime_markers: list[tuple[str, str, int, int, str]] = []
        for model_spec in self.model_specs:
            solver = self.solvers.get(model_spec)
            if not solver or model_spec in self.agent_results:
                continue
            runtime = solver.get_runtime_status()
            lifecycle = str(runtime.get("lifecycle") or "")
            if lifecycle not in {"idle", "error"}:
                continue
            runtime_markers.append(self._lane_advisory_runtime_marker(model_spec, runtime))
        return (
            posts,
            tuple(runtime_markers),
            self._manifest_signature(),
            self._advisory_digest_signature(findings),
        )

    async def _maybe_issue_lane_advisories(self) -> None:
        findings = await self.message_bus.snapshot_findings()
        if len(findings) < 2:
            return

        base_manifest_excerpt = self._manifest_excerpt()
        if not base_manifest_excerpt and len(findings) < 2:
            return

        for model_spec in self.model_specs:
            solver = self.solvers.get(model_spec)
            if not solver or model_spec in self.agent_results:
                continue

            runtime = solver.get_runtime_status()
            lifecycle = str(runtime.get("lifecycle") or "")
            if lifecycle not in {"idle", "error"}:
                continue

            sibling_text, focus_paths, sibling_count = self._lane_sibling_context(model_spec, findings)
            if not sibling_text:
                continue
            if not base_manifest_excerpt and sibling_count < 2:
                continue

            lane_state = self._clip_text_block(
                self._lane_advisory_state(model_spec, runtime),
                limit=ADVISOR_LANE_STATE_MAX_CHARS,
            )
            manifest_excerpt = (
                self._focused_manifest_excerpt(focus_paths)
                if focus_paths
                else base_manifest_excerpt
            )
            artifact_finding_excerpt = self._artifact_finding_excerpt_from_paths(focus_paths)
            artifact_previews = (
                self._advisor_artifact_previews(artifact_finding_excerpt, manifest_excerpt)
                if focus_paths
                else ""
            )
            fingerprint_payload = "\n".join(
                [
                    lane_state,
                    sibling_text,
                    manifest_excerpt,
                    artifact_previews,
                ]
            )
            fingerprint = hashlib.sha256(
                fingerprint_payload.encode("utf-8", errors="replace")
            ).hexdigest()
            if self._lane_advisory_fingerprints.get(model_spec) == fingerprint:
                continue

            advice = await self._run_advisor_call(
                model_spec,
                timeout_seconds=ADVISOR_LANE_HINT_TIMEOUT_SECONDS,
                operation_label="lane hint",
                call=lambda advisor, model_spec=model_spec, lane_state=lane_state, sibling_text=sibling_text, manifest_excerpt=manifest_excerpt, artifact_previews=artifact_previews: advisor.suggest_lane_hint(
                    target_model=model_spec,
                    challenge_brief=self._advisor_challenge_brief(),
                    lane_state=lane_state,
                    sibling_findings=sibling_text,
                    manifest_excerpt=manifest_excerpt,
                    artifact_previews=artifact_previews,
                ),
            )
            advice = (advice or "").strip()
            if not advice:
                continue

            self._lane_advisory_fingerprints[model_spec] = fingerprint
            self.lane_advisor_notes[model_spec] = advice
            self.last_advisor_note = advice
            self.advisor_lane_hint_count += 1
            advisory_msg = f"Private advisor note for this lane:\n{advice}"
            advisory_bump = getattr(solver, "bump_advisory", None)
            if callable(advisory_bump):
                advisory_bump(advisory_msg)
            else:
                solver.bump(advisory_msg)

    async def _monitor_lane_advisories(self) -> None:
        last_trigger_signature: tuple[object, ...] | None = None
        while not self.cancel_event.is_set():
            await asyncio.sleep(ADVISOR_LISTENER_INTERVAL_SECONDS)
            trigger_signature = await self._lane_advisory_trigger_signature()
            if trigger_signature == last_trigger_signature:
                continue
            last_trigger_signature = trigger_signature
            await self._maybe_issue_lane_advisories()

    # Escalating cooldowns after incorrect submissions (per model)
    SUBMISSION_COOLDOWNS = [0, 30, 120, 300, 600]  # 0s, 30s, 2min, 5min, 10min

    async def try_submit_flag(self, flag: str, model_spec: str) -> tuple[str, bool]:
        """Cooldown-gated, deduplicated flag submission. Returns (display, is_confirmed)."""
        async with self._flag_lock:
            if self.confirmed_flag:
                return f"ALREADY SOLVED — flag already confirmed: {self.confirmed_flag}", True

            normalized = flag.strip()

            # Dedup exact flags across all models
            if normalized in self._submitted_flags:
                return "INCORRECT — already tried this exact flag.", False

            # Escalating cooldown after incorrect submissions
            wrong_count = self._submit_count.get(model_spec, 0)
            cooldown_idx = min(wrong_count, len(self.SUBMISSION_COOLDOWNS) - 1)
            cooldown = self.SUBMISSION_COOLDOWNS[cooldown_idx]
            if cooldown > 0:
                last_time = self._last_submit_time.get(model_spec, 0)
                elapsed = time.monotonic() - last_time
                if elapsed < cooldown:
                    remaining = int(cooldown - elapsed)
                    return (
                        f"COOLDOWN — wait {remaining}s before submitting again. "
                        f"You have {wrong_count} incorrect submissions. "
                        "Use this time to do deeper analysis and verify your flag.",
                        False,
                    )

            self._submitted_flags.add(normalized)

            from backend.tools.core import do_submit_flag
            display, is_confirmed = await do_submit_flag(self.ctfd, self.meta.name, flag)
            if is_confirmed:
                self.confirmed_flag = normalized
            else:
                self._submit_count[model_spec] = wrong_count + 1
                self._last_submit_time[model_spec] = time.monotonic()
            return display, is_confirmed

    def _handoff_log_path(self, model_spec: str) -> Path:
        safe = self._safe_model_token(model_spec)
        path = Path(self.challenge_dir) / "solve" / "lanes"
        path.mkdir(parents=True, exist_ok=True)
        return path / f"{safe}.handoff.jsonl"

    def _resume_file_path(self, model_spec: str) -> Path:
        safe = self._safe_model_token(model_spec)
        return self.shared_artifacts_dir / f"lane-resume-{safe}.md"

    @staticmethod
    def _safe_model_token(model_spec: str) -> str:
        return "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in model_spec)

    def _collect_handoff_entry(
        self,
        model_spec: str,
        solver: SolverProtocol,
        result: SolverResult,
        *,
        restart_reason: str = "",
        restart_count: int = 0,
    ) -> dict[str, object]:
        runtime_getter = getattr(solver, "get_runtime_status", None)
        runtime = runtime_getter() if callable(runtime_getter) else {
            "step_count": result.step_count,
            "last_command": "",
            "current_command": "",
            "last_exit_hint": result.findings_summary,
        }
        return {
            "saved_at": datetime.now(UTC).isoformat(),
            "challenge_name": self.meta.name,
            "model_spec": model_spec,
            "status": result.status,
            "step_count": int(runtime.get("step_count", 0) or 0),
            "last_command": str(runtime.get("last_command") or runtime.get("current_command") or ""),
            "last_exit_hint": str(runtime.get("last_exit_hint") or ""),
            "findings_summary": result.findings_summary[:1000],
            "shared_artifacts_path": str(self.shared_artifacts_dir.resolve()),
            "log_path": result.log_path,
            "restart_reason": restart_reason,
            "restart_count": restart_count,
        }

    def _append_handoff_entry(self, model_spec: str, entry: dict[str, object]) -> Path:
        path = self._handoff_log_path(model_spec)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=True) + "\n")
        return path

    def _recent_handoff_entries(self, model_spec: str, limit: int = 4) -> list[dict[str, object]]:
        path = self._handoff_log_path(model_spec)
        if not path.exists():
            return []
        entries: list[dict[str, object]] = []
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                entries.append(payload)
        return entries[-limit:]

    def _write_resume_file(self, model_spec: str, latest_entry: dict[str, object]) -> Path:
        resume_path = self._resume_file_path(model_spec)
        manifest_container_path = f"{SHARED_ARTIFACTS_CONTAINER_ROOT}/manifest.md"
        recent_entries = self._recent_handoff_entries(model_spec, limit=4)

        repeated_commands: list[str] = []
        repeated_notes: list[str] = []
        findings: list[str] = []
        for entry in reversed(recent_entries):
            command = str(entry.get("last_command") or "").strip()
            if command and command not in repeated_commands:
                repeated_commands.append(command)
            note = str(entry.get("last_exit_hint") or "").strip()
            if note and note not in repeated_notes:
                repeated_notes.append(note)
            finding = str(entry.get("findings_summary") or "").strip()
            if finding and finding not in findings:
                findings.append(finding)

        restart_reason = str(latest_entry.get("restart_reason") or "").strip() or "- none recorded"
        shared_artifacts_path = str(latest_entry.get("shared_artifacts_path") or "").strip() or "-"

        command_lines = "\n".join(f"- {command}" for command in repeated_commands[:4]) or "- none captured"
        note_lines = "\n".join(f"- {note}" for note in repeated_notes[:4]) or "- none captured"
        finding_lines = "\n".join(f"- {finding}" for finding in findings[:4]) or "- none captured"

        content = "\n".join(
            [
                f"# Lane Resume: {self.meta.name} / {model_spec}",
                "",
                "Use this file to continue from the same sandbox/workspace after a lane restart.",
                "Read this summary first, then choose a different approach. Do not repeat the same dead-end.",
                "",
                "## Shared Artifact Manifest",
                f"- Read {manifest_container_path} before broad exploration if it exists.",
                f"- If manifest entries include digest paths under {SHARED_ARTIFACTS_CONTAINER_ROOT}/{ADVISOR_DIGEST_DIRNAME}/, read the digest before opening the raw artifact.",
                "- Treat manifest entries as evidence only; choose strategy independently.",
                "",
                "## Latest Restart Reason",
                restart_reason,
                "",
                "## Recent Commands To Avoid Repeating Blindly",
                command_lines,
                "",
                "## Recent Failure Notes",
                note_lines,
                "",
                "## Recent Findings",
                finding_lines,
                "",
                "## Shared Artifacts Root",
                shared_artifacts_path,
                "",
                "## Next-Step Guidance",
                "- Continue from the same sandbox/workspace; do not restart from scratch.",
                "- If a command may print more than about 100 lines, redirect it to /challenge/shared-artifacts/<name>.txt first. Large saved output may return only a path, so inspect a targeted range next.",
                "- Prefer narrower follow-up commands over repeating broad grep/find/strings output.",
                "- Try a different path from the failed one above.",
                "",
            ]
        )
        resume_path.write_text(content, encoding="utf-8")
        return resume_path

    def _latest_restart_packet(self, entry: dict[str, object], resume_path: Path) -> str:
        last_command = str(entry.get("last_command") or "").strip()
        last_exit_hint = str(entry.get("last_exit_hint") or "").strip()
        findings = str(entry.get("findings_summary") or "").strip()
        shared_artifacts_path = str(entry.get("shared_artifacts_path") or "").strip()
        resume_container_path = f"{SHARED_ARTIFACTS_CONTAINER_ROOT}/{resume_path.name}"
        manifest_container_path = f"{SHARED_ARTIFACTS_CONTAINER_ROOT}/manifest.md"

        parts = [
            "Previous lane job stalled in a dead-end. Continue from the same sandbox/workspace, but do not repeat the same approach.",
            f"First, read this resume file and use it as your working context: {resume_container_path}",
            f"Also read {manifest_container_path} first if it exists. Treat manifest entries as evidence only and choose strategy independently.",
            f"If manifest entries include digest paths under {SHARED_ARTIFACTS_CONTAINER_ROOT}/{ADVISOR_DIGEST_DIRNAME}/, read the digest before opening the raw artifact.",
            "",
            f"Last command: {last_command or '-'}",
            f"Last note: {last_exit_hint or '-'}",
            f"Findings summary: {findings or '-'}",
            f"Shared artifacts root: {shared_artifacts_path or '-'}",
            "",
            "Recovery instructions:",
            "- Do not repeat the same command or the same dead-end.",
            "- If a command may print more than about 100 lines, redirect it to /challenge/shared-artifacts/<name>.txt first, then inspect only a targeted range with sed/head/tail/rg.",
            "- Prefer narrower follow-up commands over broad grep/find/strings output in the terminal.",
        ]
        return "\n".join(parts)

    def _fingerprint_text(self, value: str) -> str:
        text = value.strip()
        if not text:
            return ""
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()

    @staticmethod
    def _is_context_refresh_reason(reason: str) -> bool:
        return reason.startswith("context refresh after ")

    def _maybe_reset_restart_budget(
        self,
        model_spec: str,
        state: LaneRestartState,
        total_steps: int,
    ) -> None:
        if (
            state.restart_count > 0
            and total_steps - state.restart_budget_baseline_step >= RESTART_BUDGET_RESET_STEP_DELTA
        ):
            state.restart_count = 0
            self._lane_restart_notes.pop(model_spec, None)

    def _compute_restart_reason(self, model_spec: str, entry: dict[str, object]) -> str:
        state = self._lane_restart_state.setdefault(model_spec, LaneRestartState())
        total_steps = _int_from_object(entry.get("step_count", 0))
        status = str(entry.get("status") or "")
        last_command = str(entry.get("last_command") or "")
        last_exit_hint = str(entry.get("last_exit_hint") or "")
        findings_summary = str(entry.get("findings_summary") or "")
        dead_end_fingerprint = self._fingerprint_text(f"{last_command}\n{last_exit_hint}")

        progressed = state.last_total_steps >= 0 and total_steps > state.last_total_steps
        no_step_growth = state.last_total_steps >= 0 and total_steps <= state.last_total_steps
        same_dead_end = bool(dead_end_fingerprint and dead_end_fingerprint == state.last_dead_end_fingerprint)

        self._maybe_reset_restart_budget(model_spec, state, total_steps)

        state.last_total_steps = total_steps
        state.last_dead_end_fingerprint = dead_end_fingerprint
        state.last_trace_fingerprint = ""

        if (
            status in (GAVE_UP, ERROR)
            and total_steps >= PROACTIVE_CONTEXT_REFRESH_MIN_STEPS
            and total_steps - state.last_context_refresh_step >= PROACTIVE_CONTEXT_REFRESH_STEP_INTERVAL
        ):
            clue = last_command or last_exit_hint or findings_summary or "high-step lane"
            return f"context refresh after {total_steps} total steps: {clue[:120]}"

        if progressed:
            self._lane_restart_notes.pop(model_spec, None)
            return ""
        if no_step_growth and same_dead_end:
            clue = last_command or last_exit_hint or "no-progress dead-end"
            return f"stalled after repeated dead-end with no new steps: {clue[:120]}"
        return ""

    @staticmethod
    def _is_in_turn_stall(result: SolverResult) -> bool:
        return result.status == ERROR and result.findings_summary.startswith("stalled:")

    @staticmethod
    def _set_solver_stop_reason(solver: SolverProtocol | None, reason: str) -> None:
        if solver is None:
            return
        setter = getattr(solver, "set_stop_reason", None)
        if callable(setter):
            setter(reason)

    def _set_all_solver_stop_reasons(
        self,
        reason: str,
        *,
        exclude: set[str] | None = None,
    ) -> None:
        excluded = exclude or set()
        for model_spec, solver in self.solvers.items():
            if model_spec in excluded:
                continue
            self._set_solver_stop_reason(solver, reason)

    async def _stop_solver_processes(
        self,
        *,
        exclude: set[str] | None = None,
    ) -> None:
        excluded = exclude or set()
        stop_tasks: list[asyncio.Task[object]] = []
        for model_spec, solver in self.solvers.items():
            if model_spec in excluded:
                continue
            stopper = getattr(solver, "stop_process", None)
            if not callable(stopper):
                continue
            stop_tasks.append(
                asyncio.create_task(
                    stopper(),
                    name=f"stop-process-{self.meta.name}-{model_spec}",
                )
            )
        if not stop_tasks:
            return
        results = await asyncio.gather(*stop_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.debug(
                    "[%s] solver stop_process raised during winner shutdown",
                    self.meta.name,
                    exc_info=result,
                )

    async def _maybe_restart_stalled_lane(
        self,
        model_spec: str,
        solver: SolverProtocol,
        result: SolverResult,
    ) -> SolverProtocol | None:
        restart_reason = ""
        transient_stall = False
        preview_entry = self._collect_handoff_entry(model_spec, solver, result)
        state = self._lane_restart_state.setdefault(model_spec, LaneRestartState())
        if self._is_in_turn_stall(result):
            restart_reason = result.findings_summary[:200]
            current_steps = _int_from_object(preview_entry.get("step_count", 0))
            self._maybe_reset_restart_budget(model_spec, state, current_steps)
            if current_steps > state.last_total_steps:
                transient_stall = True
                state.last_total_steps = current_steps
        elif result.status in (GAVE_UP, ERROR):
            restart_reason = self._compute_restart_reason(model_spec, preview_entry)

        is_context_refresh = self._is_context_refresh_reason(restart_reason)
        if restart_reason:
            if not is_context_refresh and not transient_stall:
                state.restart_count += 1
            self._lane_restart_notes[model_spec] = restart_reason
        entry = self._collect_handoff_entry(
            model_spec,
            solver,
            result,
            restart_reason=restart_reason,
            restart_count=state.restart_count,
        )
        self._append_handoff_entry(model_spec, entry)
        resume_path = self._write_resume_file(model_spec, entry)

        if not restart_reason:
            return None
        if not is_context_refresh and state.restart_count > MAX_LOCAL_RESTARTS:
            self._lane_restart_notes[model_spec] = (
                f"{restart_reason} (restart budget exhausted)"
            )
            logger.warning(
                "[%s/%s] Local restart budget exhausted after %d attempts",
                self.meta.name,
                model_spec,
                state.restart_count - 1,
            )
            return None

        restart_packet = self._latest_restart_packet(entry, resume_path)
        old_sandbox = solver.sandbox
        self._set_solver_stop_reason(solver, f"lane restart: {restart_reason}")
        await solver.stop_process()
        replacement = self._create_solver(
            model_spec,
            sandbox=old_sandbox,
            initial_step_count=_int_from_object(entry.get("step_count", 0)),
        )
        replacement.bump(restart_packet)
        self.solvers[model_spec] = replacement
        await replacement.start()
        if is_context_refresh:
            state.last_context_refresh_step = _int_from_object(entry.get("step_count", 0))
        state.restart_budget_baseline_step = _int_from_object(entry.get("step_count", 0))
        logger.info(
            "[%s/%s] Restarted stalled lane in-place (%d/%d)",
            self.meta.name,
            model_spec,
            state.restart_count,
            MAX_LOCAL_RESTARTS,
        )
        return replacement

    async def _run_solver(self, model_spec: str) -> SolverResult | None:
        solver = self._create_solver(model_spec)
        self.solvers[model_spec] = solver

        try:
            result, final_solver = await self._run_solver_loop(solver, model_spec)
            solver = final_solver
            if result.status == FLAG_FOUND:
                await self._persist_solved_artifacts(
                    model_spec=model_spec,
                    solver=solver,
                    result=result,
                )
            self.agent_results[model_spec] = result
            solver.mark_terminal_status(result.status)
            return result
        except Exception as e:
            logger.error(f"[{self.meta.name}/{model_spec}] Fatal: {e}", exc_info=True)
            solver.mark_terminal_status(ERROR)
            self.agent_results[model_spec] = SolverResult(
                flag=None,
                status=ERROR,
                findings_summary=f"Fatal: {e}",
                step_count=0,
                cost_usd=0.0,
                log_path="",
            )
            return None
        finally:
            latest_solver = self.solvers.get(model_spec, solver)
            stop_task = asyncio.create_task(latest_solver.stop(), name=f"stop-{self.meta.name}-{model_spec}")
            try:
                await asyncio.shield(stop_task)
            except asyncio.CancelledError:
                await asyncio.gather(stop_task, return_exceptions=True)
                raise
            except Exception:
                logger.debug(
                    "[%s/%s] solver stop raised during shutdown",
                    self.meta.name,
                    model_spec,
                    exc_info=True,
                )

    async def _run_solver_loop(self, solver, model_spec: str) -> tuple[SolverResult, SolverProtocol]:
        """Observe the in-sandbox runtime until it emits a terminal result."""
        result = SolverResult(
            flag=None, status=CANCELLED, findings_summary="",
            step_count=0, cost_usd=0.0, log_path="",
        )
        await solver.start()

        while not self.cancel_event.is_set():
            result = await solver.run_until_done_or_gave_up()

            # Only broadcast useful findings — skip errors and broken solvers
            if (result.status not in (ERROR, QUOTA_ERROR)
                    and not (result.step_count == 0 and result.cost_usd == 0)
                    and result.findings_summary
                    and not result.findings_summary.startswith(("Error:", "Turn failed:"))):
                finding_event = self._make_finding_event(
                    model_spec=model_spec,
                    prefix=f"finding-{self.meta.name}-{model_spec}",
                    content=result.findings_summary,
                )
                self._record_shared_finding(model_spec, finding_event)
                await self.message_bus.post(model_spec, finding_event)

            await self._maybe_share_artifact_finding(model_spec, solver, result)

            if result.status == FLAG_CANDIDATE:
                continue

            if result.status == FLAG_FOUND:
                self._set_all_solver_stop_reasons(
                    f"flag found by {model_spec}",
                    exclude={model_spec},
                )
                await self._stop_solver_processes(exclude={model_spec})
                self.cancel_event.set()
                self.winner = result
                self.winner_model_spec = model_spec
                self.winner_confirmation_source = self.winner_confirmation_source or "ctfd"
                logger.info(
                    f"[{self.meta.name}] Flag found by {model_spec}: {result.flag}"
                )
                return result, solver

            if result.status == CANCELLED:
                break

            runtime_lifecycle = str(solver.get_runtime_status().get("lifecycle") or "")
            runtime_finished = runtime_lifecycle in {"won", "finished", "cancelled", "quota_error", "error"}

            if result.status == QUOTA_ERROR:
                logger.warning(
                    f"[{self.meta.name}/{model_spec}] Quota exhausted — stopping lane"
                )
                break

            if result.status in (GAVE_UP, ERROR) and runtime_finished:
                replacement = await self._maybe_restart_stalled_lane(model_spec, solver, result)
                if replacement is not None:
                    solver = replacement
                    continue
                if result.status == ERROR:
                    logger.warning(
                        f"[{self.meta.name}/{model_spec}] Runtime finished in error: {result.findings_summary}"
                    )
                break

        if self.cancel_event.is_set() and result.status != FLAG_FOUND:
            result = SolverResult(
                flag=result.flag,
                status=CANCELLED,
                findings_summary=result.findings_summary,
                step_count=result.step_count,
                cost_usd=result.cost_usd,
                log_path=result.log_path,
            )

        return result, solver

    async def run(self) -> SolverResult | None:
        """Run all solvers in parallel. Returns the winner's result or None."""
        await self._resume_pending_candidate_reviews()
        tasks = [
            asyncio.create_task(self._run_solver(spec), name=f"solver-{spec}")
            for spec in self.model_specs
        ]
        artifact_monitor = asyncio.create_task(
            self._monitor_live_artifact_sharing(),
            name=f"artifact-share-{self.meta.name}",
        )
        advisory_monitor = asyncio.create_task(
            self._monitor_lane_advisories(),
            name=f"lane-advice-{self.meta.name}",
        )

        try:
            while tasks:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

                for task in done:
                    try:
                        result = task.result()
                    except Exception:
                        continue
                    if result and result.status == FLAG_FOUND:
                        self.cancel_event.set()
                        for p in pending:
                            p.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        return result

                tasks = list(pending)

            self.cancel_event.set()
            return self.winner
        except Exception as e:
            logger.error(f"[{self.meta.name}] Swarm error: {e}", exc_info=True)
            self._set_all_solver_stop_reasons(
                f"swarm error: {type(e).__name__}: {e}",
            )
            self.cancel_event.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            return None
        finally:
            artifact_monitor.cancel()
            advisory_monitor.cancel()
            await asyncio.gather(artifact_monitor, advisory_monitor, return_exceptions=True)

    def kill(self, reason: str = "swarm cancelled") -> None:
        """Cancel all agents for this challenge."""
        self._set_all_solver_stop_reasons(reason)
        self.cancel_event.set()
        for task in list(self._background_tasks):
            task.cancel()

    def get_status(self) -> dict:
        """Get per-agent progress and findings."""
        agents: dict[str, dict[str, object]] = {}
        for spec in self.model_specs:
            final = self.agent_results.get(spec)
            solver = self.solvers.get(spec)
            runtime = solver.get_runtime_status() if solver else {
                "lifecycle": "pending",
                "current_tool": "",
                "current_command": "",
                "current_started_at": None,
                "last_tool": "",
                "last_command": "",
                "last_completed_at": None,
                "last_exit_hint": "",
            }
            findings = self.findings.get(spec, "")
            if not findings and final:
                findings = final.findings_summary

            status = "pending"
            if final:
                status = final.status
            elif solver:
                status = "running"

            agents[spec] = {
                **runtime,
                "findings": findings,
                "advisor_note": self.lane_advisor_notes.get(spec, ""),
                "status": status,
                "last_exit_hint": runtime.get("last_exit_hint")
                or self._lane_restart_notes.get(spec, ""),
            }

        return {
            "challenge": self.meta.name,
            "cancelled": self.cancel_event.is_set(),
            "winner": self.winner.flag if self.winner else None,
            "winner_model": self.winner_model_spec,
            "advisor_note": self.last_advisor_note,
            "coordinator_advisor_note": self.last_coordinator_advisor_note,
            "shared_finding": self.last_shared_finding,
            "shared_findings": {
                model_spec: finding.snapshot()
                for model_spec, finding in sorted(self.shared_finding_events.items())
            },
            "signals": {
                **self.message_bus.stats_snapshot(),
                "coordinator_messages": self.coordinator_message_count,
                "advisor_lane_hints": self.advisor_lane_hint_count,
                "advisor_coordinator_appends": self.advisor_coordinator_count,
            },
            "solve": dict(self.saved_solve_artifacts),
            "flag_candidates": {
                flag: record.snapshot()
                for flag, record in sorted(self.flag_candidates.items())
            },
            "agents": agents,
        }
