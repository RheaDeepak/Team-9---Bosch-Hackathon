import { createApiClient, normalizeBaseline, normalizeFaultCapabilities, normalizePolicy, normalizeReadiness, normalizeTopology, proposalParameterRows, resolveApiBaseUrl } from "./api.js";
import {
  mockBaseline,
  mockFaultCapabilities,
  mockPhaseMetrics,
  mockPolicy,
  mockProposal,
  mockReadiness,
  mockResult,
  mockTrafficSpikeProposal,
  mockTopology,
} from "./mock-data.js";
import { ACTIVE_PHASES, agentModel, lifecycleModel } from "./workflow.js";
import { layeredLayout } from "./graph-layout.js";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const svgNs = "http://www.w3.org/2000/svg";
const api = createApiClient({ baseUrl: resolveApiBaseUrl() });
const demo = new URLSearchParams(globalThis.location?.search || "").get("demo") === "1";      // the offline demo is explicit: ?demo=1
const UI = globalThis.ChaosUI;
const EMPTY_BASELINE = { availability: null, errorRate: null, p95LatencyMs: null, throughputRps: null };
const SPARK_KEYS = { availability: "availabilitySpark", errorRate: "errorSpark", p95LatencyMs: "latencySpark", throughputRps: "throughputSpark" };
const STOPPABLE = new Set(["approved", "baseline", "injecting", "observing"]);       // control_plane refuses an abort once the fault is already removed

const state = {
  mode: "connecting",
  readiness: { status: "pending", checks: [] },
  topology: { nodes: [], edges: [] },
  topologyState: { state: "loading" },
  baseline: EMPTY_BASELINE,
  events: [],
  history: { availability: [], errorRate: [], p95LatencyMs: [], throughputRps: [] },
  lastActive: null,
  error: "",
  report: null,
  proposing: false,
  stopRequested: false,
  compMeta: null,
  faultCapabilities: [],
  proposal: null,
  policy: null,
  experimentId: null,
  phase: "idle",
  startedAt: null,
  timer: null,
  pollTimer: null,
  eventSource: null,
  demoTimers: [],
};

const elements = {
  connChip: $("#connChip"),
  experimentChip: $("#experimentChip"),
  banner: $("#banner"),
  readinessBlock: $("#readinessBlock"),
  wfSteps: $("#wfSteps"),
  wfHeadline: $("#wfHeadline"),
  wfAgents: $("#wfAgents"),
  wfElapsed: $("#wfElapsed"),
  connectionDetail: $("#connectionDetail"),
  refreshButton: $("#refreshButton"),
  readinessPulse: $("#readinessPulse"),
  readinessChecks: $("#readinessChecks"),
  readyCount: $("#readyCount"),
  topologyGraph: $("#topologyGraph"),
  serviceCount: $("#serviceCount"),
  edgeCount: $("#edgeCount"),
  selectedService: $("#selectedService"),
  proposalForm: $("#proposalForm"),
  goalSelect: $("#goalSelect"),
  trafficSpikeCapability: $("#trafficSpikeCapability"),
  proposeButton: $("#proposeButton"),
  proposalEmpty: $("#proposalEmpty"),
  proposalCard: $("#proposalCard"),
  proposalTarget: $("#proposalTarget"),
  proposalFailure: $("#proposalFailure"),
  proposalName: $("#proposalName"),
  proposalHypothesis: $("#proposalHypothesis"),
  proposalParameters: $("#proposalParameters"),
  validateButton: $("#validateButton"),
  policySection: $("#policySection"),
  policySummary: $("#policySummary"),
  policyChecks: $("#policyChecks"),
  approvalCheckbox: $("#approvalCheckbox"),
  approveButton: $("#approveButton"),
  phaseBadge: $("#phaseBadge"),
  stopButton: $("#stopButton"),
  elapsedTimer: $("#elapsedTimer"),
  activeFault: $("#activeFault"),
  eventFeed: $("#eventFeed"),
  baselineAvailability: $("#baselineAvailability"),
  baselineErrors: $("#baselineErrors"),
  baselineLatency: $("#baselineLatency"),
  baselineThroughput: $("#baselineThroughput"),
  liveAvailability: $("#liveAvailability"),
  liveErrors: $("#liveErrors"),
  liveLatency: $("#liveLatency"),
  liveThroughput: $("#liveThroughput"),
  resultEmpty: $("#resultEmpty"),
  resultContent: $("#resultContent"),
  resilienceScore: $("#resilienceScore"),
  resultSeverity: $("#resultSeverity"),
  resultSummary: $("#resultSummary"),
  confidenceBadge: $("#confidenceBadge"),
  evidenceList: $("#evidenceList"),
  remediationText: $("#remediationText"),
  viewReportButton: $("#viewReportButton"),
  reportDialog: $("#reportDialog"),
  reportBody: $("#reportBody"),
  closeReportButton: $("#closeReportButton"),
  toastRegion: $("#toastRegion"),
};

