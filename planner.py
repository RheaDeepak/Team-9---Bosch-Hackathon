"""Experiment planner: PROPOSES one experiment (ranker + investigating agent). It never runs anything."""
import json
import os
import re

import requests
from dotenv import load_dotenv

# Tried in order: Gemini models get overloaded or retired, so one name is a single point of failure.
LLM_MODELS = ("gemini-3.8-flash", "gemini-3-flash-preview", "gemini-flash-lite-latest")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
LLM_MAX_TOKENS = 4096  # thinking models spend part of this on reasoning before the answer
LLM_TIMEOUT_S = 20
VERDICTS = ("held", "partially held", "not held", "inconclusive")
CLAIM_STATUSES = ("supported", "contradicted", "no data")
API_KEY_VAR = "LLM_API_KEY"
PLACEHOLDER_KEY = "your-api-key-here"  # value shipped in .env.example
# Optional operator goals offered by the UI: key -> hint given to the planner.
GOALS = {
    "graceful_degradation": ("Test graceful degradation",
                             "Test graceful degradation: does the app keep serving its core journeys when one dependency degrades?"),
    "critical_dependency": ("Find a critical dependency",
                            "Find a critical dependency: the service whose failure would hurt the most user journeys."),
    "recovery_time": ("Measure recovery time",
                      "Measure recovery time: how quickly does the system return to normal once the fault is removed?"),
    "timeout_behavior": ("Expose timeout and retry behavior",
                         "Expose timeout and retry behavior of callers (a network_delay fault is the natural choice)."),
}
FALLBACK_PLAN = {
    "target": "cart", "fault": "container_stop", "magnitude": 0, "duration_s": 60,
    "hypothesis": "Stopping cart for 60s makes add-to-cart, view-cart and checkout fail visibly "
                  "(error rate rises on frontend and checkout) and the system recovers once cart restarts.",
    "reasoning": "Safe default: cart is on the checkout path and is called by both frontend and checkout, "
                 "so it exercises the core purchase journey, yet it is not a protected service.",
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


def load_api_key():
    """The Gemini API key from .env / the environment, or None if unset or still the placeholder."""
    load_dotenv()
    key = os.getenv(API_KEY_VAR, "").strip()
    return None if not key or key == PLACEHOLDER_KEY else key


def _post_gemini(api_key, body, models=LLM_MODELS):
    """POST one generateContent request, trying each model in turn -> (candidate, model)."""
    errors = []
    for model in models:  # next model on timeout, overload, or retired model
        try:
            resp = requests.post(GEMINI_URL.format(model=model), json=body, timeout=LLM_TIMEOUT_S,
                                 headers={"x-goog-api-key": api_key})
        except requests.RequestException as e:
            skipped.append(f"{model}: {type(e).__name__}")
            continue
        if resp.ok:
            cand = resp.json()["candidates"][0]
            if cand.get("finishReason") == "MAX_TOKENS":
                skipped.append(f"{model}: reply truncated (MAX_TOKENS)")
                continue
            return cand, model
        errors.append(f"{model}: HTTP {resp.status_code}")
    raise RuntimeError("all models failed (" + "; ".join(errors) + ")")


def _call_llm(api_key, content, system):
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": "Context:\n" + json.dumps(content, indent=1)}]}],
            "generationConfig": {"maxOutputTokens": LLM_MAX_TOKENS, "responseMimeType": "application/json"}}
    cand, model = _post_gemini(api_key, body)
    return "".join(p.get("text", "") for p in cand["content"]["parts"]), model


def propose_experiment():
    """Return (plan, source, note). source: "rank_agent" (LLM investigated the ranker's top-K),
    "ranker_fallback" (ranker #1, LLM unavailable or invalid) or "fallback" (FALLBACK_PLAN).
    The plan carries the top-K ranking and the agent's evidence under the extra "ranking" key."""
    from rank_agent import run_agent  # imported here: rank_agent imports this module
    try:
        out = run_agent()
    except Exception as e:  # no dependency map / baseline, or a bug: degrade to the safe plan
        return dict(FALLBACK_PLAN), "fallback", f"ranking failed: {type(e).__name__}: {e}"[:300]
    plan = dict(out["plan"])
    plan["ranking"] = {k: out[k] for k in ("source", "note", "model", "run_id", "tool_calls", "chosen",
                                           "predictions", "top_k")}
    return plan, out["source"], out["note"]


