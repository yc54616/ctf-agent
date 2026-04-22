const POLL_MS = 2000;
const FINAL_STATES = new Set(["won", "flag_found", "cancelled"]);
const TERMINAL_CANDIDATE_STATUSES = new Set(["confirmed", "rejected"]);
const HIDE_ERROR_LANES_STORAGE_KEY = "ctf-agent:hide-error-lanes";
const state = {
  snapshot: null,
  snapshotReceived: false,
  maxChallengesDraft: "",
  maxChallengesDirty: false,
  selectedChallenge: null,
  selectedLane: null,
  selectedTrace: null,
  challengeConfig: null,
  challengeConfigFor: "",
  challengeConfigLoading: false,
  stageWorkflowDraft: "",
  stageWorkflowDirty: false,
  stageWorkflowDefinitions: [],
  stageWorkflowParseError: "",
  instanceProbeResult: null,
  instanceProbeFor: "",
  instanceProbeLoading: false,
  advisoryHistory: [],
  traceFiles: [],
  traceEvents: [],
  traceWindow: null,
  traceTypeFilter: "",
  traceTextFilter: "",
  hideErrorLanes: loadHideErrorLanesPreference(),
  loadingTraceFilesFor: "",
  usingRealtime: false,
  statusPollHandle: null,
  statusStream: null,
  seenUiAlertIds: [],
  browserNotificationPermission: "",
};

const els = {
  updatedAt: document.getElementById("updatedAt"),
  runningFor: document.getElementById("runningFor"),
  syncMode: document.getElementById("syncMode"),
  browserNotificationsBtn: document.getElementById("browserNotificationsBtn"),
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
  challengeConfigSummary: document.getElementById("challengeConfigSummary"),
  challengeConfigFacts: document.getElementById("challengeConfigFacts"),
  challengeConfigForm: document.getElementById("challengeConfigForm"),
  challengeConfigStageSelect: document.getElementById("challengeConfigStageSelect"),
  challengeConfigStageAddBtn: document.getElementById("challengeConfigStageAddBtn"),
  challengeConfigStageRemoveBtn: document.getElementById("challengeConfigStageRemoveBtn"),
  challengeConfigStageIdInput: document.getElementById("challengeConfigStageIdInput"),
  challengeConfigStageTitleInput: document.getElementById("challengeConfigStageTitleInput"),
  challengeConfigStageActionInput: document.getElementById("challengeConfigStageActionInput"),
  challengeConfigStageDescriptionInput: document.getElementById("challengeConfigStageDescriptionInput"),
  challengeConfigStageNotesInput: document.getElementById("challengeConfigStageNotesInput"),
  challengeConfigEndpointSelect: document.getElementById("challengeConfigEndpointSelect"),
  challengeConfigStageStatusInput: document.getElementById("challengeConfigStageStatusInput"),
  challengeConfigStageSummary: document.getElementById("challengeConfigStageSummary"),
  challengeConfigSchemeInput: document.getElementById("challengeConfigSchemeInput"),
  challengeConfigHostInput: document.getElementById("challengeConfigHostInput"),
  challengeConfigPortInput: document.getElementById("challengeConfigPortInput"),
  challengeConfigUrlInput: document.getElementById("challengeConfigUrlInput"),
  challengeConfigRawCommandInput: document.getElementById("challengeConfigRawCommandInput"),
  challengeConfigNotesInput: document.getElementById("challengeConfigNotesInput"),
  challengeConfigStagesInput: document.getElementById("challengeConfigStagesInput"),
  challengeConfigPriorityInput: document.getElementById("challengeConfigPriorityInput"),
  challengeConfigNoSubmitInput: document.getElementById("challengeConfigNoSubmitInput"),
  challengeConfigNeedsInstanceInput: document.getElementById("challengeConfigNeedsInstanceInput"),
  instanceCheckSummary: document.getElementById("instanceCheckSummary"),
  instanceCheckBtn: document.getElementById("instanceCheckBtn"),
  instanceCheckRestartBtn: document.getElementById("instanceCheckRestartBtn"),
  challengeConfigResetBtn: document.getElementById("challengeConfigResetBtn"),
};

function cloneJson(value) {
  return JSON.parse(JSON.stringify(value));
}

function getLocalStorage() {
  try {
    return typeof globalThis.localStorage === "object" && globalThis.localStorage
      ? globalThis.localStorage
      : null;
  } catch {
    return null;
  }
}

