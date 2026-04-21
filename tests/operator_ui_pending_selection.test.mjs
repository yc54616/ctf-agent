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
    "\nglobalThis.__operatorUiTest = { state, challengeBuckets, syncSelections, getSelectedChallengeData, visibleLaneEntries };\n"
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
