const POLL_MS = 2000;
const FINAL_STATES = new Set(["won", "flag_found", "cancelled"]);
const TERMINAL_CANDIDATE_STATUSES = new Set(["confirmed", "rejected"]);
const state = {
  snapshot: null,
  snapshotReceived: false,
  selectedChallenge: null,
  selectedLane: null,
  selectedTrace: null,
  advisoryHistory: [],
  traceFiles: [],
  traceEvents: [],
  traceWindow: null,
  traceTypeFilter: "",
  traceTextFilter: "",
  hideErrorLanes: false,
  loadingTraceFilesFor: "",
  usingRealtime: false,
  statusPollHandle: null,
  statusStream: null,
};

const els = {
  updatedAt: document.getElementById("updatedAt"),
  runningFor: document.getElementById("runningFor"),
  syncMode: document.getElementById("syncMode"),
  summaryGrid: document.getElementById("summaryGrid"),
  challengeCount: document.getElementById("challengeCount"),
  challengeGroups: document.getElementById("challengeGroups"),
  selectedChallengeTitle: document.getElementById("selectedChallengeTitle"),
  selectedChallengeMeta: document.getElementById("selectedChallengeMeta"),
  selectedLaneMeta: document.getElementById("selectedLaneMeta"),
  laneFocus: document.getElementById("laneFocus"),
  coordinatorAdvisoryText: document.getElementById("coordinatorAdvisoryText"),
  laneAdvisoryText: document.getElementById("laneAdvisoryText"),
  sharedFindingList: document.getElementById("sharedFindingList"),
  flagCandidatesList: document.getElementById("flagCandidatesList"),
  advisoryHistory: document.getElementById("advisoryHistory"),
  laneStrip: document.getElementById("laneStrip"),
  traceSelect: document.getElementById("traceSelect"),
  loadOlderBtn: document.getElementById("loadOlderBtn"),
  hideErrorLanes: document.getElementById("hideErrorLanes"),
  traceTableBody: document.getElementById("traceTableBody"),
  traceTypeFilter: document.getElementById("traceTypeFilter"),
  traceTextFilter: document.getElementById("traceTextFilter"),
  activityLog: document.getElementById("activityLog"),
  msgForm: document.getElementById("msgForm"),
  msgInput: document.getElementById("msgInput"),
  laneBumpForm: document.getElementById("laneBumpForm"),
  laneBumpInput: document.getElementById("laneBumpInput"),
  challengeBumpForm: document.getElementById("challengeBumpForm"),
  challengeBumpInput: document.getElementById("challengeBumpInput"),
  maxChallengesForm: document.getElementById("maxChallengesForm"),
  maxChallengesInput: document.getElementById("maxChallengesInput"),
  queuePriorityForm: document.getElementById("queuePriorityForm"),
  selectedChallengeQueueMeta: document.getElementById("selectedChallengeQueueMeta"),
  priorityWaitBtn: document.getElementById("priorityWaitBtn"),
  normalQueueBtn: document.getElementById("normalQueueBtn"),
  restartChallengeBtn: document.getElementById("restartChallengeBtn"),
  externalSolveForm: document.getElementById("externalSolveForm"),
  externalSolveFlagInput: document.getElementById("externalSolveFlagInput"),
  externalSolveNoteInput: document.getElementById("externalSolveNoteInput"),
};

function pendingChallengeEntries(snapshot) {
  const entries = Array.isArray(snapshot?.pending_challenge_entries)
    ? snapshot.pending_challenge_entries
    : (snapshot?.pending_challenges ?? []).map((challengeName) => ({
        challenge_name: challengeName,
        priority: false,
        reason: "queued",
        local_preloaded: false,
      }));
  return entries
    .map((entry) => ({
      challenge_name: String(entry?.challenge_name || "").trim(),
      priority: Boolean(entry?.priority),
      reason: String(entry?.reason || "queued").trim(),
      local_preloaded: Boolean(entry?.local_preloaded),
    }))
    .filter((entry) => entry.challenge_name);
}

function challengeBuckets(snapshot) {
  const active = snapshot?.active_swarms ?? {};
  const pending = { ...(snapshot?.pending_swarms ?? {}) };
  const finished = { ...(snapshot?.finished_swarms ?? {}) };
  const pendingEntries = pendingChallengeEntries(snapshot);
  const pendingNames = pendingEntries.map((entry) => entry.challenge_name);
  const pendingByName = Object.fromEntries(
    pendingEntries.map((entry) => [entry.challenge_name, entry])
  );
  const results = snapshot?.results ?? {};
  for (const [name, result] of Object.entries(results)) {
    const solved = result?.status === "flag_found";
    if (!active[name] && !finished[name] && solved) {
      finished[name] = {
        challenge: name,
        started_at: result.started_at || null,
        winner: result.flag || result.status || "done",
        winner_model: result.winner_model || "",
        restored: true,
        agents: {},
        step_count: Number(result.step_count || 0),
        flag_candidates: mergeObjectMap(result.flag_candidates, {}),
        coordinator_advisor_note: result.coordinator_advisor_note || "",
        shared_finding: result.shared_finding || "",
        shared_findings: mergeObjectMap(result.shared_findings, {}),
      };
    } else if (!active[name] && !finished[name] && !pendingNames.includes(name)) {
      pendingNames.push(name);
      const resultStatus = String(result?.status || "").trim().toLowerCase();
      pendingByName[name] = {
        challenge_name: name,
        priority: false,
        reason: resultStatus === "candidate_pending" ? "candidate_pending" : "queued",
        local_preloaded: false,
      };
    } else if (finished[name] && result && typeof result === "object") {
      finished[name] = {
        ...finished[name],
        step_count: Math.max(
          Number(finished[name].step_count || 0),
          Number(result.step_count || 0)
        ),
        flag_candidates: mergeObjectMap(result.flag_candidates, finished[name].flag_candidates),
        shared_findings: mergeObjectMap(result.shared_findings, finished[name].shared_findings),
      };
    }
  }
  for (const name of pendingNames) {
    const result = results?.[name] ?? {};
    const existing = pending?.[name] ?? {};
    const pendingEntry = pendingByName?.[name] ?? {};
      pending[name] = {
        challenge: name,
        started_at: result.started_at || existing.started_at || null,
        agents: {},
        step_count: Number(result.step_count || existing.step_count || 0),
      flag_candidates: mergeObjectMap(result.flag_candidates, existing.flag_candidates),
      coordinator_advisor_note:
        result.coordinator_advisor_note || existing.coordinator_advisor_note || "",
      shared_finding: result.shared_finding || existing.shared_finding || "",
      shared_findings: mergeObjectMap(result.shared_findings, existing.shared_findings),
      pending_reason: pendingEntry.reason || existing.pending_reason || "queued",
      pending_priority: Boolean(
        pendingEntry.priority ?? existing.pending_priority ?? false
      ),
      pending_local_preloaded: Boolean(
        pendingEntry.local_preloaded ?? existing.pending_local_preloaded ?? false
      ),
      status: String(result.status || existing.status || "pending"),
      winner: String(existing.winner || ""),
      winner_model: String(existing.winner_model || ""),
    };
  }
  return { active, finished, pending, pendingNames, pendingByName, results };
}

