import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { agentModel, lifecycleModel } from "./workflow.js";

// chaos-ui.js is a plain browser script (this package is "type": "module"), so evaluate it the way a page would
const U = (() => { const m = { exports: {} }; new Function("module", readFileSync(new URL("../chaos-ui.js", import.meta.url), "utf8"))(m); return m.exports; })();
const META = ["topology", "planner", "policy", "executor", "guard", "analysis"].map((id) => ({ id, name: id, summary: "s", module: `${id}.py`, implementation: "implemented", notes: [] }));
const proposal = { target_service: "quote", failure_type: "NETWORK_DELAY", source: "rank_agent", planner_note: "model x" };
const allowed = { allowed: true, checks: [{ name: "a", passed: true }, { name: "b", passed: true }] };
const blocked = { allowed: false, checks: [{ name: "Target is not protected", passed: false }, { name: "ok", passed: true }] };
const topology = { state: "ok", nodes: 21, edges: 32, protected: 3 };
const states = (m) => Object.fromEntries(m.steps.map((s) => [s.id, s.state]));
const rows = (ctx) => Object.fromEntries(agentModel({ meta: META, topology, ...ctx }).map((r) => [r.id, r]));

test("idle with no proposal: every step pending, components idle, topology from the real response", () => {
  assert.ok(lifecycleModel({}, U).steps.every((s) => s.state === "pending"));
  const r = rows({});
  assert.deepEqual(["planner", "policy", "executor", "guard", "analysis"].map((k) => r[k].runtime), Array(5).fill("idle"));
  assert.equal(r.topology.latest, "21 services, 32 dependencies discovered, 3 protected");
});

test("draft then validated: approval is the human checkpoint and the executor waits instead of running", () => {
  assert.equal(states(lifecycleModel({ hasProposal: true }, U)).draft, "current");
  const m = lifecycleModel({ hasProposal: true, policy: allowed }, U), r = rows({ proposal, policy: allowed });
  assert.deepEqual([states(m).draft, states(m).validated, states(m).approved], ["done", "done", "current"]);
  assert.ok(m.steps.find((s) => s.id === "approved").checkpoint);
  assert.equal(r.executor.runtime, "waiting");
  assert.match(r.executor.latest, /Approval required before injecting network delay on quote/);
  assert.match(r.planner.latest, /planning agent.*model x/);
});

test("a policy rejection fails Validated and blocks the executor", () => {
  const m = lifecycleModel({ hasProposal: true, policy: blocked }, U), r = rows({ proposal, policy: blocked });
  assert.equal(states(m).validated, "failed");
  assert.deepEqual([r.policy.runtime, r.executor.runtime], ["blocked", "blocked"]);
  assert.match(r.policy.latest, /Target is not protected/);
});

test("observing: guard and executor run, with the backend's own event messages", () => {
  const events = [{ type: "fault_verified", message: "The bounded fault is active on the approved target." }, { type: "observation_started", message: "Comparing Jaeger spans." }];
  const m = lifecycleModel({ phase: "observing", hasProposal: true, policy: allowed }, U), r = rows({ phase: "observing", proposal, policy: allowed, events });
  assert.deepEqual([states(m).injecting, states(m).observing, states(m).rolling_back], ["done", "current", "pending"]);
  assert.deepEqual([r.executor.runtime, r.guard.runtime], ["running", "running"]);
  assert.equal(r.executor.latest, "The bounded fault is active on the approved target.");
  assert.equal(r.guard.latest, "Comparing Jaeger spans.");
});

test("recovery checks come from recovery_sample events", () => {
  const events = [{ type: "recovery_sample", message: "Recovery check 1/2 healthy samples passed." }];
  const r = rows({ phase: "recovering", proposal, policy: allowed, events });
  assert.deepEqual([r.executor.runtime, r.guard.runtime], ["completed", "running"]);
  assert.equal(r.guard.latest, "Recovery check 1/2 healthy samples passed.");
});

test("completed: every step done, the analysis row shows the real score", () => {
  const m = lifecycleModel({ phase: "completed", hasProposal: true }, U), r = rows({ phase: "completed", proposal, policy: allowed, report: { resilience_score: 71.6, severity: "Moderate" } });
  assert.ok(m.steps.every((s) => s.state === "done"));
  assert.equal(r.analysis.latest, "Resilience 72/100, Moderate impact");
});

test("an aborted run is never green: Aborted at the end, recovery honestly not waited for", () => {
  const m = lifecycleModel({ phase: "aborted", hasProposal: true }, U), last = m.steps.at(-1);
  assert.deepEqual([last.label, last.state, m.outcome], ["Aborted", "aborted", "aborted"]);
  assert.deepEqual([states(m).rolling_back, states(m).recovering, states(m).analyzing], ["done", "pending", "pending"]);
  assert.match(m.steps.find((s) => s.id === "recovering").note, /not waited for/);
});

test("a failed run stops at the last phase the stream reported", () => {
  const m = lifecycleModel({ phase: "failed", hasProposal: true, lastActive: "injecting", error: "executor could not verify" }, U);
  assert.deepEqual([states(m).baseline, states(m).injecting, states(m).observing, m.outcome], ["done", "failed", "pending", "failed"]);
  assert.match(m.headline, /executor could not verify/);
  assert.equal(states(lifecycleModel({ phase: "failed", hasProposal: true }, U)).baseline, "failed");   // nothing reported yet: stops at the first run phase
  assert.equal(rows({ phase: "failed", proposal, policy: allowed, events: [{ type: "experiment_failed", message: "boom" }] }).executor.runtime, "failed");
});

test("implementation maturity stays separate from runtime and unknown maturity is not guessed", () => {
  const meta = META.map((m) => (m.id === "guard" ? { ...m, implementation: "in_development" } : m));
  const guard = agentModel({ meta, topology, phase: "idle" }).find((r) => r.id === "guard");
  assert.deepEqual([guard.maturity, guard.runtime], ["in_development", "idle"]);
  assert.equal(agentModel({ meta: [], topology })[0].maturity, "unknown");
});

test("offline mode marks every component as not connected, never completed", () => {
  const all = agentModel({ meta: META, mode: "offline" });
  assert.ok(all.every((r) => r.runtime === "unavailable" && r.latest === "API unreachable"));
});

test("topology loading and error states are explicit", () => {
  assert.equal(agentModel({ meta: META, topology: { state: "loading" } })[0].runtime, "loading");
  const failed = agentModel({ meta: META, topology: { state: "error", error: "HTTP 500" } })[0];
  assert.deepEqual([failed.runtime, failed.latest], ["failed", "Topology unavailable: HTTP 500"]);
});
