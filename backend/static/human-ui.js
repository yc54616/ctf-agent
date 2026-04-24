/* ══════════════════════════════════════════════════════════════════════════
   CTF Human Coordinator — frontend logic
   ══════════════════════════════════════════════════════════════════════════ */
"use strict";

const SSE_SNAP_URL  = "/api/runtime/stream";
const SSE_EVENT_URL = "/api/runtime/human-events";
const POLL_MS       = 8_000;

/* ─── State ─────────────────────────────────────────────────────────────── */
const S = {
  challenges: {},
  selectedName: null,
  snapSse: null,
  eventSse: null,
  pollTimer: null,
  activeTab: "overview",
  queueOpen: false,
  advisorReports: [],
};

const $ = id => document.getElementById(id);

/* ══════════════════════════════════════════════════════════════════════════
   Utility
   ══════════════════════════════════════════════════════════════════════════ */
function ts() {
  return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function logActivity(msg, cls = "al-info") {
  const li = document.createElement("li");
  li.innerHTML = `<span class="al-ts">${ts()}</span><span class="${cls}">${esc(msg)}</span>`;
  const log = $("activityLog");
  log.prepend(li);
  while (log.children.length > 60) log.lastChild.remove();
}
function pushEvent(text, cls = "info") {
  const feed = $("eventFeed");
  feed.querySelector(".empty")?.remove();
  const div = document.createElement("div");
  div.className = `event-item ${cls}`;
  div.innerHTML = `<span class="ev-ts">${ts()}</span>${esc(text)}`;
  feed.prepend(div);
  while (feed.children.length > 120) feed.lastChild.remove();
}
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers ?? {}) },
    ...opts,
  });
  const body = await res.json().catch(() => ({}));
  return { ok: res.ok, status: res.status, body };
}
function flashResult(elId, msg, ok) {
  const el = $(elId);
  if (!el) return;
  el.textContent = msg;
  el.className = el.className.replace(/ ok| error/g, "") + " " + (ok ? "ok" : "error");
  setTimeout(() => { el.textContent = ""; el.className = el.className.replace(/ ok| error/g, ""); }, 6000);
}

/* ══════════════════════════════════════════════════════════════════════════
   Tab bar
   ══════════════════════════════════════════════════════════════════════════ */
document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    const tab = btn.dataset.tab;
    S.activeTab = tab;
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".tab-pane").forEach(p => p.classList.toggle("active", p.id === "tab-" + tab));
    if (tab === "advisor"   && S.selectedName) loadAdvisoryHistory();
    if (tab === "health"    && S.selectedName) renderHealth();
    if (tab === "config"    && S.selectedName) loadConfig();
    if (tab === "intervene")                    renderReports();
  });
});

/* ══════════════════════════════════════════════════════════════════════════
   SSE — snapshot stream
   ══════════════════════════════════════════════════════════════════════════ */
function connectSnapSse() {
  S.snapSse?.close();
  const sse = new EventSource(SSE_SNAP_URL);
  S.snapSse = sse;
  sse.addEventListener("snapshot", e => { try { applySnapshot(JSON.parse(e.data)); } catch { /**/ } });
  sse.onopen  = () => setSyncDot("ok");
  sse.onerror = () => {
    setSyncDot("error"); sse.close(); S.snapSse = null;
    if (!S.pollTimer) S.pollTimer = setInterval(pollSnapshot, POLL_MS);
  };
}
async function pollSnapshot() {
  try { const r = await api("/api/runtime/snapshot"); if (r.ok) { applySnapshot(r.body); setSyncDot("ok"); } }
  catch { setSyncDot("error"); }
}
function setSyncDot(s) { $("syncDot").className = "sync-dot " + s; }

/* ══════════════════════════════════════════════════════════════════════════
   SSE — human events stream
   ══════════════════════════════════════════════════════════════════════════ */
function connectEventSse() {
  S.eventSse?.close();
  const sse = new EventSource(SSE_EVENT_URL);
  S.eventSse = sse;
  sse.addEventListener("coordinator", e => {
    try {
      const ev = JSON.parse(e.data);
      const text = ev.message ?? JSON.stringify(ev);
      const cls  = ev.level === "error" ? "error" : ev.level === "warn" ? "warn"
                 : ev.level === "success" ? "success" : "info";
      pushEvent(text, cls);
    } catch { /**/ }
  });
  sse.onerror = () => { sse.close(); S.eventSse = null; setTimeout(connectEventSse, 5_000); };
}

/* ══════════════════════════════════════════════════════════════════════════
   Snapshot → unified challenge map
   ══════════════════════════════════════════════════════════════════════════ */
