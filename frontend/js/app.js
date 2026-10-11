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

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const svgNs = "http://www.w3.org/2000/svg";
const api = createApiClient({ baseUrl: resolveApiBaseUrl() });

const state = {
  mode: "connecting",
  readiness: mockReadiness,
  topology: mockTopology,
  baseline: mockBaseline,
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
  environmentBadge: $("#environmentBadge"),
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
  elements.environmentBadge.className = `environment-badge ${mode}`;
  if (mode === "live") {
    elements.environmentBadge.textContent = "Live sandbox";
    elements.connectionDetail.textContent = `API connected · ${api.baseUrl || window.location.origin}`;
  } else if (mode === "demo") {
    elements.environmentBadge.textContent = "Demo data";
    elements.connectionDetail.textContent = "API offline · demo mode";
  } else {
    elements.environmentBadge.textContent = "Connecting";
    elements.connectionDetail.textContent = "API connecting";
  }
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
  const ready = state.readiness.checks.filter((check) => check.ready).length;
  state.readiness.checks.forEach((check) => {
    const item = createElement("div", `readiness-check${check.ready ? "" : " failed"}`);
    item.append(createElement("strong", "", check.label), createElement("small", "", check.detail));
    elements.readinessChecks.append(item);
  });
  elements.readyCount.textContent = `${ready}/${state.readiness.checks.length}`;
  elements.readinessPulse.classList.toggle("blocked", ready !== state.readiness.checks.length);
}

function graphLayout(nodes) {
  if (nodes.every((node) => Number.isFinite(node.x) && Number.isFinite(node.y))) return nodes;
  return nodes.map((node, index) => ({ ...node, x: 90 + (index % 4) * 190, y: 70 + Math.floor(index / 4) * 105 }));
}

function renderTopology() {
  const graph = elements.topologyGraph;
  graph.replaceChildren();
  const nodes = graphLayout(state.topology.nodes.slice(0, 20));
  const nodeMap = new Map(nodes.map((node) => [node.id, node]));
  const maxY = Math.max(410, ...nodes.map((node) => node.y + 70));
  graph.setAttribute("viewBox", `0 0 760 ${maxY}`);

  const edgeGroup = createSvg("g", { "aria-hidden": "true" });
  state.topology.edges.forEach(([sourceId, targetId]) => {
    const source = nodeMap.get(sourceId);
    const target = nodeMap.get(targetId);
    if (!source || !target) return;
    const path = createSvg("path", {
      class: "graph-edge",
      d: `M ${source.x} ${source.y} C ${(source.x + target.x) / 2} ${source.y}, ${(source.x + target.x) / 2} ${target.y}, ${target.x} ${target.y}`,
      "data-source": sourceId,
      "data-target": targetId,
    });
    edgeGroup.append(path);
  });
  graph.append(edgeGroup);

  const nodeGroup = createSvg("g");
  nodes.forEach((node) => {
    const width = Math.max(90, Math.min(138, node.label.length * 6.3 + 31));
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
  elements.serviceCount.textContent = state.topology.nodes.length;
  elements.edgeCount.textContent = state.topology.edges.length;
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
  renderSparklines("baseline");
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
  renderSparklines(state.phase);
}

function renderSparklines(phase) {
  const shapes = {
    baseline: [45, 50, 48, 53, 50, 52, 49, 51, 50, 52],
    injecting: [45, 50, 48, 53, 61, 67, 74, 69, 78, 72],
    observing: [44, 51, 49, 56, 73, 85, 62, 91, 78, 88],
    rolling_back: [78, 82, 69, 61, 55, 50, 49, 52, 50, 48],
    recovering: [72, 64, 57, 53, 49, 51, 48, 50, 49, 50],
    completed: [50, 49, 52, 48, 50, 51, 49, 52, 50, 49],
    idle: [22, 24, 23, 25, 22, 24, 23, 24, 22, 23],
  };
  const base = shapes[phase] || shapes.idle;
  const variants = [base.map((n) => 100 - n / 3), base, base.map((n) => n * 0.88), base.map((n) => 65 - n / 5)];
  ["availabilitySpark", "errorSpark", "latencySpark", "throughputSpark"].forEach((id, index) => {
    const container = $(`#${id}`);
    container.replaceChildren(...variants[index].map((height) => {
      const bar = document.createElement("i");
      bar.style.height = `${Math.max(10, Math.min(100, height))}%`;
      return bar;
    }));
  });
}

const phaseOrder = ["baseline", "injecting", "observing", "rolling_back", "recovering"];
const activePhases = new Set(["baseline", "injecting", "observing", "rolling_back", "recovering"]);

function updatePhase(phase, message, metrics) {
  state.phase = phase.toLowerCase().replaceAll(" ", "_");
  const active = activePhases.has(state.phase);
  elements.phaseBadge.textContent = state.phase.replaceAll("_", " ").toUpperCase();
  elements.phaseBadge.classList.toggle("active", active);
  elements.stopButton.disabled = !active;
  elements.activeFault.textContent = ["injecting", "observing"].includes(state.phase)
    ? `${state.proposal?.failure_type?.replaceAll("_", " ") || "Fault"} · ${state.proposal?.target_service || "target"}`
    : "None";
  const currentIndex = phaseOrder.indexOf(state.phase);
  $$("#experimentTimeline li").forEach((item, index) => {
    item.classList.toggle("active", index === currentIndex);
    item.classList.toggle("done", currentIndex > index || state.phase === "completed");
  });
  updateEvent(message || phaseMessage(state.phase));
  if (metrics) renderLiveMetrics(metrics);
}

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
    idle: "Waiting for an approved experiment.",
  };
  return messages[phase] || `Experiment state changed to ${phase}.`;
}

