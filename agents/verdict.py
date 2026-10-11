"""Typed one-shot hypothesis verdict; this is deliberately not an agent graph."""
import json
from pathlib import Path

from langchain_core.messages import HumanMessage

from agents.model import API_KEY_VAR, GeminiModels, load_api_key
from agents.models import HypothesisVerdict

PROMPT_VERSION = "verdict-structured-v1"
PROMPT = (Path(__file__).parent / "prompts" / "verdict.md").read_text(encoding="utf-8")


def remediation_for(plan):
    target = plan.get("target", "the dependency")
    if plan.get("fault") == "network_delay":
        return (f"Add a bounded timeout, a retry budget and a circuit breaker around calls to {target}, and keep a "
                f"degraded response path for when the latency budget is exhausted.")
    if plan.get("fault") == "traffic_spike":
        return ("Tune frontend concurrency and autoscaling from the measured saturation point; preserve request "
                "limits and backpressure for downstream services.")
    return (f"Add graceful fallback behaviour for {target}, cap caller timeouts, and verify that core journeys stay "
            f"available while {target} restarts.")


def judge_hypothesis(plan, results, *, model_client=None, api_key=None):
    key = api_key or load_api_key()
    if model_client is None and not key:
        return {"claims": [], "verdict": "inconclusive", "explanation": f"LLM unavailable: {API_KEY_VAR} is not set",
                "recommendation": remediation_for(plan)}
    client = model_client or GeminiModels(key)
    content = {"hypothesis": plan["hypothesis"],
               "experiment": {field: plan[field] for field in ("target", "fault", "magnitude", "duration_s")},
               "results": results}
    try:
        verdict, model = client.invoke_structured(PROMPT, [HumanMessage(content=json.dumps(content, indent=1))],
                                                  HypothesisVerdict)
        output = verdict.model_dump()
        output["recommendation"] = output["recommendation"].strip() or remediation_for(plan)
        output["model"] = model
        return output
    except Exception as exc:
        return {"claims": [], "verdict": "inconclusive",
                "explanation": f"LLM unavailable: {type(exc).__name__}: {exc}"[:300],
                "recommendation": remediation_for(plan)}