function mergeObjectMap(primary, fallback) {
  const primaryIsObject = primary && typeof primary === "object" && !Array.isArray(primary);
  const fallbackIsObject = fallback && typeof fallback === "object" && !Array.isArray(fallback);
  if (!primaryIsObject && !fallbackIsObject) {
    return {};
  }
  if (!fallbackIsObject) {
    return { ...primary };
  }
  if (!primaryIsObject) {
    return { ...fallback };
  }
  return {
    ...fallback,
    ...primary,
  };
}

function challengeWinnerLabel(challenge) {
  const winnerModel = String(challenge?.winner_model || "").trim();
  if (winnerModel) {
    return shortModelName(winnerModel);
  }
  const winner = String(challenge?.winner || "").trim();
  if (winner) {
    return "solved";
  }
  return "";
}

function laneEntries(challenge) {
  return Object.entries(challenge?.agents ?? {});
}

function isErrorLane(agent) {
  const lifecycle = String(agent?.lifecycle || "").trim().toLowerCase();
  return lifecycle === "error" || lifecycle === "quota_error";
}

function visibleLaneEntries(challenge) {
  const lanes = laneEntries(challenge);
  if (!state.hideErrorLanes) {
    return lanes;
  }
  return lanes.filter(([, agent]) => !isErrorLane(agent));
}

function preferredLane(challenge, lanes = null) {
  const lanesToSort = Array.isArray(lanes) ? lanes : laneEntries(challenge);
  if (!lanesToSort.length) {
    return null;
  }
  const priority = new Map([
    ["busy", 0],
    ["idle", 1],
    ["error", 2],
    ["quota_error", 3],
    ["won", 4],
    ["flag_found", 4],
    ["cancelled", 5],
  ]);
  return lanesToSort
    .slice()
    .sort((a, b) => {
      const left = priority.get(a[1].lifecycle) ?? 99;
      const right = priority.get(b[1].lifecycle) ?? 99;
      if (left !== right) {
        return left - right;
      }
      return (b[1].step_count || 0) - (a[1].step_count || 0);
    })[0]?.[0] ?? null;
}

function getSelectedChallengeData() {
  const buckets = challengeBuckets(state.snapshot || {});
  if (state.selectedChallenge && buckets.active[state.selectedChallenge]) {
    return { bucket: "active", data: buckets.active[state.selectedChallenge] };
  }
  if (state.selectedChallenge && buckets.finished[state.selectedChallenge]) {
    return { bucket: "finished", data: buckets.finished[state.selectedChallenge] };
  }
  if (state.selectedChallenge && buckets.pending[state.selectedChallenge]) {
    return {
      bucket: "pending",
      data: buckets.pending[state.selectedChallenge],
    };
  }
  return { bucket: "", data: null };
}

function previewEvent(event) {
  const candidates = [
    event.text,
    event.result,
    event.args,
    event.error,
    event.findings,
    event.kind,
  ];
  for (const value of candidates) {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    if (text) {
      return text.slice(0, 240);
    }
  }
  return JSON.stringify(event).slice(0, 240);
}

function laneDetail(agent) {
  const detail = [
    agent.current_command,
    agent.commentary_preview,
    agent.last_command,
    agent.findings,
    agent.last_exit_hint,
  ]
    .map((value) => String(value || "").trim())
    .find(Boolean);
  return detail || "no detail";
}

function shortModelName(spec) {
  return String(spec || "").split("/").slice(-1)[0] || String(spec || "");
}

function humanizeProgressKind(kind) {
  return String(kind || "")
    .replaceAll("_", " ")
    .trim();
}

function badgeClass(value) {
  return `badge ${String(value || "").replace(/[^a-z_]/gi, "")}`;
}

function formatNumber(value) {
  return Intl.NumberFormat("en-US").format(Number(value || 0));
}

function formatTime(ts) {
  if (!ts) {
    return "-";
  }
  return new Date(Number(ts) * 1000).toLocaleTimeString("ko-KR", {
    hour12: false,
  });
}

function parseTraceStartedAt(traceName) {
  const match = String(traceName || "").match(/-(\d{8})-(\d{6})\.jsonl$/);
  if (!match) {
    return null;
  }
  const [, datePart, timePart] = match;
  const year = Number(datePart.slice(0, 4));
  const month = Number(datePart.slice(4, 6)) - 1;
  const day = Number(datePart.slice(6, 8));
  const hour = Number(timePart.slice(0, 2));
  const minute = Number(timePart.slice(2, 4));
  const second = Number(timePart.slice(4, 6));
  const startedAt = new Date(year, month, day, hour, minute, second).getTime();
  return Number.isNaN(startedAt) ? null : startedAt;
}

function formatElapsed(secondsTotal) {
  if (!Number.isFinite(secondsTotal) || secondsTotal < 0) {
    return "-";
  }
  const total = Math.max(0, Math.floor(secondsTotal));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (days > 0) {
    return `${days}일 ${hours}시 ${minutes}분 ${seconds}초`;
  }
  return `${hours}시 ${minutes}분 ${seconds}초`;
}

function formatTokenCount(value) {
  const total = Number(value ?? 0);
  if (!Number.isFinite(total) || total <= 0) {
    return "0";
  }
  if (total >= 1_000_000) {
    return `${(total / 1_000_000).toFixed(1)}M`;
  }
  if (total >= 1_000) {
    return `${(total / 1_000).toFixed(1)}k`;
  }
  return `${Math.round(total)}`;
}

function challengeElapsedSeconds(challenge, bucket = "") {
  const startedAt = Number(challenge?.started_at || 0);
  if (!startedAt || bucket === "finished") {
    return null;
  }
  const elapsed = (Date.now() / 1000) - startedAt;
  if (!Number.isFinite(elapsed) || elapsed < 0) {
    return null;
  }
  return elapsed;
}

function currentRunStartedAtMs() {
  const sessionStartedAt = state.snapshot?.session_started_at;
  if (sessionStartedAt) {
    return Number(sessionStartedAt) * 1000;
  }
  const challenge = getSelectedChallengeData().data;
  const lane = state.selectedLane ? challenge?.agents?.[state.selectedLane] : null;
  const traceStartedAt = parseTraceStartedAt(state.traceFiles[0] || state.selectedTrace);
  if (traceStartedAt) {
    return traceStartedAt;
  }
  if (lane?.current_started_at) {
    return Number(lane.current_started_at) * 1000;
  }
  if (lane?.heartbeat_at) {
    return Number(lane.heartbeat_at) * 1000;
  }
  return null;
}

function renderRunningFor() {
  if (!els.runningFor) {
    return;
  }
  const startedAtMs = currentRunStartedAtMs();
  if (!startedAtMs) {
    els.runningFor.textContent = "-";
    return;
  }
  els.runningFor.textContent = formatElapsed((Date.now() - startedAtMs) / 1000);
}

