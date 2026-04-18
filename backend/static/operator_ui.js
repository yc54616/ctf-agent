const POLL_MS = 2000;
const FINAL_STATES = new Set(["won", "flag_found", "cancelled"]);
const state = {
  snapshot: null,
  selectedChallenge: null,
  selectedLane: null,
  selectedTrace: null,
  advisoryHistory: [],
  traceFiles: [],
  traceEvents: [],
  traceWindow: null,
  traceTypeFilter: "",
  traceTextFilter: "",
  hideErrorLanes: true,
  loadingTraceFilesFor: "",
  usingRealtime: false,
  statusPollHandle: null,
  statusStream: null,
};

const els = {
  updatedAt: document.getElementById("updatedAt"),
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
  sharedFindingText: document.getElementById("sharedFindingText"),
  advisoryHistory: document.getElementById("advisoryHistory"),
  laneStrip: document.getElementById("laneStrip"),
  hideErrorLanesToggle: document.getElementById("hideErrorLanesToggle"),
  traceSelect: document.getElementById("traceSelect"),
  loadOlderBtn: document.getElementById("loadOlderBtn"),
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
};

function challengeBuckets(snapshot) {
  const active = snapshot?.active_swarms ?? {};
  const finished = { ...(snapshot?.finished_swarms ?? {}) };
  const pending = snapshot?.pending_challenges ?? [];
  const results = snapshot?.results ?? {};
  for (const [name, result] of Object.entries(results)) {
    if (!active[name] && !finished[name]) {
      finished[name] = {
        challenge: name,
        winner: result.flag || result.status || "done",
        restored: true,
        agents: {},
        step_count: Number(result.step_count || 0),
      };
    } else if (finished[name] && result && typeof result === "object") {
      finished[name] = {
        ...finished[name],
        step_count: Math.max(
          Number(finished[name].step_count || 0),
          Number(result.step_count || 0)
        ),
      };
    }
  }
  return { active, finished, pending };
}

function laneEntries(challenge) {
  return Object.entries(challenge?.agents ?? {});
}

