"""Experiment planner: asks an LLM to PROPOSE one experiment as JSON. It never runs anything."""
import json
import os
import re

import requests
from dotenv import load_dotenv

from discovery import load_flows, load_graph
from metrics import load_baseline
from policy import ALLOWED_FAULTS, MAX_DURATION_S, MAX_NETWORK_DELAY_MS, PROTECTED, REQUIRED_KEYS

# Tried in order: Gemini models get overloaded or retired, so one name is a single point of failure.
LLM_MODELS = ("gemini-3.8-flash", "gemini-3-flash-preview", "gemini-flash-lite-latest")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
LLM_MAX_TOKENS = 800
LLM_TIMEOUT_S = 20
VERDICTS = ("held", "not held", "inconclusive")
API_KEY_VAR = "LLM_API_KEY"
PLACEHOLDER_KEY = "your-api-key-here"  # value shipped in .env.example
FALLBACK_PLAN = {
    "target": "cart", "fault": "container_stop", "magnitude": 0, "duration_s": 60,
    "hypothesis": "Stopping cart for 60s makes add-to-cart, view-cart and checkout fail visibly "
                  "(error rate rises on frontend and checkout) and the system recovers once cart restarts.",
    "reasoning": "Safe default: cart is on the checkout path and is called by both frontend and checkout, "
                 "so it exercises the core purchase journey, yet it is not a protected service.",
}
SYSTEM_PROMPT = f"""You are the experiment planner of a chaos engineering tool for a microservice app.
Propose exactly ONE experiment. You only propose; code validates and a human approves.
Reply with a single JSON object and nothing else: no prose, no markdown, no code fences.
Schema: {{"target": str, "fault": str, "magnitude": number, "duration_s": integer,
"hypothesis": str, "reasoning": str}}
Rules:
- fault is one of {list(ALLOWED_FAULTS)}.
- magnitude is the delay in ms for network_delay (1-{MAX_NETWORK_DELAY_MS}); use 0 for container_stop.
- duration_s is at most {MAX_DURATION_S}.
- target must be one of "candidate_targets" in the context. Never pick a protected service.
- hypothesis: what you expect to observe (which services/metrics degrade, and that it recovers).
- reasoning: why this target is informative; cite the dependency map (fan-in, fan-out, checkout path,
  who depends on it) and the baseline."""


def build_context(g, stats, baseline, flows):
    """Compact summary of the dependency map and baseline for the prompt."""
    base = (baseline or {}).get("services", {})
    services = {}
    for n in sorted(g):
        m = base.get(n, {})
        mean = lambda k: round(m[k]["mean"], 2) if m.get(k) else None
        services[n] = {"fan_in": stats["fan_in"][n], "fan_out": stats["fan_out"][n],
                       "on_checkout_path": n in stats["checkout_path"], "protected": n in PROTECTED,
                       "baseline": {"req_per_s": mean("request_rate"), "error_pct": mean("error_rate"),
                                    "p95_ms": mean("p95_ms")}}
    containers = set((baseline or {}).get("containers", g.nodes))
    return {
        "candidate_targets": [n for n in sorted(g) if n not in PROTECTED and n in containers],
        "protected_services": sorted(PROTECTED),
        "services": services,
        "edges": [f"{u} -> {v} ({d.get('type', 'unknown')}{', topic ' + d['topic'] if d.get('topic') else ''})"
                  for u, v, d in g.edges(data=True)],
        "checkout_path": stats["checkout_path"],
        "top_fan_in": stats["top_fan_in"],
        "user_journeys": {k: sorted({s for st in f["steps"] for s in (st["from"], st["to"])})
                          for k, f in flows.items()},
        "baseline_flags_enabled": (baseline or {}).get("flags_enabled", []),
    }


def _parse_json(text):
    """Strip code fences, fall back to the outermost {...}. Raises ValueError."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    try:
        obj = json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError("no JSON object in LLM reply")
        obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("LLM reply is not a JSON object")
    return obj


def parse_plan(text):
    plan = _parse_json(text)
    missing = [k for k in REQUIRED_KEYS if k not in plan]
    if missing:
        raise ValueError(f"LLM plan missing fields: {', '.join(missing)}")
    return {k: plan[k] for k in REQUIRED_KEYS}


def _call_llm(api_key, context, system=SYSTEM_PROMPT):
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": "Context:\n" + json.dumps(context, indent=1)}]}],
            "generationConfig": {"maxOutputTokens": LLM_MAX_TOKENS, "responseMimeType": "application/json"}}
    errors = []
    for model in LLM_MODELS:  # next model on timeout, overload, or retired model
        try:
            resp = requests.post(GEMINI_URL.format(model=model), json=body, timeout=LLM_TIMEOUT_S,
                                 headers={"x-goog-api-key": api_key})
        except requests.RequestException as e:
            errors.append(f"{model}: {type(e).__name__}")
            continue
        if resp.ok:
            parts = resp.json()["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts), model
        errors.append(f"{model}: HTTP {resp.status_code}")
    raise RuntimeError("all models failed (" + "; ".join(errors) + ")")


def propose_experiment():
    """Return (plan, source, note); source is "llm" or "fallback" and note says why."""
    load_dotenv()
    api_key = os.getenv(API_KEY_VAR, "").strip()
    if not api_key or api_key == PLACEHOLDER_KEY:
        return dict(FALLBACK_PLAN), "fallback", f"{API_KEY_VAR} is not set (empty or still the placeholder)"
    try:
        g, stats = load_graph()
        context = build_context(g, stats, load_baseline(), load_flows())
        text, model = _call_llm(api_key, context)
        return parse_plan(text), "llm", f"model {model}"
    except Exception as e:  # any LLM/network/parse/file failure must degrade to the safe plan
        return dict(FALLBACK_PLAN), "fallback", f"LLM planning failed: {type(e).__name__}: {e}"[:300]


JUDGE_PROMPT = """You judge the outcome of a chaos engineering experiment.
Given the hypothesis and the measured results, decide whether the hypothesis held.
Reply with a single JSON object and nothing else (no prose, no code fences):
{"verdict": "held" | "not held" | "inconclusive", "explanation": str}
Use "inconclusive" when the data is too sparse or noisy to decide, or the experiment was aborted
before the hypothesis could be tested. Telemetry notes: error rate and p95 are computed from server
spans seen in the last few seconds; services with low traffic may show no data (null).
Units: every error_rate value is a PERCENTAGE (0.6 means 0.6%, not 60%); p95_ms is milliseconds;
"change" is peak minus baseline in the same unit. Quote numbers exactly as given."""


def judge_hypothesis(plan, results):
    """Ask the same LLM whether the hypothesis held -> {verdict, explanation}; inconclusive on any failure."""
    load_dotenv()
    api_key = os.getenv(API_KEY_VAR, "").strip()
    if not api_key or api_key == PLACEHOLDER_KEY:
        return {"verdict": "inconclusive", "explanation": f"LLM unavailable: {API_KEY_VAR} is not set"}
    try:
        content = {"hypothesis": plan["hypothesis"], "experiment": {k: plan[k] for k in ("target", "fault", "magnitude", "duration_s")},
                   "results": results}
        text, model = _call_llm(api_key, content, system=JUDGE_PROMPT)
        out = _parse_json(text)
        if out.get("verdict") not in VERDICTS:
            raise ValueError(f"bad verdict {out.get('verdict')!r}")
        return {"verdict": out["verdict"], "explanation": str(out.get("explanation", "")), "model": model}
    except Exception as e:
        return {"verdict": "inconclusive", "explanation": f"LLM unavailable: {type(e).__name__}: {e}"[:300]}