function challengeSummary(name, challenge, bucket = "") {
  const lanes = laneEntries(challenge);
  const busy = lanes.filter(([, lane]) => lane.lifecycle === "busy").length;
  const laneSteps = lanes.reduce((sum, [, lane]) => sum + Number(lane.step_count || 0), 0);
  const stepCount = Math.max(laneSteps, Number(challenge.step_count || 0));
  const candidateEntries = Object.values(challenge.flag_candidates || {});
  const pendingCandidates = candidateEntries.filter((candidate) =>
    !TERMINAL_CANDIDATE_STATUSES.has(String(candidate.status || "").trim().toLowerCase())
  ).length;
  const winnerLabel = challengeWinnerLabel(challenge);
  const lead = winnerLabel ? `winner ${winnerLabel}` : `${lanes.length} lanes`;
  const details = [lead];
  const pendingReason = String(challenge?.pending_reason || "").trim();
  if (pendingReason === "priority_waiting") {
    details.push("priority waiting");
  } else if (pendingReason === "resume_requested") {
    details.push("resume queued");
  } else if (pendingReason === "candidate_retry") {
    details.push("candidate retry");
  } else if (pendingReason === "candidate_pending") {
    details.push("candidate paused");
  } else if (pendingReason === "ctfd_retry") {
    details.push("ctfd retry");
  }
  if (lanes.length) {
    details.push(`busy ${busy}`);
  }
  if (challenge?.pending_local_preloaded && pendingReason) {
    details.push("local");
  }
  if (pendingCandidates) {
    details.push(`candidates ${pendingCandidates}`);
  }
  details.push(`steps ${stepCount}`);
  const elapsed = challengeElapsedSeconds(challenge, bucket);
  if (elapsed !== null) {
    details.push(`elapsed ${formatElapsed(elapsed)}`);
  }
  const usage = challenge?.usage && typeof challenge.usage === "object" ? challenge.usage : {};
  const totalTokens = Number(usage.total_tokens ?? 0);
  const costUsd = Number(usage.cost_usd ?? 0);
  if (totalTokens > 0) {
    details.push(`tokens ${formatTokenCount(totalTokens)}`);
  }
  if (costUsd > 0) {
    details.push(`$${costUsd.toFixed(costUsd < 0.01 ? 4 : 2)}`);
  }
  return details.join(" · ");
}

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function escapeAttr(text) {
  return escapeHtml(text).replaceAll('"', "&quot;");
}

function renderFlagCandidates(challenge) {
  const entries = Object.values(challenge?.flag_candidates || {}).sort((left, right) => {
    const priority = {
      pending: 0,
      pending_coordinator: 1,
      incorrect: 2,
      rejected: 3,
      confirmed: 4,
    };
    const leftPriority = priority[String(left?.status || "")] ?? 99;
    const rightPriority = priority[String(right?.status || "")] ?? 99;
    if (leftPriority !== rightPriority) {
      return leftPriority - rightPriority;
    }
    return Number(right?.last_seen_at || 0) - Number(left?.last_seen_at || 0);
  });
  if (!entries.length) {
    return '<li class="empty">No candidate flags yet.</li>';
  }
  return entries
    .map((candidate) => {
      const status = String(candidate.status || "pending");
      const sourceModels = Array.isArray(candidate.source_models) ? candidate.source_models.join(", ") : "";
      const evidenceDigests =
        candidate && typeof candidate.evidence_digest_paths === "object" && candidate.evidence_digest_paths
          ? Object.values(candidate.evidence_digest_paths)
              .map((value) => String(value || "").trim())
              .filter(Boolean)
          : [];
      const evidencePointers =
        candidate && typeof candidate.evidence_pointer_paths === "object" && candidate.evidence_pointer_paths
          ? Object.values(candidate.evidence_pointer_paths)
              .map((value) => String(value || "").trim())
              .filter(Boolean)
          : [];
      const evidenceDigest = evidenceDigests[0] || "";
      const evidencePointer = evidencePointers[0] || "";
      const evidence = evidenceDigest || evidencePointer || (
        Array.isArray(candidate.evidence_snippets)
          ? String(candidate.evidence_snippets[0] || "").trim()
          : ""
      );
      const submitDisplay = String(candidate.submit_display || "").trim();
      const note = String(candidate.advisor_note || "").trim();
      const advisorDecision = String(candidate.advisor_decision || "").trim();
      const canApprove = canApproveLocalCandidate(candidate);
      const canReject = canRejectLocalCandidate(candidate);
      return `
        <li>
          <div class="candidate-head">
            <div class="candidate-flag">${escapeHtml(candidate.flag || "-")}</div>
            <span class="${badgeClass(status)}">${escapeHtml(status)}</span>
          </div>
          <div class="candidate-meta">
            ${advisorDecision ? `<span class="event-tag">advisor ${escapeHtml(advisorDecision)}</span>` : ""}
            ${sourceModels ? `<span class="event-tag">${escapeHtml(sourceModels)}</span>` : ""}
            ${canApprove ? `<button type="button" class="candidate-action" data-approve-flag="${escapeAttr(candidate.flag || "")}">${escapeHtml(candidateApproveLabel())}</button>` : ""}
            ${canReject ? `<button type="button" class="candidate-action candidate-action-secondary" data-reject-flag="${escapeAttr(candidate.flag || "")}">Reject</button>` : ""}
          </div>
          ${
            note
              ? `<div class="candidate-subtle"><strong>Advisor:</strong> ${escapeHtml(note)}</div>`
              : ""
          }
          ${
            submitDisplay
              ? `<div class="candidate-subtle"><strong>Submit:</strong> ${escapeHtml(submitDisplay)}</div>`
              : ""
          }
          ${
            evidenceDigest
              ? `<div class="candidate-subtle"><strong>Evidence digest:</strong> ${escapeHtml(evidenceDigest)}</div>`
              : ""
          }
          ${
            evidencePointer && evidencePointer !== evidenceDigest
              ? `<div class="candidate-subtle"><strong>Evidence pointer:</strong> ${escapeHtml(evidencePointer)}</div>`
              : ""
          }
          ${
            evidence && evidence !== evidenceDigest && evidence !== evidencePointer
              ? `<div class="candidate-subtle"><strong>Evidence:</strong> ${escapeHtml(evidence)}</div>`
              : ""
          }
        </li>
      `;
    })
    .join("");
}

function candidateApprovalMode() {
  if (!state.selectedChallenge) {
    return "";
  }
  if (state.snapshot?.local_mode) {
    return "local";
  }
  return "manual";
}

function candidateApproveLabel() {
  return candidateApprovalMode() === "local" ? "Confirm locally" : "Confirm manually";
}

function canActOnCandidate(candidate) {
  if (!candidateApprovalMode()) {
    return false;
  }
  const status = String(candidate?.status || "").trim().toLowerCase();
  if (!String(candidate?.flag || "").trim()) {
    return false;
  }
  return !TERMINAL_CANDIDATE_STATUSES.has(status);
}

function canApproveLocalCandidate(candidate) {
  return canActOnCandidate(candidate);
}

function canRejectLocalCandidate(candidate) {
  return canActOnCandidate(candidate);
}