function toState(lc) {
  if (!lc) return "idle";
  if (["running", "busy", "thinking"].includes(lc)) return "running";
  if (["done", "won", "flag_found", "finished"].includes(lc)) return "done";
  if (["error", "quota_error", "cancelled", "failed"].includes(lc)) return "failed";
  return lc;
}
function mkLanes(agents) {
  if (!agents || typeof agents !== "object") return [];
  return Object.entries(agents).map(([model_spec, ag]) => ({
    model_spec,
    state:          toState(ag?.lifecycle ?? ag?.status ?? ""),
    steps:          ag?.step_count ?? 0,
    runtime_health: ag?.runtime_health ?? "healthy",
  }));
}
function mkCandidates(raw) {
  if (!raw || typeof raw !== "object") return [];
  return Object.entries(raw)
    .filter(([, c]) => !["confirmed", "rejected"].includes((c?.status ?? "").toLowerCase()))
    .map(([flag, c]) => ({ flag, source: c?.source_model ?? c?.source ?? "", status: c?.status ?? "" }));
}
function mkFindings(raw) {
  if (!raw) return [];
  const arr = Array.isArray(raw) ? raw : Object.values(raw);
  return arr.map(f => typeof f === "string" ? f : (f?.content ?? f?.summary ?? JSON.stringify(f)));
}
function chStatus(swarm, result) {
  const s = swarm?.status ?? result?.status ?? "idle";
  if (s === "flag_found" || s === "solved") return "solved";
  // Swarm reports "running" or "candidate_pending" for live challenges — normalise to "active"
  if (s === "running" || s === "candidate_pending") return "active";
  return s;
}
function buildChallenges(snap) {
  const out = {};
  for (const group of [snap.active_swarms ?? {}, snap.pending_swarms ?? {}, snap.finished_swarms ?? {}]) {
    for (const [name, sw] of Object.entries(group)) {
      if (!out[name]) out[name] = {
        name, status: chStatus(sw, null),
        category: sw?.category ?? "", points: sw?.points ?? null,
        source: "local",
        local_preloaded: true,
        lanes: mkLanes(sw?.agents ?? {}),
        flag_candidates: mkCandidates(sw?.flag_candidates ?? {}),
        shared_findings: mkFindings(sw?.shared_findings ?? {}),
      };
    }
  }
  for (const [name, result] of Object.entries(snap.results ?? {})) {
    if (!out[name] && result && typeof result === "object") {
      out[name] = {
        name, status: chStatus(null, result),
        category: result?.category ?? "", points: result?.points ?? null,
        source: "local",
        local_preloaded: true,
        lanes: [],
        flag_candidates: mkCandidates(result?.flag_candidates ?? {}),
        shared_findings: mkFindings(result?.shared_findings ?? {}),
      };
    }
  }
  // Idle/not-yet-spawned challenges: on-disk metas + remote-fetched cache.
  for (const entry of (snap.known_challenges ?? [])) {
    const name = entry?.name; if (!name || out[name]) continue;
    out[name] = {
      name,
      status: "idle",
      category: entry.category ?? "",
      points: entry.value ?? null,
      source: entry.source ?? "local",
      local_preloaded: !!entry.local_preloaded,
      lanes: [],
      flag_candidates: [],
      shared_findings: [],
    };
  }
  return out;
}

/* ══════════════════════════════════════════════════════════════════════════
   Apply snapshot
   ══════════════════════════════════════════════════════════════════════════ */
function applySnapshot(snap) {
  S.challenges = buildChallenges(snap);
  const names  = Object.keys(S.challenges);

  const solved = names.filter(n => S.challenges[n].status === "solved").length;
  const active = names.filter(n => S.challenges[n].status === "active").length;
  const queued = (snap.pending_challenges ?? []).length;

  let stale = 0;
  for (const ch of Object.values(S.challenges))
    for (const l of ch.lanes)
      if (l.runtime_health === "stale" || l.runtime_health === "error") stale++;
  const hs = snap.health_summary ?? {};
  stale = Math.max(stale, (hs.stale_lanes ?? 0) + (hs.error_lanes ?? 0));

  $("metSolved").textContent = solved;
  $("metActive").textContent = active;
  $("metQueued").textContent = queued;
  $("metCost").textContent   = `$${Number(snap.cost_usd ?? 0).toFixed(3)}`;
  $("metSteps").textContent  = snap.total_step_count ?? 0;
  const staleEl = $("metStale");
  staleEl.textContent = stale;
  staleEl.style.color = stale > 0 ? "var(--orange)" : "";
  $("challengeCount").textContent = names.length;

  const mode  = snap.mode ?? (snap.human_mode ? "human" : "llm");
  const badge = $("modeBadge");
  badge.textContent = mode;
  badge.className   = "mode-badge " + mode;

  renderChallengeList();
  renderQueue(snap.pending_challenge_entries ?? []);
  // Cache latest reports for filter toggle + re-render on challenge selection.
  S.advisorReports = snap.advisor_reports ?? [];
  if (S.activeTab === "intervene") renderReports();

  if (S.selectedName && S.challenges[S.selectedName]) {
    renderCenterPanel(S.challenges[S.selectedName]);
    if (S.activeTab === "health") renderHealth();
  }
}

/* ══════════════════════════════════════════════════════════════════════════
   Challenge list
   ══════════════════════════════════════════════════════════════════════════ */
function renderChallengeList() {
  const list  = $("challengeList");
  const names = Object.keys(S.challenges).sort((a, b) => {
    const sa = S.challenges[a].status === "solved" ? 1 : 0;
    const sb = S.challenges[b].status === "solved" ? 1 : 0;
    return sa - sb || a.localeCompare(b);
  });
  if (!names.length) { list.innerHTML = '<div class="empty">No challenges loaded.</div>'; return; }
  const scroll = list.scrollTop;
  list.innerHTML = "";
  for (const name of names) {
    const ch   = S.challenges[name];
    const staleN = ch.lanes.filter(l => l.runtime_health === "stale" || l.runtime_health === "error").length;
    const item   = document.createElement("div");
    item.className    = "challenge-item" + (name === S.selectedName ? " active" : "");
    item.dataset.name = name;
    // Remote-only = discovered via CTFd but not yet on disk (auto-import failed
    // or was skipped).  Spawn would fail without a challenge_dir.
    const isRemoteOnly = !ch.local_preloaded && ch.status === "idle";
    item.innerHTML = `
      <span class="ch-status-dot ${chStatusCls(ch.status)}"></span>
      <span class="ch-name" title="${esc(name)}${isRemoteOnly ? " (remote-only — import failed; re-click Fetch)" : ""}">${esc(name)}</span>
      ${ch.category ? `<span class="ch-cat">${esc(ch.category)}</span>` : ""}
      ${isRemoteOnly ? `<span class="ch-cat" style="color:var(--purple)" title="Not yet imported to disk">☁</span>` : ""}
      ${staleN > 0 ? `<span class="ch-cat" style="color:var(--orange)">⚠${staleN}</span>` : ""}
      ${ch.points ? `<span class="ch-pts">${ch.points}pt</span>` : ""}
    `;
    item.addEventListener("click", () => selectChallenge(name));
    list.appendChild(item);
  }
  list.scrollTop = scroll;
}
function chStatusCls(s) {
  return { solved: "solved", active: "active", running: "active", candidate_pending: "active", pending: "pending", failed: "failed" }[s] ?? "idle";
}

