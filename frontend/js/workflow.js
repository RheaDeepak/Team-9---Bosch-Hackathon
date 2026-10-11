// Lifecycle stepper and "Agent & Workflow Activity" models for the api.py UI. Pure functions over what the backend really reported:
// the experiment states of control_plane.py (draft, validated, approved, baseline, injecting, observing, rolling_back, recovering,
// analyzing, completed, aborted, failed, rejected), the policy response, and the events from the SSE stream. Nothing here keeps its own
// state machine, and nothing invents progress, messages or timestamps. Rendering lives in chaos-ui.js (shared with the app.py UI).

export const LIFECYCLE = [
  { id: "draft", label: "Draft" }, { id: "validated", label: "Validated" }, { id: "approved", label: "Approved", checkpoint: true },
  { id: "baseline", label: "Baseline" }, { id: "injecting", label: "Injecting" }, { id: "observing", label: "Observing" },
  { id: "rolling_back", label: "Rolling back" }, { id: "recovering", label: "Recovering" }, { id: "analyzing", label: "Analyzing" },
  { id: "completed", label: "Completed" },
];
const HEADLINES = {
  baseline: "Loading the baseline and checking the guard can see", injecting: "Injecting the fault", observing: "Fault active; the guard is watching",
  rolling_back: "Rolling the fault back", recovering: "Fault removed; waiting for recovery", analyzing: "Scoring the run",
};
export const ACTIVE_PHASES = new Set(["approved", "baseline", "injecting", "observing", "rolling_back", "recovering", "analyzing"]);
export const TERMINAL_PHASES = new Set(["completed", "aborted", "failed", "rejected"]);

// stage: the backend state, or before a run starts: null | "draft" | "validated" | "rejected" (derived from the proposal and policy response).
export function lifecycleModel({ phase = "idle", hasProposal = false, policy = null, lastActive = null, error = "", reportStatus = "" } = {}, U) {
  const stage = phase !== "idle" ? phase : !hasProposal ? null : !policy ? "draft" : policy.allowed ? "validated" : "rejected";
  const build = (at, outcome) => U.buildSteps(LIFECYCLE, at, outcome);
  if (!stage) return { steps: build(null, null), headline: "No experiment in progress", outcome: null };
  if (stage === "draft") return { steps: build("draft", null), headline: "Draft: run policy validation next", outcome: null };
  if (stage === "validated") return { steps: build("approved", null), headline: "Validated; waiting for human approval, nothing has been injected", outcome: null };
  if (stage === "rejected") return { steps: build("validated", "failed"), headline: "Blocked by the safety policy: nothing can run", outcome: "failed" };
  if (stage === "failed") return { steps: build(lastActive && lastActive !== "idle" ? lastActive : "baseline", "failed"), headline: `Failed: ${error || "unexpected error"}`, outcome: "failed" };
  if (stage === "aborted") {                                       // the backend finishes an abort after the rollback, without the recovery wait
    const steps = LIFECYCLE.map((s) => ({ ...s, state: LIFECYCLE.findIndex((x) => x.id === s.id) <= LIFECYCLE.findIndex((x) => x.id === "rolling_back") ? "done" : "pending" }));
    const last = steps[steps.length - 1];
    Object.assign(last, { label: "Aborted", state: "aborted", note: "stopped by operator" });
    steps.find((s) => s.id === "recovering").note = "not waited for after a stop";
    return { steps, headline: "Aborted by the operator; the fault was rolled back", outcome: "aborted" };
  }
  if (stage === "completed") {
    const steps = LIFECYCLE.map((s) => ({ ...s, state: "done" }));
    return { steps, headline: reportStatus ? `Completed (${reportStatus})` : "Completed: rolled back and recovered", outcome: "completed" };
  }
  return { steps: build(stage === "approved" ? "approved" : stage, null), headline: stage === "approved" ? "Approved; the executor is starting" : HEADLINES[stage] || "", outcome: null };
}

const SOURCES = {
  rank_agent: "planning agent (an LLM investigated the top candidates)", ranker_fallback: "ranker fallback (the LLM was not used)", llm: "LLM planner",
  fallback: "fixed fallback plan", "deterministic-capability": "deterministic capability check",
};
const fault = (p) => String(p?.failure_type || "").toLowerCase().replaceAll("_", " ");
const latestOf = (events, types) => { const e = [...(events || [])].reverse().find((x) => types.includes(x.type)); return e ? e.message : ""; };