function selectedChallengePendingEntry() {
  const buckets = challengeBuckets(state.snapshot || {});
  return state.selectedChallenge ? buckets.pendingByName?.[state.selectedChallenge] || null : null;
}

function renderSchedulerControls(selected, challenge) {
  if (els.maxChallengesInput) {
    els.maxChallengesInput.value = String(Number(state.snapshot?.max_concurrent_challenges ?? 0));
  }

  if (!els.selectedChallengeQueueMeta || !els.priorityWaitBtn || !els.normalQueueBtn || !els.restartChallengeBtn) {
    return;
  }

  const pendingEntry = selected.bucket === "pending" ? selectedChallengePendingEntry() : null;
  const isSolved = challenge?.flag || challenge?.winner;
  const isPendingPriority = Boolean(pendingEntry?.priority);
  const bucketLabel = selected.bucket || "challenge";
  const challengeName = String(challenge?.challenge || state.selectedChallenge || "").trim();

  els.selectedChallengeQueueMeta.textContent = challengeName
    ? `${bucketLabel} · ${challengeSummary(challengeName, challenge)}`
    : "Select a challenge to change queue priority.";
  els.priorityWaitBtn.disabled = !challengeName || Boolean(isSolved);
  els.normalQueueBtn.disabled = !challengeName || !pendingEntry || !isPendingPriority;
  els.restartChallengeBtn.disabled = !challengeName || Boolean(isSolved);
  els.priorityWaitBtn.textContent =
    selected.bucket === "active" ? "Pause to priority waiting" : "Move to priority waiting";
  els.normalQueueBtn.textContent = selected.bucket === "pending" ? "Restore waiting" : "Restore normal waiting";
  if (selected.bucket === "active") {
    els.restartChallengeBtn.textContent = "Resume after stop";
  } else if (isPendingPriority) {
    els.restartChallengeBtn.textContent = "Restore and resume";
  } else {
    els.restartChallengeBtn.textContent = "Resume previous work";
  }
}

function sharedFindingEntries(challenge) {
  const raw = challenge?.shared_findings;
  const entries = [];
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    for (const [modelSpec, finding] of Object.entries(raw)) {
      if (!finding || typeof finding !== "object") {
        continue;
      }
      const summary = String(finding.summary || finding.content || "").trim();
      const artifactPath = String(finding.artifact_path || "").trim();
      const pointerPath = String(finding.pointer_path || "").trim();
      const digestPath = String(finding.digest_path || "").trim();
      if (!summary && !artifactPath && !pointerPath && !digestPath) {
        continue;
      }
      entries.push({
        modelSpec,
        summary,
        artifactPath,
        pointerPath,
        digestPath,
        kind: String(finding.kind || "finding_ref"),
      });
    }
  }
  if (entries.length) {
    return entries.sort((left, right) => String(left.modelSpec).localeCompare(String(right.modelSpec)));
  }
  const legacy = String(challenge?.shared_finding || "").trim();
  if (!legacy) {
    return [];
  }
  return [
    {
      modelSpec: "",
      summary: legacy,
      artifactPath: "",
      pointerPath: "",
      digestPath: "",
      kind: "message",
    },
  ];
}

function renderSharedFindings(challenge) {
  const entries = sharedFindingEntries(challenge);
  if (!entries.length) {
    return '<li class="empty">No shared findings yet.</li>';
  }
  return entries
    .map((entry) => {
      const modelTag = entry.modelSpec
        ? `<span class="event-tag">${escapeHtml(shortModelName(entry.modelSpec))}</span>`
        : "";
      const kindTag = entry.kind ? `<span class="event-tag">${escapeHtml(entry.kind)}</span>` : "";
      const summary = entry.summary || entry.artifactPath || entry.pointerPath || entry.digestPath || "-";
      return `
        <li>
          <div class="shared-finding-head">
            ${modelTag}
            ${kindTag}
          </div>
          <div class="shared-finding-summary">${escapeHtml(summary)}</div>
          ${
            entry.artifactPath
              ? `<div class="shared-finding-path"><strong>Artifact:</strong> ${escapeHtml(entry.artifactPath)}</div>`
              : ""
          }
          ${
            entry.digestPath
              ? `<div class="shared-finding-path"><strong>Digest:</strong> ${escapeHtml(entry.digestPath)}</div>`
              : ""
          }
          ${
            entry.pointerPath
              ? `<div class="shared-finding-path"><strong>Pointer:</strong> ${escapeHtml(entry.pointerPath)}</div>`
              : ""
          }
        </li>
      `;
    })
    .join("");
}

function pushActivity(message, tone = "") {
  const entry = document.createElement("li");
  const label = tone ? `[${tone}] ` : "";
  entry.textContent = `${label}${message}`;
  els.activityLog.prepend(entry);
  while (els.activityLog.children.length > 12) {
    els.activityLog.removeChild(els.activityLog.lastChild);
  }
}

function pushCoordinatorActivity(message, tone = "") {
  pushActivity(`coordinator: ${message}`, tone);
}

function pushSchedulerActivity(message, tone = "") {
  pushActivity(`scheduler: ${message}`, tone);
}

function pushChallengeActivity(challengeName, message, tone = "") {
  const prefix = challengeName ? `challenge ${challengeName}: ` : "challenge: ";
  pushActivity(`${prefix}${message}`, tone);
}

function pushLaneActivity(challengeName, laneId, message, tone = "") {
  const laneLabel = laneId ? `lane ${laneId}` : "lane";
  if (challengeName) {
    pushActivity(`challenge ${challengeName}: ${laneLabel} ${message}`, tone);
    return;
  }
  pushActivity(`${laneLabel}: ${message}`, tone);
}

function pushServerActivity(result, tone = "ok") {
  const text = String(result || "").trim();
  if (!text) {
    return;
  }
  pushActivity(`server: ${text}`, tone);
}

function setSyncMode(label) {
  if (els.syncMode) {
    els.syncMode.textContent = label;
  }
}

function markDisconnected(reason = "disconnected") {
  state.usingRealtime = false;
  setSyncMode(reason);
  if (!state.snapshot && els.updatedAt) {
    els.updatedAt.textContent = "-";
  }
}

function syncSelections() {
  const buckets = challengeBuckets(state.snapshot || {});
  const activeNames = Object.keys(buckets.active);
  const finishedNames = Object.keys(buckets.finished);
  const pendingNames = Array.isArray(buckets.pendingNames)
    ? buckets.pendingNames
    : Object.keys(buckets.pending || {});
  const known = new Set([...activeNames, ...finishedNames, ...pendingNames]);

  if (!state.selectedChallenge || !known.has(state.selectedChallenge)) {
    state.selectedChallenge = activeNames[0] || finishedNames[0] || pendingNames[0] || null;
    state.selectedTrace = null;
    state.traceFiles = [];
    state.traceEvents = [];
    state.traceWindow = null;
  }

  const selected = getSelectedChallengeData().data;
  const lanes = laneEntries(selected);
  const visibleLanes = visibleLaneEntries(selected);
  if (!lanes.length) {
    state.selectedLane = null;
    return;
  }
  const hasVisibleSelection =
    !!state.selectedLane &&
    visibleLanes.some(([modelSpec]) => modelSpec === state.selectedLane);
  if (!state.selectedLane || !selected.agents[state.selectedLane] || !hasVisibleSelection) {
    state.selectedLane = preferredLane(selected, visibleLanes);
    state.selectedTrace = null;
    state.traceFiles = [];
    state.traceEvents = [];
    state.traceWindow = null;
  }
}