/* ══════════════════════════════════════════════════════════════════════════
   Pending queue (left panel collapsible)
   ══════════════════════════════════════════════════════════════════════════ */
function renderQueue(entries) {
  const sec  = $("queueSection");
  const body = $("queueBody");
  const cnt  = $("queueCount");
  if (!entries.length) { sec.style.display = "none"; return; }
  sec.style.display = "";
  cnt.textContent = entries.length;
  body.innerHTML = "";
  entries.forEach((entry, idx) => {
    const isPri = entry.priority;
    const div = document.createElement("div");
    div.className = "queue-item";
    div.innerHTML = `
      <span class="queue-pos ${isPri ? "priority" : ""}">${idx + 1}</span>
      <span class="queue-name" title="${esc(entry.challenge_name)}">${esc(entry.challenge_name)}</span>
      <span class="queue-reason">${esc(entry.reason ?? "")}</span>
    `;
    body.appendChild(div);
  });
}

$("queueToggle").addEventListener("click", () => {
  S.queueOpen = !S.queueOpen;
  $("queueToggle").classList.toggle("open", S.queueOpen);
  $("queueBody").classList.toggle("open", S.queueOpen);
});

/* ══════════════════════════════════════════════════════════════════════════
   Selection
   ══════════════════════════════════════════════════════════════════════════ */
function selectChallenge(name) {
  S.selectedName = name;
  document.querySelectorAll(".challenge-item").forEach(el => el.classList.toggle("active", el.dataset.name === name));
  const ch = S.challenges[name];
  if (!ch) return;
  enableCommands(name, ch);
  renderCenterPanel(ch);
  $("tabBar").style.display = "";
}
function enableCommands(name, ch) {
  const solved = ch.status === "solved";
  const active = ch.status === "active";
  const remoteOnly = !ch.local_preloaded && ch.status === "idle";
  $("selectedBadge").textContent  = name;
  $("selectedBadge").style.display = "";
  const metaSuffix = remoteOnly ? " — ☁ remote-only (run ctf-import first)" : "";
  $("swarmMeta").textContent = `${name} — ${ch.status}${metaSuffix}`;
  // Spawning a remote-only challenge fails because there's no challenge_dir on disk.
  $("spawnBtn").disabled        = solved || active || remoteOnly;
  $("killBtn").disabled         = !active;
  $("restartBtn").disabled      = solved || remoteOnly;
  $("priorityOnBtn").disabled   = remoteOnly;
  $("priorityOffBtn").disabled  = remoteOnly;
  $("checkInstanceBtn").disabled = false;
  $("submitFlagBtn").disabled   = false;
  $("markSolvedBtn").disabled   = solved;
  $("broadcastBtn").disabled    = !active;
  $("strategicBtn").disabled    = !active;
  $("tacticalBtn").disabled     = !active;
  $("saveResultBtn").disabled   = false;
  $("clearHistoryBtn").disabled = active;
  $("bumpAllStaleBtn").disabled = !active;
}

/* ══════════════════════════════════════════════════════════════════════════
   Center panel
   ══════════════════════════════════════════════════════════════════════════ */
function renderCenterPanel(ch) {
  $("centerTitle").textContent = ch.name;
  $("centerMeta").textContent  = [ch.category, ch.points ? ch.points + "pt" : null, ch.status].filter(Boolean).join(" · ");
  $("centerEmpty").style.display = "none";
  renderLanes(ch.lanes);
  renderCandidates(ch.flag_candidates);
  renderFindings(ch.shared_findings);
  populateLaneSelects(ch.lanes);
}

/* ── Lanes ─────────────────────────────────────────────────────────────── */
function renderLanes(lanes) {
  const sec  = $("lanesSection");
  const grid = $("laneGrid");
  if (!lanes.length) { sec.style.display = "none"; return; }
  sec.style.display = "";
  grid.innerHTML = "";
  for (const l of lanes) {
    const card = document.createElement("div");
    card.className = `lane-card ${l.state}`;
    card.innerHTML = `
      <div class="lane-model" title="${esc(l.model_spec)}">${esc(shortModel(l.model_spec))}</div>
      <div class="lane-state ${l.state}">${esc(l.state)}</div>
      ${l.steps ? `<div style="font-size:11px;color:var(--muted);margin-top:2px">${l.steps} steps</div>` : ""}
    `;
    grid.appendChild(card);
  }
}
function shortModel(s) { const m = String(s ?? "?").split(":").pop(); return m.length > 24 ? m.slice(0,24)+"…" : m; }

/* ── Candidates ─────────────────────────────────────────────────────────── */
function renderCandidates(candidates) {
  const sec  = $("candidatesSection");
  const list = $("candidateList");
  if (!candidates.length) { sec.style.display = "none"; return; }
  sec.style.display = "";
  list.innerHTML = "";
  for (const c of candidates) {
    const row = document.createElement("div");
    row.className = "candidate-row";
    row.innerHTML = `
      <span class="candidate-flag" title="${esc(c.flag)}">${esc(c.flag)}</span>
      ${c.source ? `<span class="candidate-source">${esc(c.source)}</span>` : ""}
      <span class="candidate-actions">
        <button class="btn-success" data-flag="${esc(c.flag)}" data-action="approve">✓</button>
        <button class="btn-danger"  data-flag="${esc(c.flag)}" data-action="reject">✗</button>
        <button class="btn-accent"  data-flag="${esc(c.flag)}" data-action="submit">→ Submit</button>
      </span>
    `;
    list.appendChild(row);
  }
  list.querySelectorAll("[data-action]").forEach(btn =>
    btn.addEventListener("click", () => handleCandidate(btn.dataset.action, btn.dataset.flag))
  );
}
async function handleCandidate(action, flag) {
  const name = S.selectedName; if (!name) return;
  const endpoints = { approve: "/api/runtime/approve-candidate", reject: "/api/runtime/reject-candidate", submit: "/api/runtime/submit-flag" };
  const r = await api(endpoints[action], { method: "POST", body: JSON.stringify({ challenge_name: name, flag }) });
  const msg = r.body.result ?? r.body.error ?? r.body.detail ?? (r.ok ? "ok" : "failed");
  logActivity(`${action} ${name}: ${msg}`, r.ok ? "al-ok" : "al-err");
  if (action === "submit" && r.ok) pushEvent(`🏁 Flag submitted for ${name}: ${msg}`, "success");
}

