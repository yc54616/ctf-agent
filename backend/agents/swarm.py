"""ChallengeSwarm — Parallel solvers racing on one challenge."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from backend.agents.advisor_base import AdvisorProtocol, NoopAdvisor
from backend.cost_tracker import CostTracker
from backend.ctfd import CTFdClient
from backend.message_bus import ChallengeMessageBus
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
    FLAG_FOUND,
    GAVE_UP,
    QUOTA_ERROR,
    SolverProtocol,
    SolverResult,
)

if TYPE_CHECKING:
    from backend.config import Settings

logger = logging.getLogger(__name__)


FINDING_ARTIFACT_THRESHOLD_CHARS = 500
COORDINATOR_ARTIFACT_THRESHOLD_CHARS = 500
ARTIFACT_PREVIEW_CHARS = 500
MAX_LOCAL_RESTARTS = 5
RESTART_BUDGET_RESET_STEP_DELTA = 10
MANIFEST_ENTRY_LIMIT = 8
ADVISOR_LISTENER_INTERVAL_SECONDS = 2.0
ADVISOR_COORDINATOR_TIMEOUT_SECONDS = 8.0
ADVISOR_LANE_HINT_TIMEOUT_SECONDS = 30.0
ADVISOR_ARTIFACT_PREVIEW_MAX_FILES = 3
ADVISOR_ARTIFACT_PREVIEW_BYTES = 2048
ADVISOR_ARTIFACT_ESCALATED_MAX_FILES = 1
ADVISOR_ARTIFACT_ESCALATED_HEAD_BYTES = 8192
ADVISOR_ARTIFACT_ESCALATED_TAIL_BYTES = 4096
ADVISOR_ARTIFACT_FINDING_LIMIT = 4
ADVISOR_DIGEST_DIRNAME = ".advisor"
ADVISOR_DIGEST_SAMPLE_BYTES = 2048
ADVISOR_DIGEST_EXPANDED_HEAD_BYTES = 8192
ADVISOR_DIGEST_EXPANDED_TAIL_BYTES = 4096
ADVISOR_DIGEST_MAX_HITS = 10
ADVISOR_DIGEST_MAX_ITEMS = 8
PROACTIVE_CONTEXT_REFRESH_MIN_STEPS = 180
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
IGNORED_ARTIFACT_PREFIXES = ("stdout-", "stderr-", "lane-resume-")


@dataclass
class LaneRestartState:
    last_total_steps: int = -1
    last_dead_end_fingerprint: str = ""
    last_trace_fingerprint: str = ""
    restart_count: int = 0
    last_context_refresh_step: int = 0
    restart_budget_baseline_step: int = 0


@dataclass
class ChallengeSwarm:
    """Parallel solvers racing on one challenge."""

    challenge_dir: str
    meta: ChallengeMeta
    ctfd: CTFdClient
    cost_tracker: CostTracker
    settings: Settings
    model_specs: list[str] = field(default_factory=lambda: list(DEFAULT_MODELS))
    no_submit: bool = False
    coordinator_inbox: asyncio.Queue | None = None

    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    solvers: dict[str, SolverProtocol] = field(default_factory=dict)
    agent_results: dict[str, SolverResult] = field(default_factory=dict)
    findings: dict[str, str] = field(default_factory=dict)
    winner: SolverResult | None = None
    confirmed_flag: str | None = None
    _flag_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _submit_count: dict[str, int] = field(default_factory=dict)  # per-model wrong submission count
    _submitted_flags: set[str] = field(default_factory=set)  # dedup exact flags
    _last_submit_time: dict[str, float] = field(default_factory=dict)  # per-model last submit timestamp
    message_bus: ChallengeMessageBus = field(default_factory=ChallengeMessageBus)
    shared_artifacts_dir: Path = field(init=False)
    winner_model_spec: str | None = None
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
    _artifact_digest_cache: dict[str, tuple[str, str]] = field(default_factory=dict, init=False, repr=False)
    _lane_seen_digest_revisions: dict[str, dict[str, str]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.shared_artifacts_dir = resolve_shared_artifacts_dir(self.challenge_dir)

    def _persist_shared_text(self, prefix: str, content: str, suffix: str = ".txt") -> str:
        pointer = allocate_artifact_pointer(
            self.shared_artifacts_dir,
            SHARED_ARTIFACTS_CONTAINER_ROOT,
            prefix,
            suffix,
        )
        assert pointer.host_path is not None
        Path(pointer.host_path).write_text(content, encoding="utf-8")
        pointer.size_bytes = len(content.encode("utf-8"))
        preview = content[:ARTIFACT_PREVIEW_CHARS].strip()
        preview_block = preview or "(empty preview)"
        return (
            f"[artifact] {pointer.container_path} ({pointer.size_bytes} bytes)\n"
            f"[preview]\n{preview_block}"
        )

    def _manifest_file_path(self) -> Path:
        return self.shared_artifacts_dir / "manifest.md"

    def _advisor_digest_dir(self) -> Path:
        path = self.shared_artifacts_dir / ADVISOR_DIGEST_DIRNAME
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _shareable_text(self, prefix: str, content: str, *, threshold: int) -> str:
        text = content.strip()
        if not text:
            return text
        if len(text) <= threshold:
            return text
        return self._persist_shared_text(prefix, text)

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
        name = Path(artifact_path).name
        if name in IGNORED_ARTIFACT_BASENAMES:
            return False
        return not any(name.startswith(prefix) for prefix in IGNORED_ARTIFACT_PREFIXES)

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
            if lower.startswith(NON_FACTUAL_PREFIXES):
                continue
            if any(token in lower for token in NON_FACTUAL_SUBSTRINGS):
                continue
            if any(
                lower.startswith(prefix)
                for prefix in ("message sent", "no new findings", "yolo mode", "tool failed:")
            ):
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

        try:
            with host_path.open("r", encoding="utf-8", errors="replace") as fh:
                for lineno, raw_line in enumerate(fh, start=1):
                    line = raw_line.strip()
                    if not line:
                        continue
                    lowered = line.lower()
                    if len(signal_hits) < ADVISOR_DIGEST_MAX_HITS and any(term in lowered for term in ADVISOR_SIGNAL_TERMS):
                        signal_hits.append(f"L{lineno}: {self._truncate_text(line, 180)}")
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
        except OSError:
            pass

        return {
            "mode": ["text-scan-v1"],
            "head": [self._truncate_text(self._decode_artifact_preview(head), 900)] if head else [],
            "middle": [self._truncate_text(self._decode_artifact_preview(middle), 500)] if middle else [],
            "tail": [self._truncate_text(self._decode_artifact_preview(tail), 500)] if tail else [],
            "signal_hits": self._truncate_lines(signal_hits),
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
            try:
                return digest_container_path, cached[1], digest_host_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass

        digest_text = self._build_artifact_digest(artifact_path, host_path)
        revision = hashlib.sha256(digest_text.encode("utf-8", errors="replace")).hexdigest()
        digest_host_path.write_text(digest_text, encoding="utf-8")
        self._artifact_digest_cache[artifact_path] = (signature, revision)
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
        shared_finding = f"Artifact path: {artifact_path}"
        self.findings[model_spec] = shared_finding
        self.last_shared_finding = shared_finding
        digest_path, _revision, _digest_text = self._ensure_artifact_digest(artifact_path)
        self._record_artifact_manifest_entry(
            model_spec=model_spec,
            fact_summary=fact_summary,
            artifact_path=artifact_path,
            digest_path=digest_path,
        )
        await self.message_bus.post(model_spec, shared_finding)
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
            result_payload = {
                "challenge_name": self.meta.name,
                "status": result.status,
                "flag": result.flag,
                "step_count": result.step_count,
                "winner_model": model_spec,
                "findings_summary": result.findings_summary,
                "advisor_note": self.last_advisor_note,
                "trace_path": trace_path,
                "workspace_path": workspace_path,
                "shared_artifacts_path": str(self.shared_artifacts_dir.resolve()),
                "saved_at": saved_at,
            }

            result_path = solve_dir / "result.json"
            result_path.write_text(json.dumps(result_payload, indent=2), encoding="utf-8")

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

        def _submit_fn(flag): return self.try_submit_flag(flag, model_spec)
        _notify = self._make_notify_fn(model_spec)

        if provider == "claude-sdk":
            raise ValueError(
                f"Claude solver lanes are disabled for {model_spec}. "
                "Use Claude as coordinator/advisor only."
            )

        if provider == "codex":
            from backend.agents.codex_solver import CodexSolver
            return CodexSolver(
                model_spec=model_spec,
                challenge_dir=self.challenge_dir,
                meta=self.meta,
                ctfd=self.ctfd,
                cost_tracker=self.cost_tracker,
                settings=self.settings,
                cancel_event=self.cancel_event,
                no_submit=self.no_submit,
                submit_fn=_submit_fn,
                message_bus=self.message_bus,
                notify_coordinator=_notify,
                sandbox=sandbox,
                initial_step_count=initial_step_count,
            )

        if provider in ("gemini", "google"):
            from backend.agents.gemini_solver import GeminiSolver
            return GeminiSolver(
                model_spec=model_spec,
                challenge_dir=self.challenge_dir,
                meta=self.meta,
                ctfd=self.ctfd,
                cost_tracker=self.cost_tracker,
                settings=self.settings,
                cancel_event=self.cancel_event,
                no_submit=self.no_submit,
                submit_fn=_submit_fn,
                message_bus=self.message_bus,
                notify_coordinator=_notify,
                sandbox=sandbox,
                initial_step_count=initial_step_count,
            )

        raise ValueError(f"Unsupported solver provider in model spec: {model_spec}")

    def _make_notify_fn(self, model_spec: str):
        """Create a callback that pushes solver messages to the coordinator inbox."""
        async def _notify(message: str) -> None:
            if self.coordinator_inbox:
                advised_message = await self._build_advised_coordinator_message(model_spec, message)
                shared_message = self._shareable_text(
                    f"coordinator-{self.meta.name}-{model_spec}",
                    advised_message,
                    threshold=COORDINATOR_ARTIFACT_THRESHOLD_CHARS,
                )
                self.coordinator_inbox.put_nowait(
                    f"[{self.meta.name}/{model_spec}] {shared_message}"
                )
                self.coordinator_message_count += 1
        return _notify

    @staticmethod
    def _advisor_backend_for_source(model_spec: str) -> str:
        if model_spec.startswith("codex/"):
            return "codex"
        return "claude"

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

        resolved: AdvisorProtocol = advisor or NoopAdvisor()
        self._advisors[backend] = resolved
        return resolved

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

    async def _build_advised_coordinator_message(self, model_spec: str, message: str) -> str:
        advisor = self._get_advisor(model_spec)
        try:
            advice = await asyncio.wait_for(
                advisor.annotate_coordinator_message(
                    source_model=model_spec,
                    challenge_brief=self._advisor_challenge_brief(),
                    message=message,
                    sibling_insights=self._gather_sibling_insights(model_spec),
                ),
                timeout=ADVISOR_COORDINATOR_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.debug("[%s/%s] %s coordinator advice skipped: %s", self.meta.name, model_spec, self._advisor_backend_for_source(model_spec), exc)
            return message

        advice = advice.strip()
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
        return "\n\n".join(parts) if parts else "No sibling insights available yet."

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
        path = self._manifest_file_path()
        if not path.exists():
            return ""
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) <= max_lines:
            return "\n".join(lines).strip()
        return "\n".join(lines[-max_lines:]).strip()

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

        try:
            raw = host_path.read_bytes()[:ADVISOR_ARTIFACT_PREVIEW_BYTES]
        except OSError:
            return ""
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
            head = host_path.read_bytes()[:ADVISOR_ARTIFACT_ESCALATED_HEAD_BYTES]
        except OSError:
            return ""
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

    def _artifact_digest_block(self, artifact_path: str) -> str:
        digest_path, revision, digest_text = self._ensure_artifact_digest(artifact_path)
        if not digest_text:
            return ""
        compact = digest_text.strip()
        if len(compact) > 2600:
            compact = compact[:2597].rstrip() + "..."
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
                if escalated_count < ADVISOR_ARTIFACT_ESCALATED_MAX_FILES:
                    base_body = block.split("\n", 2)[-1]
                    host_path = self._shared_artifact_host_path(artifact_path)
                    text_like = False
                    truncated = False
                    if host_path is not None and host_path.exists() and host_path.is_file():
                        try:
                            head = host_path.read_bytes()[:ADVISOR_ARTIFACT_PREVIEW_BYTES]
                            truncated = host_path.stat().st_size > ADVISOR_ARTIFACT_PREVIEW_BYTES
                            text_like = self._is_text_like_artifact(host_path, head)
                        except OSError:
                            text_like = False
                            truncated = False
                    repeated = path_counts.get(artifact_path, 0) >= 2
                    if text_like and (repeated or (truncated and self._artifact_preview_has_signal(base_body))):
                        expanded = self._artifact_preview_block_expanded(artifact_path)
                        if expanded:
                            block = expanded
                            escalated_count += 1
            previews.append(block)
            if len(previews) >= ADVISOR_ARTIFACT_PREVIEW_MAX_FILES:
                break
        return "\n\n---\n\n".join(previews)

    def _advisor_artifact_finding_excerpt(self, findings: list[str]) -> str:
        explicit_paths = [
            finding.strip()
            for finding in findings
            if "Artifact path:" in finding
        ]
        if not explicit_paths:
            return ""
        return "\n".join(explicit_paths[-ADVISOR_ARTIFACT_FINDING_LIMIT:])

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

    async def _maybe_issue_lane_advisories(self) -> None:
        findings = await self.message_bus.snapshot_findings()
        if len(findings) < 2:
            return

        manifest_excerpt = self._manifest_excerpt()
        if not manifest_excerpt and len(findings) < 2:
            return

        for model_spec in self.model_specs:
            solver = self.solvers.get(model_spec)
            if not solver or model_spec in self.agent_results:
                continue

            runtime = solver.get_runtime_status()
            lifecycle = str(runtime.get("lifecycle") or "")
            if lifecycle not in {"idle", "error"}:
                continue

            sibling_findings = [
                f"[{finding.model}] {finding.content}"
                for finding in findings
                if finding.model != model_spec
            ]
            if not sibling_findings:
                continue
            if not manifest_excerpt and len(sibling_findings) < 2:
                continue

            lane_state = self._lane_advisory_state(model_spec, runtime)
            sibling_text = "\n".join(sibling_findings[-8:])
            artifact_finding_excerpt = self._advisor_artifact_finding_excerpt(sibling_findings)
            artifact_previews = (
                self._advisor_artifact_previews(artifact_finding_excerpt, manifest_excerpt)
                if artifact_finding_excerpt
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

            advisor = self._get_advisor(model_spec)
            try:
                advice = await asyncio.wait_for(
                    advisor.suggest_lane_hint(
                        target_model=model_spec,
                        challenge_brief=self._advisor_challenge_brief(),
                        lane_state=lane_state,
                        sibling_findings=sibling_text,
                        manifest_excerpt=manifest_excerpt,
                        artifact_previews=artifact_previews,
                    ),
                    timeout=ADVISOR_LANE_HINT_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                logger.debug(
                    "[%s/%s] %s lane advice skipped: %s",
                    self.meta.name,
                    model_spec,
                    self._advisor_backend_for_source(model_spec),
                    exc,
                )
                continue

            advice = advice.strip()
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
        last_seen_posts = -1
        while not self.cancel_event.is_set():
            await asyncio.sleep(ADVISOR_LISTENER_INTERVAL_SECONDS)
            posts = int(self.message_bus.stats_snapshot().get("total_posts", 0))
            if posts == last_seen_posts:
                continue
            last_seen_posts = posts
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
        recent_trace_tail = self._recent_trace_commands(result.log_path, limit=8)
        return {
            "saved_at": datetime.now(UTC).isoformat(),
            "challenge_name": self.meta.name,
            "model_spec": model_spec,
            "status": result.status,
            "step_count": int(runtime.get("step_count", 0) or 0),
            "last_command": str(runtime.get("last_command") or runtime.get("current_command") or ""),
            "last_exit_hint": str(runtime.get("last_exit_hint") or ""),
            "findings_summary": result.findings_summary[:1000],
            "recent_trace_tail": recent_trace_tail,
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

        trace_tail = latest_entry.get("recent_trace_tail") or []
        trace_lines = "\n".join(f"- {line}" for line in trace_tail[:8]) or "- no recent trace tail captured"
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
                "## Recent Trace Tail",
                trace_lines,
                "",
                "## Next-Step Guidance",
                "- Continue from the same sandbox/workspace; do not restart from scratch.",
                "- If a command may print more than about 100 lines, redirect it to /challenge/shared-artifacts/<name>.txt first and only inspect a small preview.",
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
        trace_tail = entry.get("recent_trace_tail") or []
        trace_lines = "\n".join(str(line) for line in trace_tail[:8]) or "- no recent trace tail captured"
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
            "Recent trace tail:",
            trace_lines,
            "",
            "Recovery instructions:",
            "- Do not repeat the same command or the same dead-end.",
            "- If a command may print more than about 100 lines, redirect it to /challenge/shared-artifacts/<name>.txt first, then inspect only a small preview with sed/head/tail/rg.",
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

    def _compute_restart_reason(self, model_spec: str, entry: dict[str, object]) -> str:
        state = self._lane_restart_state.setdefault(model_spec, LaneRestartState())
        total_steps = int(entry.get("step_count", 0) or 0)
        status = str(entry.get("status") or "")
        last_command = str(entry.get("last_command") or "")
        last_exit_hint = str(entry.get("last_exit_hint") or "")
        findings_summary = str(entry.get("findings_summary") or "")
        recent_trace_tail = "\n".join(str(line) for line in entry.get("recent_trace_tail") or [])
        dead_end_fingerprint = self._fingerprint_text(f"{last_command}\n{last_exit_hint}")
        trace_fingerprint = self._fingerprint_text(recent_trace_tail)

        progressed = state.last_total_steps >= 0 and total_steps > state.last_total_steps
        no_step_growth = state.last_total_steps >= 0 and total_steps <= state.last_total_steps
        same_dead_end = bool(dead_end_fingerprint and dead_end_fingerprint == state.last_dead_end_fingerprint)
        same_trace = bool(trace_fingerprint and trace_fingerprint == state.last_trace_fingerprint)

        if (
            state.restart_count > 0
            and total_steps - state.restart_budget_baseline_step >= RESTART_BUDGET_RESET_STEP_DELTA
        ):
            state.restart_count = 0
            self._lane_restart_notes.pop(model_spec, None)

        state.last_total_steps = total_steps
        state.last_dead_end_fingerprint = dead_end_fingerprint
        state.last_trace_fingerprint = trace_fingerprint

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
        if no_step_growth and (same_dead_end or same_trace):
            clue = last_command or last_exit_hint or "no-progress dead-end"
            return f"stalled after repeated dead-end with no new steps: {clue[:120]}"
        return ""

    @staticmethod
    def _is_in_turn_stall(result: SolverResult) -> bool:
        return result.status == ERROR and result.findings_summary.startswith("stalled:")

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
            current_steps = int(preview_entry.get("step_count", 0) or 0)
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
        await solver.stop_process()
        replacement = self._create_solver(
            model_spec,
            sandbox=old_sandbox,
            initial_step_count=int(entry.get("step_count", 0) or 0),
        )
        replacement.bump(restart_packet)
        self.solvers[model_spec] = replacement
        await replacement.start()
        if is_context_refresh:
            state.last_context_refresh_step = int(entry.get("step_count", 0) or 0)
        state.restart_budget_baseline_step = int(entry.get("step_count", 0) or 0)
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
            await latest_solver.stop()

    async def _run_solver_loop(self, solver, model_spec: str) -> tuple[SolverResult, SolverProtocol]:
        """Inner loop: start → run → bump → run → ..."""
        bump_count = 0
        consecutive_errors = 0
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
                shared_finding = self._shareable_text(
                    f"finding-{self.meta.name}-{model_spec}",
                    result.findings_summary,
                    threshold=FINDING_ARTIFACT_THRESHOLD_CHARS,
                )
                self.findings[model_spec] = shared_finding
                self.last_shared_finding = shared_finding
                await self.message_bus.post(model_spec, shared_finding)

            await self._maybe_share_artifact_finding(model_spec, solver, result)

            replacement = await self._maybe_restart_stalled_lane(model_spec, solver, result)
            if replacement is not None:
                solver = replacement
                continue
            if self._is_in_turn_stall(result):
                logger.warning(
                    "[%s/%s] In-turn stall was not recovered locally; stopping lane",
                    self.meta.name,
                    model_spec,
                )
                break

            if result.status == FLAG_FOUND:
                self.cancel_event.set()
                self.winner = result
                self.winner_model_spec = model_spec
                logger.info(
                    f"[{self.meta.name}] Flag found by {model_spec}: {result.flag}"
                )
                return result, solver

            if result.status == CANCELLED:
                break

            if result.status == QUOTA_ERROR:
                logger.warning(
                    f"[{self.meta.name}/{model_spec}] Quota exhausted — stopping lane"
                )
                break

            if result.status in (GAVE_UP, ERROR):
                if result.step_count == 0 and result.cost_usd == 0:
                    logger.warning(
                        f"[{self.meta.name}/{model_spec}] Broken (0 steps, $0) — not bumping"
                    )
                    break

                # Track consecutive errors — stop after 3 in a row
                if result.status == ERROR:
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        logger.warning(
                            f"[{self.meta.name}/{model_spec}] {consecutive_errors} consecutive errors — giving up"
                        )
                        break
                else:
                    consecutive_errors = 0

                bump_count += 1
                # Cooldown between bumps — check cancellation during wait
                try:
                    await asyncio.wait_for(
                        self.cancel_event.wait(),
                        timeout=min(bump_count * 30, 300),
                    )
                    break  # cancelled during cooldown
                except TimeoutError:
                    pass  # cooldown elapsed, proceed with bump
                insights = self._gather_sibling_insights(model_spec)
                solver.bump(insights)
                logger.info(
                    f"[{self.meta.name}/{model_spec}] Bumped ({bump_count}), resuming"
                )
                continue

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
            self.cancel_event.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            return None
        finally:
            artifact_monitor.cancel()
            advisory_monitor.cancel()
            await asyncio.gather(artifact_monitor, advisory_monitor, return_exceptions=True)

    def kill(self) -> None:
        """Cancel all agents for this challenge."""
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
                "findings": findings,
                "advisor_note": self.lane_advisor_notes.get(spec, ""),
                "status": status,
                **(
                    runtime
                    | {
                        "last_exit_hint": runtime.get("last_exit_hint")
                        or self._lane_restart_notes.get(spec, "")
                    }
                ),
            }

        return {
            "challenge": self.meta.name,
            "cancelled": self.cancel_event.is_set(),
            "winner": self.winner.flag if self.winner else None,
            "winner_model": self.winner_model_spec,
            "advisor_note": self.last_advisor_note,
            "coordinator_advisor_note": self.last_coordinator_advisor_note,
            "shared_finding": self.last_shared_finding,
            "signals": {
                **self.message_bus.stats_snapshot(),
                "coordinator_messages": self.coordinator_message_count,
                "advisor_lane_hints": self.advisor_lane_hint_count,
                "advisor_coordinator_appends": self.advisor_coordinator_count,
            },
            "solve": dict(self.saved_solve_artifacts),
            "agents": agents,
        }