function preferredLane(challenge) {
  const lanes = laneEntries(challenge);
  if (!lanes.length) {
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
  return lanes
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
  if (state.selectedChallenge && buckets.pending.includes(state.selectedChallenge)) {
    return { bucket: "pending", data: { challenge: state.selectedChallenge, agents: {} } };
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

function challengeSummary(name, challenge) {
  const lanes = laneEntries(challenge);
  const busy = lanes.filter(([, lane]) => lane.lifecycle === "busy").length;
  const laneSteps = lanes.reduce((sum, [, lane]) => sum + Number(lane.step_count || 0), 0);
  const stepCount = Math.max(laneSteps, Number(challenge.step_count || 0));
  const lead = challenge.winner ? `winner ${challenge.winner}` : `${lanes.length} lanes`;
  const details = [lead];
  if (lanes.length) {
    details.push(`busy ${busy}`);
  }
  details.push(`steps ${stepCount}`);
  return details.join(" · ");
}

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
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

function setSyncMode(label) {
  if (els.syncMode) {
    els.syncMode.textContent = label;
  }
}

function syncSelections() {
  const buckets = challengeBuckets(state.snapshot || {});
  const activeNames = Object.keys(buckets.active);
  const finishedNames = Object.keys(buckets.finished);
  const pendingNames = buckets.pending;
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
  if (!lanes.length) {
    state.selectedLane = null;
    return;
  }
  if (!state.selectedLane || !selected.agents[state.selectedLane]) {
    state.selectedLane = preferredLane(selected);
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
  const metrics = [
    ["Known", snapshot.known_challenge_count],
    ["Solved", snapshot.known_solved_count],
    ["Active", snapshot.active_swarm_count],
    ["Pending", snapshot.pending_challenge_count],
    ["Steps", snapshot.total_step_count],
    ["Cost", `$${Number(snapshot.cost_usd || 0).toFixed(2)}`],
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
    ["Pending", buckets.pending.map((name) => [name, { challenge: name, agents: {} }])],
  ];
  els.challengeCount.textContent =
    `${Object.keys(buckets.active).length + Object.keys(buckets.finished).length + buckets.pending.length} total`;
  els.challengeGroups.innerHTML = groups
    .map(([label, entries]) => {
      const body = entries.length
        ? entries
            .map(([name, challenge]) => {
              const selected = state.selectedChallenge === name ? "selected" : "";
              const status = challenge.winner || label.toLowerCase();
              const summary = challengeSummary(name, challenge);
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
    els.coordinatorAdvisoryText.textContent = "-";
    els.laneAdvisoryText.textContent = "-";
    els.sharedFindingText.textContent = "-";
    els.advisoryHistory.innerHTML = '<li class="empty">No advisory history yet.</li>';
    els.laneStrip.innerHTML = '<div class="empty">No challenge selected.</div>';
    els.laneFocus.innerHTML = '<div class="empty">Select a lane to inspect current activity.</div>';
    els.traceTableBody.innerHTML = '<tr><td class="empty" colspan="3">No trace selected.</td></tr>';
    return;
  }

  const lanes = laneEntries(challenge);
  const visibleLanes = lanes.filter(
    ([modelSpec, agent]) =>
      !state.hideErrorLanes ||
      !["error", "quota_error"].includes(String(agent.lifecycle || "")) ||
      state.selectedLane === modelSpec
  );
  els.selectedChallengeTitle.textContent = challenge.challenge || state.selectedChallenge;
  els.selectedChallengeMeta.textContent =
    `${selected.bucket || "challenge"} · ${challengeSummary(state.selectedChallenge, challenge)}`;
  els.coordinatorAdvisoryText.textContent = challenge.coordinator_advisor_note || "-";
  els.sharedFindingText.textContent = challenge.shared_finding || "-";
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

  const lane = state.selectedLane ? challenge.agents?.[state.selectedLane] : null;
  els.selectedLaneMeta.textContent = lane
    ? `${state.selectedLane} · ${lane.lifecycle || "unknown"} · step ${lane.step_count || 0}`
    : "Select a lane to inspect it.";
  els.laneAdvisoryText.textContent = lane?.advisor_note || "-";
  els.laneFocus.innerHTML = lane
    ? `
        <div class="lane-focus-header">
          <div class="lane-focus-title">${escapeHtml(state.selectedLane)}</div>
          <div class="event-meta">
            <span class="${badgeClass(lane.lifecycle)}">${escapeHtml(lane.lifecycle || "unknown")}</span>
            <span class="event-tag">step ${escapeHtml(lane.step_count || 0)}</span>
            <span class="event-tag">${escapeHtml(lane.current_tool || lane.last_tool || "no tool")}</span>
          </div>
        </div>
        <div class="lane-focus-detail">${escapeHtml(laneDetail(lane))}</div>
        ${
          lane.advisor_note
            ? `<div class="lane-focus-advisory"><strong>Lane advisory</strong>${escapeHtml(lane.advisor_note)}</div>`
            : ""
        }
        <div class="lane-focus-subtle">${escapeHtml(lane.findings || lane.last_exit_hint || "No additional lane note.")}</div>
      `
    : '<div class="empty">Select a lane to inspect current activity.</div>';
  els.advisoryHistory.innerHTML = state.advisoryHistory.length
    ? state.advisoryHistory
        .map((entry) => {
          const selectedRow = shortModelName(state.selectedLane) === entry.model_id ? "selected" : "";
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
    const snapshot = await fetchJson("/status");
    await applyStatusSnapshot(snapshot);
  } catch (error) {
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
      `/advisories?${new URLSearchParams({ challenge_name: state.selectedChallenge, limit: "10" })}`
    );
    state.advisoryHistory = payload.entries || [];
    render();
  } catch (error) {
    pushActivity(`advisory history failed: ${error.message}`, "error");
  }
}

async function fetchTraceFiles({ preserveSelection = false, refreshTrace = true } = {}) {
  const selected = getSelectedChallengeData().data;
  if (!selected || !state.selectedLane) {
    state.traceFiles = [];
    state.selectedTrace = null;
    state.traceEvents = [];
    state.traceWindow = null;
    render();
    return;
  }
  const key = `${state.selectedChallenge}:${state.selectedLane}`;
  state.loadingTraceFilesFor = key;
  const params = new URLSearchParams({
    challenge_name: state.selectedChallenge,
    model_spec: state.selectedLane,
  });
  try {
    const payload = await fetchJson(`/trace-files?${params}`);
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
    const source = new EventSource("/status/stream");
    state.statusStream = source;
    source.addEventListener("open", () => {
      stopStatusPolling();
      state.usingRealtime = true;
      setSyncMode("realtime");
    });
    source.addEventListener("status", async (event) => {
      const snapshot = JSON.parse(event.data);
      await applyStatusSnapshot(snapshot);
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
    pushActivity(`realtime stream failed: ${error.message}`, "warn");
    return false;
  }
}

async function fetchTrace(cursor = null, { appendOlder = false } = {}) {
  if (!state.selectedChallenge || !state.selectedLane || !state.selectedTrace) {
    return;
  }
  const params = new URLSearchParams({
    challenge_name: state.selectedChallenge,
    model_spec: state.selectedLane,
    trace_name: state.selectedTrace,
    limit: "200",
  });
  if (cursor !== null && cursor !== undefined) {
    params.set("cursor", String(cursor));
  }
  try {
    const payload = await fetchJson(`/trace?${params}`);
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
    await postOperator("/msg", { message });
    pushActivity(`coordinator message sent`, "ok");
    els.msgInput.value = "";
  } catch (error) {
    pushActivity(`message failed: ${error.message}`, "error");
  }
}

async function handleLaneBump(event) {
  event.preventDefault();
  const insights = els.laneBumpInput.value.trim();
  if (!insights || !state.selectedChallenge || !state.selectedLane) {
    return;
  }
  try {
    await postOperator("/bump", {
      challenge_name: state.selectedChallenge,
      model_spec: state.selectedLane,
      insights,
    });
    pushActivity(`lane bumped: ${state.selectedLane}`, "ok");
    els.laneBumpInput.value = "";
  } catch (error) {
    pushActivity(`lane bump failed: ${error.message}`, "error");
  }
}

async function handleChallengeBump(event) {
  event.preventDefault();
  const insights = els.challengeBumpInput.value.trim();
  const challenge = getSelectedChallengeData().data;
  if (!insights || !challenge || !state.selectedChallenge) {
    return;
  }
  const lanes = laneEntries(challenge).filter(([, lane]) => !FINAL_STATES.has(lane.lifecycle));
  if (!lanes.length) {
    pushActivity(`no bumpable lanes in ${state.selectedChallenge}`, "warn");
    return;
  }

  const results = [];
  for (const [modelSpec] of lanes) {
    try {
      await postOperator("/bump", {
        challenge_name: state.selectedChallenge,
        model_spec: modelSpec,
        insights,
      });
      results.push(`${modelSpec}: ok`);
    } catch (error) {
      results.push(`${modelSpec}: ${error.message}`);
    }
  }
  pushActivity(`challenge bump ${state.selectedChallenge} -> ${results.join(" | ")}`, "ok");
  els.challengeBumpInput.value = "";
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
  els.hideErrorLanesToggle.addEventListener("change", () => {
    state.hideErrorLanes = els.hideErrorLanesToggle.checked;
    render();
  });
  els.traceTextFilter.addEventListener("input", () => {
    state.traceTextFilter = els.traceTextFilter.value.trim();
    renderTraceTable();
  });
  els.msgForm.addEventListener("submit", handleCoordinatorMessage);
  els.laneBumpForm.addEventListener("submit", handleLaneBump);
  els.challengeBumpForm.addEventListener("submit", handleChallengeBump);
}

async function main() {
  bindEvents();
  await fetchStatus();
  if (!startStatusStream()) {
    startStatusPolling();
  }
  setInterval(() => {
    if (state.selectedTrace) {
      fetchTrace();
    }
  }, POLL_MS);
}

main();