/* ── Findings ───────────────────────────────────────────────────────────── */
function renderFindings(findings) {
  const sec  = $("findingsSection");
  const list = $("findingList");
  if (!findings.length) { sec.style.display = "none"; return; }
  sec.style.display = "";
  list.innerHTML = "";
  for (const f of findings) {
    const div = document.createElement("div");
    div.className = "finding-item"; div.textContent = f;
    list.appendChild(div);
  }
}

/* ── Lane selects ───────────────────────────────────────────────────────── */
function populateLaneSelects(lanes) {
  for (const id of ["tacticalLaneSelect", "traceLaneSelect"]) {
    const sel  = $(id), prev = sel.value;
    sel.innerHTML = '<option value="">— lane —</option>';
    for (const l of lanes) {
      const o = document.createElement("option");
      o.value = l.model_spec; o.textContent = shortModel(l.model_spec);
      sel.appendChild(o);
    }
    if (prev) sel.value = prev;
  }
}

/* ══════════════════════════════════════════════════════════════════════════
   Advisor tab
   ══════════════════════════════════════════════════════════════════════════ */
$("loadAdvisoryBtn").addEventListener("click", loadAdvisoryHistory);
async function loadAdvisoryHistory() {
  const name = S.selectedName; if (!name) return;
  const limit = $("advisoryLimit").value;
  $("advisoryList").innerHTML = "<div class='empty'>Loading…</div>";
  const r = await api(`/api/runtime/advisories?challenge_name=${encodeURIComponent(name)}&limit=${limit}`);
  if (!r.ok) { $("advisoryList").innerHTML = `<div class='empty'>Error: ${esc(r.body.error ?? r.status)}</div>`; return; }
  const entries = r.body.entries ?? r.body.history ?? (Array.isArray(r.body) ? r.body : []);
  if (!entries.length) { $("advisoryList").innerHTML = "<div class='empty'>No advisory history yet.</div>"; return; }
  const list = $("advisoryList"); list.innerHTML = "";
  for (const e of entries) {
    const item = document.createElement("div");
    item.className = "advisory-item";
    const when = e.ts ? new Date(e.ts * 1000).toLocaleTimeString() : "";
    const lane = e.lane_id ?? e.model_spec ?? "";
    const kind = e.kind ?? e.type ?? "advisory";
    const body = e.note ?? e.content ?? e.text ?? JSON.stringify(e);
    item.innerHTML = `
      <div class="advisory-meta">
        ${when ? `<span>${esc(when)}</span>` : ""}
        ${lane ? `<span class="advisory-tag">${esc(shortModel(lane))}</span>` : ""}
        <span class="advisory-tag" style="color:var(--purple)">${esc(kind)}</span>
      </div>
      <div class="advisory-body">${esc(body)}</div>
    `;
    list.appendChild(item);
  }
}

/* ══════════════════════════════════════════════════════════════════════════
   Intervene tab — live advisor reports panel
   ══════════════════════════════════════════════════════════════════════════ */
function renderReports() {
  const list = $("reportsList");
  if (!list) return;
  const onlyThis = $("reportsThisChallengeOnly")?.checked ?? true;
  let reports = S.advisorReports ?? [];
  if (onlyThis && S.selectedName) {
    reports = reports.filter(r => !r.challenge_name || r.challenge_name === S.selectedName);
  }
  // Newest first
  reports = [...reports].sort((a, b) => (b.ts ?? 0) - (a.ts ?? 0)).slice(0, 40);
  if (!reports.length) {
    list.innerHTML = '<div class="empty">No advisor reports yet. They appear here as solvers run.</div>';
    return;
  }
  list.innerHTML = "";
  for (const r of reports) {
    const kind = String(r.kind ?? "coordinator_annotation");
    const item = document.createElement("div");
    item.className = "report-item " + kind;
    const when  = r.ts ? new Date(r.ts * 1000).toLocaleTimeString() : "";
    const lane  = r.lane_id ? shortModel(r.lane_id) : "";
    const ch    = r.challenge_name ?? "";
    const dec   = (r.advisor_decision ?? "").toLowerCase();
    const decHtml = dec ? `<span class="report-decision ${esc(dec)}">verdict: ${esc(dec)}</span>` : "";
    const flagHtml = r.flag ? `<span title="${esc(r.flag)}">🏁 ${esc(r.flag).slice(0, 40)}${r.flag.length > 40 ? "…" : ""}</span>` : "";
    item.innerHTML = `
      <div class="report-meta">
        ${when ? `<span>${esc(when)}</span>` : ""}
        <span class="report-kind ${esc(kind)}">${esc(kind.replace(/_/g, " "))}</span>
        ${ch   ? `<span>${esc(ch)}</span>`   : ""}
        ${lane ? `<span>${esc(lane)}</span>` : ""}
        ${decHtml}
        ${flagHtml}
      </div>
      <div class="report-text">${esc(r.text ?? "")}</div>
      ${lane ? `<div class="report-actions"><button class="report-reply-btn" data-lane="${esc(r.lane_id)}" data-ch="${esc(ch)}">↩ Reply to lane</button></div>` : ""}
    `;
    list.appendChild(item);
  }
  list.querySelectorAll(".report-reply-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const lane = btn.dataset.lane;
      const ch   = btn.dataset.ch;
      // If the report was for a different challenge, switch to it first.
      if (ch && ch !== S.selectedName && S.challenges[ch]) selectChallenge(ch);
      const sel = $("tacticalLaneSelect");
      if (sel && lane) {
        // Ensure option exists even if the lane isn't in the current dropdown yet.
        let opt = Array.from(sel.options).find(o => o.value === lane);
        if (!opt) {
          opt = document.createElement("option");
          opt.value = lane; opt.textContent = shortModel(lane);
          sel.appendChild(opt);
        }
        sel.value = lane;
      }
      $("tacticalInput").focus();
      $("tacticalInput").scrollIntoView({ behavior: "smooth", block: "center" });
    });
  });
}