function setMode(mode) {
  state.mode = mode;
  const [kind, label, icon] = mode === "live" ? ["ok", "API connected", "check"] : mode === "demo" ? ["warn", "Demo data (offline)", "alert"]
    : mode === "offline" ? ["bad", "API unreachable", "plug"] : ["", "Connecting", "spinner"];
  elements.connChip.className = `cc-chip ${kind}`;
  elements.connChip.innerHTML = UI.icon(icon, icon === "spinner" ? "cc-spin" : "") + label;
  elements.connectionDetail.textContent = mode === "live" ? `API connected · ${api.baseUrl || window.location.origin}`
    : mode === "demo" ? "Demo mode · no backend calls are made" : mode === "offline" ? "API unreachable" : "API connecting";
  elements.banner.classList.toggle("is-hidden", mode !== "offline" && mode !== "demo");
  elements.banner.textContent = mode === "offline"
    ? "The API is unreachable, so nothing below is live. Start it with python -m uvicorn api:app --host 127.0.0.1 --port 8000, then refresh. Add ?demo=1 to the address for the offline demo."
    : mode === "demo" ? "Demo mode: every number, event and result on this page is sample data, not your system." : "";
  elements.banner.className = `banner${mode === "demo" ? " demo" : ""}${mode === "offline" || mode === "demo" ? "" : " is-hidden"}`;
  elements.proposeButton.disabled = mode === "offline" || mode === "connecting";
  renderWorkflow();
}

function createElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function createSvg(tag, attrs = {}) {
  const element = document.createElementNS(svgNs, tag);
  Object.entries(attrs).forEach(([key, value]) => element.setAttribute(key, String(value)));
  return element;
}

function toast(message, tone = "default") {
  const item = createElement("div", `toast ${tone}`, message);
  elements.toastRegion.append(item);
  setTimeout(() => item.remove(), 4200);
}

function setBusy(button, busy, busyText) {
  if (!button.dataset.label) button.dataset.label = button.textContent.trim();
  button.disabled = busy;
  button.textContent = busy ? busyText : button.dataset.label;
}

function renderReadiness() {
  elements.readinessChecks.replaceChildren();
  const checks = state.mode === "offline"
    ? [{ id: "api", label: "Backend API", detail: "Unreachable: start api.py", ready: false }] : state.readiness.checks;
  const ready = checks.filter((check) => check.ready).length, down = checks.filter((check) => !check.ready);
  if (state.mode === "connecting") elements.readinessChecks.append(createElement("p", "muted", "Checking the system…"));
  checks.forEach((check) => {
    const item = createElement("div", `readiness-check${check.ready ? "" : " failed"}`);
    const body = createElement("div");
    body.append(createElement("strong", "", check.label), createElement("small", "", check.detail));
    item.insertAdjacentHTML("beforeend", UI.icon(check.ready ? "check" : "x"));
    item.append(body, createElement("span", "sr", check.ready ? "passed" : "failed"));
    elements.readinessChecks.append(item);
  });
  elements.readyCount.textContent = checks.length ? `${ready}/${checks.length} ready` : "—";
  elements.readyCount.className = `cc-chip ${!checks.length ? "" : down.length ? "bad" : "ok"}`;
  elements.readinessPulse.className = `pulse-dot${state.mode === "connecting" ? " pending" : down.length ? " blocked" : ""}`;
  elements.readinessBlock.classList.toggle("is-hidden", !down.length || state.mode === "connecting");
  elements.readinessBlock.textContent = down.length ? `Experiments cannot safely proceed: ${down.map((check) => `${check.label} (${check.detail})`).join("; ")}.` : "";
}

const nodeWidth = (label) => Math.max(90, Math.min(138, label.length * 6.3 + 31));

// Coordinates from the backend win; without them the graph is laid out left to right by call depth.
function graphLayout(nodes, edges) {
  if (nodes.length && nodes.every((node) => Number.isFinite(node.x) && Number.isFinite(node.y))) {
    return { nodes, width: 760, height: Math.max(410, ...nodes.map((node) => node.y + 70)) };
  }
  const { positions, width, height } = layeredLayout(nodes, edges);
  return { nodes: nodes.map((node) => ({ ...node, ...positions.get(node.id) })), width, height: Math.max(300, height) };
}

