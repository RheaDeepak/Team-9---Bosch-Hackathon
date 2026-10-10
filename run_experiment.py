"""Day-1 flow: propose -> validate -> print -> ask for approval. Nothing is injected."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from planner import propose_experiment
from policy import validate

APPROVED_JSON = Path("approved_experiment.json")


def print_summary(plan, source, note, result):
    line = "=" * 64
    print(f"{line}\nPROPOSED EXPERIMENT\n{line}")
    for k in ("target", "fault", "magnitude", "duration_s"):
        print(f"{k:>12}: {plan.get(k)}")
    print(f"{'hypothesis':>12}: {plan.get('hypothesis')}\n{'reasoning':>12}: {plan.get('reasoning')}")
    print(f"\nSource: {'LLM' if source == 'llm' else 'FALLBACK PLAN (LLM not used)'} - {note}")
    print(f"\nValidation: {'PASSED' if result['valid'] else 'FAILED'}")
    for r in result["reasons"]:
        print(f"  - {r}")
    br = result["blast_radius"]
    print(f"Blast radius ({len(br)} services depend on {plan.get('target')}): {', '.join(br) or 'none'}\n{line}")


def main():
    plan, source, note = propose_experiment()
    result = validate(plan)
    print_summary(plan, source, note, result)
    if not result["valid"]:
        print("Plan rejected by policy; nothing to approve.")
        return 1
    try:
        answer = input("Approve this experiment? (y/n) ").strip().lower()
    except EOFError:
        answer = ""
    if answer != "y":
        print("Rejected")
        return 0
    APPROVED_JSON.write_text(json.dumps({
        "plan": plan, "source": source, "blast_radius": result["blast_radius"],
        "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}, indent=2), encoding="utf-8")
    print(f"Approved - injection not implemented yet (saved to {APPROVED_JSON})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