function renderSummary() {
  const snapshot = state.snapshot;
  if (!snapshot) {
    els.summaryGrid.innerHTML = "";
    return;
  }
  const health = snapshot.health_summary || {};
  const challengeSummary = snapshot.challenge_summary || {};
  const costSummary = snapshot.cost_summary || {};
  const metrics = [
    ["Healthy", health.healthy_lanes ?? 0],
    ["Stale", health.stale_lanes ?? 0],
    ["Resetting", health.resetting_lanes ?? 0],
    ["Active", challengeSummary.active_challenge_count ?? snapshot.active_swarm_count ?? 0],
    ["Steps", Number(snapshot.total_step_count ?? 0)],
    ["Candidates", challengeSummary.pending_candidate_count ?? 0],
    ["Cost", `$${Number(costSummary.cost_usd ?? snapshot.cost_usd ?? 0).toFixed(2)}`],
    ["Codex Cache", `${Math.round(Number((costSummary.cache_hit_rate ?? 0) * 100))}%`],
  ];
  els.summaryGrid.innerHTML = metrics
    .map(
      ([label, value]) => `
        <div class="metric-pill">
          <span class="metric-label">${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
        </div>
      `
    )
    .join("");
}

function renderChallenges() {
  const snapshot = state.snapshot;
  if (!snapshot) {
    els.challengeGroups.innerHTML = '<div class="empty">No snapshot loaded.</div>';
    els.challengeCount.textContent = "-";
    return;
  }
  const buckets = challengeBuckets(snapshot);
  const groups = [
    ["Active", Object.entries(buckets.active)],
    ["Finished", Object.entries(buckets.finished)],
    ["Pending", Object.entries(buckets.pending)],
  ];
  els.challengeCount.textContent =
    `${Object.keys(buckets.active).length + Object.keys(buckets.finished).length + Object.keys(buckets.pending).length} total`;
  els.challengeGroups.innerHTML = groups
    .map(([label, entries]) => {
      const body = entries.length
        ? entries
            .map(([name, challenge]) => {
              const selected = state.selectedChallenge === name ? "selected" : "";
              const status = label.toLowerCase();
              const summary = challengeSummary(name, challenge, status);
              return `
                <button
                  class="challenge-card ${selected}"
                  data-challenge="${escapeHtml(name)}"
                  type="button"
                >
                  <strong>${escapeHtml(name)}</strong>
                  <div class="challenge-meta">
                    <span class="${badgeClass(status)}">${escapeHtml(status)}</span>
                    <span>${escapeHtml(summary)}</span>
                  </div>
                </button>
              `;
            })
            .join("")
        : '<div class="empty">None</div>';
      return `
        <section class="challenge-group">
          <h3>${escapeHtml(label)}</h3>
          <div class="challenge-list">${body}</div>
        </section>
      `;
    })
    .join("");

  els.challengeGroups.querySelectorAll("[data-challenge]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedChallenge = button.dataset.challenge;
      state.selectedLane = null;
      state.selectedTrace = null;
      state.traceFiles = [];
      state.traceEvents = [];
      state.traceWindow = null;
      syncSelections();
      render();
      fetchTraceFiles();
    });
  });
}

