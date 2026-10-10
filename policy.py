"""Deterministic safety policy: validates an experiment plan. No LLM involved."""
import networkx as nx

from discovery import load_graph

ALLOWED_FAULTS = ("container_stop", "network_delay")
MAX_DURATION_S = 120
MAX_NETWORK_DELAY_MS = 1000
PROTECTED = {"frontend-proxy", "otel-collector", "jaeger", "prometheus", "grafana", "opensearch", "flagd"}
REQUIRED_KEYS = ("target", "fault", "magnitude", "duration_s", "hypothesis", "reasoning")


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def blast_radius(g, target):
    """Every service that depends on the target, directly or indirectly (edges are caller -> callee)."""
    return sorted(nx.ancestors(g, target)) if target in g else []


def validate(plan, graph=None):
    """Return {valid, reasons, blast_radius}; reasons lists every failed check."""
    reasons = []
    if graph is None:
        try:
            graph, _ = load_graph()
        except (OSError, ValueError, KeyError) as e:
            graph = nx.DiGraph()
            reasons.append(f"dependency map unavailable ({e}); cannot verify target")
    if not isinstance(plan, dict):
        return {"valid": False, "reasons": ["plan is not a JSON object"], "blast_radius": []}

    reasons += [f"missing field: {k}" for k in REQUIRED_KEYS if k not in plan]
    fault, magnitude, duration, target = (plan.get(k) for k in ("fault", "magnitude", "duration_s", "target"))

    if "fault" in plan and fault not in ALLOWED_FAULTS:
        reasons.append(f"fault {fault!r} not allowed (allowed: {', '.join(ALLOWED_FAULTS)})")
    if "magnitude" in plan:
        if not _is_num(magnitude) or magnitude < 0:
            reasons.append(f"magnitude {magnitude!r} must be a non-negative number")
        elif fault == "network_delay" and not 0 < magnitude <= MAX_NETWORK_DELAY_MS:
            reasons.append(f"network delay {magnitude}ms outside 1-{MAX_NETWORK_DELAY_MS}ms")
    if "duration_s" in plan and (not _is_num(duration) or not 0 < duration <= MAX_DURATION_S):
        reasons.append(f"duration {duration!r}s must be between 1 and {MAX_DURATION_S}s")
    if "target" in plan:
        if target not in graph:
            reasons.append(f"target {target!r} is not a service in the dependency map")
        if target in PROTECTED:
            reasons.append(f"target {target!r} is protected")

    return {"valid": not reasons, "reasons": reasons, "blast_radius": blast_radius(graph, target)}