function renderTopology() {
  const graph = elements.topologyGraph;
  graph.replaceChildren();
  const laid = graphLayout(state.topology.nodes, state.topology.edges);
  const nodes = laid.nodes;
  const nodeMap = new Map(nodes.map((node) => [node.id, node]));
  graph.setAttribute("viewBox", `0 0 ${laid.width} ${laid.height}`);

  const defs = createSvg("defs");                                   // edges end in an arrow: the caller is where the line starts
  const marker = createSvg("marker", { id: "edgeArrow", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" });
  marker.append(createSvg("path", { d: "M 0 0 L 10 5 L 0 10 z", class: "edge-arrow" }));
  defs.append(marker);
  graph.append(defs);

  const edgeGroup = createSvg("g", { "aria-hidden": "true" });
  state.topology.edges.forEach(([sourceId, targetId]) => {
    const source = nodeMap.get(sourceId);
    const target = nodeMap.get(targetId);
    if (!source || !target) return;
    const forward = target.x > source.x + 20;
    const x1 = forward ? source.x + nodeWidth(source.label) / 2 : source.x, x2 = forward ? target.x - nodeWidth(target.label) / 2 - 3 : target.x;
    const mid = (x1 + x2) / 2;
    edgeGroup.append(createSvg("path", {
      class: "graph-edge",
      d: `M ${x1} ${source.y} C ${mid} ${source.y}, ${mid} ${target.y}, ${x2} ${target.y}`,
      "marker-end": "url(#edgeArrow)",
      "data-source": sourceId,
      "data-target": targetId,
    }));
  });
  graph.append(edgeGroup);

  const nodeGroup = createSvg("g");
  nodes.forEach((node) => {
    const width = nodeWidth(node.label);
    const group = createSvg("g", {
      class: `graph-node${node.protected ? " protected" : ""}`,
      transform: `translate(${node.x - width / 2}, ${node.y - 19})`,
      tabindex: "0",
      role: "button",
      "aria-label": `${node.label}, ${node.health}${node.protected ? ", protected" : ""}`,
    });
    group.dataset.nodeId = node.id;
    group.append(createSvg("rect", { class: "node-body", width, height: 38, rx: 10 }));
    group.append(createSvg("circle", { class: node.protected ? "protect-dot" : "status-dot", cx: 11, cy: 11, r: 3 }));
    const text = createSvg("text", { x: width / 2, y: 23 });
    text.textContent = node.label;
    group.append(text);
    const select = () => selectService(node, group);
    group.addEventListener("click", select);
    group.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") select(); });
    nodeGroup.append(group);
  });
  graph.append(nodeGroup);
  if (!nodes.length) {
    const note = createSvg("text", { x: 380, y: 205, class: "graph-empty" });
    note.setAttribute("text-anchor", "middle");
    note.setAttribute("fill", "#8896A8");
    note.textContent = state.topologyState.state === "error" ? `Topology unavailable: ${state.topologyState.error}` : state.topologyState.state === "loading" ? "Loading topology…" : "No services reported";
    graph.append(note);
  }
  elements.serviceCount.textContent = nodes.length ? state.topology.nodes.length : "—";
  elements.edgeCount.textContent = nodes.length ? state.topology.edges.length : "—";
}

function selectService(node, group) {
  $$(".graph-node").forEach((item) => item.classList.toggle("selected", item === group));
  $$(".graph-edge").forEach((edge) => edge.classList.toggle("highlighted", edge.dataset.source === node.id || edge.dataset.target === node.id));
  elements.selectedService.replaceChildren();
  elements.selectedService.append(
    createElement("strong", "", node.label),
    createElement("span", "", `${node.role} · ${node.health}`),
    createElement("span", "detail-spacer", ""),
    createElement("span", node.protected ? "protected-copy" : "", node.protected ? "Protected · cannot be targeted" : `Criticality ${Math.round(node.criticality * 100)}/100 · eligible target`),
  );
}

function renderBaseline() {
  const metric = state.baseline;
  elements.baselineAvailability.textContent = formatMetric("availability", metric.availability);
  elements.baselineErrors.textContent = formatMetric("errorRate", metric.errorRate);
  elements.baselineLatency.textContent = formatMetric("p95LatencyMs", metric.p95LatencyMs);
  elements.baselineThroughput.textContent = formatMetric("throughputRps", metric.throughputRps);
}

function renderFaultCapabilities() {
  elements.goalSelect.querySelector('option[value="capacity"]')?.remove();
  const traffic = state.faultCapabilities.find((capability) => capability.failureType === "traffic_spike");

  elements.trafficSpikeCapability.className = "capability-note";
  if (!traffic?.selectable) {
    elements.trafficSpikeCapability.classList.add("unavailable");
    elements.trafficSpikeCapability.textContent = `Traffic spike unavailable · ${traffic?.detail || "executor and rollback readiness not confirmed"}`;
    return;
  }

  const option = document.createElement("option");
  option.value = "capacity";
  option.textContent = "Test bounded traffic capacity";
  elements.goalSelect.append(option);
  const limits = traffic.limits;
  elements.trafficSpikeCapability.classList.add("ready");
  elements.trafficSpikeCapability.textContent = `Traffic spike ready · ${traffic.detail} · max ${limits.peak_users} users for ${limits.duration_seconds}s`;
}

function normalizeProposal(payload = {}) {
  const proposal = payload.proposal || payload.experiment || payload;
  return {
    ...proposal,
    id: proposal.id || payload.experiment_id || `experiment-${Date.now()}`,
    title: proposal.title || "Proposed resilience experiment",
    target_service: proposal.target_service || proposal.target || "Unknown target",
    failure_type: proposal.failure_type || proposal.fault_type || "Unknown fault",
    hypothesis: proposal.hypothesis || "No hypothesis supplied.",
    parameters: proposal.parameters || {},
    blast_radius: proposal.blast_radius || "1 service",
    rollback: proposal.rollback || proposal.rollback_action || "Executor rollback",
  };
}

function renderProposal() {
  const proposal = state.proposal;
  elements.proposalEmpty.classList.toggle("is-hidden", Boolean(proposal));
  elements.proposalCard.classList.toggle("is-hidden", !proposal);
  if (!proposal) return;
  elements.proposalTarget.textContent = proposal.target_service;
  elements.proposalFailure.textContent = proposal.failure_type.replaceAll("_", " ");
  elements.proposalName.textContent = proposal.title;
  elements.proposalHypothesis.textContent = proposal.hypothesis;
  elements.proposalHypothesis.title = proposal.reasoning_summary || "";
  elements.proposalParameters.replaceChildren(...proposalParameterRows(proposal).map(([label, value]) => {
    const item = document.createElement("div");
    item.append(createElement("dt", "", label), createElement("dd", "", value));
    return item;
  }));
}

function renderPolicy() {
  elements.policyChecks.replaceChildren();
  if (!state.policy) {
    elements.policyChecks.append(createElement("div", "policy-placeholder", "Policy results will appear here."));
    elements.policySection.classList.add("is-muted");
    renderWorkflow();
    return;
  }

  state.policy.checks.forEach((check) => {
    const row = createElement("div", `policy-check${check.passed ? "" : " failed"}`);
    row.append(
      createElement("span", "policy-check-icon", check.passed ? "✓" : "×"),
      createElement("strong", "", check.name),
      createElement("small", "", check.detail),
    );
    elements.policyChecks.append(row);
  });
  elements.policySection.classList.remove("is-muted");
  elements.policySummary.textContent = state.policy.allowed
    ? `${state.policy.checks.length} deterministic checks passed. Human approval is now required.`
    : "Policy blocked this proposal. It cannot be approved or executed.";
  elements.approvalCheckbox.disabled = !state.policy.allowed;
  elements.approvalCheckbox.checked = false;
  elements.approveButton.disabled = true;
}

function formatMetric(name, value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "No data";
  if (name === "availability" || name === "errorRate") return `${Number(value).toFixed(1)}%`;
  if (name === "p95LatencyMs") return `${Math.round(Number(value))} ms`;
  if (name === "throughputRps") return `${Number(value).toFixed(1)} rps`;
  return String(value);
}

function renderLiveMetrics(metrics) {
  elements.liveAvailability.textContent = formatMetric("availability", metrics?.availability);
  elements.liveErrors.textContent = formatMetric("errorRate", metrics?.errorRate);
  elements.liveLatency.textContent = formatMetric("p95LatencyMs", metrics?.p95LatencyMs);
  elements.liveThroughput.textContent = formatMetric("throughputRps", metrics?.throughputRps);
  const phaseClass = { baseline: "baseline", approved: "baseline", injecting: "fault", observing: "fault", rolling_back: "rollback", recovering: "recovery" }[state.phase];
  if (metrics && phaseClass) Object.keys(SPARK_KEYS).forEach((key) => {
    if (metrics[key] !== null && metrics[key] !== undefined && Number.isFinite(Number(metrics[key]))) state.history[key].push({ value: Number(metrics[key]), phase: phaseClass });
  });
  renderSparklines();
}

function renderSparklines() {
  Object.entries(SPARK_KEYS).forEach(([metric, id]) => {
    const points = state.history[metric].slice(-24), values = points.map((point) => point.value);
    const low = Math.min(...values), high = Math.max(...values), span = high - low;
    $(`#${id}`).replaceChildren(...points.map((point) => {
      const bar = document.createElement("i");
      bar.className = `ph-${point.phase}`;
      bar.style.height = `${span > 0 ? 18 + ((point.value - low) / span) * 82 : 55}%`;       // scaled to the values seen so far, nothing invented
      bar.title = `${point.value}`;
      return bar;
    }));
  });
}


function updatePhase(phase, message, metrics, event) {
  state.phase = phase.toLowerCase().replaceAll(" ", "_");
  const active = ACTIVE_PHASES.has(state.phase);
  if (active) state.lastActive = state.phase;
  if (!active) state.stopRequested = false;
  const label = state.stopRequested ? "STOP REQUESTED" : state.phase.replaceAll("_", " ").toUpperCase();
  const kind = state.stopRequested ? "warn" : active ? "info" : state.phase === "completed" ? "ok" : state.phase === "aborted" ? "warn" : state.phase === "failed" ? "bad" : "";
  const icon = state.stopRequested ? "stop" : active ? "spinner" : state.phase === "completed" ? "check" : state.phase === "failed" ? "x" : state.phase === "aborted" ? "stop" : "idle";
  elements.phaseBadge.className = `cc-chip ${kind}`;
  elements.phaseBadge.innerHTML = UI.icon(icon, icon === "spinner" ? "cc-spin" : "") + label;
  elements.experimentChip.className = `cc-chip ${kind}`;
  elements.experimentChip.innerHTML = UI.icon(icon, icon === "spinner" ? "cc-spin" : "") + (state.phase === "idle" ? "No experiment" : label);
  renderStopButton();
  elements.activeFault.textContent = ["injecting", "observing"].includes(state.phase)
    ? `${state.proposal?.failure_type?.replaceAll("_", " ") || "Fault"} · ${state.proposal?.target_service || "target"}`
    : "None";
  if (event) pushEvent(event);
  else if (message) pushEvent({ message, state: state.phase, timestamp: new Date().toISOString(), type: "update" });
  if (metrics) renderLiveMetrics(metrics);
  renderWorkflow();
}

function renderStopButton() {
  const stoppable = STOPPABLE.has(state.phase);
  elements.stopButton.disabled = !stoppable || state.stopRequested;
  elements.stopButton.classList.toggle("pending", state.stopRequested);
  elements.stopButton.setAttribute("aria-busy", String(state.stopRequested));
  elements.stopButton.lastChild.textContent = state.stopRequested ? " Stop requested: rolling back…" : " Emergency stop";
}

// Demo mode only: the backend sends its own messages in live mode.
function phaseMessage(phase) {
  const messages = {
    baseline: "Collecting healthy behavior under repeatable load.",
    injecting: "Applying one policy-approved fault to the allowlisted target.",
    observing: "Fault verified. Comparing live telemetry with the baseline.",
    rolling_back: "Rollback invoked. Removing all active fault handles.",
    recovering: "Fault removed. Waiting for service health and journeys to recover.",
    analyzing: "Recovery verified. Calculating impact and resilience score.",
    completed: "Experiment complete. Evidence and report are ready.",
    aborted: "Experiment stopped by operator. Recovery was verified.",
  };
  return messages[phase] || `Experiment state changed to ${phase}.`;
}

function pushEvent(event) {
  const key = event.sequence ?? `${event.timestamp}|${event.message}`;
  if (state.events.some((item) => (item.sequence ?? `${item.timestamp}|${item.message}`) === key)) return;
  state.events.push(event);
  state.events = state.events.slice(-60);
  const log = elements.eventFeed;
  if (log.querySelector(".muted")) log.replaceChildren();
  const time = new Date(event.timestamp || Date.now()).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const item = document.createElement("li");
  item.append(createElement("span", "event-time", time));
  const text = createElement("span");
  text.append(createElement("b", "", String(event.type || "update").replaceAll("_", " ")), document.createTextNode(` ${event.message || ""}`));
  item.append(text);
  log.append(item);
  log.scrollTop = log.scrollHeight;
}

function startTimer() {
  state.startedAt = Date.now();
  clearInterval(state.timer);
  state.timer = setInterval(() => {
    const seconds = Math.floor((Date.now() - state.startedAt) / 1000);
    elements.elapsedTimer.textContent = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
    renderElapsed();
  }, 250);
}

function stopTimer() {
  clearInterval(state.timer);
  state.timer = null;
  renderElapsed();
}

function renderElapsed() {                       // the page's own timer, started at approval: shown only once a run has started
  const show = Boolean(state.startedAt);
  elements.wfElapsed.classList.toggle("is-hidden", !show);
  if (show) elements.wfElapsed.textContent = `${state.timer ? "Elapsed" : "Ran"} ${elements.elapsedTimer.textContent}`;
}

function clearLiveConnections() {
  state.eventSource?.close();
  state.eventSource = null;
  clearInterval(state.pollTimer);
  state.pollTimer = null;
  state.demoTimers.forEach(clearTimeout);
  state.demoTimers = [];
}

function extractExperiment(payload = {}) {
  const experiment = payload.experiment || payload;
  const rawMetrics = experiment.live_metrics || experiment.metrics || experiment.observation;
  return {
    phase: experiment.phase || experiment.state || "idle",
    message: experiment.message || experiment.latest_event,
    event: payload.event || null,
    error: experiment.error || "",
    metrics: rawMetrics ? normalizeBaseline(rawMetrics) : null,
  };
}

function connectLiveExperiment() {
  const id = encodeURIComponent(state.experimentId);
  if (typeof EventSource !== "undefined") {
    state.eventSource = new EventSource(api.eventsUrl(id));
    state.eventSource.onmessage = (event) => {
      try { applyExperimentUpdate(JSON.parse(event.data)); } catch { pushEvent({ message: String(event.data), state: state.phase, timestamp: new Date().toISOString(), type: "update" }); }
    };
    state.eventSource.onerror = () => state.eventSource?.close();
  }
  state.pollTimer = setInterval(async () => {
    try { applyExperimentUpdate(await api.experiment(state.experimentId)); } catch { /* SSE may still be active */ }
  }, 2500);
}

function applyExperimentUpdate(payload) {
  const update = extractExperiment(payload);
  if (update.error) state.error = update.error;
  updatePhase(update.phase, update.event ? null : update.message, update.metrics, update.event);
  const terminal = update.phase.toLowerCase();
  if (["completed", "aborted", "failed"].includes(terminal)) finishLiveExperiment(terminal);
}

function simulateExperiment() {
  const schedule = [
    [0, "baseline"],
    [4200, "injecting"],
    [7200, "observing"],
    [14500, "rolling_back"],
    [17600, "recovering"],
    [22100, "analyzing"],
    [24100, "completed"],
  ];
  schedule.forEach(([delay, phase]) => {
    const timer = setTimeout(() => {
      updatePhase(phase, null, mockPhaseMetrics[phase] || mockPhaseMetrics.completed);
      if (phase === "completed") finishDemoExperiment();
    }, delay);
    state.demoTimers.push(timer);
  });
}

async function finishLiveExperiment(terminal = "completed") {
  clearLiveConnections();
  stopTimer();
  if (terminal === "failed") {
    toast("The experiment failed. Inspect the event message and verify recovery manually.", "warn");
    return;
  }
  try {
    renderResult(await api.report(state.experimentId));
    toast(terminal === "aborted" ? "Emergency stop completed and recovery was verified." : "Rollback verified. The finding is ready.", "success");
  } catch {
    toast("Experiment completed, but the report is not available yet.", "warn");
  }
}

function finishDemoExperiment() {
  clearLiveConnections();
  stopTimer();
  renderResult(mockResult);
  toast("Rollback verified. The finding is ready.", "success");
  $("#resultSection").scrollIntoView({ behavior: "smooth", block: "start" });
}

function normalizeResult(payload = {}) {
  const report = payload.report || payload;
  const score = report.resilience_score ?? report.score ?? 0;
  const evidence = report.evidence || report.observations || [];
  return {
    resilience_score: Number(score),
    impact_score: Number(report.impact_score ?? 100 - score),
    severity: report.severity || "Unknown",
    confidence: report.confidence || "Confidence unavailable",
    summary: report.summary || report.finding || "Experiment report generated.",
    recovery_seconds: report.recovery_seconds,
    evidence: evidence.map((item) => typeof item === "string" ? { label: "Observation", value: item, tone: "" } : item),
    remediation: report.remediation || report.recommended_fix || report.recommendations?.[0] || "Review the full report for remediation guidance.",
  };
}

function renderResult(payload) {
  const result = normalizeResult(payload);
  state.report = result;
  elements.resultEmpty.classList.add("is-hidden");
  elements.resultContent.classList.remove("is-hidden");
  elements.resilienceScore.textContent = Math.round(result.resilience_score);
  $(".score-ring").style.setProperty("--score", Math.max(0, Math.min(100, result.resilience_score)));
  elements.resultSeverity.textContent = `${result.severity} impact`;
  elements.resultSummary.textContent = result.summary;
  elements.confidenceBadge.textContent = result.confidence;
  elements.confidenceBadge.className = `cc-chip ${/^high/i.test(result.confidence) ? "ok" : /^low/i.test(result.confidence) ? "bad" : "warn"}`;
  elements.remediationText.textContent = result.remediation;
  elements.evidenceList.replaceChildren(...result.evidence.map((item) => {
    const row = createElement("div", "evidence-item");
    row.append(createElement("span", "", item.label || item.metric || "Observation"), createElement("strong", item.tone || "", item.value ?? item.result ?? "No data"));
    return row;
  }));
  elements.reportBody.replaceChildren(buildReportTable(result));
  renderWorkflow();
}

function buildReportTable(result) {
  const table = createElement("table", "report-table");
  const rows = [
    ["Experiment ID", state.experimentId || "—"],
    ["Target", state.proposal?.target_service || "—"],
    ["Fault", state.proposal?.failure_type?.replaceAll("_", " ") || "—"],
    ["Hypothesis", state.proposal?.hypothesis || "—"],
    ["Impact score", `${Math.round(result.impact_score)}/100`],
    ["Resilience score", `${Math.round(result.resilience_score)}/100`],
    ["Recovery", result.recovery_seconds != null ? `${result.recovery_seconds} seconds` : "No data"],
    ["Rollback", state.phase === "failed" ? "Not verified" : "Verified by the backend"],
  ];
  rows.forEach(([label, value]) => {
    const row = document.createElement("tr");
    row.append(createElement("th", "", label), createElement("td", "", value));
    table.append(row);
  });
  return table;
}


async function loadComponentMeta() {
  try {
    const response = await fetch("./component-meta.json");
    if (!response.ok) throw new Error(String(response.status));
    state.compMeta = (await response.json()).components || [];
  } catch { state.compMeta = []; }
  renderWorkflow();
}

let lastWorkflowSignature = "";
function renderWorkflow() {
  const life = lifecycleModel({ phase: state.phase, hasProposal: Boolean(state.proposal), policy: state.policy, lastActive: state.lastActive,
    error: state.error, reportStatus: state.report?.status }, UI);
  const rows = agentModel({ meta: state.compMeta || [], mode: state.mode === "offline" ? "offline" : state.mode, phase: state.phase, proposal: state.proposal,
    proposing: state.proposing, policy: state.policy, topology: { ...state.topologyState, nodes: state.topology.nodes.length, edges: state.topology.edges.length,
      protected: state.topology.nodes.filter((node) => node.protected).length }, events: state.events, report: state.report });
  const signature = JSON.stringify([life, rows, state.compMeta === null]);
  if (signature === lastWorkflowSignature) return;
  lastWorkflowSignature = signature;
  elements.wfSteps.innerHTML = UI.stepperHtml(life);
  elements.wfHeadline.textContent = life.headline;
  elements.wfAgents.innerHTML = UI.agentsHtml(rows);
}

async function loadData({ announce = false } = {}) {
  elements.refreshButton.classList.add("is-spinning");
  setMode("connecting");
  try {
    if (demo) throw new Error("demo");
    await api.health();
    const [readinessPayload, topologyPayload, baselinePayload, capabilityPayload] = await Promise.all([
      api.readiness(), api.topology(), api.latestBaseline().catch(() => null), api.faultCapabilities().catch(() => ({ capabilities: [] })),
    ]);
    state.readiness = normalizeReadiness(readinessPayload);
    state.topology = normalizeTopology(topologyPayload);
    state.topologyState = { state: "ok" };
    state.baseline = baselinePayload ? normalizeBaseline(baselinePayload) : EMPTY_BASELINE;       // no baseline reported: "No data", never a sample value
    state.faultCapabilities = normalizeFaultCapabilities(capabilityPayload);
    setMode("live");
    if (announce) toast("Live system data refreshed.", "success");
  } catch (error) {
    if (demo) {
      state.readiness = mockReadiness;
      state.topology = mockTopology;
      state.topologyState = { state: "ok" };
      state.baseline = mockBaseline;
      state.faultCapabilities = mockFaultCapabilities;
      setMode("demo");
    } else {
      state.readiness = { status: "blocked", checks: [] };
      state.topology = { nodes: [], edges: [] };
      state.topologyState = { state: "error", error: error.message || "API unreachable" };
      state.baseline = EMPTY_BASELINE;
      state.faultCapabilities = [];
      setMode("offline");
      if (announce) toast("The API is still unreachable.", "warn");
    }
  } finally {
    renderReadiness();
    renderTopology();
    renderBaseline();
    renderFaultCapabilities();
    renderWorkflow();
    elements.refreshButton.classList.remove("is-spinning");
  }
}

elements.proposalForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setBusy(elements.proposeButton, true, "Designing experiment…");
  state.policy = null;
  state.proposing = true;
  renderPolicy();
  renderWorkflow();
  try {
    const payload = state.mode === "live"
      ? await api.propose(elements.goalSelect.value)
      : elements.goalSelect.value === "capacity" ? mockTrafficSpikeProposal : mockProposal;
    state.proposal = normalizeProposal(payload);
    state.experimentId = state.proposal.id;
    state.events = [];
    elements.eventFeed.replaceChildren(createElement("li", "muted", "Waiting for an approved experiment."));
    renderProposal();
    toast("One bounded experiment proposed.", "success");
  } catch (error) {
    toast(`Proposal failed: ${error.message}`, "warn");
  } finally {
    state.proposing = false;
    setBusy(elements.proposeButton, false);
    elements.proposeButton.disabled = state.mode === "offline";
    renderWorkflow();
  }
});