// ctx: { phase, hasProposal, proposal, proposing, policy, topology: {state, nodes, edges, protected, error}, events, report, meta, mode, apiError }
export function agentModel(ctx) {
  const phase = ctx.phase || "idle", p = ctx.proposal, pol = ctx.policy, ev = ctx.events || [], t = ctx.topology || { state: "loading" };
  const target = p ? `${fault(p)} on ${p.target_service}` : "";
  const rt = {}, latest = {};
  if (ctx.mode === "offline") { for (const id of ["topology", "planner", "policy", "executor", "guard", "analysis"]) { rt[id] = "unavailable"; latest[id] = "API unreachable"; } }
  else {
    if (t.state === "loading") rt.topology = "loading";
    else if (t.state === "error") { rt.topology = "failed"; latest.topology = `Topology unavailable: ${t.error}`; }
    else { rt.topology = "completed"; latest.topology = `${t.nodes} services, ${t.edges} dependencies discovered${t.protected ? `, ${t.protected} protected` : ""}`; }

    if (ctx.proposing) { rt.planner = "running"; latest.planner = "Generating one structured proposal"; }
    else if (p) { rt.planner = "completed"; latest.planner = `Proposed ${target} via ${SOURCES[p.source] || p.source || "unknown source"}${p.planner_note ? ` (${p.planner_note})` : ""}`; }
    else rt.planner = "idle";

    if (!p) rt.policy = "idle";
    else if (!pol) { rt.policy = "idle"; latest.policy = "Not validated yet: run policy validation"; }
    else if (pol.allowed) { rt.policy = "completed"; latest.policy = `${pol.checks.filter((c) => c.passed).length} of ${pol.checks.length} checks passed`; }
    else { const bad = pol.checks.filter((c) => !c.passed); rt.policy = "blocked"; latest.policy = `${bad.length} check(s) failed: ${bad.map((c) => c.name).join("; ")}`; }

    const ex = (types) => latestOf(ev, types);
    if (phase === "idle") {
      if (p && pol?.allowed) { rt.executor = "waiting"; latest.executor = `Approval required before injecting ${target}`; }
      else if (p && pol && !pol.allowed) { rt.executor = "blocked"; latest.executor = "Blocked by policy: nothing will be injected"; }
      else rt.executor = "idle";
    } else if (["approved", "baseline"].includes(phase)) { rt.executor = "running"; latest.executor = ex(["approval_granted"]) || "Approved; preparing to inject"; }
    else if (["injecting", "observing", "rolling_back"].includes(phase)) { rt.executor = "running"; latest.executor = ex(["fault_verified", "rollback_invoked", "fault_injecting"]) || `Fault active: ${target}`; }
    else if (["recovering", "analyzing", "completed"].includes(phase)) { rt.executor = "completed"; latest.executor = ex(["rollback_completed"]) || "Fault rolled back"; }
    else if (phase === "aborted") { rt.executor = "completed"; latest.executor = ex(["manual_abort"]) || "Stopped by the operator; rolled back"; }
    else if (phase === "failed") { rt.executor = "failed"; latest.executor = ex(["experiment_failed"]) || "The experiment failed"; }
    else if (phase === "rejected") { rt.executor = "blocked"; latest.executor = "Blocked by policy: nothing will be injected"; }
    else rt.executor = "idle";

    if (phase === "baseline") { rt.guard = "running"; latest.guard = ex(["baseline_started", "baseline_completed"]); }
    else if (phase === "observing") { rt.guard = "running"; latest.guard = ex(["observation_started", "manual_abort"]) || "Watching live telemetry"; }
    else if (["injecting", "rolling_back"].includes(phase)) { rt.guard = phase === "rolling_back" ? "running" : "idle"; latest.guard = phase === "rolling_back" ? ex(["rollback_invoked"]) : ""; }
    else if (phase === "recovering") { rt.guard = "running"; latest.guard = ex(["recovery_sample", "recovery_check_started"]); }
    else if (phase === "analyzing") { rt.guard = "completed"; latest.guard = ex(["recovery_detected", "recovery_unverified"]); }
    else if (phase === "completed") { const bad = ev.some((e) => e.type === "recovery_unverified"); rt.guard = bad ? "failed" : "completed"; latest.guard = ex(["recovery_detected", "recovery_unverified"]); }
    else if (phase === "aborted") { rt.guard = "completed"; latest.guard = ex(["manual_abort", "recovery_detected"]); }
    else if (phase === "failed") { rt.guard = "failed"; latest.guard = ex(["experiment_failed"]); }
    else rt.guard = "idle";

    if (phase === "analyzing") { rt.analysis = "running"; latest.analysis = ex(["analysis_started"]) || "Scoring the run"; }
    else if (["completed", "aborted"].includes(phase) && ctx.report) {
      rt.analysis = "completed";
      latest.analysis = `${ctx.report.resilience_score != null ? `Resilience ${Math.round(ctx.report.resilience_score)}/100` : "Report ready"}${ctx.report.severity ? `, ${ctx.report.severity} impact` : ""}`;
    } else rt.analysis = "idle";
  }
  const byId = Object.fromEntries((ctx.meta || []).map((m) => [m.id, m]));
  const names = { topology: "Topology Discovery", planner: "Experiment Planner", policy: "Safety Policy Engine", executor: "Experiment Executor", guard: "Telemetry Guard", analysis: "Analysis & Scoring" };
  return Object.keys(names).map((id) => { const m = byId[id] || {};
    return { id, name: m.name || names[id], summary: m.summary || "", module: m.module, maturity: m.implementation || "unknown", notes: m.notes || [], runtime: rt[id], latest: latest[id] || "" }; });
}
