"""Deterministic safety policy: validates an experiment plan. No LLM involved."""
import os

import networkx as nx
from dotenv import load_dotenv
from pydantic import ValidationError

from discovery import load_graph
from traffic_spike import (
    FAULT_TYPE as TRAFFIC_SPIKE,
    MAX_DURATION_SECONDS as MAX_TRAFFIC_SPIKE_DURATION_S,
    MAX_PEAK_USERS as MAX_TRAFFIC_SPIKE_USERS,
    TARGET_SERVICE as TRAFFIC_SPIKE_TARGET,
    TrafficSpikeParameters,
)

load_dotenv()

ALLOWED_FAULTS = ("container_stop", "network_delay", TRAFFIC_SPIKE)
MAX_DURATION_S = int(os.getenv("MAX_FAULT_DURATION_SECONDS", "120"))
MAX_NETWORK_DELAY_MS = int(os.getenv("MAX_NETWORK_DELAY_MS", "1000"))
PROTECTED = {"frontend-proxy", "otel-collector", "jaeger", "prometheus", "grafana", "opensearch", "flagd", "load-generator"}
REQUIRED_KEYS = ("target", "fault", "magnitude", "duration_s", "hypothesis", "reasoning")


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def blast_radius(g, target):
    """Every service that depends on the target, directly or indirectly (edges are caller -> callee)."""
    return sorted(nx.ancestors(g, target)) if target in g else []


def validate(plan, graph=None):
    """Return {valid, reasons, blast_radius, checks}.
    `reasons` lists every failed check; `checks` lists all of them, passed or not, for display."""
    reasons, checks = [], []

    def check(name, ok, passed, failed):
        checks.append({"name": name, "passed": bool(ok), "detail": passed if ok else failed})
        if not ok:
            reasons.append(failed)

    if graph is None:
        try:
            graph, _ = load_graph()
        except (OSError, ValueError, KeyError) as e:
            graph = nx.DiGraph()
            check("Dependency map available", False, "", f"dependency map unavailable ({e}); cannot verify target")
    if not isinstance(plan, dict):
        check("Plan is a JSON object", False, "", "plan is not a JSON object")
        return {"valid": False, "reasons": reasons, "blast_radius": [], "checks": checks}

    missing = [k for k in REQUIRED_KEYS if k not in plan]
    checks.append({"name": "Plan is complete", "passed": not missing,
                   "detail": "all fields present" if not missing else "missing: " + ", ".join(missing)})
    reasons += [f"missing field: {k}" for k in missing]
    fault, magnitude, duration, target = (plan.get(k) for k in ("fault", "magnitude", "duration_s", "target"))

    if "fault" in plan:
        check("Fault type is allowed", fault in ALLOWED_FAULTS, f"{fault} is on the allowlist",
              f"fault {fault!r} not allowed (allowed: {', '.join(ALLOWED_FAULTS)})")
    if "magnitude" in plan:
        if not _is_num(magnitude) or magnitude < 0:
            check("Magnitude within cap", False, "", f"magnitude {magnitude!r} must be a non-negative number")
        elif fault == "network_delay":
            check("Magnitude within cap", 0 < magnitude <= MAX_NETWORK_DELAY_MS,
                  f"{magnitude} ms delay (cap {MAX_NETWORK_DELAY_MS} ms)",
                  f"network delay {magnitude}ms outside 1-{MAX_NETWORK_DELAY_MS}ms")
        elif fault == TRAFFIC_SPIKE:
            check("Magnitude within cap", 2 <= magnitude <= MAX_TRAFFIC_SPIKE_USERS,
                  f"{magnitude} users (cap {MAX_TRAFFIC_SPIKE_USERS})",
                  f"traffic spike {magnitude} users outside 2-{MAX_TRAFFIC_SPIKE_USERS}")
        else:
            check("Magnitude within cap", True, "no magnitude needed for this fault", "")
    if fault == TRAFFIC_SPIKE:
        try:
            parameters = TrafficSpikeParameters.model_validate(plan.get("parameters"))
            parameters_ok = (
                target == TRAFFIC_SPIKE_TARGET
                and magnitude == parameters.peak_users
                and duration == parameters.duration_seconds
            )
            detail = "typed parameters match the approved user count and duration"
            failure = (
                f"traffic_spike requires target {TRAFFIC_SPIKE_TARGET!r}, magnitude equal to peak_users, "
                "and duration_s equal to duration_seconds"
            )
        except ValidationError as exc:
            parameters_ok = False
            detail = ""
            failure = "traffic_spike parameters invalid (" + "; ".join(
                f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                for item in exc.errors(include_url=False)
            ) + ")"
        check("Traffic spike parameters are bounded", parameters_ok, detail, failure)
    if "duration_s" in plan:
        duration_cap = MAX_TRAFFIC_SPIKE_DURATION_S if fault == TRAFFIC_SPIKE else MAX_DURATION_S
        check("Duration within cap", _is_num(duration) and 0 < duration <= duration_cap,
              f"{duration} s (cap {duration_cap} s)", f"duration {duration!r}s must be between 1 and {duration_cap}s")
    if "target" in plan:
        check("Target exists in the dependency map", target in graph, f"{target} is a discovered service",
              f"target {target!r} is not a service in the dependency map")
        check("Target is not protected", target not in PROTECTED, f"{target} is not on the protected list",
              f"target {target!r} is protected")

    return {"valid": not reasons, "reasons": reasons, "blast_radius": blast_radius(graph, target), "checks": checks}