elements.validateButton.addEventListener("click", async () => {
  if (!state.proposal) return;
  setBusy(elements.validateButton, true, "Checking safety policy…");
  try {
    const payload = state.mode === "live" ? await api.validate(state.experimentId) : mockPolicy;
    state.policy = normalizePolicy(payload);
    renderPolicy();
    renderWorkflow();
    toast(state.policy.allowed ? "Safety policy passed." : "Safety policy blocked this plan.", state.policy.allowed ? "success" : "warn");
    elements.policySection.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    toast(`Validation failed: ${error.message}`, "warn");
  } finally {
    setBusy(elements.validateButton, false);
  }
});

elements.approvalCheckbox.addEventListener("change", () => {
  elements.approveButton.disabled = !(state.policy?.allowed && elements.approvalCheckbox.checked);
});

elements.approveButton.addEventListener("click", async () => {
  if (!state.policy?.allowed || !elements.approvalCheckbox.checked) return;
  setBusy(elements.approveButton, true, "Starting safely…");
  try {
    if (state.mode === "live") await api.approve(state.experimentId);
    state.history = { availability: [], errorRate: [], p95LatencyMs: [], throughputRps: [] };
    state.stopRequested = false;
    state.error = "";
    state.report = null;
    startTimer();
    updatePhase(state.mode === "live" ? "approved" : "baseline", null, state.mode === "live" ? null : state.baseline);
    $("#liveSection").scrollIntoView({ behavior: "smooth", block: "start" });
    if (state.mode === "live") connectLiveExperiment(); else simulateExperiment();
    toast("Experiment approved. Automatic rollback is armed.", "success");
  } catch (error) {
    toast(`Experiment could not start: ${error.message}`, "warn");
  } finally {
    setBusy(elements.approveButton, false);
    elements.approveButton.disabled = true;
  }
});