$("reportsThisChallengeOnly")?.addEventListener("change", renderReports);

/* ══════════════════════════════════════════════════════════════════════════
   Intervene tab
   ══════════════════════════════════════════════════════════════════════════ */
$("strategicForm").addEventListener("submit", async e => {
  e.preventDefault();
  const name = S.selectedName, critique = $("strategicInput").value.trim();
  if (!name || !critique) return;
  $("strategicBtn").disabled = true;
  const r = await api("/api/runtime/advisor-intervene", { method: "POST", body: JSON.stringify({ challenge_name: name, critique }) });
  $("strategicBtn").disabled = false;
  const msg = r.body.result ?? r.body.error ?? r.body.detail ?? (r.ok ? "sent" : "failed");
  flashResult("strategicResult", msg, r.ok);
  logActivity(`Strategic override → ${name}: ${msg}`, r.ok ? "al-ok" : "al-err");
  if (r.ok) $("strategicInput").value = "";
});

$("tacticalForm").addEventListener("submit", async e => {
  e.preventDefault();
  const name = S.selectedName, lane = $("tacticalLaneSelect").value, insights = $("tacticalInput").value.trim();
  if (!name || !lane || !insights) return;
  $("tacticalBtn").disabled = true;
  const r = await api("/api/runtime/lane-bump", { method: "POST", body: JSON.stringify({ challenge_name: name, lane_id: lane, insights }) });
  $("tacticalBtn").disabled = false;
  const msg = r.body.result ?? r.body.error ?? r.body.detail ?? (r.ok ? "sent" : "failed");
  flashResult("tacticalResult", msg, r.ok);
  logActivity(`Tactical → ${shortModel(lane)} on ${name}: ${msg}`, r.ok ? "al-ok" : "al-err");
  if (r.ok) $("tacticalInput").value = "";
});

/* ══════════════════════════════════════════════════════════════════════════
   Health tab
   ══════════════════════════════════════════════════════════════════════════ */
function renderHealth() {
  const ch   = S.challenges[S.selectedName];
  const list = $("healthList");
  if (!ch?.lanes.length) { list.innerHTML = '<div class="empty">No active lanes.</div>'; $("bumpAllStaleBtn").disabled = true; return; }
  const problems = ch.lanes.filter(l => l.runtime_health !== "healthy" || l.state === "failed");
  $("bumpAllStaleBtn").disabled = !problems.length;
  list.innerHTML = "";
  const sorted = [...ch.lanes].sort((a, b) => {
    const r = h => ({ stale: 0, error: 1, resetting: 2, healthy: 3 })[h] ?? 4;
    return r(a.runtime_health) - r(b.runtime_health);
  });
  for (const lane of sorted) {
    const h   = lane.runtime_health ?? "healthy";
    const row = document.createElement("div");
    row.className = `health-row ${h}`;
    row.innerHTML = `
      <span class="health-dot ${h}"></span>
      <span class="health-model" title="${esc(lane.model_spec)}">${esc(shortModel(lane.model_spec))}</span>
      <span class="health-state ${h}">${esc(h)}</span>
      <span style="font-size:11px;color:var(--muted)">${lane.steps} steps</span>
      <button class="ghost-btn small" data-lane="${esc(lane.model_spec)}">Bump</button>
    `;
    list.appendChild(row);
  }
  list.querySelectorAll("[data-lane]").forEach(btn =>
    btn.addEventListener("click", async () => {
      const model = btn.dataset.lane;
      const r = await api("/api/runtime/lane-bump", {
        method: "POST", body: JSON.stringify({
          challenge_name: S.selectedName, lane_id: model,
          insights: "You appear stuck or in an error state. Stop your current approach, take stock of what you know, and try a fundamentally different strategy.",
        }),
      });
      logActivity(r.ok ? `Bumped ${shortModel(model)}` : `Bump failed: ${r.body.error}`, r.ok ? "al-ok" : "al-err");
    })
  );
}

$("bumpAllStaleBtn").addEventListener("click", async () => {
  const name = S.selectedName; const ch = S.challenges[name]; if (!name || !ch) return;
  const problems = ch.lanes.filter(l => l.runtime_health !== "healthy" || l.state === "failed");
  await Promise.all(problems.map(l => api("/api/runtime/lane-bump", {
    method: "POST", body: JSON.stringify({
      challenge_name: name, lane_id: l.model_spec,
      insights: "You appear stuck or in an error state. Stop what you are doing, reconsider from scratch, and try a completely different approach.",
    }),
  })));
  logActivity(`Bumped ${problems.length} stale/error lanes on ${name}`, "al-info");
});

/* ══════════════════════════════════════════════════════════════════════════
   Trace tab
   ══════════════════════════════════════════════════════════════════════════ */
$("refreshTraceBtn").addEventListener("click", async () => {
  const name = S.selectedName, model = $("traceLaneSelect").value, lastN = $("traceLastN").value;
  if (!name || !model) { $("traceOutput").textContent = "Select a challenge and lane first."; return; }
  $("traceOutput").textContent = "Loading…";
  const r = await api(`/api/runtime/solver-trace?${new URLSearchParams({ challenge_name: name, model_spec: model, last_n: lastN })}`);
  $("traceOutput").textContent = r.ok ? (r.body.trace || "(empty)") : `Error ${r.status}: ${r.body.error ?? r.body.detail}`;
});

