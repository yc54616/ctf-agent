import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

function createLocalStorage(seed = {}) {
  const store = new Map(Object.entries(seed));
  return {
    getItem(key) {
      return store.has(key) ? store.get(key) : null;
    },
    setItem(key, value) {
      store.set(key, String(value));
    },
    removeItem(key) {
      store.delete(key);
    },
    clear() {
      store.clear();
    },
  };
}

function loadOperatorUiTestHarness({ localStorageSeed = {} } = {}) {
  const sourcePath = path.resolve("backend/static/operator_ui.js");
  const source = fs.readFileSync(sourcePath, "utf8");
  const instrumented = source.replace(
    /\nmain\(\);\s*$/,
    "\nglobalThis.__operatorUiTest = { state, els, bindEvents, challengeBuckets, syncSelections, syncSnapshotAlerts, handleBrowserNotificationsClick, getSelectedChallengeData, visibleLaneEntries, formatElapsed, renderSchedulerControls, populateChallengeConfigForm, parseStageWorkflowText, workflowStageDefinitions, effectiveInstanceStages, syncStageWorkflowText };\n"
  );

  const notifications = [];
  const localStorage = createLocalStorage(localStorageSeed);

  const makeElement = () => ({
    value: "",
    checked: false,
    textContent: "",
    innerHTML: "",
    children: [],
    dataset: {},
    disabled: false,
    _listeners: {},
    addEventListener(type, handler) {
      if (!this._listeners[type]) {
        this._listeners[type] = [];
      }
      this._listeners[type].push(handler);
    },
    async trigger(type, event = {}) {
      const handlers = this._listeners[type] || [];
      for (const handler of handlers) {
        await handler({ target: this, currentTarget: this, ...event });
      }
    },
    querySelectorAll() {
      return [];
    },
    prepend(child) {
      this.children.unshift(child);
    },
    removeChild(child) {
      const index = this.children.indexOf(child);
      if (index >= 0) {
        this.children.splice(index, 1);
      } else {
        this.children.pop();
      }
    },
    appendChild() {},
    setAttribute() {},
    getAttribute() {
      return "";
    },
    closest() {
      return null;
    },
  });

  const elementMap = new Map();
  const getElement = (id) => {
    if (!elementMap.has(id)) {
      elementMap.set(id, makeElement());
    }
    return elementMap.get(id);
  };

  const context = {
    console,
    setInterval() {
      return 0;
    },
    setTimeout() {
      return 0;
    },
    clearInterval() {},
    clearTimeout() {},
    fetch() {
      throw new Error("fetch should not be called in operator_ui unit tests");
    },
    window: {
      focus() {},
    },
    document: {
      activeElement: null,
      getElementById(id) {
        return getElement(id);
      },
      createElement() {
        return makeElement();
      },
    },
    URLSearchParams,
    EventSource: class {},
    localStorage,
    Notification: class FakeNotification {
      static permission = "default";

      static async requestPermission() {
        return FakeNotification.permission;
      }

      constructor(title, options = {}) {
        this.title = title;
        this.options = options;
        this.onclick = null;
        notifications.push({ title, options });
      }

      close() {}
    },
  };
  context.globalThis = context;
  vm.runInNewContext(instrumented, context, { filename: sourcePath });
  return {
    ...context.__operatorUiTest,
    localStorage,
    notifications,
    Notification: context.Notification,
  };
}

test("syncSelections selects pending challenges without throwing", () => {
  const harness = loadOperatorUiTestHarness();
  harness.state.snapshot = {
    active_swarms: {},
    finished_swarms: {},
    pending_swarms: {
      "sanity check": {
        challenge: "sanity check",
        agents: {},
        step_count: 12,
        status: "pending",
        flag_candidates: {},
      },
    },
    pending_challenge_entries: [
      {
        challenge_name: "sanity check",
        priority: true,
        reason: "priority_waiting",
        local_preloaded: false,
      },
    ],
    results: {
      "sanity check": {
        status: "pending",
        step_count: 12,
      },
    },
  };

  assert.doesNotThrow(() => harness.syncSelections());
  assert.equal(harness.state.selectedChallenge, "sanity check");
  assert.equal(harness.getSelectedChallengeData().bucket, "pending");
});

