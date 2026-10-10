export const mockReadiness = {
  status: "ready",
  checks: [
    { id: "docker", label: "Docker", detail: "Connected", ready: true },
    { id: "target", label: "Astronomy Shop", detail: "21 services", ready: true },
    { id: "prometheus", label: "Prometheus", detail: "Receiving data", ready: true },
    { id: "jaeger", label: "Jaeger", detail: "Traces available", ready: true },
    { id: "safety", label: "Safety policy", detail: "Loaded", ready: true },
  ],
};

export const mockTopology = {
  nodes: [
    { id: "frontend", label: "frontend", role: "application", health: "healthy", criticality: 0.92, protected: false, x: 76, y: 180 },
    { id: "frontend-proxy", label: "frontend-proxy", role: "application", health: "healthy", criticality: 0.88, protected: false, x: 76, y: 74 },
    { id: "product-catalog", label: "product-catalog", role: "application", health: "healthy", criticality: 0.86, protected: false, x: 282, y: 92 },
    { id: "recommendation", label: "recommendation", role: "application", health: "healthy", criticality: 0.67, protected: false, x: 282, y: 186 },
    { id: "cart", label: "cart", role: "application", health: "healthy", criticality: 0.72, protected: false, x: 282, y: 284 },
    { id: "checkout", label: "checkout", role: "application", health: "healthy", criticality: 0.84, protected: false, x: 490, y: 258 },
    { id: "valkey-cart", label: "valkey-cart", role: "datastore", health: "healthy", criticality: 0.64, protected: false, x: 490, y: 350 },
    { id: "payment", label: "payment", role: "application", health: "healthy", criticality: 0.55, protected: false, x: 676, y: 205 },
    { id: "shipping", label: "shipping", role: "application", health: "healthy", criticality: 0.58, protected: false, x: 676, y: 287 },
    { id: "otel-collector", label: "otel-collector", role: "observability", health: "healthy", criticality: 1, protected: true, x: 490, y: 66 },
    { id: "prometheus", label: "prometheus", role: "observability", health: "healthy", criticality: 1, protected: true, x: 676, y: 66 },
    { id: "load-generator", label: "load-generator", role: "control-plane", health: "healthy", criticality: 0.9, protected: true, x: 76, y: 326 },
  ],
  edges: [
    ["frontend-proxy", "frontend"], ["frontend", "product-catalog"], ["frontend", "recommendation"],
    ["frontend", "cart"], ["frontend", "checkout"], ["recommendation", "product-catalog"],
    ["cart", "valkey-cart"], ["checkout", "cart"], ["checkout", "payment"],
    ["checkout", "shipping"], ["load-generator", "frontend-proxy"], ["frontend", "otel-collector"],
    ["otel-collector", "prometheus"], ["product-catalog", "otel-collector"], ["recommendation", "otel-collector"],
  ],
};

export const mockBaseline = {
  availability: 99.8,
  errorRate: 0.4,
  p95LatencyMs: 184,
  throughputRps: 42.1,
};

export const mockProposal = {
  id: "exp-rec-042",
  title: "Recommendation service crash",
  goal: "Test whether product browsing survives recommendation failure",
  hypothesis: "Core browsing remains available while recommendations fail gracefully.",
  target_service: "recommendation",
  failure_type: "CONTAINER_KILL",
  parameters: { duration_seconds: 30, signal: "SIGKILL" },
  blast_radius: "1 service",
  rollback: "Restart container",
  expected_impact: ["Recommendation requests fail", "Catalog browsing remains available"],
  observe: ["frontend error rate", "frontend p95 latency", "recommendation span errors", "recovery time"],
  reasoning_summary: "Recommendation is user-visible but should not be required for the core catalog journey.",
};

export const mockPolicy = {
  allowed: true,
  checks: [
    { name: "Exact target is allowlisted", passed: true, detail: "recommendation" },
    { name: "Control plane remains protected", passed: true, detail: "No protected targets" },
    { name: "Fault duration is within limit", passed: true, detail: "30s ≤ 60s" },
    { name: "Rollback action is available", passed: true, detail: "Restart + health check" },
    { name: "Single experiment limit", passed: true, detail: "No active run" },
    { name: "Abort thresholds configured", passed: true, detail: "5 automatic guards" },
  ],
};

export const mockResult = {
  resilience_score: 68,
  impact_score: 32,
  severity: "Moderate",
  confidence: "High confidence",
  summary: "Core browsing stayed available, but recommendation errors leaked into the frontend response path.",
  recovery_seconds: 8.4,
  evidence: [
    { label: "Browse catalog", value: "97.6% available", tone: "good" },
    { label: "Recommendation spans", value: "31.2% errors", tone: "warn" },
    { label: "p95 latency", value: "+118 ms", tone: "warn" },
    { label: "Verified recovery", value: "8.4 seconds", tone: "good" },
  ],
  remediation: "Isolate recommendation calls behind a short timeout and render product pages without the recommendation block when the dependency is unavailable.",
};

export const mockPhaseMetrics = {
  baseline: { availability: 99.8, errorRate: 0.4, p95LatencyMs: 184, throughputRps: 42.1 },
  injecting: { availability: 98.9, errorRate: 3.8, p95LatencyMs: 221, throughputRps: 41.5 },
  observing: { availability: 97.6, errorRate: 8.7, p95LatencyMs: 302, throughputRps: 39.8 },
  rolling_back: { availability: 98.4, errorRate: 4.2, p95LatencyMs: 236, throughputRps: 40.7 },
  recovering: { availability: 99.6, errorRate: 0.7, p95LatencyMs: 192, throughputRps: 41.9 },
  completed: { availability: 99.8, errorRate: 0.4, p95LatencyMs: 184, throughputRps: 42.1 },
};