/* ══════════════════════════════════════════════════════════════════════════
   Config tab
   ══════════════════════════════════════════════════════════════════════════ */
$("loadConfigBtn").addEventListener("click", loadConfig);

async function loadConfig() {
  const name = S.selectedName; if (!name) return;
  const r = await api(`/api/runtime/challenge-config?challenge_name=${encodeURIComponent(name)}`);
  if (!r.ok) { $("configEmpty").textContent = r.body.error ?? "Challenge not found."; $("configEditor").style.display = "none"; return; }
  const cfg  = r.body;
  const conn = cfg.effective?.connection ?? {};
  $("cfgHost").value     = conn.host ?? "";
  $("cfgPort").value     = conn.port ?? "";
  $("cfgProto").value    = conn.protocol ?? "";
  $("cfgCategory").value = cfg.effective?.category ?? "";
  $("cfgHint").value     = (cfg.override?.hints ?? []).join("\n");
  $("configEditor").style.display = "";
  $("configEmpty").style.display  = "none";
}

$("saveConfigBtn").addEventListener("click", async () => {
  const name = S.selectedName; if (!name) return;
  const host  = $("cfgHost").value.trim();
  const port  = parseInt($("cfgPort").value, 10);
  const proto = $("cfgProto").value;
  const cat   = $("cfgCategory").value.trim();
  const hint  = $("cfgHint").value.trim();
  const patch = {};
  if (host || port || proto) {
    patch.connection = {};
    if (host) patch.connection.host = host;
    if (!isNaN(port) && port > 0) patch.connection.port = port;
    if (proto) patch.connection.protocol = proto;
  }
  if (cat) patch.category = cat;
  if (hint) patch.hints = hint.split("\n").filter(Boolean);
  const r = await api("/api/runtime/challenge-config", { method: "PATCH", body: JSON.stringify({ challenge_name: name, override: patch, replace: false }) });
  const msg = r.ok ? "Saved." : (r.body.error ?? r.body.detail ?? "Failed");
  flashResult("configResult", msg, r.ok);
  logActivity(`Config update ${name}: ${msg}`, r.ok ? "al-ok" : "al-err");
});

$("resetConfigBtn").addEventListener("click", async () => {
  const name = S.selectedName; if (!name) return;
  if (!confirm(`Reset config override for "${name}"?`)) return;
  const r = await api(`/api/runtime/challenge-config?challenge_name=${encodeURIComponent(name)}`, { method: "DELETE" });
  flashResult("configResult", r.ok ? "Override reset." : (r.body.error ?? "Failed"), r.ok);
  logActivity(`Config reset ${name}`, r.ok ? "al-ok" : "al-err");
  if (r.ok) loadConfig();
});

/* ══════════════════════════════════════════════════════════════════════════
   Parse URL
   ══════════════════════════════════════════════════════════════════════════ */
$("parseForm").addEventListener("submit", async e => {
  e.preventDefault();
  const url = $("parseUrl").value.trim(); if (!url) return;
  const res = $("parseResult");
  res.textContent = "Parsing…"; res.className = "parse-result";
  const r = await api("/api/runtime/parse-challenge-url", { method: "POST", body: JSON.stringify({ url }) });
  if (r.ok) {
    const n = r.body.challenges?.length ?? 0;
    res.textContent = `Found ${n} challenge(s)${r.body.competition_name ? " — " + r.body.competition_name : ""}.`;
    res.className   = "parse-result ok";
    logActivity(`Parsed: ${n} challenges`, "al-ok");
  } else {
    res.textContent = r.body.error ?? r.body.detail ?? "Parse failed";
    res.className   = "parse-result error";
  }
});

/* ══════════════════════════════════════════════════════════════════════════
   Fetch all challenges from CTFd
   ══════════════════════════════════════════════════════════════════════════ */
$("fetchChallengesBtn").addEventListener("click", async () => {
  const btn = $("fetchChallengesBtn");
  btn.disabled = true; btn.textContent = "⏳ Fetching + importing…";
  // Auto-import is on by default; the server downloads attachments + writes
  // metadata.yml for each remote challenge under challenges/<ctfd-host>/.
  const r = await api("/api/runtime/fetch-challenges");
  btn.disabled = false; btn.textContent = "🔄 Fetch";
  if (r.ok) {
    const n = r.body.count ?? r.body.challenges?.length ?? 0;
    const summary = r.body.import_summary ?? { imported: [], skipped: [], failed: [] };
    const imp = (summary.imported ?? []).length;
    const skip = (summary.skipped ?? []).length;
    const fail = (summary.failed ?? []).length;
    const parts = [`${n} total`];
    if (imp)  parts.push(`${imp} imported`);
    if (skip) parts.push(`${skip} already on disk`);
    if (fail) parts.push(`${fail} failed`);
    const summaryText = parts.join(", ");
    logActivity(`Fetched: ${summaryText}`, fail > 0 ? "al-err" : "al-ok");
    pushEvent(`🔄 ${summaryText}${summary.error ? ` (${summary.error})` : ""}`,
              fail > 0 ? "warn" : "success");
    if (fail > 0) {
      const firstFew = (summary.failed ?? []).slice(0, 3).map(f =>
        Array.isArray(f) ? `${f[0]}: ${f[1]}` : String(f)
      ).join("; ");
      logActivity(`Import errors: ${firstFew}`, "al-err");
    }
    // Force an immediate snapshot refresh so newly imported challenges appear.
    pollSnapshot();
  } else {
    logActivity(`Fetch failed: ${r.body.error ?? r.status}`, "al-err");
  }
});

/* ══════════════════════════════════════════════════════════════════════════
   Swarm controls
   ══════════════════════════════════════════════════════════════════════════ */