test("hide error lanes filters error lifecycles and keeps a visible selection", () => {
  const harness = loadOperatorUiTestHarness();
  harness.state.hideErrorLanes = true;
  harness.state.snapshot = {
    active_swarms: {
      demo: {
        challenge: "demo",
        agents: {
          "codex/gpt-5.4": { lifecycle: "error", step_count: 3 },
          "codex/gpt-5.4-mini": { lifecycle: "busy", step_count: 9 },
          "gemini/2.5": { lifecycle: "quota_error", step_count: 1 },
        },
      },
    },
    finished_swarms: {},
    pending_swarms: {},
    pending_challenge_entries: [],
    results: {},
  };
  harness.state.selectedChallenge = "demo";
  harness.state.selectedLane = "codex/gpt-5.4";

  harness.syncSelections();

  const visible = harness.visibleLaneEntries(harness.getSelectedChallengeData().data);
  assert.deepEqual(
    Array.from(visible, ([modelSpec]) => modelSpec),
    ["codex/gpt-5.4-mini"]
  );
  assert.equal(harness.state.selectedLane, "codex/gpt-5.4-mini");
});

test("hide error lanes preference restores from local storage", () => {
  const harness = loadOperatorUiTestHarness({
    localStorageSeed: { "ctf-agent:hide-error-lanes": "true" },
  });

  assert.equal(harness.state.hideErrorLanes, true);
  assert.equal(harness.els.hideErrorLanes.checked, true);
});

test("hide error lanes preference saves when the checkbox changes", async () => {
  const harness = loadOperatorUiTestHarness();
  harness.bindEvents();

  harness.els.hideErrorLanes.checked = true;
  await harness.els.hideErrorLanes.trigger("change");

  assert.equal(harness.localStorage.getItem("ctf-agent:hide-error-lanes"), "true");
  assert.equal(harness.state.hideErrorLanes, true);
});

test("challengeBuckets keeps live shared findings when results only have empty maps", () => {
  const harness = loadOperatorUiTestHarness();
  harness.state.snapshot = {
    active_swarms: {},
    finished_swarms: {
      demo: {
        challenge: "demo",
        shared_finding: "Potential admin API at /api/v1/k8s/get",
        shared_findings: {
          "codex/gpt-5.4": {
            kind: "finding_ref",
            summary: "Potential admin API at /api/v1/k8s/get",
            digest_path: "/challenge/shared-artifacts/.advisor/finding.digest.md",
          },
        },
        agents: {},
      },
    },
    pending_swarms: {},
    pending_challenge_entries: [],
    results: {
      demo: {
        status: "flag_found",
        shared_findings: {},
        flag_candidates: {},
      },
    },
  };

  const buckets = harness.challengeBuckets(harness.state.snapshot);

  assert.ok(buckets.finished.demo.shared_findings["codex/gpt-5.4"]);
  assert.equal(
    buckets.finished.demo.shared_findings["codex/gpt-5.4"].summary,
    "Potential admin API at /api/v1/k8s/get"
  );
});

test("formatElapsed uses compact English duration labels", () => {
  const harness = loadOperatorUiTestHarness();

  assert.equal(harness.formatElapsed(81), "1m 21s");
  assert.equal(harness.formatElapsed(3725), "1h 2m 5s");
  assert.equal(harness.formatElapsed(93784), "1d 2h 3m 4s");
});

test("renderSchedulerControls preserves max active draft while editing", () => {
  const harness = loadOperatorUiTestHarness();
  harness.state.snapshot = { max_concurrent_challenges: 4 };
  harness.state.maxChallengesDirty = true;
  harness.state.maxChallengesDraft = "7";
  harness.els.maxChallengesInput.value = "7";

  harness.renderSchedulerControls({ bucket: "", data: null }, null);

  assert.equal(harness.els.maxChallengesInput.value, "7");
});

test("renderSchedulerControls syncs max active input from snapshot when idle", () => {
  const harness = loadOperatorUiTestHarness();
  harness.state.snapshot = { max_concurrent_challenges: 6 };
  harness.state.maxChallengesDirty = false;
  harness.els.maxChallengesInput.value = "0";

  harness.renderSchedulerControls({ bucket: "", data: null }, null);

  assert.equal(harness.els.maxChallengesInput.value, "6");
});

test("syncSelections keeps the last selected lane when a challenge moves to pending without live agents", () => {
  const harness = loadOperatorUiTestHarness();
  harness.state.selectedChallenge = "sanity check";
  harness.state.selectedLane = "codex/gpt-5.4";
  harness.state.selectedTrace = "trace-sanity_check-gpt-5.4-20260421-120000.jsonl";
  harness.state.snapshot = {
    active_swarms: {},
    finished_swarms: {},
    pending_swarms: {
      "sanity check": {
        challenge: "sanity check",
        agents: {},
        step_count: 12,
        status: "pending",
        flag_candidates: {},
      },
    },
    pending_challenge_entries: [
      {
        challenge_name: "sanity check",
        priority: true,
        reason: "priority_waiting",
        local_preloaded: false,
      },
    ],
    results: {
      "sanity check": {
        status: "pending",
        step_count: 12,
      },
    },
  };

  harness.syncSelections();

  assert.equal(harness.state.selectedLane, "codex/gpt-5.4");
  assert.equal(
    harness.state.selectedTrace,
    "trace-sanity_check-gpt-5.4-20260421-120000.jsonl"
  );
});

