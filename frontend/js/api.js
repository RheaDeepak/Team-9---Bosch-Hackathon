const DEFAULT_TIMEOUT_MS = 3500;

export function resolveApiBaseUrl(locationLike = globalThis.location, configuredUrl = globalThis.CHAOS_COPILOT_API_URL) {
  if (typeof configuredUrl === "string" && configuredUrl.trim()) return configuredUrl.trim().replace(/\/$/, "");
  if (!locationLike) return "";
  if (locationLike.protocol === "file:") return "http://127.0.0.1:8000";
  const isLocal = locationLike.hostname === "127.0.0.1" || locationLike.hostname === "localhost";
  if (isLocal && locationLike.port && locationLike.port !== "8000") {
    return `${locationLike.protocol}//${locationLike.hostname}:8000`;
  }
  return "";
}

export class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function createApiClient({ baseUrl = "", timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  const endpoint = (path) => `${baseUrl}${path}`;
  const request = async (path, options = {}) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const headers = { Accept: "application/json", ...options.headers };
    if (options.body) headers["Content-Type"] = "application/json";

    try {
      const response = await fetch(endpoint(path), { ...options, headers, signal: controller.signal });
      if (!response.ok) throw new ApiError(`Request failed with ${response.status}`, response.status);
      if (response.status === 204) return null;
      return await response.json();
    } catch (error) {
      if (error.name === "AbortError") throw new ApiError("Request timed out");
      throw error;
    } finally {
      clearTimeout(timer);
    }
  };

  const mutate = (path, body = {}) => request(path, {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(body),
  });

  return {
    baseUrl,
    eventsUrl: (id) => endpoint(`/api/experiments/${encodeURIComponent(id)}/events`),
    health: () => request("/api/health"),
    readiness: () => request("/api/readiness"),
    faultCapabilities: () => request("/api/fault-capabilities"),
    topology: () => request("/api/topology"),
    latestBaseline: () => request("/api/baselines/latest"),
    propose: (goal) => mutate("/api/experiments/propose", { goal }),
    validate: (id) => mutate(`/api/experiments/${encodeURIComponent(id)}/validate`),
    approve: (id, approvalName = "hackathon-operator") => mutate(`/api/experiments/${encodeURIComponent(id)}/approve`, { approval_name: approvalName }),
    abort: (id, reason = "Manual emergency stop") => mutate(`/api/experiments/${encodeURIComponent(id)}/abort`, { reason }),
    experiment: (id) => request(`/api/experiments/${encodeURIComponent(id)}`),
    report: (id) => request(`/api/reports/${encodeURIComponent(id)}`),
    findings: () => request("/api/findings"),
  };
}

export function normalizeFaultCapabilities(payload = {}) {
  const capabilities = Array.isArray(payload.capabilities) ? payload.capabilities : [];
  return capabilities.map((capability) => ({
    failureType: String(capability.failure_type || "").toLowerCase(),
    version: capability.capability_version || "unknown",
    targetServices: Array.isArray(capability.supported_targets) ? capability.supported_targets : [],
    controllerService: capability.controller_service || "unknown",
    detail: capability.detail || "Capability status unavailable",
    limits: capability.limits || {},
    ready: Boolean(capability.ready),
    selectable: Boolean(
      capability.ready
      && capability.executor_ready
      && capability.rollback_ready
      && capability.integration_test_evidence,
    ),
    integrationTestEvidence: capability.integration_test_evidence || null,
  })).filter((capability) => capability.failureType);
}

export function proposalParameterRows(proposal = {}) {
  const parameters = proposal.parameters || {};
  const rows = [["Duration", parameters.duration_seconds ? `${parameters.duration_seconds} sec` : "Bounded"]];
  if (String(proposal.failure_type || "").toLowerCase() === "traffic_spike") {
    rows.push(
      ["Original load", `${parameters.baseline_users} users`],
      ["Peak load", `${parameters.peak_users} users`],
      ["Spawn rate", `${parameters.spawn_rate_per_second} users/sec`],
      ["Journey", String(parameters.journey_profile || "Fixed profile").replaceAll("-", " ")],
    );
  } else if (parameters.delay_ms != null) {
    rows.push(["Network delay", `${parameters.delay_ms} ms`]);
  }
  rows.push(["Blast radius", proposal.blast_radius || "Unknown"], ["Rollback", proposal.rollback || "Executor rollback"]);
  return rows;
}

export function normalizeReadiness(payload = {}) {
  const rawChecks = Array.isArray(payload.checks)
    ? payload.checks
    : Object.entries(payload.checks || {}).map(([id, value]) => ({ id, ...(typeof value === "object" ? value : { ready: Boolean(value) }) }));
  return {
    status: payload.status || (rawChecks.every((check) => check.ready ?? check.passed) ? "ready" : "blocked"),
    checks: rawChecks.map((check, index) => ({
      id: check.id || check.name || `check-${index}`,
      label: check.label || check.name || check.id || `Check ${index + 1}`,
      detail: check.detail || check.message || (check.ready ?? check.passed ? "Ready" : "Unavailable"),
      ready: Boolean(check.ready ?? check.passed),
    })),
  };
}

export function normalizeTopology(payload = {}) {
  const graph = payload.graph || payload;
  const nodes = (graph.nodes || graph.services || []).map((node, index) => ({
    id: node.id || node.name || node.service_name,
    label: node.label || node.name || node.service_name,
    role: node.role || node.type || "application",
    health: node.health || node.status || "unknown",
    criticality: Number(node.criticality ?? node.criticality_score ?? 0.5),
    protected: Boolean(node.protected ?? node.is_protected),
    x: node.x ?? null,       // no coordinates from the backend: the page lays the graph out itself (graph-layout.js)
    y: node.y ?? null,
  })).filter((node) => node.id);
  const edges = (graph.edges || graph.dependencies || []).map((edge) => Array.isArray(edge)
    ? edge
    : [edge.source || edge.from || edge.caller, edge.target || edge.to || edge.callee]);
  return { nodes, edges: edges.filter(([source, target]) => source && target) };
}

// A metric the backend did not report stays null ("No data" in the UI); it is never turned into a zero.
const metricOrNull = (...values) => {
  const found = values.find((value) => value !== null && value !== undefined && value !== "");
  const number = found === undefined ? NaN : Number(found);
  return Number.isFinite(number) ? number : null;
};

export function normalizeBaseline(payload = {}) {
  const source = payload.metrics || payload.summary || payload;
  return {
    availability: metricOrNull(source.availability, source.success_rate),
    errorRate: metricOrNull(source.errorRate, source.error_rate),
    p95LatencyMs: metricOrNull(source.p95LatencyMs, source.p95_latency_ms, source.p95),
    throughputRps: metricOrNull(source.throughputRps, source.throughput_rps, source.throughput),
  };
}

export function normalizePolicy(payload = {}) {
  const checks = payload.checks || payload.results || payload.rules || [];
  return {
    allowed: Boolean(payload.allowed ?? payload.passed ?? payload.valid),
    checks: checks.map((check, index) => ({
      name: check.name || check.rule || check.label || `Policy check ${index + 1}`,
      passed: Boolean(check.passed ?? check.allowed ?? check.valid),
      detail: check.detail || check.message || check.reason || "",
    })),
  };
}