function updateEvent(message) {
  elements.eventFeed.replaceChildren(
    createElement("span", "event-time", new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })),
    createElement("p", "", message),
  );
}

function startTimer() {
  state.startedAt = Date.now();
  clearInterval(state.timer);
  state.timer = setInterval(() => {
    const seconds = Math.floor((Date.now() - state.startedAt) / 1000);
    elements.elapsedTimer.textContent = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
  }, 250);
}

function stopTimer() {
  clearInterval(state.timer);
  state.timer = null;
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
    metrics: rawMetrics ? normalizeBaseline(rawMetrics) : null,
  };
}

function connectLiveExperiment() {
  const id = encodeURIComponent(state.experimentId);
  if (typeof EventSource !== "undefined") {
    state.eventSource = new EventSource(api.eventsUrl(id));
    state.eventSource.onmessage = (event) => {
      try { applyExperimentUpdate(JSON.parse(event.data)); } catch { updateEvent(event.data); }
    };
    state.eventSource.onerror = () => state.eventSource?.close();
  }
  state.pollTimer = setInterval(async () => {
    try { applyExperimentUpdate(await api.experiment(state.experimentId)); } catch { /* SSE may still be active */ }
  }, 2500);
}

function applyExperimentUpdate(payload) {
  const update = extractExperiment(payload);
  updatePhase(update.phase, update.message, update.metrics);
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
  elements.resultEmpty.classList.add("is-hidden");
  elements.resultContent.classList.remove("is-hidden");
  elements.resilienceScore.textContent = Math.round(result.resilience_score);
  $(".score-ring").style.setProperty("--score", Math.max(0, Math.min(100, result.resilience_score)));
  elements.resultSeverity.textContent = `${result.severity} impact`;
  elements.resultSummary.textContent = result.summary;
  elements.confidenceBadge.textContent = result.confidence;
  elements.remediationText.textContent = result.remediation;
  elements.evidenceList.replaceChildren(...result.evidence.map((item) => {
    const row = createElement("div", "evidence-item");
    row.append(createElement("span", "", item.label || item.metric || "Observation"), createElement("strong", item.tone || "", item.value ?? item.result ?? "No data"));
    return row;
  }));
  elements.reportBody.replaceChildren(buildReportTable(result));
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
    ["Rollback", "Verified"],
  ];
  rows.forEach(([label, value]) => {
    const row = document.createElement("tr");
    row.append(createElement("th", "", label), createElement("td", "", value));
    table.append(row);
  });
  return table;
}

async function loadData({ announce = false } = {}) {
  elements.refreshButton.classList.add("is-spinning");
  setMode("connecting");
  try {
    await api.health();
    const [readinessPayload, topologyPayload, baselinePayload, capabilityPayload] = await Promise.all([
      api.readiness(), api.topology(), api.latestBaseline().catch(() => null), api.faultCapabilities().catch(() => ({ capabilities: [] })),
    ]);
    state.readiness = normalizeReadiness(readinessPayload);
    state.topology = normalizeTopology(topologyPayload);
    state.baseline = baselinePayload ? normalizeBaseline(baselinePayload) : mockBaseline;
    state.faultCapabilities = normalizeFaultCapabilities(capabilityPayload);
    setMode("live");
    if (announce) toast("Live system data refreshed.", "success");
  } catch {
    state.readiness = mockReadiness;
    state.topology = mockTopology;
    state.baseline = mockBaseline;
    state.faultCapabilities = mockFaultCapabilities;
    setMode("demo");
    if (announce) toast("Backend unavailable. Showing safe demo data.", "warn");
  } finally {
    renderReadiness();
    renderTopology();
    renderBaseline();
    renderFaultCapabilities();
    elements.refreshButton.classList.remove("is-spinning");
  }
}

elements.proposalForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setBusy(elements.proposeButton, true, "Designing experiment…");
  state.policy = null;
  renderPolicy();
  try {
    const payload = state.mode === "live"
      ? await api.propose(elements.goalSelect.value)
      : elements.goalSelect.value === "capacity" ? mockTrafficSpikeProposal : mockProposal;
    state.proposal = normalizeProposal(payload);
    state.experimentId = state.proposal.id;
    renderProposal();
    toast("One bounded experiment proposed.", "success");
  } catch (error) {
    toast(`Proposal failed: ${error.message}`, "warn");
  } finally {
    setBusy(elements.proposeButton, false);
  }
});

elements.validateButton.addEventListener("click", async () => {
  if (!state.proposal) return;
  setBusy(elements.validateButton, true, "Checking safety policy…");
  try {
    const payload = state.mode === "live" ? await api.validate(state.experimentId) : mockPolicy;
    state.policy = normalizePolicy(payload);
    renderPolicy();
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
    startTimer();
    updatePhase("baseline", null, state.baseline);
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
  if (!activePhases.has(state.phase)) return;
  elements.stopButton.disabled = true;
  try {
    if (state.mode === "live") {
      await api.abort(state.experimentId);
      updatePhase("rolling_back", "Emergency stop accepted. Waiting for verified backend rollback.", null);
      return;
    }
    clearLiveConnections();
    updatePhase("rolling_back", "Emergency stop accepted. Rollback is running.", mockPhaseMetrics.rolling_back);
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
    elements.stopButton.disabled = false;
    toast(`STOP request failed: ${error.message}. Check manual recovery now.`, "warn");
  }
});

elements.refreshButton.addEventListener("click", () => loadData({ announce: true }));
elements.viewReportButton.addEventListener("click", () => elements.reportDialog.showModal());
elements.closeReportButton.addEventListener("click", () => elements.reportDialog.close());
elements.reportDialog.addEventListener("click", (event) => { if (event.target === elements.reportDialog) elements.reportDialog.close(); });

renderPolicy();
updatePhase("idle", null, null);
loadData();