function renderSelectedChallenge() {
  const selected = getSelectedChallengeData();
  const challenge = selected.data;
  if (!challenge) {
    els.selectedChallengeTitle.textContent = "Select a challenge";
    els.selectedChallengeMeta.textContent = "No active challenge selected.";
    els.selectedLaneMeta.textContent = "Select a lane to inspect it.";
    renderSchedulerControls({ bucket: "", data: null }, null);
    els.coordinatorAdvisoryText.textContent = "-";
    els.laneAdvisoryText.textContent = "-";
    els.sharedFindingList.innerHTML = '<li class="empty">No shared findings yet.</li>';
    els.flagCandidatesList.innerHTML = '<li class="empty">No candidate flags yet.</li>';
    els.advisoryHistory.innerHTML = '<li class="empty">No advisory history yet.</li>';
    els.laneStrip.innerHTML = '<div class="empty">No challenge selected.</div>';
    els.laneFocus.innerHTML = '<div class="empty">Select a lane to inspect current activity.</div>';
    els.traceTableBody.innerHTML = '<tr><td class="empty" colspan="3">No trace selected.</td></tr>';
    return;
  }

  const lanes = laneEntries(challenge);
  const visibleLanes = visibleLaneEntries(challenge);
  if (state.selectedLane && !visibleLanes.some(([modelSpec]) => modelSpec === state.selectedLane)) {
    state.selectedLane = preferredLane(challenge, visibleLanes);
  }
  els.selectedChallengeTitle.textContent = challenge.challenge || state.selectedChallenge;
  els.selectedChallengeMeta.textContent =
    `${selected.bucket || "challenge"} · ${challengeSummary(state.selectedChallenge, challenge, selected.bucket)}`;
  renderSchedulerControls(selected, challenge);
  els.coordinatorAdvisoryText.textContent = challenge.coordinator_advisor_note || "-";
  els.sharedFindingList.innerHTML = renderSharedFindings(challenge);
  els.flagCandidatesList.innerHTML = renderFlagCandidates(challenge);
  els.laneStrip.innerHTML = visibleLanes.length
    ? visibleLanes
        .map(([modelSpec, agent]) => {
          const selectedLane = state.selectedLane === modelSpec ? "selected" : "";
          return `
            <button
              class="lane-chip ${selectedLane}"
              data-lane="${escapeHtml(modelSpec)}"
              type="button"
              title="${escapeHtml(modelSpec)}"
            >
              <div class="model">${escapeHtml(shortModelName(modelSpec))}</div>
              <div class="spec">${escapeHtml(modelSpec)}</div>
              <div class="meta-row">
                <span class="${badgeClass(agent.lifecycle)}">${escapeHtml(agent.lifecycle || "unknown")}</span>
                <span>step ${escapeHtml(agent.step_count || 0)}</span>
              </div>
            </button>
          `;
        })
        .join("")
    : '<div class="empty">No visible lanes for this filter.</div>';

  els.laneStrip.querySelectorAll("[data-lane]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedLane = button.dataset.lane;
      state.selectedTrace = null;
      state.traceFiles = [];
      state.traceEvents = [];
      state.traceWindow = null;
      render();
      fetchTraceFiles();
    });
  });

  const lane =
    state.selectedLane ? challenge.agents?.[state.selectedLane] : null;
  if (lane) {
    const meta = [
      state.selectedLane,
      lane.lifecycle || "unknown",
      `step ${lane.step_count || 0}`,
    ];
    if (lane.provider) {
      meta.push(`provider ${lane.provider}`);
    }
    if (lane.runtime_health) {
      meta.push(`health ${lane.runtime_health}`);
    }
    if (lane.heartbeat_age_sec !== undefined && lane.heartbeat_age_sec !== null) {
      meta.push(`heartbeat ${Number(lane.heartbeat_age_sec).toFixed(1)}s`);
    }
    els.selectedLaneMeta.textContent = meta.join(" · ");
  } else if (visibleLanes.length === 0 && lanes.length > 0 && state.hideErrorLanes) {
    els.selectedLaneMeta.textContent = "All lanes are hidden by the error filter.";
  } else {
    els.selectedLaneMeta.textContent = "Select a lane to inspect it.";
  }
  els.laneAdvisoryText.textContent = lane?.advisor_note || "-";
  els.laneFocus.innerHTML = lane
    ? `
        <div class="lane-focus-header">
          <div class="lane-focus-title">${escapeHtml(state.selectedLane)}</div>
          <div class="event-meta">
            <span class="${badgeClass(lane.lifecycle)}">${escapeHtml(lane.lifecycle || "unknown")}</span>
            <span class="event-tag">step ${escapeHtml(lane.step_count || 0)}</span>
            <span class="event-tag">${escapeHtml(lane.runtime_health || "unknown")}</span>
            <span class="event-tag">${escapeHtml(lane.provider || "provider?")}</span>
          </div>
        </div>
        <div class="lane-focus-detail">${escapeHtml(lane.activity || laneDetail(lane))}</div>
        ${
          lane.commentary_preview
            ? `<div class="lane-focus-subtle"><strong>Commentary:</strong> ${escapeHtml(lane.commentary_preview)}</div>`
            : ""
        }
        ${
          lane.session?.id
            ? `<div class="lane-focus-subtle"><strong>Session:</strong> ${escapeHtml(
                `${lane.session.kind || "session"} ${lane.session.id}`
              )}</div>`
            : ""
        }
        ${
          lane.last_reset_reason
            ? `<div class="lane-focus-subtle"><strong>Last reset:</strong> ${escapeHtml(lane.last_reset_reason)}</div>`
            : ""
        }
        ${
          lane.advisor_note
            ? `<div class="lane-focus-advisory"><strong>Lane advisory</strong>${escapeHtml(lane.advisor_note)}</div>`
            : ""
        }
        <div class="lane-focus-subtle">${escapeHtml(lane.findings || lane.last_exit_hint || "No additional lane note.")}</div>
      `
    : visibleLanes.length === 0 && lanes.length > 0 && state.hideErrorLanes
      ? '<div class="empty">All lanes are hidden by the error filter.</div>'
      : '<div class="empty">Select a lane to inspect current activity.</div>';
  els.advisoryHistory.innerHTML = state.advisoryHistory.length
    ? state.advisoryHistory
        .map((entry) => {
          const entryModel = entry.model_spec || entry.model_id || "";
          const selectedRow = shortModelName(state.selectedLane) === shortModelName(entryModel) ? "selected" : "";
          return `
            <li class="${selectedRow}">
              <div class="advisory-history-meta">
                <span class="event-tag">${escapeHtml(entry.model_id)}</span>
                <span>${escapeHtml(formatTime(entry.ts))}</span>
              </div>
              <div>${escapeHtml(entry.preview)}</div>
            </li>
          `;
        })
        .join("")
    : '<li class="empty">No advisory history yet.</li>';
}

function renderTraceSelector() {
  els.traceSelect.innerHTML = state.traceFiles.length
    ? state.traceFiles
        .map((traceName) => {
          const selected = traceName === state.selectedTrace ? "selected" : "";
          return `<option value="${escapeHtml(traceName)}" ${selected}>${escapeHtml(traceName)}</option>`;
        })
        .join("")
    : '<option value="">No trace files</option>';
  els.traceSelect.disabled = !state.traceFiles.length;
  els.loadOlderBtn.disabled = !state.traceWindow?.has_older;
}

function renderTraceTable() {
  const filtered = state.traceEvents.filter((event) => {
    if (state.traceTypeFilter && event.type !== state.traceTypeFilter) {
      return false;
    }
    if (!state.traceTextFilter) {
      return true;
    }
    const haystack = `${event.type} ${event.tool || ""} ${previewEvent(event)}`.toLowerCase();
    return haystack.includes(state.traceTextFilter.toLowerCase());
  });

  const types = Array.from(new Set(state.traceEvents.map((event) => event.type))).sort();
  const currentType = state.traceTypeFilter;
  els.traceTypeFilter.innerHTML =
    '<option value="">All</option>' +
    types
      .map((value) => {
        const selected = value === currentType ? "selected" : "";
        return `<option value="${escapeHtml(value)}" ${selected}>${escapeHtml(value)}</option>`;
      })
      .join("");

  if (!filtered.length) {
    els.traceTableBody.innerHTML =
      '<tr><td class="empty" colspan="3">No trace events for this filter.</td></tr>';
    return;
  }

  els.traceTableBody.innerHTML = filtered
    .slice()
    .reverse()
    .map(
      (event) => `
        <tr>
          <td>${escapeHtml(formatTime(event.ts))}</td>
          <td>
            <div class="trace-event-cell">
              <span class="${badgeClass(event.type)}">${escapeHtml(event.type)}</span>
              <div class="event-meta">
                ${event.tool ? `<span class="event-tag">${escapeHtml(event.tool)}</span>` : ""}
                ${event.step !== undefined ? `<span class="event-tag">step ${escapeHtml(event.step)}</span>` : ""}
                ${event.line_no !== undefined ? `<span class="event-tag">line ${escapeHtml(event.line_no)}</span>` : ""}
              </div>
            </div>
          </td>
          <td><span class="preview-main">${escapeHtml(previewEvent(event))}</span></td>
        </tr>
      `
    )
    .join("");
}

function render() {
  renderSummary();
  renderChallenges();
  renderSelectedChallenge();
  renderTraceSelector();
  renderTraceTable();
  renderRunningFor();
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || `${response.status} ${response.statusText}`);
  }
  return payload;
}

async function applyStatusSnapshot(snapshot) {
  state.snapshot = snapshot;
  state.snapshotReceived = true;
  syncSelections();
  render();
  els.updatedAt.textContent = new Date().toLocaleTimeString("ko-KR", {
    hour12: false,
  });
  await Promise.all([
    fetchTraceFiles({ preserveSelection: true, refreshTrace: false }),
    fetchAdvisoryHistory(),
  ]);
}

async function fetchStatus() {
  try {
    const snapshot = await fetchJson("/api/runtime/snapshot");
    await applyStatusSnapshot(snapshot);
  } catch (error) {
    if (!state.snapshotReceived) {
      markDisconnected("disconnected");
    }
    pushActivity(`status fetch failed: ${error.message}`, "error");
  }
}