function loadHideErrorLanesPreference() {
  const storage = getLocalStorage();
  if (!storage || typeof storage.getItem !== "function") {
    return false;
  }
  try {
    return storage.getItem(HIDE_ERROR_LANES_STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

function saveHideErrorLanesPreference(value) {
  const storage = getLocalStorage();
  if (!storage || typeof storage.setItem !== "function") {
    return;
  }
  try {
    storage.setItem(HIDE_ERROR_LANES_STORAGE_KEY, value ? "true" : "false");
  } catch {
    // Ignore unavailable or quota-limited storage so the UI still works.
  }
}

if (els.hideErrorLanes) {
  els.hideErrorLanes.checked = state.hideErrorLanes;
}

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
    return `${days}d ${hours}h ${minutes}m ${seconds}s`;
  }
  if (hours > 0) {
    return `${hours}h ${minutes}m ${seconds}s`;
  }
  if (minutes > 0) {
    return `${minutes}m ${seconds}s`;
  }
  return `${seconds}s`;
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
  } else if (pendingReason === "restart_requested" || pendingReason === "resume_requested") {
    details.push("restart queued");
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

function challengeConfigSourceInfo(payload) {
  const effective = payload?.effective && typeof payload.effective === "object" ? payload.effective : {};
  const source = effective?.source && typeof effective.source === "object"
    ? effective.source
    : (payload?.source?.source && typeof payload.source.source === "object" ? payload.source.source : {});
  return source;
}

function prettyConfigState(value) {
  const normalized = String(value || "").trim().replaceAll("_", " ");
  if (!normalized) {
    return "-";
  }
  return normalized.replace(/\b\w/g, (char) => char.toUpperCase());
}

function challengeConfigCapabilitiesLabel(payload) {
  const sourceInfo = challengeConfigSourceInfo(payload);
  const capabilities = sourceInfo?.capabilities && typeof sourceInfo.capabilities === "object"
    ? sourceInfo.capabilities
    : {};
  const entries = [
    ["import", capabilities.import],
    ["poll", capabilities.poll_solved],
    ["submit", capabilities.submit_flag],
    ["pull", capabilities.pull_files],
  ].filter(([, value]) => String(value || "").trim());
  if (!entries.length) {
    return "-";
  }
  return entries
    .map(([label, value]) => `${label} ${String(value).replaceAll("_", "-")}`)
    .join(" · ");
}

function challengeConfigRuntimeLabel(payload) {
  const runtimeMode = String(payload?.runtime_mode || "").trim();
  const automaticSubmit = Boolean(payload?.automatic_submit);
  if (!runtimeMode) {
    return "-";
  }
  const details = [prettyConfigState(runtimeMode)];
  details.push(automaticSubmit ? "auto submit on" : "auto submit off");
  return details.join(" · ");
}

function effectiveConnectionLabel(payload) {
  const effective = payload?.effective && typeof payload.effective === "object" ? payload.effective : {};
  const connectionInfo = String(effective.connection_info || "").trim();
  if (connectionInfo) {
    return connectionInfo;
  }
  const connection = effective.connection && typeof effective.connection === "object" ? effective.connection : {};
  const url = String(connection.url || "").trim();
  if (url) {
    return url;
  }
  const rawCommand = String(connection.raw_command || "").trim();
  if (rawCommand) {
    return rawCommand;
  }
  const host = String(connection.host || "").trim();
  const port = String(connection.port || "").trim();
  if (host && port) {
    return `${host}:${port}`;
  }
  return "-";
}

function challengeNeedsInstance(payload) {
  const effective = payload?.effective && typeof payload.effective === "object" ? payload.effective : {};
  return Boolean(effective.needs_instance);
}

function serverInstanceStages(payload) {
  const effective = payload?.effective && typeof payload.effective === "object" ? payload.effective : {};
  if (!Array.isArray(effective.instance_stages)) {
    return [];
  }
  return effective.instance_stages.filter((stage) => stage && typeof stage === "object");
}

function editableStageDefinitions(payload) {
  return serverInstanceStages(payload).map((stage) => {
    const definition = {
      id: String(stage?.id || "").trim(),
    };
    ["title", "description", "manual_action", "notes"].forEach((field) => {
      const value = String(stage?.[field] || "").trim();
      if (value) {
        definition[field] = value;
      }
    });
    const stageConnection =
      stage?.stage_connection && typeof stage.stage_connection === "object"
        ? stage.stage_connection
        : (stage?.connection && typeof stage.connection === "object" ? stage.connection : {});
    if (Object.keys(stageConnection).length) {
      definition.connection = JSON.parse(JSON.stringify(stageConnection));
    }
    const endpoints = Array.isArray(stage?.endpoints)
      ? stage.endpoints
          .filter((endpoint) => endpoint && typeof endpoint === "object")
          .map((endpoint) => {
            const result = {
              id: String(endpoint?.id || "").trim(),
            };
            ["title", "description"].forEach((field) => {
              const value = String(endpoint?.[field] || "").trim();
              if (value) {
                result[field] = value;
              }
            });
            const connection = endpoint?.connection && typeof endpoint.connection === "object" ? endpoint.connection : {};
            if (Object.keys(connection).length) {
              result.connection = JSON.parse(JSON.stringify(connection));
            }
            return result;
          })
          .filter((endpoint) => endpoint.id)
      : [];
    if (endpoints.length) {
      definition.endpoints = endpoints;
    }
    return definition;
  }).filter((stage) => stage.id);
}

function parseStageWorkflowText(rawText) {
  const normalizedText = String(rawText || "").trim();
  if (!normalizedText) {
    return [];
  }
  const parsed = JSON.parse(normalizedText);
  if (!Array.isArray(parsed)) {
    throw new Error("stage workflow JSON must be an array");
  }
  const normalized = parsed
    .map((stage) => {
      if (!stage || typeof stage !== "object") {
        return null;
      }
      const stageId = String(stage.id || "").trim();
      if (!stageId) {
        return null;
      }
      const result = { id: stageId };
      ["title", "description", "manual_action", "notes"].forEach((field) => {
        const value = String(stage[field] || "").trim();
        if (value) {
          result[field] = value;
        }
      });
      if (stage.connection && typeof stage.connection === "object") {
        result.connection = {
          scheme: String(stage.connection.scheme || "").trim(),
          host: String(stage.connection.host || "").trim(),
          port: stage.connection.port === undefined || stage.connection.port === null ? undefined : Number(stage.connection.port),
          url: String(stage.connection.url || "").trim(),
          raw_command: String(stage.connection.raw_command || "").trim(),
        };
      }
      if (Array.isArray(stage.endpoints)) {
        result.endpoints = stage.endpoints
          .map((endpoint) => {
            if (!endpoint || typeof endpoint !== "object") {
              return null;
            }
            const endpointId = String(endpoint.id || "").trim();
            if (!endpointId) {
              return null;
            }
            const normalizedEndpoint = { id: endpointId };
            ["title", "description"].forEach((field) => {
              const value = String(endpoint[field] || "").trim();
              if (value) {
                normalizedEndpoint[field] = value;
              }
            });
            if (endpoint.connection && typeof endpoint.connection === "object") {
              normalizedEndpoint.connection = {
                scheme: String(endpoint.connection.scheme || "").trim(),
                host: String(endpoint.connection.host || "").trim(),
                port:
                  endpoint.connection.port === undefined || endpoint.connection.port === null
                    ? undefined
                    : Number(endpoint.connection.port),
                url: String(endpoint.connection.url || "").trim(),
                raw_command: String(endpoint.connection.raw_command || "").trim(),
              };
            }
            return normalizedEndpoint;
          })
          .filter(Boolean);
      }
      return result;
    })
    .filter(Boolean);
  return normalized;
}

function stageWorkflowText(payload) {
  const definitions = editableStageDefinitions(payload);
  return definitions.length ? JSON.stringify(definitions, null, 2) : "";
}

function workflowStageDefinitions() {
  const rawText = String(els.challengeConfigStagesInput?.value || state.stageWorkflowDraft || "").trim();
  if (!rawText) {
    return [];
  }
  return parseStageWorkflowText(rawText);
}

function stageDefinitionsForUi(payload) {
  if (state.challengeConfigFor === state.selectedChallenge && Array.isArray(state.stageWorkflowDefinitions)) {
    if (state.stageWorkflowDefinitions.length) {
      return cloneJson(state.stageWorkflowDefinitions);
    }
    if (state.stageWorkflowDirty && !String(state.stageWorkflowDraft || "").trim()) {
      return [];
    }
  }
  return editableStageDefinitions(payload);
}

function effectiveInstanceStages(payload) {
  const serverStages = serverInstanceStages(payload);
  const definitions = stageDefinitionsForUi(payload);
  if (!definitions.length) {
    return serverStages;
  }
  const serverById = new Map(
    serverStages.map((stage) => [String(stage?.id || "").trim(), stage])
  );
  return definitions.map((definition) => {
    const stageId = String(definition?.id || "").trim();
    const serverStage = serverById.get(stageId);
    const merged = serverStage && typeof serverStage === "object" ? cloneJson(serverStage) : {};
    merged.id = stageId;
    ["title", "description", "manual_action", "notes"].forEach((field) => {
      const value = String(definition?.[field] || "").trim();
      if (value) {
        merged[field] = value;
      } else {
        delete merged[field];
      }
    });
    if (definition?.connection && typeof definition.connection === "object") {
      merged.connection = cloneJson(definition.connection);
      merged.stage_connection = cloneJson(definition.connection);
    } else if (!merged.stage_connection && merged.connection) {
      merged.stage_connection = cloneJson(merged.connection);
    }
    if (Array.isArray(definition?.endpoints)) {
      const serverEndpoints = Array.isArray(serverStage?.endpoints) ? serverStage.endpoints : [];
      const serverEndpointById = new Map(
        serverEndpoints.map((endpoint) => [String(endpoint?.id || "").trim(), endpoint])
      );
      merged.endpoints = definition.endpoints
        .map((endpointDefinition) => {
          const endpointId = String(endpointDefinition?.id || "").trim();
          if (!endpointId) {
            return null;
          }
          const serverEndpoint = serverEndpointById.get(endpointId);
          const endpoint =
            serverEndpoint && typeof serverEndpoint === "object" ? cloneJson(serverEndpoint) : {};
          endpoint.id = endpointId;
          ["title", "description"].forEach((field) => {
            const value = String(endpointDefinition?.[field] || "").trim();
            if (value) {
              endpoint[field] = value;
            } else {
              delete endpoint[field];
            }
          });
          if (endpointDefinition?.connection && typeof endpointDefinition.connection === "object") {
            endpoint.connection = cloneJson(endpointDefinition.connection);
          }
          return endpoint;
        })
        .filter(Boolean);
    } else if (!Array.isArray(merged.endpoints)) {
      merged.endpoints = [];
    }
    return merged;
  });
}

function syncStageWorkflowText(definitions) {
  const normalized = Array.isArray(definitions)
    ? parseStageWorkflowText(JSON.stringify(definitions.filter((stage) => stage && stage.id)))
    : [];
  state.stageWorkflowDefinitions = cloneJson(normalized);
  state.stageWorkflowDraft = normalized.length ? JSON.stringify(normalized, null, 2) : "";
  state.stageWorkflowDirty = true;
  state.stageWorkflowParseError = "";
  if (els.challengeConfigStagesInput) {
    els.challengeConfigStagesInput.value = state.stageWorkflowDraft;
    delete els.challengeConfigStagesInput.dataset.state;
  }
}

function nextGeneratedStageId(definitions) {
  const used = new Set(
    (Array.isArray(definitions) ? definitions : [])
      .map((stage) => String(stage?.id || "").trim())
      .filter(Boolean)
  );
  let index = used.size + 1;
  while (used.has(`stage_${index}`)) {
    index += 1;
  }
  return `stage_${index}`;
}

function ensureUniqueStageId(rawId, definitions, currentIndex = -1) {
  const baseId = String(rawId || "").trim() || nextGeneratedStageId(definitions);
  let candidate = baseId;
  let suffix = 2;
  while (
    definitions.some(
      (stage, index) => index !== currentIndex && String(stage?.id || "").trim() === candidate
    )
  ) {
    candidate = `${baseId}_${suffix}`;
    suffix += 1;
  }
  return candidate;
}

function updateSelectedWorkflowStage(mutator) {
  const challengePayload =
    state.challengeConfig && state.challengeConfigFor === state.selectedChallenge
      ? state.challengeConfig
      : { effective: {} };
  let definitions;
  try {
    definitions = workflowStageDefinitions().map((stage) => cloneJson(stage));
  } catch (_error) {
    definitions = Array.isArray(state.stageWorkflowDefinitions)
      ? state.stageWorkflowDefinitions.map((stage) => cloneJson(stage))
      : [];
  }
  const selectedStageId = String(els.challengeConfigStageSelect?.value || "").trim();
  const selectedEndpointId = String(els.challengeConfigEndpointSelect?.value || "").trim();
  const index = definitions.findIndex((stage) => String(stage?.id || "").trim() === selectedStageId);
  if (index < 0) {
    return;
  }
  const nextSelectedStageId = mutator(definitions, index) || String(definitions[index]?.id || "").trim();
  syncStageWorkflowText(definitions);
  renderChallengeConfigStageFields(challengePayload, nextSelectedStageId, selectedEndpointId);
  renderInstanceCheckSummary(challengePayload);
}

function effectiveCurrentStageId(payload) {
  const effective = payload?.effective && typeof payload.effective === "object" ? payload.effective : {};
  return String(effective.current_stage || "").trim();
}

function effectiveInstanceStageEntry(payload, stageId = "") {
  const stages = effectiveInstanceStages(payload);
  const targetStageId = String(stageId || effectiveCurrentStageId(payload) || "").trim();
  return stages.find((stage) => String(stage.id || "").trim() === targetStageId) || null;
}

function effectiveCurrentEndpointId(payload, stageId = "") {
  const stage = effectiveInstanceStageEntry(payload, stageId);
  return String(stage?.current_endpoint || "").trim();
}

function effectiveEndpointEntries(stage) {
  if (!Array.isArray(stage?.endpoints)) {
    return [];
  }
  return stage.endpoints.filter((endpoint) => endpoint && typeof endpoint === "object");
}

function effectiveInstanceEndpointEntry(payload, stageId = "", endpointId = "") {
  const stage = effectiveInstanceStageEntry(payload, stageId);
  const endpoints = effectiveEndpointEntries(stage);
  const targetEndpointId = String(endpointId || stage?.current_endpoint || "").trim();
  return endpoints.find((endpoint) => String(endpoint.id || "").trim() === targetEndpointId) || null;
}

function connectionFieldsFromSource(sourceConnection = {}) {
  const connection = sourceConnection && typeof sourceConnection === "object" ? sourceConnection : {};
  return {
    scheme: String(connection.scheme || "").trim(),
    host: String(connection.host || "").trim(),
    port: connection.port === undefined || connection.port === null ? "" : String(connection.port),
    url: String(connection.url || "").trim(),
    raw_command: String(connection.raw_command || "").trim(),
  };
}

function connectionFieldsFromSelection(stage, endpoint, fallbackConnection = {}) {
  if (endpoint) {
    return connectionFieldsFromSource(endpoint.connection);
  }
  if (stage) {
    const stageConnection =
      stage?.stage_connection && typeof stage.stage_connection === "object"
        ? stage.stage_connection
        : (stage?.connection && typeof stage.connection === "object" ? stage.connection : {});
    return connectionFieldsFromSource(stageConnection);
  }
  return connectionFieldsFromSource(fallbackConnection);
}

function challengeInstanceWorkflowLabel(payload) {
  const needsInstance = challengeNeedsInstance(payload);
  const connectionLabel = effectiveConnectionLabel(payload);
  const currentStage = effectiveInstanceStageEntry(payload);
  const stageLabel = currentStage
    ? `stage ${String(currentStage.title || currentStage.id || "").trim()}`
    : "";
  if (needsInstance && connectionLabel !== "-") {
    return [stageLabel, "manual deploy required", "connection saved"].filter(Boolean).join(" · ");
  }
  if (needsInstance) {
    return [stageLabel, "manual deploy required", "waiting for connection details"].filter(Boolean).join(" · ");
  }
  return "not required";
}

function setChallengeConfigEnabled(enabled) {
  [
    els.challengeConfigStageSelect,
    els.challengeConfigStageAddBtn,
    els.challengeConfigStageRemoveBtn,
    els.challengeConfigStageIdInput,
    els.challengeConfigStageTitleInput,
    els.challengeConfigStageActionInput,
    els.challengeConfigStageDescriptionInput,
    els.challengeConfigStageNotesInput,
    els.challengeConfigEndpointSelect,
    els.challengeConfigStageStatusInput,
    els.challengeConfigSchemeInput,
    els.challengeConfigHostInput,
    els.challengeConfigPortInput,
    els.challengeConfigUrlInput,
    els.challengeConfigRawCommandInput,
    els.challengeConfigNotesInput,
    els.challengeConfigStagesInput,
    els.challengeConfigPriorityInput,
    els.challengeConfigNoSubmitInput,
    els.challengeConfigNeedsInstanceInput,
    els.instanceCheckBtn,
    els.instanceCheckRestartBtn,
    els.challengeConfigResetBtn,
  ].forEach((input) => {
    if (input) {
      input.disabled = !enabled;
    }
  });
}

function renderChallengeConfigStageFields(payload, preferredStageId = "", preferredEndpointId = "") {
  const effective = payload?.effective && typeof payload.effective === "object" ? payload.effective : {};
  const fallbackConnection = effective?.connection && typeof effective.connection === "object" ? effective.connection : {};
  const stages = effectiveInstanceStages(payload);
  const currentSelectedStageId = String(els.challengeConfigStageSelect?.value || "").trim();
  const defaultStageId =
    preferredStageId ||
    currentSelectedStageId ||
    effectiveCurrentStageId(payload) ||
    String(stages[0]?.id || "").trim();
  const selectedStage = effectiveInstanceStageEntry(payload, defaultStageId);
  const endpoints = effectiveEndpointEntries(selectedStage);
  const defaultEndpointId = preferredEndpointId || effectiveCurrentEndpointId(payload, defaultStageId);
  const selectedEndpoint = effectiveInstanceEndpointEntry(payload, defaultStageId, defaultEndpointId);

  if (els.challengeConfigStageSelect) {
    const options = ['<option value="">No stage workflow</option>'];
    stages.forEach((stage) => {
      const stageId = String(stage.id || "").trim();
      const label = String(stage.title || stageId || "").trim() || stageId;
      const status = String(stage.status || "").trim();
      const selected = selectedStage && String(selectedStage.id || "").trim() === stageId ? " selected" : "";
      options.push(
        `<option value="${escapeAttr(stageId)}"${selected}>${escapeHtml(status ? `${label} (${status})` : label)}</option>`
      );
    });
    els.challengeConfigStageSelect.innerHTML = options.join("");
    els.challengeConfigStageSelect.disabled = !stages.length;
  }

  if (els.challengeConfigStageIdInput) {
    els.challengeConfigStageIdInput.value = String(selectedStage?.id || "").trim();
    els.challengeConfigStageIdInput.disabled = !stages.length;
  }
  if (els.challengeConfigStageTitleInput) {
    els.challengeConfigStageTitleInput.value = String(selectedStage?.title || "").trim();
    els.challengeConfigStageTitleInput.disabled = !stages.length;
  }
  if (els.challengeConfigStageActionInput) {
    els.challengeConfigStageActionInput.value = String(selectedStage?.manual_action || "").trim();
    els.challengeConfigStageActionInput.disabled = !stages.length;
  }
  if (els.challengeConfigStageDescriptionInput) {
    els.challengeConfigStageDescriptionInput.value = String(selectedStage?.description || "").trim();
    els.challengeConfigStageDescriptionInput.disabled = !stages.length;
  }
  if (els.challengeConfigStageNotesInput) {
    els.challengeConfigStageNotesInput.value = String(selectedStage?.notes || "").trim();
    els.challengeConfigStageNotesInput.disabled = !stages.length;
  }
  if (els.challengeConfigStageRemoveBtn) {
    els.challengeConfigStageRemoveBtn.disabled = !stages.length || !selectedStage;
  }

  if (els.challengeConfigEndpointSelect) {
    const options = ['<option value="">Stage connection</option>'];
    endpoints.forEach((endpoint) => {
      const endpointId = String(endpoint.id || "").trim();
      const label = String(endpoint.title || endpointId || "").trim() || endpointId;
      const selected = selectedEndpoint && String(selectedEndpoint.id || "").trim() === endpointId ? " selected" : "";
      options.push(`<option value="${escapeAttr(endpointId)}"${selected}>${escapeHtml(label)}</option>`);
    });
    els.challengeConfigEndpointSelect.innerHTML = options.join("");
    els.challengeConfigEndpointSelect.disabled = !selectedStage || !endpoints.length;
  }

  if (els.challengeConfigStageStatusInput) {
    const stageStatus = String(selectedStage?.status || "pending").trim().toLowerCase() || "pending";
    els.challengeConfigStageStatusInput.value = stageStatus;
    els.challengeConfigStageStatusInput.disabled = !stages.length;
  }

  const selectedConnection = connectionFieldsFromSelection(selectedStage, selectedEndpoint, fallbackConnection);
  if (els.challengeConfigSchemeInput) {
    els.challengeConfigSchemeInput.value = selectedConnection.scheme;
  }
  if (els.challengeConfigHostInput) {
    els.challengeConfigHostInput.value = selectedConnection.host;
  }
  if (els.challengeConfigPortInput) {
    els.challengeConfigPortInput.value = selectedConnection.port;
  }
  if (els.challengeConfigUrlInput) {
    els.challengeConfigUrlInput.value = selectedConnection.url;
  }
  if (els.challengeConfigRawCommandInput) {
    els.challengeConfigRawCommandInput.value = selectedConnection.raw_command;
  }

  if (els.challengeConfigStageSummary) {
    if (state.stageWorkflowParseError) {
      els.challengeConfigStageSummary.textContent = `Workflow JSON is invalid: ${state.stageWorkflowParseError}`;
      els.challengeConfigStageSummary.dataset.state = "error";
    } else if (!stages.length) {
      els.challengeConfigStageSummary.textContent = "No instance stages configured for this challenge.";
      delete els.challengeConfigStageSummary.dataset.state;
    } else if (!selectedStage) {
      els.challengeConfigStageSummary.textContent = "Select a stage to edit its current runtime connection details.";
      delete els.challengeConfigStageSummary.dataset.state;
    } else {
      const stageBits = [
        String(selectedStage.title || selectedStage.id || "").trim(),
        String(selectedStage.status || "").trim() ? `status ${String(selectedStage.status || "").trim()}` : "",
        selectedEndpoint
          ? `endpoint ${String(selectedEndpoint.title || selectedEndpoint.id || "").trim()}`
          : (endpoints.length ? `endpoints ${endpoints.length}` : ""),
        String(selectedStage.manual_action || "").trim() ? `action ${String(selectedStage.manual_action || "").trim()}` : "",
        String(selectedStage.description || "").trim(),
        String(selectedStage.notes || "").trim(),
      ].filter(Boolean);
      els.challengeConfigStageSummary.textContent = stageBits.join(" · ");
      delete els.challengeConfigStageSummary.dataset.state;
    }
  }
}

function populateChallengeConfigForm(payload, preferredStageId = "") {
  const effective = payload?.effective && typeof payload.effective === "object" ? payload.effective : {};
  if (!state.stageWorkflowDirty) {
    state.stageWorkflowDefinitions = editableStageDefinitions(payload);
    state.stageWorkflowDraft = stageWorkflowText(payload);
    state.stageWorkflowParseError = "";
  }
  renderChallengeConfigStageFields(payload, preferredStageId);
  if (els.challengeConfigStagesInput) {
    const nextWorkflowText = state.stageWorkflowDraft;
    const inputFocused = document.activeElement === els.challengeConfigStagesInput;
    if (!state.stageWorkflowDirty && !inputFocused) {
      els.challengeConfigStagesInput.value = nextWorkflowText;
    }
    if (!state.stageWorkflowParseError) {
      delete els.challengeConfigStagesInput.dataset.state;
    }
  }
  if (els.challengeConfigNotesInput) {
    els.challengeConfigNotesInput.value = String(effective.notes || "");
  }
  if (els.challengeConfigPriorityInput) {
    els.challengeConfigPriorityInput.checked = Boolean(effective.priority);
  }
  if (els.challengeConfigNoSubmitInput) {
    els.challengeConfigNoSubmitInput.checked = Boolean(effective.no_submit);
  }
  if (els.challengeConfigNeedsInstanceInput) {
    els.challengeConfigNeedsInstanceInput.checked = Boolean(effective.needs_instance);
  }
}

function clearChallengeConfigForm() {
  state.stageWorkflowDraft = "";
  state.stageWorkflowDirty = false;
  state.stageWorkflowDefinitions = [];
  state.stageWorkflowParseError = "";
  populateChallengeConfigForm({ effective: {} });
}

function renderInstanceCheckSummary(payload) {
  if (!els.instanceCheckSummary) {
    return;
  }
  if (!state.selectedChallenge || !payload || state.challengeConfigFor !== state.selectedChallenge) {
    els.instanceCheckSummary.textContent = "Use Check instance to verify the current connection before restarting lanes.";
    els.instanceCheckSummary.dataset.state = "";
    return;
  }
  const challengeName = String(state.selectedChallenge || "").trim();
  const sameChallenge = state.instanceProbeFor === challengeName;
  if (sameChallenge && state.instanceProbeLoading) {
    els.instanceCheckSummary.textContent = "Checking the current challenge connection...";
    els.instanceCheckSummary.dataset.state = "checking";
    return;
  }
  if (sameChallenge && state.instanceProbeResult && typeof state.instanceProbeResult === "object") {
    const probe =
      state.instanceProbeResult.probe && typeof state.instanceProbeResult.probe === "object"
        ? state.instanceProbeResult.probe
        : {};
    const detail = String(probe.detail || probe.error || "").trim();
    const stageTitle = String(probe.current_stage_title || probe.current_stage || "").trim();
    const endpointTitle = String(probe.current_stage_endpoint_title || probe.current_stage_endpoint || "").trim();
    const scopeTitle = [stageTitle, endpointTitle].filter(Boolean).join(" / ");
    const detailWithStage = scopeTitle ? `${scopeTitle}: ${detail}` : detail;
    if (state.instanceProbeResult.ready) {
      const restartResult = String(state.instanceProbeResult.restart_result || "").trim();
      els.instanceCheckSummary.textContent = restartResult ? `${detailWithStage} · ${restartResult}` : detailWithStage;
      els.instanceCheckSummary.dataset.state = "ready";
      return;
    }
    els.instanceCheckSummary.textContent = detailWithStage || "Current challenge connection is not ready yet.";
    els.instanceCheckSummary.dataset.state = "warn";
    return;
  }
  if (challengeNeedsInstance(payload)) {
    els.instanceCheckSummary.textContent =
      "Deploy or refresh the challenge instance, save the new host/port/url if needed, then use Check instance.";
    els.instanceCheckSummary.dataset.state = "warn";
    return;
  }
  els.instanceCheckSummary.textContent = "Use Check instance to verify the current connection before restarting lanes.";
  els.instanceCheckSummary.dataset.state = "";
}

function renderChallengeConfigPanel() {
  if (!els.challengeConfigSummary || !els.challengeConfigFacts) {
    return;
  }
  if (!state.selectedChallenge) {
    els.challengeConfigSummary.textContent = "Select a challenge to inspect imported metadata and overrides.";
    els.challengeConfigFacts.innerHTML = "";
    clearChallengeConfigForm();
    setChallengeConfigEnabled(false);
    renderInstanceCheckSummary(null);
    return;
  }
  if (state.challengeConfigLoading) {
    els.challengeConfigSummary.textContent = "Loading challenge config...";
    els.challengeConfigFacts.innerHTML = "";
    setChallengeConfigEnabled(false);
    renderInstanceCheckSummary(null);
    return;
  }
  if (!state.challengeConfig || state.challengeConfigFor !== state.selectedChallenge) {
    els.challengeConfigSummary.textContent = "Challenge config is unavailable for this selection.";
    els.challengeConfigFacts.innerHTML = "";
    clearChallengeConfigForm();
    setChallengeConfigEnabled(false);
    renderInstanceCheckSummary(null);
    return;
  }

  const payload = state.challengeConfig;
  const sourceInfo = challengeConfigSourceInfo(payload);
  const competition = sourceInfo?.competition && typeof sourceInfo.competition === "object" ? sourceInfo.competition : {};
  const status = sourceInfo?.status && typeof sourceInfo.status === "object" ? sourceInfo.status : {};
  const currentStage = effectiveInstanceStageEntry(payload);
  const currentEndpoint = effectiveInstanceEndpointEntry(payload);
  const instanceStages = effectiveInstanceStages(payload);
  const endpointCount = instanceStages.reduce(
    (total, stage) => total + (Array.isArray(stage?.endpoints) ? stage.endpoints.length : 0),
    0
  );
  const facts = [
    {
      label: "Source",
      value: [
        sourceInfo.platform ? `platform ${sourceInfo.platform}` : "",
        competition.title ? `competition ${competition.title}` : "",
      ].filter(Boolean).join(" · ") || "-",
    },
    {
      label: "Source URL",
      value: String(sourceInfo.challenge_url || competition.url || "").trim() || "-",
    },
    {
      label: "Import Status",
      value: [
        `solved ${Boolean(status.solved) ? "yes" : "no"}`,
        `writeup ${Boolean(status.writeup_submitted) ? "yes" : "no"}`,
        `override ${payload.override_present ? "present" : "none"}`,
      ].join(" · "),
    },
    {
      label: "Runtime Mode",
      value: challengeConfigRuntimeLabel(payload),
    },
    {
      label: "Capabilities",
      value: challengeConfigCapabilitiesLabel(payload),
    },
    {
      label: "Effective Connection",
      value: effectiveConnectionLabel(payload),
    },
    {
      label: "Current Stage",
      value: currentStage
        ? [
            String(currentStage.title || currentStage.id || "").trim(),
            String(currentStage.status || "").trim() || "",
            currentEndpoint ? `endpoint ${String(currentEndpoint.title || currentEndpoint.id || "").trim()}` : "",
          ].filter(Boolean).join(" · ")
        : (instanceStages.length ? "configured, no current stage selected" : "-"),
    },
    {
      label: "Instance Workflow",
      value: challengeInstanceWorkflowLabel(payload),
    },
    {
      label: "Stage Count",
      value: instanceStages.length
        ? `${instanceStages.length} stage${instanceStages.length === 1 ? "" : "s"} · ${endpointCount} endpoint${endpointCount === 1 ? "" : "s"}`
        : "-",
    },
  ];

  els.challengeConfigSummary.textContent = `Editing ${state.selectedChallenge}`;
  els.challengeConfigFacts.innerHTML = facts
    .map(
      (fact) => `
        <div class="config-fact">
          <strong>${escapeHtml(fact.label)}</strong>
          <div>${escapeHtml(fact.value)}</div>
        </div>
      `
    )
    .join("");
  setChallengeConfigEnabled(true);
  if (els.instanceCheckBtn) {
    els.instanceCheckBtn.disabled = state.instanceProbeLoading;
  }
  if (els.instanceCheckRestartBtn) {
    els.instanceCheckRestartBtn.disabled = state.instanceProbeLoading;
  }
  renderInstanceCheckSummary(payload);
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
    const snapshotValue = String(Number(state.snapshot?.max_concurrent_challenges ?? 0));
    const inputFocused = document.activeElement === els.maxChallengesInput;
    if (!state.maxChallengesDirty && !inputFocused) {
      els.maxChallengesInput.value = snapshotValue;
      state.maxChallengesDraft = snapshotValue;
    }
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
    els.restartChallengeBtn.textContent = "Restart after stop";
  } else if (isPendingPriority) {
    els.restartChallengeBtn.textContent = "Restore and restart";
  } else {
    els.restartChallengeBtn.textContent = "Restart from saved notes";
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

function browserNotificationPermission() {
  if (typeof Notification === "undefined") {
    return "unsupported";
  }
  return String(Notification.permission || "default");
}

function renderBrowserNotificationsControl() {
  if (!els.browserNotificationsBtn) {
    return;
  }
  const permission = browserNotificationPermission();
  state.browserNotificationPermission = permission;
  if (permission === "unsupported") {
    els.browserNotificationsBtn.textContent = "Browser alerts unsupported";
    els.browserNotificationsBtn.disabled = true;
    return;
  }
  if (permission === "granted") {
    els.browserNotificationsBtn.textContent = "Browser alerts enabled";
    els.browserNotificationsBtn.disabled = false;
    return;
  }
  if (permission === "denied") {
    els.browserNotificationsBtn.textContent = "Browser alerts blocked";
    els.browserNotificationsBtn.disabled = true;
    return;
  }
  els.browserNotificationsBtn.textContent = "Enable browser alerts";
  els.browserNotificationsBtn.disabled = false;
}

function sendBrowserNotification(alert) {
  if (browserNotificationPermission() !== "granted" || typeof Notification === "undefined") {
    return;
  }
  const id = String(alert?.id || "").trim();
  const message = String(alert?.message || "").trim();
  if (!message) {
    return;
  }
  const challengeName = String(alert?.challenge_name || "").trim();
  const laneId = String(alert?.lane_id || "").trim();
  const titleBits = ["CTF Agent"];
  if (challengeName) {
    titleBits.push(challengeName);
  }
  if (laneId) {
    titleBits.push(shortModelName(laneId));
  }
  let notification;
  try {
    notification = new Notification(titleBits.join(" · "), {
      body: message,
      tag: id || undefined,
      renotify: true,
    });
  } catch (error) {
    pushActivity(`browser notification failed: ${error.message}`, "error");
    return;
  }
  if (typeof notification.close === "function") {
    setTimeout(() => notification.close(), 8000);
  }
  notification.onclick = () => {
    try {
      window.focus?.();
    } catch (error) {
      console.warn("browser notification focus failed", error);
    }
    if (typeof notification.close === "function") {
      notification.close();
    }
  };
}

async function handleBrowserNotificationsClick() {
  const permission = browserNotificationPermission();
  if (permission === "unsupported") {
    pushActivity("browser notifications are unsupported in this browser", "warn");
    renderBrowserNotificationsControl();
    return;
  }
  if (permission === "granted") {
    pushActivity("browser notifications already enabled", "ok");
    renderBrowserNotificationsControl();
    return;
  }
  try {
    const updated = await Notification.requestPermission();
    state.browserNotificationPermission = String(updated || "default");
    renderBrowserNotificationsControl();
    if (updated === "granted") {
      pushActivity("browser notifications enabled", "ok");
      return;
    }
    if (updated === "denied") {
      pushActivity("browser notifications blocked by browser settings", "warn");
      return;
    }
    pushActivity("browser notifications left disabled", "warn");
  } catch (error) {
    pushActivity(`browser notifications failed: ${error.message}`, "error");
    renderBrowserNotificationsControl();
  }
}

function syncSnapshotAlerts(snapshot) {
  const alerts = Array.isArray(snapshot?.ui_alerts) ? snapshot.ui_alerts : [];
  const activeIds = [];
  alerts.forEach((alert) => {
    const id = String(alert?.id || "").trim();
    if (!id) {
      return;
    }
    activeIds.push(id);
    if (state.seenUiAlertIds.includes(id)) {
      return;
    }
    const message = String(alert?.message || "").trim();
    const tone = String(alert?.tone || "warn").trim();
    const challengeName = String(alert?.challenge_name || "").trim();
    const laneId = String(alert?.lane_id || "").trim();
    if (challengeName && laneId) {
      pushLaneActivity(challengeName, laneId, message, tone);
    } else if (challengeName) {
      pushChallengeActivity(challengeName, message, tone);
    } else {
      pushActivity(message, tone);
    }
    sendBrowserNotification(alert);
    state.seenUiAlertIds.unshift(id);
  });
  state.seenUiAlertIds = state.seenUiAlertIds
    .filter((id, index, arr) => activeIds.includes(id) && arr.indexOf(id) === index)
    .slice(0, 64);
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
      state.challengeConfig = null;
      state.challengeConfigFor = "";
      state.challengeConfigLoading = true;
      state.instanceProbeResult = null;
      state.instanceProbeFor = "";
      state.instanceProbeLoading = false;
      state.traceFiles = [];
      state.traceEvents = [];
      state.traceWindow = null;
      syncSelections();
      render();
      fetchChallengeConfig({ force: true });
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
  if (els.hideErrorLanes) {
    els.hideErrorLanes.checked = state.hideErrorLanes;
  }
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
  renderBrowserNotificationsControl();
  renderSummary();
  renderChallenges();
  renderSelectedChallenge();
  renderChallengeConfigPanel();
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
  const previousSelectedChallenge = state.selectedChallenge;
  state.snapshot = snapshot;
  state.snapshotReceived = true;
  syncSnapshotAlerts(snapshot);
  syncSelections();
  render();
  els.updatedAt.textContent = new Date().toLocaleTimeString("ko-KR", {
    hour12: false,
  });
  const challengeChanged = previousSelectedChallenge !== state.selectedChallenge;
  await Promise.all([
    fetchTraceFiles({ preserveSelection: true, refreshTrace: false }),
    fetchAdvisoryHistory(),
    fetchChallengeConfig({
      force: challengeChanged || state.challengeConfigFor !== String(state.selectedChallenge || ""),
    }),
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

async function fetchChallengeConfig({ force = false } = {}) {
  const challengeName = String(state.selectedChallenge || "").trim();
  if (!challengeName) {
    state.challengeConfig = null;
    state.challengeConfigFor = "";
    state.challengeConfigLoading = false;
    state.instanceProbeResult = null;
    state.instanceProbeFor = "";
    state.instanceProbeLoading = false;
    clearChallengeConfigForm();
    render();
    return;
  }
  if (!force && state.challengeConfigFor === challengeName && state.challengeConfig) {
    return;
  }
  if (state.challengeConfigFor !== challengeName) {
    state.stageWorkflowDraft = "";
    state.stageWorkflowDirty = false;
    state.stageWorkflowDefinitions = [];
    state.stageWorkflowParseError = "";
  }

  state.challengeConfigLoading = true;
  state.challengeConfig = null;
  state.challengeConfigFor = challengeName;
  if (state.instanceProbeFor !== challengeName) {
    state.instanceProbeResult = null;
    state.instanceProbeLoading = false;
  }
  render();
  try {
    const payload = await fetchJson(
      `/api/runtime/challenge-config?${new URLSearchParams({ challenge_name: challengeName })}`
    );
    if (state.selectedChallenge !== challengeName) {
      return;
    }
    state.challengeConfig = payload;
    state.challengeConfigFor = challengeName;
    populateChallengeConfigForm(payload);
    render();
  } catch (error) {
    if (state.selectedChallenge === challengeName) {
      state.challengeConfig = null;
      state.challengeConfigFor = challengeName;
      clearChallengeConfigForm();
      render();
    }
    pushChallengeActivity(challengeName, `config fetch failed: ${error.message}`, "error");
  } finally {
    if (state.selectedChallenge === challengeName) {
      state.challengeConfigLoading = false;
      render();
    }
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
    state.maxChallengesDirty = false;
    state.maxChallengesDraft = String(value);
    if (state.snapshot && typeof state.snapshot === "object") {
      state.snapshot.max_concurrent_challenges = value;
    }
    if (els.maxChallengesInput) {
      els.maxChallengesInput.value = String(value);
    }
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
    pushChallengeActivity("", "restart failed: select a challenge first", "error");
    return;
  }
  try {
    const payload = await postOperator("/api/runtime/restart-challenge", {
      challenge_name: challengeName,
    });
    pushChallengeActivity(challengeName, "restart requested", "ok");
    pushServerActivity(payload?.result, "ok");
    await fetchStatus();
  } catch (error) {
    pushChallengeActivity(challengeName, `restart failed: ${error.message}`, "error");
  }
}

async function handleChallengeConfigSave(event) {
  event.preventDefault();
  const challengeName = String(state.selectedChallenge || "").trim();
  if (!challengeName) {
    pushChallengeActivity("", "config save failed: select a challenge first", "error");
    return;
  }

  let stageDefinitions = [];
  try {
    stageDefinitions = workflowStageDefinitions();
  } catch (error) {
    pushChallengeActivity(challengeName, `config save failed: ${error.message}`, "error");
    return;
  }

  const connection = {
    scheme: String(els.challengeConfigSchemeInput?.value || "").trim(),
    host: String(els.challengeConfigHostInput?.value || "").trim(),
    port: String(els.challengeConfigPortInput?.value || "").trim(),
    url: String(els.challengeConfigUrlInput?.value || "").trim(),
    raw_command: String(els.challengeConfigRawCommandInput?.value || "").trim(),
  };
  const stageId = String(els.challengeConfigStageSelect?.value || "").trim();
  const endpointId = String(els.challengeConfigEndpointSelect?.value || "").trim();
  const stageStatus = String(els.challengeConfigStageStatusInput?.value || "pending").trim().toLowerCase() || "pending";
  const baseOverride =
    state.challengeConfig && state.challengeConfigFor === challengeName && state.challengeConfig.override
      ? JSON.parse(JSON.stringify(state.challengeConfig.override))
      : {};
  const override = {
    ...baseOverride,
    notes: String(els.challengeConfigNotesInput?.value || "").trim(),
    priority: Boolean(els.challengeConfigPriorityInput?.checked),
    no_submit: Boolean(els.challengeConfigNoSubmitInput?.checked),
    needs_instance: Boolean(els.challengeConfigNeedsInstanceInput?.checked),
  };
  const stageOrder = stageDefinitions.map((stage) => String(stage.id || "").trim()).filter(Boolean);
  const existingStageStates =
    baseOverride.stages && typeof baseOverride.stages === "object" ? baseOverride.stages : {};
  const prunedStageStates = Object.fromEntries(
    Object.entries(existingStageStates).filter(([existingStageId]) => stageOrder.includes(String(existingStageId || "").trim()))
  );
  if (stageDefinitions.length) {
    override.instance_stages = stageDefinitions;
    if (Object.keys(prunedStageStates).length) {
      override.stages = prunedStageStates;
    } else {
      delete override.stages;
    }
  } else {
    delete override.instance_stages;
    delete override.stages;
    delete override.current_stage;
  }
  if (stageId) {
    const stages = override.stages && typeof override.stages === "object" ? override.stages : {};
    const stageIndex = stageOrder.indexOf(stageId);
    const nextStageId =
      stageStatus === "done" && stageIndex >= 0 && stageIndex + 1 < stageOrder.length
        ? stageOrder[stageIndex + 1]
        : "";
    override.current_stage = nextStageId || stageId;
    const nextStageState =
      stages[stageId] && typeof stages[stageId] === "object" ? stages[stageId] : {};
    const updatedStage = {
      ...nextStageState,
      status: stageStatus,
    };
    if (endpointId) {
      const existingEndpoints =
        nextStageState.endpoints && typeof nextStageState.endpoints === "object"
          ? nextStageState.endpoints
          : {};
      updatedStage.current_endpoint = endpointId;
      updatedStage.endpoints = {
        ...existingEndpoints,
        [endpointId]: {
          ...(existingEndpoints[endpointId] && typeof existingEndpoints[endpointId] === "object"
            ? existingEndpoints[endpointId]
            : {}),
          connection,
        },
      };
      delete updatedStage.connection;
    } else {
      updatedStage.connection = connection;
      delete updatedStage.current_endpoint;
    }
    override.stages = {
      ...stages,
      [stageId]: updatedStage,
    };
    delete override.connection;
  } else {
    override.connection = connection;
    delete override.current_stage;
  }
  const submitButton = event.submitter instanceof HTMLButtonElement ? event.submitter : null;
  if (submitButton) {
    submitButton.disabled = true;
  }
  try {
    const payload = await fetchJson("/api/runtime/challenge-config", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        challenge_name: challengeName,
        replace: true,
        override,
      }),
    });
    state.challengeConfig = payload;
    state.challengeConfigFor = challengeName;
    state.challengeConfigLoading = false;
    state.instanceProbeResult = null;
    state.instanceProbeFor = challengeName;
    state.instanceProbeLoading = false;
    state.stageWorkflowDirty = false;
    state.stageWorkflowDefinitions = editableStageDefinitions(payload);
    state.stageWorkflowDraft = stageWorkflowText(payload);
    state.stageWorkflowParseError = "";
    populateChallengeConfigForm(payload);
    render();
    pushChallengeActivity(challengeName, "challenge override saved", "ok");
  } catch (error) {
    pushChallengeActivity(challengeName, `config save failed: ${error.message}`, "error");
  } finally {
    if (submitButton) {
      submitButton.disabled = false;
    }
  }
}

async function handleChallengeConfigReset() {
  const challengeName = String(state.selectedChallenge || "").trim();
  if (!challengeName) {
    pushChallengeActivity("", "config reset failed: select a challenge first", "error");
    return;
  }
  if (els.challengeConfigResetBtn) {
    els.challengeConfigResetBtn.disabled = true;
  }
  try {
    const payload = await fetchJson(
      `/api/runtime/challenge-config?${new URLSearchParams({ challenge_name: challengeName })}`,
      {
        method: "DELETE",
      }
    );
    state.challengeConfig = payload;
    state.challengeConfigFor = challengeName;
    state.challengeConfigLoading = false;
    state.instanceProbeResult = null;
    state.instanceProbeFor = challengeName;
    state.instanceProbeLoading = false;
    state.stageWorkflowDirty = false;
    state.stageWorkflowDefinitions = [];
    state.stageWorkflowDraft = "";
    state.stageWorkflowParseError = "";
    populateChallengeConfigForm(payload);
    render();
    pushChallengeActivity(challengeName, "challenge override reset", "ok");
  } catch (error) {
    pushChallengeActivity(challengeName, `config reset failed: ${error.message}`, "error");
  } finally {
    if (els.challengeConfigResetBtn) {
      els.challengeConfigResetBtn.disabled = false;
    }
  }
}

async function handleInstanceCheck({ restartOnSuccess = false } = {}) {
  const challengeName = String(state.selectedChallenge || "").trim();
  if (!challengeName) {
    pushChallengeActivity("", "instance check failed: select a challenge first", "error");
    return;
  }
  state.instanceProbeFor = challengeName;
  state.instanceProbeLoading = true;
  render();
  try {
    const payload = await postOperator("/api/runtime/check-instance", {
      challenge_name: challengeName,
      restart_on_success: restartOnSuccess,
    });
    state.instanceProbeResult = payload;
    state.instanceProbeFor = challengeName;
    state.instanceProbeLoading = false;
    if (payload.challenge_config && typeof payload.challenge_config === "object") {
      state.challengeConfig = payload.challenge_config;
      state.challengeConfigFor = challengeName;
      populateChallengeConfigForm(payload.challenge_config);
    }
    render();
    const detail = String(payload?.probe?.detail || payload?.probe?.error || "").trim();
    const restartResult = String(payload?.restart_result || "").trim();
    let message = payload?.ready ? detail || "instance ready" : detail || "instance not ready";
    if (restartResult) {
      message = `${message}; ${restartResult}`;
    }
    pushChallengeActivity(challengeName, message, payload?.ready ? "ok" : "warn");
  } catch (error) {
    state.instanceProbeLoading = false;
    render();
    pushChallengeActivity(challengeName, `instance check failed: ${error.message}`, "error");
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
    saveHideErrorLanesPreference(state.hideErrorLanes);
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
  els.maxChallengesInput.addEventListener("input", () => {
    state.maxChallengesDraft = String(els.maxChallengesInput.value || "");
    state.maxChallengesDirty = true;
  });
  els.maxChallengesInput.addEventListener("blur", () => {
    const snapshotValue = String(Number(state.snapshot?.max_concurrent_challenges ?? 0));
    const inputValue = String(els.maxChallengesInput.value || "").trim();
    if (inputValue === snapshotValue) {
      state.maxChallengesDirty = false;
      state.maxChallengesDraft = snapshotValue;
    }
  });
  if (els.challengeConfigStagesInput) {
    els.challengeConfigStagesInput.addEventListener("input", () => {
      state.stageWorkflowDraft = String(els.challengeConfigStagesInput.value || "");
      state.stageWorkflowDirty = true;
      try {
        state.stageWorkflowDefinitions = parseStageWorkflowText(state.stageWorkflowDraft);
        state.stageWorkflowParseError = "";
        delete els.challengeConfigStagesInput.dataset.state;
        if (state.challengeConfig && state.challengeConfigFor === state.selectedChallenge) {
          renderChallengeConfigStageFields(
            state.challengeConfig,
            String(els.challengeConfigStageSelect?.value || "").trim(),
            String(els.challengeConfigEndpointSelect?.value || "").trim()
          );
          renderInstanceCheckSummary(state.challengeConfig);
        }
      } catch (error) {
        state.stageWorkflowParseError = error instanceof Error ? error.message : String(error || "invalid JSON");
        els.challengeConfigStagesInput.dataset.state = "error";
        if (state.challengeConfig && state.challengeConfigFor === state.selectedChallenge) {
          renderChallengeConfigStageFields(
            state.challengeConfig,
            String(els.challengeConfigStageSelect?.value || "").trim(),
            String(els.challengeConfigEndpointSelect?.value || "").trim()
          );
        }
      }
    });
  }
  if (els.challengeConfigStageAddBtn) {
    els.challengeConfigStageAddBtn.addEventListener("click", () => {
      const payload =
        state.challengeConfig && state.challengeConfigFor === state.selectedChallenge
          ? state.challengeConfig
          : { effective: {} };
      const definitions = workflowStageDefinitions().map((stage) => cloneJson(stage));
      const newId = nextGeneratedStageId(definitions);
      definitions.push({
        id: newId,
        title: `Stage ${definitions.length + 1}`,
      });
      syncStageWorkflowText(definitions);
      renderChallengeConfigStageFields(payload, newId, "");
      renderInstanceCheckSummary(payload);
    });
  }
  if (els.challengeConfigStageRemoveBtn) {
    els.challengeConfigStageRemoveBtn.addEventListener("click", () => {
      const payload =
        state.challengeConfig && state.challengeConfigFor === state.selectedChallenge
          ? state.challengeConfig
          : { effective: {} };
      const definitions = workflowStageDefinitions().map((stage) => cloneJson(stage));
      const selectedStageId = String(els.challengeConfigStageSelect?.value || "").trim();
      const index = definitions.findIndex((stage) => String(stage?.id || "").trim() === selectedStageId);
      if (index < 0) {
        return;
      }
      definitions.splice(index, 1);
      const nextStageId = String(definitions[Math.max(0, index - 1)]?.id || definitions[0]?.id || "").trim();
      syncStageWorkflowText(definitions);
      renderChallengeConfigStageFields(payload, nextStageId, "");
      renderInstanceCheckSummary(payload);
    });
  }
  if (els.challengeConfigStageSelect) {
    els.challengeConfigStageSelect.addEventListener("change", () => {
      if (!state.challengeConfig || state.challengeConfigFor !== state.selectedChallenge) {
        return;
      }
      renderChallengeConfigStageFields(
        state.challengeConfig,
        els.challengeConfigStageSelect.value || "",
        ""
      );
      renderInstanceCheckSummary(state.challengeConfig);
    });
  }
  if (els.challengeConfigStageIdInput) {
    els.challengeConfigStageIdInput.addEventListener("change", () => {
      updateSelectedWorkflowStage((definitions, index) => {
        const nextId = ensureUniqueStageId(
          String(els.challengeConfigStageIdInput?.value || ""),
          definitions,
          index
        );
        definitions[index].id = nextId;
        return nextId;
      });
    });
  }
  if (els.challengeConfigStageTitleInput) {
    els.challengeConfigStageTitleInput.addEventListener("input", () => {
      updateSelectedWorkflowStage((definitions, index) => {
        definitions[index].title = String(els.challengeConfigStageTitleInput?.value || "").trim();
        return definitions[index].id;
      });
    });
  }
  if (els.challengeConfigStageActionInput) {
    els.challengeConfigStageActionInput.addEventListener("input", () => {
      updateSelectedWorkflowStage((definitions, index) => {
        definitions[index].manual_action = String(els.challengeConfigStageActionInput?.value || "").trim();
        return definitions[index].id;
      });
    });
  }
  if (els.challengeConfigStageDescriptionInput) {
    els.challengeConfigStageDescriptionInput.addEventListener("input", () => {
      updateSelectedWorkflowStage((definitions, index) => {
        definitions[index].description = String(els.challengeConfigStageDescriptionInput?.value || "").trim();
        return definitions[index].id;
      });
    });
  }
  if (els.challengeConfigStageNotesInput) {
    els.challengeConfigStageNotesInput.addEventListener("input", () => {
      updateSelectedWorkflowStage((definitions, index) => {
        definitions[index].notes = String(els.challengeConfigStageNotesInput?.value || "").trim();
        return definitions[index].id;
      });
    });
  }
  if (els.challengeConfigEndpointSelect) {
    els.challengeConfigEndpointSelect.addEventListener("change", () => {
      if (!state.challengeConfig || state.challengeConfigFor !== state.selectedChallenge) {
        return;
      }
      renderChallengeConfigStageFields(
        state.challengeConfig,
        els.challengeConfigStageSelect?.value || "",
        els.challengeConfigEndpointSelect.value || ""
      );
      renderInstanceCheckSummary(state.challengeConfig);
    });
  }
  els.msgForm.addEventListener("submit", handleCoordinatorMessage);
  if (els.browserNotificationsBtn) {
    els.browserNotificationsBtn.addEventListener("click", handleBrowserNotificationsClick);
  }
  els.laneBumpForm.addEventListener("submit", handleLaneBump);
  els.challengeBumpForm.addEventListener("submit", handleChallengeBump);
  els.maxChallengesForm.addEventListener("submit", handleMaxChallenges);
  els.queuePriorityForm.addEventListener("submit", handlePriorityWaiting);
  els.normalQueueBtn.addEventListener("click", handleNormalWaiting);
  els.restartChallengeBtn.addEventListener("click", handleRestartChallenge);
  els.externalSolveForm.addEventListener("submit", handleExternalSolve);
  els.challengeConfigForm.addEventListener("submit", handleChallengeConfigSave);
  els.instanceCheckBtn.addEventListener("click", () => {
    handleInstanceCheck();
  });
  els.instanceCheckRestartBtn.addEventListener("click", () => {
    handleInstanceCheck({ restartOnSuccess: true });
  });
  els.challengeConfigResetBtn.addEventListener("click", handleChallengeConfigReset);
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
