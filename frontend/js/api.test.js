import test from "node:test";
import assert from "node:assert/strict";
import { normalizeBaseline, normalizeFaultCapabilities, normalizePolicy, normalizeReadiness, normalizeTopology, proposalParameterRows, resolveApiBaseUrl } from "./api.js";

test("routes a standalone local frontend to the FastAPI port", () => {
  assert.equal(resolveApiBaseUrl({ protocol: "http:", hostname: "127.0.0.1", port: "4173" }), "http://127.0.0.1:8000");
  assert.equal(resolveApiBaseUrl({ protocol: "http:", hostname: "127.0.0.1", port: "8000" }), "");
});

test("normalizes readiness maps without treating failed checks as ready", () => {
  const result = normalizeReadiness({ checks: { docker: { passed: true }, prometheus: { passed: false, message: "No data" } } });
  assert.equal(result.status, "blocked");
  assert.deepEqual(result.checks.map((check) => check.ready), [true, false]);
});

test("normalizes service graph aliases", () => {
  const result = normalizeTopology({ services: [{ service_name: "cart", is_protected: false }], dependencies: [{ caller: "frontend", callee: "cart" }] });
  assert.equal(result.nodes[0].id, "cart");
  assert.deepEqual(result.edges[0], ["frontend", "cart"]);
});

test("normalizes snake-case baseline metrics", () => {
  assert.deepEqual(normalizeBaseline({ summary: { availability: 99.5, error_rate: 0.5, p95_latency_ms: 220, throughput_rps: 38 } }), {
    availability: 99.5,
    errorRate: 0.5,
    p95LatencyMs: 220,
    throughputRps: 38,
  });
});

test("normalizes policy decisions", () => {
  const result = normalizePolicy({ valid: true, rules: [{ rule: "target allowlist", valid: true }] });
  assert.equal(result.allowed, true);
  assert.equal(result.checks[0].name, "target allowlist");
});

test("exposes traffic spike only when capability and rollback evidence are ready", () => {
  const [ready, missingRollback] = normalizeFaultCapabilities({ capabilities: [
    {
      failure_type: "traffic_spike",
      capability_version: "traffic-spike-v1",
      supported_targets: ["frontend"],
      executor_ready: true,
      rollback_ready: true,
      ready: true,
      integration_test_evidence: "tests/test_traffic_spike_integration.py",
      limits: { peak_users: 10 },
    },
    {
      failure_type: "traffic_spike_preview",
      executor_ready: true,
      rollback_ready: false,
      ready: true,
      integration_test_evidence: "tests/test_preview.py",
    },
  ] });

  assert.equal(ready.selectable, true);
  assert.deepEqual(ready.targetServices, ["frontend"]);
  assert.equal(ready.limits.peak_users, 10);
  assert.equal(missingRollback.selectable, false);
});

test("renders every bounded traffic-spike parameter for operator review", () => {
  const rows = proposalParameterRows({
    failure_type: "TRAFFIC_SPIKE",
    parameters: {
      baseline_users: 5,
      peak_users: 8,
      spawn_rate_per_second: 2,
      duration_seconds: 20,
      journey_profile: "astronomy-shop-default",
    },
    blast_radius: "1 service(s)",
    rollback: "Restore original Locust state",
  });

  assert.deepEqual(rows, [
    ["Duration", "20 sec"],
    ["Original load", "5 users"],
    ["Peak load", "8 users"],
    ["Spawn rate", "2 users/sec"],
    ["Journey", "astronomy shop default"],
    ["Blast radius", "1 service(s)"],
    ["Rollback", "Restore original Locust state"],
  ]);
});