async function fetchAdvisoryHistory() {
  if (!state.selectedChallenge) {
    state.advisoryHistory = [];
    render();
    return;
  }
  try {
    const payload = await fetchJson(
      `/api/runtime/advisories?${new URLSearchParams({ challenge_name: state.selectedChallenge, limit: "10" })}`
    );
    state.advisoryHistory = payload.entries || [];
    render();
  } catch (error) {
    pushActivity(`advisory history failed: ${error.message}`, "error");
  }
}

async function fetchTraceFiles({ preserveSelection = false, refreshTrace = true } = {}) {
  const selected = getSelectedChallengeData().data;
  if (!selected || !state.selectedChallenge) {
    state.traceFiles = [];
    state.selectedTrace = null;
    state.traceEvents = [];
    state.traceWindow = null;
    render();
    return;
  }
  const laneId = state.selectedLane || "";
  const key = `${state.selectedChallenge}:${laneId || "__challenge__"}`;
  state.loadingTraceFilesFor = key;
  const params = new URLSearchParams({ challenge_name: state.selectedChallenge });
  if (laneId) {
    params.set("lane_id", laneId);
  }
  try {
    const payload = await fetchJson(`/api/runtime/traces?${params}`);
    if (state.loadingTraceFilesFor !== key) {
      return;
    }
    state.traceFiles = payload.trace_files || [];
    const keepSelection = preserveSelection && state.traceFiles.includes(state.selectedTrace);
    const shouldRefreshTrace = refreshTrace || !keepSelection || !state.traceEvents.length;
    if (!keepSelection) {
      state.selectedTrace = state.traceFiles[0] || null;
      state.traceEvents = [];
      state.traceWindow = null;
    }
    render();
    if (state.selectedTrace && shouldRefreshTrace) {
      await fetchTrace();
    }
  } catch (error) {
    pushActivity(`trace list failed: ${error.message}`, "error");
  }
}

function stopStatusPolling() {
  if (state.statusPollHandle !== null) {
    clearInterval(state.statusPollHandle);
    state.statusPollHandle = null;
  }
}

function startStatusPolling({ immediate = false } = {}) {
  if (state.statusPollHandle !== null) {
    return;
  }
  state.usingRealtime = false;
  setSyncMode(`polling ${POLL_MS / 1000}s`);
  if (immediate) {
    fetchStatus();
  }
  state.statusPollHandle = setInterval(fetchStatus, POLL_MS);
}

function stopStatusStream() {
  if (state.statusStream) {
    state.statusStream.close();
    state.statusStream = null;
  }
}

function startStatusStream() {
  if (typeof window.EventSource === "undefined") {
    return false;
  }
  try {
    setSyncMode("connecting...");
    const source = new EventSource("/api/runtime/stream");
    state.statusStream = source;
    source.addEventListener("open", () => {
      stopStatusPolling();
      state.usingRealtime = true;
      setSyncMode("realtime");
    });
    source.addEventListener("snapshot", async (event) => {
      try {
        const snapshot = JSON.parse(event.data);
        await applyStatusSnapshot(snapshot);
      } catch (error) {
        markDisconnected("snapshot error");
        pushActivity(`snapshot apply failed: ${error.message}`, "error");
      }
    });
    source.onerror = () => {
      const wasRealtime = state.usingRealtime;
      stopStatusStream();
      state.usingRealtime = false;
      if (wasRealtime) {
        pushActivity("realtime stream disconnected; falling back to polling", "warn");
      }
      startStatusPolling({ immediate: true });
    };
    return true;
  } catch (error) {
    markDisconnected("stream failed");
    pushActivity(`realtime stream failed: ${error.message}`, "warn");
    return false;
  }
}

async function fetchTrace(cursor = null, { appendOlder = false } = {}) {
  if (!state.selectedChallenge || !state.selectedTrace) {
    return;
  }
  const params = new URLSearchParams({
    challenge_name: state.selectedChallenge,
    trace_name: state.selectedTrace,
    limit: "200",
  });
  if (state.selectedLane) {
    params.set("lane_id", state.selectedLane);
  }
  if (cursor !== null && cursor !== undefined) {
    params.set("cursor", String(cursor));
  }
  try {
    const payload = await fetchJson(`/api/runtime/trace-window?${params}`);
    state.traceWindow = payload;
    state.traceEvents = appendOlder
      ? [...payload.events, ...state.traceEvents]
      : payload.events;
    render();
  } catch (error) {
    pushActivity(`trace fetch failed: ${error.message}`, "error");
  }
}

