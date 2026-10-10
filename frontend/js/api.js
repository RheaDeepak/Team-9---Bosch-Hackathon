const DEFAULT_TIMEOUT_MS = 8000;
const PLANNER_TIMEOUT_MS = 65000;

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

export function resolveGrafanaBaseUrl(locationLike = globalThis.location, configuredUrl = globalThis.CHAOS_COPILOT_GRAFANA_URL) {
  if (typeof configuredUrl === "string" && configuredUrl.trim()) return configuredUrl.trim().replace(/\/$/, "");
  const protocol = locationLike?.protocol === "https:" ? "https:" : "http:";
  const hostname = locationLike?.hostname || "127.0.0.1";
  return `${protocol}//${hostname}:8080/grafana`;
}

export function buildGrafanaPanelUrl({ baseUrl, panelId, service = "frontend" }) {
  const params = new URLSearchParams({
    orgId: "1",
    panelId: String(panelId),
    "var-service": service,
    from: "now-15m",
    to: "now",
    refresh: "5s",
  });
  return `${baseUrl.replace(/\/$/, "")}/d-solo/W2gX2zHVk/demo-dashboard?${params}`;
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
    const { timeoutMs: requestTimeoutMs = timeoutMs, ...fetchOptions } = options;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), requestTimeoutMs);
    const headers = { Accept: "application/json", ...fetchOptions.headers };
    if (fetchOptions.body) headers["Content-Type"] = "application/json";

    try {
      const response = await fetch(endpoint(path), { ...fetchOptions, headers, signal: controller.signal });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new ApiError(payload.detail || `Request failed with ${response.status}`, response.status);
      }
      if (response.status === 204) return null;
      return await response.json();
    } catch (error) {
      if (error.name === "AbortError") throw new ApiError("Request timed out");
      throw error;
    } finally {
      clearTimeout(timer);
    }
  };

  const mutate = (path, body = {}, timeoutOverride) => request(path, {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(body),
    timeoutMs: timeoutOverride,
  });

  return {
    baseUrl,
    eventsUrl: (id) => endpoint(`/api/experiments/${encodeURIComponent(id)}/events`),
    health: () => request("/api/health"),
    readiness: () => request("/api/readiness"),
    topology: () => request("/api/topology"),
    latestBaseline: () => request("/api/baselines/latest"),
    propose: (goal = null) => mutate("/api/experiments/propose", goal ? { goal } : {}, PLANNER_TIMEOUT_MS),
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
  const numeric = (value) => value === null || value === undefined || value === "" ? null : Number(value);
  return {
    availability: numeric(source.availability ?? source.success_rate),
    errorRate: numeric(source.errorRate ?? source.error_rate),
    p95LatencyMs: numeric(source.p95LatencyMs ?? source.p95_latency_ms ?? source.p95),
    throughputRps: numeric(source.throughputRps ?? source.throughput_rps ?? source.throughput),
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