async function swarmCmd(ep, extra = {}) {
  const name = S.selectedName; if (!name) return null;
  const r = await api(ep, { method: "POST", body: JSON.stringify({ challenge_name: name, ...extra }) });
  logActivity(r.ok ? `${ep.split("/").pop()}: ${name}` : `Failed: ${r.body.error ?? r.body.detail}`, r.ok ? "al-ok" : "al-err");
  return r;
}
$("spawnBtn").addEventListener("click",   () => swarmCmd("/api/runtime/spawn-swarm"));
$("killBtn").addEventListener("click",    () => swarmCmd("/api/runtime/kill-swarm"));
$("restartBtn").addEventListener("click", async () => { await swarmCmd("/api/runtime/kill-swarm"); await swarmCmd("/api/runtime/spawn-swarm"); });
$("priorityOnBtn").addEventListener("click",  () => swarmCmd("/api/runtime/set-challenge-priority", { priority: true }));
$("priorityOffBtn").addEventListener("click", () => swarmCmd("/api/runtime/set-challenge-priority", { priority: false }));

/* ─── Check Instance ─────────────────────────────────────────────────────── */
$("checkInstanceBtn").addEventListener("click", async () => {
  const name = S.selectedName; if (!name) return;
  $("instanceResult").textContent = "Probing…"; $("instanceResult").className = "instance-result";
  const r = await api("/api/runtime/check-instance", { method: "POST", body: JSON.stringify({ challenge_name: name }) });
  if (r.ok) {
    const ready = r.body.ready;
    const probe = r.body.probe ?? {};
    const summary = ready ? `✅ Service reachable (${probe.host ?? ""}:${probe.port ?? ""})` : `❌ Not reachable: ${probe.error ?? "timeout"}`;
    $("instanceResult").textContent = summary;
    $("instanceResult").className   = "instance-result " + (ready ? "ok" : "error");
    logActivity(`Instance check ${name}: ${summary}`, ready ? "al-ok" : "al-err");
  } else {
    const msg = r.body.error ?? r.body.detail ?? "Failed";
    $("instanceResult").textContent = msg;
    $("instanceResult").className   = "instance-result error";
    logActivity(`Instance check failed: ${msg}`, "al-err");
  }
});

/* ══════════════════════════════════════════════════════════════════════════
   Submit flag
   ══════════════════════════════════════════════════════════════════════════ */
$("submitFlagForm").addEventListener("submit", async e => {
  e.preventDefault();
  const name = S.selectedName, flag = $("submitFlagInput").value.trim();
  if (!name || !flag) return;
  const r = await api("/api/runtime/submit-flag", { method: "POST", body: JSON.stringify({ challenge_name: name, flag }) });
  const msg = r.body.result ?? r.body.error ?? r.body.detail ?? (r.ok ? "submitted" : "failed");
  logActivity(`Submit ${name}: ${msg}`, r.ok ? "al-ok" : "al-err");
  if (r.ok) { $("submitFlagInput").value = ""; pushEvent(`🏁 Flag submitted for ${name}: ${msg}`, "success"); }
});

/* ══════════════════════════════════════════════════════════════════════════
   Mark solved externally
   ══════════════════════════════════════════════════════════════════════════ */
$("markSolvedForm").addEventListener("submit", async e => {
  e.preventDefault();
  const name = S.selectedName, flag = $("markSolvedFlag").value.trim(), note = $("markSolvedNote").value.trim();
  if (!name || !flag) return;
  const r = await api("/api/runtime/mark-solved", { method: "POST", body: JSON.stringify({ challenge_name: name, flag, note }) });
  const msg = r.body.result ?? r.body.error ?? r.body.detail ?? (r.ok ? "marked" : "failed");
  flashResult("markSolvedResult", msg, r.ok);
  logActivity(`Mark solved ${name}: ${msg}`, r.ok ? "al-ok" : "al-err");
  if (r.ok) { $("markSolvedFlag").value = ""; $("markSolvedNote").value = ""; pushEvent(`🏆 Externally solved: ${name}`, "success"); }
});

/* ══════════════════════════════════════════════════════════════════════════
   CTFd session cookie management
   ══════════════════════════════════════════════════════════════════════════ */
function renderCookieStatus(summary, probe) {
  const dot   = $("cookieDot");
  const info  = $("cookieStatus");
  if (!summary) return;

  let cls = summary.configured ? "unknown" : "missing";
  let title = summary.configured ? "Cookie loaded — probe to verify" : "No session cookie loaded";
  if (probe?.ok) { cls = "ok"; title = `Valid (user: ${probe.user || "unknown"})`; }
  else if (probe && probe.ok === false) { cls = "invalid"; title = probe.error || "Probe failed"; }
  dot.className = "cookie-dot " + cls;
  dot.title = title;

  const parts = [];
  parts.push(`<div><strong>${summary.configured ? "Configured" : "Not configured"}</strong> · ${esc(summary.platform || "remote")} · <code>${esc(summary.base_url || "—")}</code></div>`);
  if (summary.configured) {
    parts.push(`<div>Length: ${summary.length} chars · Cookies: ${summary.cookie_count}</div>`);
    if (summary.cookie_names?.length) {
      parts.push(`<div>${summary.cookie_names.map(n => `<span class="cookie-chip">${esc(n)}</span>`).join("")}</div>`);
    }
    if (summary.source) parts.push(`<div>Source: ${esc(summary.source)}</div>`);
  }
  if (summary.username)      parts.push(`<div>CTFd user: ${esc(summary.username)}</div>`);
  if (summary.token_present) parts.push(`<div>API token: configured</div>`);
  if (probe?.ok)   parts.push(`<div style="color:var(--green)">✅ Probe OK · user: ${esc(probe.user || "?")}</div>`);
  if (probe && probe.ok === false) parts.push(`<div style="color:var(--red)">❌ ${esc(probe.error || "probe failed")}</div>`);
  info.innerHTML = parts.join("");
}

async function loadCookieStatus() {
  const r = await api("/api/runtime/cookie");
  if (r.ok) renderCookieStatus(r.body);
  else      logActivity(`Cookie status fetch failed: ${r.body.error ?? r.status}`, "al-err");
}

