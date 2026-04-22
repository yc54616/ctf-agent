import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

function loadOperatorUiTestHarness() {
  const sourcePath = path.resolve("backend/static/operator_ui.js");
  const source = fs.readFileSync(sourcePath, "utf8");
  const instrumented = source.replace(
    /\nmain\(\);\s*$/,
    "\nglobalThis.__operatorUiTest = { state, els, challengeBuckets, syncSelections, getSelectedChallengeData, visibleLaneEntries, formatElapsed, renderSchedulerControls };\n"
  );

  const makeElement = () => ({
    value: "",
    checked: false,
    textContent: "",
    innerHTML: "",
    children: [],
    dataset: {},
    disabled: false,
    addEventListener() {},
    querySelectorAll() {
      return [];
    },
    prepend() {},
    removeChild() {},
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
    clearInterval() {},
    fetch() {
      throw new Error("fetch should not be called in operator_ui unit tests");
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
  };
  context.globalThis = context;
  vm.runInNewContext(instrumented, context, { filename: sourcePath });
  return context.__operatorUiTest;
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
