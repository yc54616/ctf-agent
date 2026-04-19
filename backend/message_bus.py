"""Per-challenge message bus for inter-agent communication."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


def _normalize_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


@dataclass
class SharedFindingRef:
    model: str
    content: str = ""
    kind: str = "message"
    summary: str = ""
    pointer_path: str = ""
    digest_path: str = ""
    revision: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def rendered_text(self) -> str:
        if self.kind == "artifact_ref":
            lines: list[str] = []
            if self.content:
                lines.append(self.content)
            elif self.pointer_path:
                lines.append(f"Artifact path: {self.pointer_path}")
            if self.summary:
                summary_line = f"Summary: {self.summary}"
                if summary_line not in lines:
                    lines.append(summary_line)
            if self.digest_path:
                lines.append(f"Digest: {self.digest_path}")
            return "\n".join(lines).strip()

        if self.kind in {"finding_ref", "candidate_ref", "coordinator_note"}:
            lines = []
            if self.summary:
                lines.append(self.summary)
            elif self.content:
                lines.append(self.content)
            if self.digest_path:
                lines.append(f"Digest: {self.digest_path}")
            if self.pointer_path:
                lines.append(f"Pointer: {self.pointer_path}")
            return "\n".join(lines).strip()

        return (self.content or self.summary).strip()

    def snapshot(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "content": self.content,
            "kind": self.kind,
            "summary": self.summary,
            "pointer_path": self.pointer_path,
            "digest_path": self.digest_path,
            "revision": self.revision,
            "metadata": dict(self.metadata),
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_snapshot(cls, payload: object) -> SharedFindingRef | None:
        if not isinstance(payload, dict):
            return None
        raw_payload = _normalize_mapping(payload)
        raw_metadata = raw_payload.get("metadata")
        metadata = (
            {str(key): value for key, value in raw_metadata.items()}
            if isinstance(raw_metadata, dict)
            else {}
        )
        raw_timestamp = raw_payload.get("timestamp")
        timestamp = float(raw_timestamp) if isinstance(raw_timestamp, (int, float)) else time.time()
        return cls(
            model=str(raw_payload.get("model") or ""),
            content=str(raw_payload.get("content") or ""),
            kind=str(raw_payload.get("kind") or "message"),
            summary=str(raw_payload.get("summary") or ""),
            pointer_path=str(raw_payload.get("pointer_path") or ""),
            digest_path=str(raw_payload.get("digest_path") or ""),
            revision=str(raw_payload.get("revision") or ""),
            metadata=metadata,
            timestamp=timestamp,
        )


Finding = SharedFindingRef


@dataclass
class CandidateRef:
    challenge_name: str
    flag: str
    source_models: list[str] = field(default_factory=list)
    advisor_decision: str = "insufficient"
    advisor_note: str = ""
    summary: str = ""
    evidence_digest_paths: dict[str, str] = field(default_factory=dict)
    evidence_pointer_paths: dict[str, str] = field(default_factory=dict)
    trace_paths: dict[str, str] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def rendered_text(self) -> str:
        prefix = f"[{self.challenge_name}] " if self.challenge_name else ""
        lines = [
            f"{prefix}FLAG CANDIDATE: {self.flag.strip()}",
            f"Advisor verdict: {self.advisor_decision or 'insufficient'}",
        ]
        if self.source_models:
            lines.append(f"Source models: {', '.join(self.source_models)}")
        if self.advisor_note.strip():
            lines.extend(["Advisor note:", self.advisor_note.strip()])
        if self.summary.strip():
            lines.extend(["Evidence summary:", self.summary.strip()])
        for digest in self.evidence_digest_paths.values():
            if str(digest).strip():
                lines.append(f"Evidence digest: {str(digest).strip()}")
        for pointer in self.evidence_pointer_paths.values():
            if str(pointer).strip():
                lines.append(f"Evidence pointer: {str(pointer).strip()}")
        for trace in self.trace_paths.values():
            if str(trace).strip():
                lines.append(f"Trace: {str(trace).strip()}")
        lines.append("Review this candidate. Submit it only if the evidence is strong; otherwise keep lanes exploring.")
        return "\n".join(lines)

    def snapshot(self) -> dict[str, Any]:
        return {
            "kind": "candidate_ref",
            "challenge_name": self.challenge_name,
            "flag": self.flag,
            "source_models": list(self.source_models),
            "advisor_decision": self.advisor_decision,
            "advisor_note": self.advisor_note,
            "summary": self.summary,
            "evidence_digest_paths": dict(self.evidence_digest_paths),
            "evidence_pointer_paths": dict(self.evidence_pointer_paths),
            "trace_paths": dict(self.trace_paths),
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_snapshot(cls, payload: object) -> CandidateRef | None:
        raw_payload = _normalize_mapping(payload)
        if str(raw_payload.get("kind") or "") != "candidate_ref":
            return None
        raw_source_models = raw_payload.get("source_models", [])
        source_models = (
            [str(item).strip() for item in raw_source_models if str(item).strip()]
            if isinstance(raw_source_models, list)
            else []
        )
        raw_evidence_digest_paths = raw_payload.get("evidence_digest_paths", {})
        evidence_digest_paths = (
            {
                str(model): str(pointer)
                for model, pointer in raw_evidence_digest_paths.items()
                if str(model).strip() and str(pointer).strip()
            }
            if isinstance(raw_evidence_digest_paths, dict)
            else {}
        )
        raw_evidence_pointer_paths = raw_payload.get("evidence_pointer_paths", {})
        evidence_pointer_paths = (
            {
                str(model): str(pointer)
                for model, pointer in raw_evidence_pointer_paths.items()
                if str(model).strip() and str(pointer).strip()
            }
            if isinstance(raw_evidence_pointer_paths, dict)
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
        raw_timestamp = raw_payload.get("timestamp")
        timestamp = float(raw_timestamp) if isinstance(raw_timestamp, (int, float)) else time.time()
        return cls(
            challenge_name=str(raw_payload.get("challenge_name") or ""),
            flag=str(raw_payload.get("flag") or "").strip(),
            source_models=source_models,
            advisor_decision=str(raw_payload.get("advisor_decision") or "insufficient"),
            advisor_note=str(raw_payload.get("advisor_note") or ""),
            summary=str(raw_payload.get("summary") or ""),
            evidence_digest_paths=evidence_digest_paths,
            evidence_pointer_paths=evidence_pointer_paths,
            trace_paths=trace_paths,
            timestamp=timestamp,
        )


@dataclass
class CoordinatorNoteRef:
    challenge_name: str
    source_model: str
    summary: str = ""
    pointer_path: str = ""
    digest_path: str = ""
    timestamp: float = field(default_factory=time.time)

    def rendered_text(self) -> str:
        prefix = f"[{self.challenge_name}/{self.source_model}] " if self.challenge_name and self.source_model else ""
        lines = [f"ADVISOR MESSAGE: {prefix}{self.summary.strip()}".rstrip()]
        if self.digest_path:
            lines.append(f"Digest: {self.digest_path}")
        if self.pointer_path:
            lines.append(f"Pointer: {self.pointer_path}")
        return "\n".join(line for line in lines if line.strip())

    def snapshot(self) -> dict[str, Any]:
        return {
            "kind": "coordinator_note",
            "challenge_name": self.challenge_name,
            "source_model": self.source_model,
            "summary": self.summary,
            "pointer_path": self.pointer_path,
            "digest_path": self.digest_path,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_snapshot(cls, payload: object) -> CoordinatorNoteRef | None:
        raw_payload = _normalize_mapping(payload)
        if str(raw_payload.get("kind") or "") != "coordinator_note":
            return None
        raw_timestamp = raw_payload.get("timestamp")
        timestamp = float(raw_timestamp) if isinstance(raw_timestamp, (int, float)) else time.time()
        return cls(
            challenge_name=str(raw_payload.get("challenge_name") or ""),
            source_model=str(raw_payload.get("source_model") or ""),
            summary=str(raw_payload.get("summary") or ""),
            pointer_path=str(raw_payload.get("pointer_path") or ""),
            digest_path=str(raw_payload.get("digest_path") or ""),
            timestamp=timestamp,
        )


MAX_FINDINGS = 200


@dataclass
class ChallengeMessageBus:
    """Append-only shared findings list with per-model cursors."""

    findings: list[SharedFindingRef] = field(default_factory=list)
    cursors: dict[str, int] = field(default_factory=dict)
    total_posts: int = 0
    total_checks: int = 0
    total_delivered: int = 0
    posts_by_source: dict[str, int] = field(default_factory=dict)
    last_post_model: str = ""
    last_post_content: str = ""
    last_post_at: float | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def post(
        self,
        model: str,
        content: str | SharedFindingRef,
        *,
        kind: str = "message",
        summary: str = "",
        pointer_path: str = "",
        digest_path: str = "",
        revision: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Post a finding from a solver."""
        if isinstance(content, SharedFindingRef):
            finding = content
        else:
            finding = SharedFindingRef(
                model=model,
                content=content,
                kind=kind,
                summary=summary,
                pointer_path=pointer_path,
                digest_path=digest_path,
                revision=revision,
                metadata=dict(metadata or {}),
            )
        async with self._lock:
            self.findings.append(finding)
            self.total_posts += 1
            self.posts_by_source[model] = self.posts_by_source.get(model, 0) + 1
            self.last_post_model = model
            self.last_post_content = finding.rendered_text()
            self.last_post_at = time.time()
            if len(self.findings) > MAX_FINDINGS:
                trim = len(self.findings) - MAX_FINDINGS
                self.findings = self.findings[trim:]
                self.cursors = {k: max(0, v - trim) for k, v in self.cursors.items()}

    async def check(self, model: str) -> list[SharedFindingRef]:
        """Get unread findings from other models. Advances the cursor."""
        async with self._lock:
            cursor = self.cursors.get(model, 0)
            unread = [f for f in self.findings[cursor:] if f.model != model]
            self.total_checks += 1
            self.total_delivered += len(unread)
            self.cursors[model] = len(self.findings)
            return unread

    async def broadcast(self, content: str, source: str = "coordinator") -> None:
        """Coordinator broadcasts a message to all solvers."""
        await self.post(
            source,
            SharedFindingRef(
                model=source,
                content=content,
                kind="coordinator_note",
                summary=content,
            ),
        )

    async def snapshot_findings(self) -> list[SharedFindingRef]:
        """Return a copy of all current findings without advancing cursors."""
        async with self._lock:
            return list(self.findings)

    def format_unread(self, findings: list[SharedFindingRef]) -> str:
        """Format findings for injection into a solver prompt."""
        if not findings:
            return ""
        parts = [f"[{f.model}] {f.rendered_text()}" for f in findings]
        return "**Findings from other agents:**\n\n" + "\n\n".join(parts)

    def stats_snapshot(self) -> dict[str, object]:
        return {
            "total_posts": self.total_posts,
            "total_checks": self.total_checks,
            "total_delivered": self.total_delivered,
            "posts_by_source": dict(self.posts_by_source),
            "last_post_model": self.last_post_model,
            "last_post_content": self.last_post_content,
            "last_post_at": self.last_post_at,
        }