JUDGE_PROMPT = """You judge the outcome of a chaos engineering experiment.
Reply with a single JSON object and nothing else (no prose, no code fences):
{"claims": [{"claim": str, "status": "supported" | "contradicted" | "no data", "evidence": str}],
 "verdict": "held" | "partially held" | "not held" | "inconclusive", "explanation": str,
 "recommendation": str}

How to judge:
1. Split the hypothesis into its separate, testable claims and evaluate each one on its own. A sentence that
   names several services or conditions is SEVERAL claims: write one claim per service and metric, plus one for
   recovery. Example: "p95 of checkout and frontend rises, and it recovers" becomes three claims: "frontend p95
   rises above baseline", "checkout p95 rises above baseline", "the system recovers after the fault".
2. For each claim give a status: "supported" (the measured data shows it), "contradicted" (the measured data
   shows the opposite), or "no data" (that service had no traffic or the value is null). Quote the numbers.
3. An early abort is NOT missing evidence. The guard stopping the experiment when a safety threshold was
   breached is the safety system working as designed; the breach itself is evidence. Judge on the data that
   was collected before the abort. Do not use "inconclusive" just because the run ended early.
4. Verdict from the claims: "held" if every claim is supported; "partially held" if at least one claim is
   supported and at least one is contradicted or has no data; "not held" if none is supported and at least one
   is contradicted; "inconclusive" only if every claim has no data.
5. Recovery: use the recovery data (probes are the primary signal, span metrics the secondary one).
6. recommendation: ONE concrete resilience improvement for the weakness this experiment exposed (for example a
   bounded timeout, a retry budget, a circuit breaker, a cached or degraded fallback), tied to the measured
   evidence and naming the services involved. If nothing was exposed, say what to test next instead.

Telemetry notes: error rate and p95 are computed from server spans in a short window; services with low traffic
may show no data (null). Units: every error_rate value is a PERCENTAGE (0.6 means 0.6%, not 60%); p95_ms is
milliseconds; "change" is peak minus baseline in the same unit. "amplification" is the observed p95 increase
divided by the injected delay. Quote numbers exactly as given."""


def remediation_for(plan):
    """Generic, deterministic next step, used when the LLM gives no recommendation."""
    target = plan.get("target", "the dependency")
    if plan.get("fault") == "network_delay":
        return (f"Add a bounded timeout, a retry budget and a circuit breaker around calls to {target}, and keep a "
                f"degraded response path for when the latency budget is exhausted.")
    return (f"Add graceful fallback behaviour for {target}, cap caller timeouts, and verify that core journeys stay "
            f"available while {target} restarts.")


def judge_hypothesis(plan, results):
    """Ask the same LLM whether the hypothesis held -> {verdict, explanation}; inconclusive on any failure."""
    api_key = load_api_key()
    if not api_key:
        return {"verdict": "inconclusive", "explanation": f"LLM unavailable: {API_KEY_VAR} is not set"}
    try:
        content = {"hypothesis": plan["hypothesis"], "experiment": {k: plan[k] for k in ("target", "fault", "magnitude", "duration_s")},
                   "results": results}
        text, model, skipped = _call_llm(api_key, content, system=JUDGE_PROMPT)
        out = _parse_json(text)
        if out.get("verdict") not in VERDICTS:
            raise ValueError(f"bad verdict {out.get('verdict')!r}")
        claims = [{"claim": str(c.get("claim", "")), "evidence": str(c.get("evidence", "")),
                   "status": c["status"] if c.get("status") in CLAIM_STATUSES else "no data"}
                  for c in out.get("claims", []) if isinstance(c, dict)]
        return {"verdict": out["verdict"], "explanation": str(out.get("explanation", "")), "claims": claims,
                "recommendation": str(out.get("recommendation") or "").strip() or remediation_for(plan),
                "model": model, "skipped_models": skipped}
    except Exception as e:
        return {"verdict": "inconclusive", "explanation": f"LLM unavailable: {type(e).__name__}: {e}"[:300],
                "recommendation": remediation_for(plan)}