elements.stopButton.addEventListener("click", async () => {
  if (!STOPPABLE.has(state.phase)) return;
  state.stopRequested = true;                                      // pending until the backend reports rollback; nothing is assumed recovered
  renderStopButton();
  try {
    if (state.mode === "live") {
      await api.abort(state.experimentId);
      updatePhase(state.phase, null, null);
      return;
    }
    clearLiveConnections();
    state.stopRequested = false;
    updatePhase("rolling_back", "Demo: emergency stop accepted. Rollback is running.", mockPhaseMetrics.rolling_back);
    const recoveryTimer = setTimeout(() => {
      updatePhase("recovering", "Rollback completed. Verifying target recovery.", mockPhaseMetrics.recovering);
      const finishTimer = setTimeout(() => {
        updatePhase("aborted", "Experiment stopped safely. Recovery verified.", mockPhaseMetrics.completed);
        stopTimer();
        toast("Emergency stop completed and recovery verified.", "success");
      }, 2200);
      state.demoTimers.push(finishTimer);
    }, 1800);
    state.demoTimers.push(recoveryTimer);
  } catch (error) {
    state.stopRequested = false;
    renderStopButton();
    toast(`STOP request failed: ${error.message}. Check manual recovery now.`, "warn");
  }
});

elements.refreshButton.addEventListener("click", () => loadData({ announce: true }));
elements.viewReportButton.addEventListener("click", () => elements.reportDialog.showModal());
elements.closeReportButton.addEventListener("click", () => elements.reportDialog.close());
elements.reportDialog.addEventListener("click", (event) => { if (event.target === elements.reportDialog) elements.reportDialog.close(); });

renderPolicy();
updatePhase("idle", null, null);
renderSparklines();
loadComponentMeta();
loadData();
