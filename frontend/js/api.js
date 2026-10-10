const DEFAULT_TIMEOUT_MS = 3500;

export class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function createApiClient({ baseUrl = "", timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  const request = async (path, options = {}) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const headers = { Accept: "application/json", ...options.headers };
    if (options.body) headers["Content-Type"] = "application/json";

    try {
      const response = await fetch(`${baseUrl}${path}`, { ...options, headers, signal: controller.signal });
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
    health: () => request("/api/health"),
    readiness: () => request("/api/readiness"),
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
    x: node.x ?? 90 + (index % 4) * 190,
    y: node.y ?? 70 + Math.floor(index / 4) * 105,
  })).filter((node) => node.id);
  const edges = (graph.edges || graph.dependencies || []).map((edge) => Array.isArray(edge)
    ? edge
    : [edge.source || edge.from || edge.caller, edge.target || edge.to || edge.callee]);
  return { nodes, edges: edges.filter(([source, target]) => source && target) };
}

export function normalizeBaseline(payload = {}) {
  const source = payload.metrics || payload.summary || payload;
  return {
    availability: Number(source.availability ?? source.success_rate ?? 0),
    errorRate: Number(source.errorRate ?? source.error_rate ?? 0),
    p95LatencyMs: Number(source.p95LatencyMs ?? source.p95_latency_ms ?? source.p95 ?? 0),
    throughputRps: Number(source.throughputRps ?? source.throughput_rps ?? source.throughput ?? 0),
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