test("syncSnapshotAlerts logs each server alert once", () => {
  const harness = loadOperatorUiTestHarness();

  harness.syncSnapshotAlerts({
    ui_alerts: [
      {
        id: "alert-1",
        challenge_name: "aeBPF",
        lane_id: "codex/gpt-5.4",
        message: 'candidate "BLOCKED_NO_FLAG" rejected (placeholder sentinel.); cooling down 15s before continuing',
        tone: "warn",
      },
    ],
  });
  harness.syncSnapshotAlerts({
    ui_alerts: [
      {
        id: "alert-1",
        challenge_name: "aeBPF",
        lane_id: "codex/gpt-5.4",
        message: 'candidate "BLOCKED_NO_FLAG" rejected (placeholder sentinel.); cooling down 15s before continuing',
        tone: "warn",
      },
    ],
  });

  assert.equal(harness.els.activityLog.children.length, 1);
});

test("syncSnapshotAlerts also dispatches browser notifications when granted", () => {
  const harness = loadOperatorUiTestHarness();
  harness.Notification.permission = "granted";

  harness.syncSnapshotAlerts({
    ui_alerts: [
      {
        id: "alert-2",
        challenge_name: "aeBPF",
        lane_id: "codex/gpt-5.4",
        message: 'candidate "BLOCKED_NO_FLAG" rejected (placeholder sentinel.); cooling down 15s before continuing',
        tone: "warn",
      },
    ],
  });
  harness.syncSnapshotAlerts({
    ui_alerts: [
      {
        id: "alert-2",
        challenge_name: "aeBPF",
        lane_id: "codex/gpt-5.4",
        message: 'candidate "BLOCKED_NO_FLAG" rejected (placeholder sentinel.); cooling down 15s before continuing',
        tone: "warn",
      },
    ],
  });

  assert.equal(harness.notifications.length, 1);
  assert.match(harness.notifications[0].title, /CTF Agent/);
  assert.match(harness.notifications[0].options.body, /BLOCKED_NO_FLAG/);
});

test("handleBrowserNotificationsClick requests permission and updates the button", async () => {
  const harness = loadOperatorUiTestHarness();
  harness.Notification.permission = "granted";

  await harness.handleBrowserNotificationsClick();

  assert.equal(harness.els.browserNotificationsBtn.textContent, "Browser alerts enabled");
  assert.equal(harness.els.activityLog.children.length, 1);
});

test("populateChallengeConfigForm fills stage workflow form fields", () => {
  const harness = loadOperatorUiTestHarness();
  harness.state.selectedChallenge = "demo";
  harness.state.challengeConfigFor = "demo";

  harness.populateChallengeConfigForm({
    effective: {
      current_stage: "public_lab",
      instance_stages: [
        {
          id: "public_lab",
          title: "Public Lab",
          description: "Deploy from the portal first.",
          manual_action: "deploy_from_portal",
          notes: "Wait for the target hostname.",
          status: "pending",
        },
      ],
    },
  });

  assert.equal(harness.els.challengeConfigStageIdInput.value, "public_lab");
  assert.equal(harness.els.challengeConfigStageTitleInput.value, "Public Lab");
  assert.equal(harness.els.challengeConfigStageActionInput.value, "deploy_from_portal");
  assert.equal(
    harness.els.challengeConfigStageDescriptionInput.value,
    "Deploy from the portal first."
  );
  assert.equal(
    harness.els.challengeConfigStageNotesInput.value,
    "Wait for the target hostname."
  );
});

test("parseStageWorkflowText still supports advanced JSON editing", () => {
  const harness = loadOperatorUiTestHarness();
  const parsed = harness.parseStageWorkflowText(
    JSON.stringify([
      {
        id: "public_lab",
        title: "Public Lab",
        manual_action: "deploy_from_portal",
        endpoints: [
          {
            id: "shell",
            title: "Shell",
            connection: { host: "host8.dreamhack.games", port: 17039 },
          },
        ],
      },
    ])
  );

  assert.equal(parsed.length, 1);
  assert.equal(parsed[0].id, "public_lab");
  assert.equal(parsed[0].endpoints[0].id, "shell");
  assert.equal(parsed[0].endpoints[0].connection.port, 17039);
});