async function postOperator(path, payload) {
  return fetchJson(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

async function handleCoordinatorMessage(event) {
  event.preventDefault();
  const message = els.msgInput.value.trim();
  if (!message) {
    return;
  }
  try {
    await postOperator("/api/runtime/coordinator-message", { message });
    pushCoordinatorActivity("message sent", "ok");
    els.msgInput.value = "";
  } catch (error) {
    pushCoordinatorActivity(`message failed: ${error.message}`, "error");
  }
}

async function handleLaneBump(event) {
  event.preventDefault();
  const insights = els.laneBumpInput.value.trim();
  if (!insights || !state.selectedChallenge || !state.selectedLane) {
    return;
  }
  try {
    await postOperator("/api/runtime/lane-bump", {
      challenge_name: state.selectedChallenge,
      lane_id: state.selectedLane,
      insights,
    });
    pushLaneActivity(state.selectedChallenge, state.selectedLane, "bumped", "ok");
    els.laneBumpInput.value = "";
  } catch (error) {
    pushLaneActivity(state.selectedChallenge, state.selectedLane, `bump failed: ${error.message}`, "error");
  }
}

async function handleChallengeBump(event) {
  event.preventDefault();
  const insights = els.challengeBumpInput.value.trim();
  const challenge = getSelectedChallengeData().data;
  if (!insights || !challenge || !state.selectedChallenge) {
    return;
  }
  try {
    const payload = await postOperator("/api/runtime/challenge-bump", {
      challenge_name: state.selectedChallenge,
      insights,
    });
    const results = Array.isArray(payload.results)
      ? payload.results.map((entry) => `${entry.lane_id}: ${entry.result}`).join(" | ")
      : "ok";
    pushChallengeActivity(state.selectedChallenge, `bumped (${results})`, "ok");
    els.challengeBumpInput.value = "";
  } catch (error) {
    pushChallengeActivity(state.selectedChallenge, `bump failed: ${error.message}`, "error");
  }
}

async function handleCandidateAction(event) {
  const approveButton = event.target.closest("[data-approve-flag]");
  if (approveButton && state.selectedChallenge) {
    const flag = String(approveButton.getAttribute("data-approve-flag") || "").trim();
    if (!flag) {
      return;
    }
    approveButton.disabled = true;
    try {
      const payload = await postOperator("/api/runtime/approve-candidate", {
        challenge_name: state.selectedChallenge,
        flag,
      });
      const actionLabel = candidateApprovalMode() === "local" ? "confirmed locally" : "confirmed manually";
      pushChallengeActivity(state.selectedChallenge, `candidate ${actionLabel} (${flag})`, "ok");
      await fetchStatus();
      render();
      pushServerActivity(payload?.result, "ok");
    } catch (error) {
      pushChallengeActivity(state.selectedChallenge, `candidate approval failed: ${error.message}`, "error");
    } finally {
      approveButton.disabled = false;
    }
    return;
  }

  const rejectButton = event.target.closest("[data-reject-flag]");
  if (!rejectButton || !state.selectedChallenge) {
    return;
  }
  const flag = String(rejectButton.getAttribute("data-reject-flag") || "").trim();
  if (!flag) {
    return;
  }
  rejectButton.disabled = true;
  try {
    const payload = await postOperator("/api/runtime/reject-candidate", {
      challenge_name: state.selectedChallenge,
      flag,
    });
    pushChallengeActivity(state.selectedChallenge, `candidate rejected (${flag})`, "ok");
    await fetchStatus();
    render();
    pushServerActivity(payload?.result, "ok");
  } catch (error) {
    pushChallengeActivity(state.selectedChallenge, `candidate rejection failed: ${error.message}`, "error");
  } finally {
    rejectButton.disabled = false;
  }
}

async function handleExternalSolve(event) {
  event.preventDefault();
  const challengeName = String(state.selectedChallenge || "").trim();
  const flag = String(els.externalSolveFlagInput?.value || "").trim();
  const note = String(els.externalSolveNoteInput?.value || "").trim();
  if (!challengeName) {
    pushChallengeActivity("", "external solve failed: select a challenge first", "error");
    return;
  }
  if (!flag) {
    pushChallengeActivity(challengeName, "external solve failed: flag is required", "error");
    return;
  }

  const submitButton = event.submitter instanceof HTMLButtonElement ? event.submitter : null;
  if (submitButton) {
    submitButton.disabled = true;
  }
  try {
    const payload = await postOperator("/api/runtime/mark-solved", {
      challenge_name: challengeName,
      flag,
      note,
    });
    pushChallengeActivity(challengeName, `marked solved externally (${flag})`, "ok");
    if (els.externalSolveFlagInput) {
      els.externalSolveFlagInput.value = "";
    }
    if (els.externalSolveNoteInput) {
      els.externalSolveNoteInput.value = "";
    }
    await fetchStatus();
    render();
    pushServerActivity(payload?.result, "ok");
  } catch (error) {
    pushChallengeActivity(challengeName, `external solve failed: ${error.message}`, "error");
  } finally {
    if (submitButton) {
      submitButton.disabled = false;
    }
  }
}

async function handleMaxChallenges(event) {
  event.preventDefault();
  const raw = String(els.maxChallengesInput?.value || "").trim();
  if (!raw) {
    return;
  }
  const value = Number(raw);
  if (!Number.isInteger(value) || value < 0) {
    pushSchedulerActivity("max active change failed: enter an integer >= 0", "error");
    return;
  }
  try {
    const payload = await postOperator("/api/runtime/set-max-challenges", {
      max_active: value,
    });
    pushSchedulerActivity(`max active set to ${value}`, "ok");
    pushServerActivity(payload?.result, "ok");
    await fetchStatus();
  } catch (error) {
    pushSchedulerActivity(`max active change failed: ${error.message}`, "error");
  }
}

async function handlePriorityWaiting(event) {
  event.preventDefault();
  const challengeName = String(state.selectedChallenge || "").trim();
  if (!challengeName) {
    pushChallengeActivity("", "priority waiting failed: select a challenge first", "error");
    return;
  }
  try {
    const payload = await postOperator("/api/runtime/set-challenge-priority", {
      challenge_name: challengeName,
      priority: true,
    });
    pushChallengeActivity(challengeName, "moved to priority waiting", "ok");
    pushServerActivity(payload?.result, "ok");
    await fetchStatus();
  } catch (error) {
    pushChallengeActivity(challengeName, `priority waiting failed: ${error.message}`, "error");
  }
}

async function handleNormalWaiting() {
  const challengeName = String(state.selectedChallenge || "").trim();
  if (!challengeName) {
    pushChallengeActivity("", "restore waiting failed: select a challenge first", "error");
    return;
  }
  try {
    const payload = await postOperator("/api/runtime/set-challenge-priority", {
      challenge_name: challengeName,
      priority: false,
    });
    pushChallengeActivity(challengeName, "restored to normal waiting", "ok");
    pushServerActivity(payload?.result, "ok");
    await fetchStatus();
  } catch (error) {
    pushChallengeActivity(challengeName, `restore waiting failed: ${error.message}`, "error");
  }
}

async function handleRestartChallenge() {
  const challengeName = String(state.selectedChallenge || "").trim();
  if (!challengeName) {
    pushChallengeActivity("", "resume failed: select a challenge first", "error");
    return;
  }
  try {
    const payload = await postOperator("/api/runtime/resume-challenge", {
      challenge_name: challengeName,
    });
    pushChallengeActivity(challengeName, "resume requested", "ok");
    pushServerActivity(payload?.result, "ok");
    await fetchStatus();
  } catch (error) {
    pushChallengeActivity(challengeName, `resume failed: ${error.message}`, "error");
  }
}

function bindEvents() {
  els.traceSelect.addEventListener("change", async () => {
    state.selectedTrace = els.traceSelect.value || null;
    state.traceEvents = [];
    state.traceWindow = null;
    render();
    await fetchTrace();
  });
  els.loadOlderBtn.addEventListener("click", async () => {
    if (state.traceWindow?.older_cursor === null || state.traceWindow?.older_cursor === undefined) {
      return;
    }
    await fetchTrace(state.traceWindow.older_cursor, { appendOlder: true });
  });
  els.traceTypeFilter.addEventListener("change", () => {
    state.traceTypeFilter = els.traceTypeFilter.value;
    renderTraceTable();
  });
  els.hideErrorLanes.addEventListener("change", () => {
    state.hideErrorLanes = Boolean(els.hideErrorLanes.checked);
    syncSelections();
    render();
    if (state.selectedChallenge && state.selectedLane) {
      fetchTraceFiles();
    }
  });
  els.traceTextFilter.addEventListener("input", () => {
    state.traceTextFilter = els.traceTextFilter.value.trim();
    renderTraceTable();
  });
  els.msgForm.addEventListener("submit", handleCoordinatorMessage);
  els.laneBumpForm.addEventListener("submit", handleLaneBump);
  els.challengeBumpForm.addEventListener("submit", handleChallengeBump);
  els.maxChallengesForm.addEventListener("submit", handleMaxChallenges);
  els.queuePriorityForm.addEventListener("submit", handlePriorityWaiting);
  els.normalQueueBtn.addEventListener("click", handleNormalWaiting);
  els.restartChallengeBtn.addEventListener("click", handleRestartChallenge);
  els.externalSolveForm.addEventListener("submit", handleExternalSolve);
  els.flagCandidatesList.addEventListener("click", handleCandidateAction);
}

async function main() {
  bindEvents();
  await fetchStatus();
  if (!startStatusStream()) {
    startStatusPolling();
  }
  renderRunningFor();
  setInterval(renderRunningFor, 1000);
  setInterval(() => {
    if (state.selectedTrace) {
      fetchTrace();
    }
  }, POLL_MS);
}

main();
