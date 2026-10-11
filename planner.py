"""Compatibility facade for planning and hypothesis evaluation.

All proposals use the LangGraph planning service. Experiment approval and
execution remain outside this module.
"""
import json
import re

from agents.planning import CapabilityUnavailable, run_agent
from agents.verdict import judge_hypothesis, remediation_for
from policy import REQUIRED_KEYS
from traffic_spike import capability as traffic_spike_capability

TRAFFIC_SPIKE_GOAL = "capacity"
_RANKED_DEFAULT = object()

GOALS = {
    "graceful_degradation": (
        "Test graceful degradation",
        "Test graceful degradation: does the app keep serving its core journeys when one dependency degrades?",
    ),
    "critical_dependency": (
        "Find a critical dependency",
        "Find a critical dependency: select the service whose failure would hurt the most user journeys.",
    ),
    "recovery_time": (
        "Measure recovery time",
        "Measure recovery time: select an experiment that clearly measures return to baseline after rollback.",
    ),
    "timeout_behavior": (
        "Expose timeout and retry behavior",
        "Expose timeout and retry behavior of callers; prefer a network-delay candidate when evidence supports it.",
    ),
}

FALLBACK_PLAN = {
    "target": "cart",
    "fault": "container_stop",
    "magnitude": 0,
    "duration_s": 60,
    "hypothesis": (
        "Stopping cart for 60s makes add-to-cart, view-cart and checkout fail visibly "
        "and the system recovers once cart restarts."
    ),
    "reasoning": (
        "Emergency deterministic fallback: cart exercises the purchase journey and is not a protected service."
    ),
}


def parse_plan(text):
    """Legacy JSON parser retained for callers that validate stored model replies."""
    fence = chr(96) * 3
    text = re.sub(r"^\s*" + fence + r"(?:json)?\s*|\s*" + fence + r"\s*$", "", text.strip(), flags=re.I)
    try:
        value = json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise ValueError("no JSON object in LLM reply")
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("LLM reply is not a JSON object")
    missing = [key for key in REQUIRED_KEYS if key not in value]
    if missing:
        raise ValueError("LLM plan missing fields: " + ", ".join(missing))
    return {key: value[key] for key in REQUIRED_KEYS}


def propose_experiment(goal=_RANKED_DEFAULT):
    """Run the planning graph and preserve the established tuple API."""
    requested = None if goal is _RANKED_DEFAULT else goal
    graph_goal = TRAFFIC_SPIKE_GOAL if requested == TRAFFIC_SPIKE_GOAL else GOALS.get(requested, (None, None))[1]
    try:
        output = run_agent(goal=graph_goal, traffic_capability=traffic_spike_capability())
    except CapabilityUnavailable:
        raise
    except Exception as exc:
        return dict(FALLBACK_PLAN), "ranker_fallback", (
            f"planning failed before a candidate could be selected: {type(exc).__name__}: {exc}"
        )[:300]
    plan = dict(output["plan"])
    plan["ranking"] = {
        key: output[key]
        for key in ("source", "note", "model", "run_id", "tool_calls", "chosen", "predictions", "top_k")
    }
    return plan, output["source"], output["note"]