$("cookieSaveBtn")?.addEventListener("click", async () => {
  const cookie = $("cookieInput").value;
  if (!cookie.trim()) { flashResult("cookieResult", "Paste a Cookie header first.", false); return; }
  const test = $("cookieTestOnSave").checked;
  const btn  = $("cookieSaveBtn");
  btn.disabled = true; const orig = btn.textContent; btn.textContent = test ? "Saving + testing…" : "Saving…";
  const r = await api("/api/runtime/cookie", { method: "PUT", body: JSON.stringify({ cookie, test }) });
  btn.disabled = false; btn.textContent = orig;
  if (r.ok) {
    renderCookieStatus(r.body.cookie, r.body.probe);
    $("cookieInput").value = "";
    const probeMsg = r.body.probe?.ok ? " (probe OK)" : r.body.probe?.error ? ` (probe: ${r.body.probe.error})` : "";
    flashResult("cookieResult", `Saved ${r.body.cookie.length} chars` + probeMsg, r.body.probe?.ok !== false);
    logActivity(`Cookie saved (${r.body.cookie.cookie_count} cookies)${probeMsg}`, r.body.probe?.ok === false ? "al-err" : "al-ok");
    pushEvent(`🍪 Session cookie updated (${r.body.cookie.cookie_count} cookies)`, "info");
  } else {
    flashResult("cookieResult", r.body.error ?? "Save failed", false);
    logActivity(`Cookie save failed: ${r.body.error ?? r.status}`, "al-err");
  }
});

$("cookieTestBtn")?.addEventListener("click", async () => {
  const btn = $("cookieTestBtn");
  btn.disabled = true; const orig = btn.textContent; btn.textContent = "Testing…";
  const r = await api("/api/runtime/cookie/test", { method: "POST" });
  btn.disabled = false; btn.textContent = orig;
  if (r.ok) {
    renderCookieStatus(r.body.cookie, r.body.probe);
    const msg = r.body.probe?.ok ? `OK — user: ${r.body.probe.user || "?"}` : r.body.probe?.error || "probe failed";
    flashResult("cookieResult", msg, r.body.probe?.ok === true);
    logActivity(`Cookie probe: ${msg}`, r.body.probe?.ok === true ? "al-ok" : "al-err");
  } else {
    flashResult("cookieResult", r.body.error ?? "Probe failed", false);
  }
});

$("cookieClearBtn")?.addEventListener("click", async () => {
  if (!confirm("Clear the session cookie? Solvers will lose authenticated access.")) return;
  const r = await api("/api/runtime/cookie", { method: "DELETE" });
  if (r.ok) {
    renderCookieStatus(r.body.cookie);
    flashResult("cookieResult", "Cookie cleared.", true);
    logActivity("Cookie cleared", "al-info");
    pushEvent("🍪 Session cookie cleared", "warn");
  } else {
    flashResult("cookieResult", r.body.error ?? "Clear failed", false);
  }
});

/* ══════════════════════════════════════════════════════════════════════════
   Broadcast
   ══════════════════════════════════════════════════════════════════════════ */
$("broadcastForm").addEventListener("submit", async e => {
  e.preventDefault();
  const name = S.selectedName, msg = $("broadcastInput").value.trim();
  if (!name || !msg) return;
  const r = await api("/api/runtime/broadcast", { method: "POST", body: JSON.stringify({ challenge_name: name, message: msg }) });
  logActivity(r.ok ? `Broadcast → ${name}` : `Broadcast failed: ${r.body.error}`, r.ok ? "al-ok" : "al-err");
  if (r.ok) $("broadcastInput").value = "";
});

/* ══════════════════════════════════════════════════════════════════════════
   Concurrency
   ══════════════════════════════════════════════════════════════════════════ */
$("maxForm").addEventListener("submit", async e => {
  e.preventDefault();
  const max = parseInt($("maxInput").value, 10);
  if (isNaN(max)) return;
  const r = await api("/api/runtime/set-max-challenges", { method: "POST", body: JSON.stringify({ max_active: max }) });
  logActivity(r.ok ? `Max active → ${max}` : `Failed: ${r.body.error}`, r.ok ? "al-ok" : "al-err");
});

/* ══════════════════════════════════════════════════════════════════════════
   History
   ══════════════════════════════════════════════════════════════════════════ */
$("saveResultBtn").addEventListener("click", () => {
  const name = S.selectedName; const ch = S.challenges[name]; if (!name || !ch) return;
  const blob = new Blob([JSON.stringify({ name, ...ch }, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name.replace(/[^a-z0-9_-]/gi, "_") + "_result.json";
  a.click(); URL.revokeObjectURL(a.href);
  logActivity(`Saved snapshot for ${name}`, "al-info");
});

$("clearHistoryBtn").addEventListener("click", async () => {
  const name = S.selectedName; if (!name) return;
  const del  = $("deleteTracesChk").checked;
  if (!confirm(`Clear solve history for "${name}"?${del ? "\n\nAlso deleting trace files." : ""}`)) return;
  const r = await api("/api/runtime/clear-challenge-history", { method: "POST", body: JSON.stringify({ challenge_name: name, delete_traces: del }) });
  const msg = r.body.result ?? r.body.error ?? (r.ok ? "cleared" : "failed");
  logActivity(`History ${name}: ${msg}`, r.ok ? "al-ok" : "al-err");
  if (r.ok) pushEvent(`🗑 History cleared: ${name}`, "warn");
});

/* ══════════════════════════════════════════════════════════════════════════
   Event feed
   ══════════════════════════════════════════════════════════════════════════ */
$("clearEventsBtn").addEventListener("click", () => {
  $("eventFeed").innerHTML = '<div class="empty">Waiting for events…</div>';
});

/* ══════════════════════════════════════════════════════════════════════════
   Boot
   ══════════════════════════════════════════════════════════════════════════ */
function init() {
  connectSnapSse();
  connectEventSse();
  pollSnapshot();
  loadCookieStatus();
}
init();
