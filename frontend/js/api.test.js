import test from "node:test";
import assert from "node:assert/strict";
import { normalizeBaseline, normalizePolicy, normalizeReadiness, normalizeTopology, resolveApiBaseUrl } from "./api.js";

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

test("preserves missing live metrics instead of displaying fake zeroes", () => {
  assert.deepEqual(normalizeBaseline({ availability: 100, error_rate: 0, p95_latency_ms: 109, throughput_rps: null }), {
    availability: 100,
    errorRate: 0,
    p95LatencyMs: 109,
    throughputRps: null,
  });
});

test("normalizes policy decisions", () => {
  const result = normalizePolicy({ valid: true, rules: [{ rule: "target allowlist", valid: true }] });
  assert.equal(result.allowed, true);
  assert.equal(result.checks[0].name, "target allowlist");
});
