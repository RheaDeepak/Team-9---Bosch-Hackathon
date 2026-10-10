"""Experiment flow: propose -> validate -> approve -> inject -> guard -> rollback -> recover -> report.

Terminal mode (python run_experiment.py) asks y/n; the web UI (app.py) calls propose() and execute() directly.
"""
import json
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import grafana
import guard
import inject
from metrics import load_baseline
from planner import judge_hypothesis, propose_experiment
from policy import validate

APPROVED_JSON = Path("approved_experiment.json")
RESULTS_DIR = Path("results")
ROLLBACK_TRIES = 3
# Impact score weights (sum to 1), after the PRD formula: error rise, latency rise, lost availability, slow recovery.
SCORE_WEIGHTS = {"error_impact": 0.35, "latency_impact": 0.25, "availability_loss": 0.20, "recovery_impact": 0.20}
SEVERITY_BANDS = ((20, "Low"), (40, "Moderate"), (70, "High"), (101, "Critical"))   # upper bound of impact -> label
AMPLIFIED_ABOVE = 1.5     # a p95 rise above this many times the injected delay is flagged as amplification
# Kill-switch proof: deliberately tiny thresholds, so the guard aborts within seconds of injection.
FORCE_ABORT_TEST = False
FORCE_THRESHOLDS = {"error_rate": 0.0, "p95_ms": 0.001}
SOURCES = {"rank_agent": "RANK AGENT (LLM investigated the ranker's top candidates)",
           "ranker_fallback": "RANKER #1 (LLM investigation not used)",
           "fallback": "FALLBACK PLAN (ranking unavailable)"}


def print_ranking(ranking):
    """Ranked top-K table, then the chosen candidate's evidence and predictions."""
    rows = ranking.get("top_k") or []
    print(f"\nTop-{len(rows)} candidates (ranker score -> agent-adjusted):")
    print(f"  {'':2}{'candidate':<34}{'risk':>6}{'info':>6}{'prio':>6}{'adj risk':>10}{'adj prio':>10}")
    adj = lambda x: "-" if x is None else f"{x:.2f}"
    for r in rows:
        mark = "->" if r["candidate_id"] == ranking.get("chosen") else ""
        print(f"  {mark:<2}{r['candidate_id']:<34}{r['risk']:>6.2f}{r['info']:>6.2f}{r['priority']:>6.2f}"
              f"{adj(r['adjusted_risk']):>10}{adj(r['adjusted_priority']):>10}")
        print(f"  {'':2}  why: {r['why']}")
    chosen = next((r for r in rows if r["candidate_id"] == ranking.get("chosen")), None)
    if chosen and chosen["evidence"]:
        print(f"\nEvidence for {chosen['candidate_id']}:")
        for e in chosen["evidence"]:
            print(f"  - [{e['tool']}] {e['finding']}")
    if ranking.get("predictions"):
        print("Predictions: " + "; ".join(f"{p['svc']} {p['metric']} {p['direction']} (x{p['min_factor']})"
                                          for p in ranking["predictions"]))


def print_summary(plan, source, note, result):
    line = "=" * 64
    print(f"{line}\nPROPOSED EXPERIMENT\n{line}")
    for k in ("target", "fault", "magnitude", "duration_s"):
        print(f"{k:>12}: {plan.get(k)}")
    print(f"{'hypothesis':>12}: {plan.get('hypothesis')}\n{'reasoning':>12}: {plan.get('reasoning')}")
    if plan.get("ranking"):
        print_ranking(plan["ranking"])
    print(f"\nSource: {SOURCES.get(source, source)} - {note}")
    print(f"\nValidation: {'PASSED' if result['valid'] else 'FAILED'}")
    for r in result["reasons"]:
        print(f"  - {r}")
    br = result["blast_radius"]
    print(f"Blast radius ({len(br)} services depend on {plan.get('target')}): {', '.join(br) or 'none'}\n{line}")


def _rollback(emit=print):
    """Roll back with retries, immune to Ctrl+C. Never skipped, never silent about failure.
    Auto-abort, STOP and Ctrl+C all end up here."""
    in_main = threading.current_thread() is threading.main_thread()  # signal() only works there
    old = signal.signal(signal.SIGINT, signal.SIG_IGN) if in_main else None
    try:
        for attempt in range(1, ROLLBACK_TRIES + 1):
            emit(f"Rolling back (attempt {attempt}/{ROLLBACK_TRIES})...")
            if inject.rollback():
                emit("Rollback verified.")
                return True
            time.sleep(3)
        emit(f"!!! ROLLBACK FAILED. Fix manually using {inject.ACTIVE_FILE} "
             f"(container_stop: `docker start <container>`; network_delay: remove the netem qdisc).")
        return False
    finally:
        if in_main:
            signal.signal(signal.SIGINT, old)


def _recover_crashed_run(emit=print):
    """If a previous run died mid-experiment, undo its fault before doing anything else."""
    if not inject.ACTIVE_FILE.exists():
        return True
    try:
        active = json.loads(inject.ACTIVE_FILE.read_text(encoding="utf-8"))
        emit(f"Found {inject.ACTIVE_FILE} from an unfinished run ({active['fault']} on {active['target']}, "
             f"started {active['started_at']}). Rolling it back before anything else.")
    except (OSError, ValueError, KeyError) as e:
        emit(f"{inject.ACTIVE_FILE} exists but is unreadable ({e}); fix or delete it by hand.")
        return False
    return _rollback(emit)


def _fmt(x, unit=""):
    return "-" if x is None else f"{x:.1f}{unit}"


def print_results(plan, g, rolled_back, recovery, impact, containers, amplified, verdict, score=None):
    line = "=" * 64
    print(f"\n{line}\nEXPERIMENT RESULT\n{line}")
    print(f"Experiment: {plan['fault']} on {plan['target']}, magnitude {plan['magnitude']}, planned {plan['duration_s']}s")
    print(f"Outcome:    {'ABORTED' if g['aborted'] else 'COMPLETED'} after {g['elapsed_s']}s - {g['reason']}")
    print(f"Rollback:   {'verified' if rolled_back else 'FAILED - see message above'}")
    if score:
        avail = f", availability during the fault {score['availability_pct']}%" if score["availability_pct"] is not None else ""
        print(f"Resilience: {score['resilience_score']}/100 ({score['severity']} impact, {score['confidence']}{avail})")
    print("\nImpact vs baseline (baseline -> peak, change):")
    print(f"  {'service':<16}{'error rate %':<28}{'p95 latency ms'}")
    for r in impact:
        e, p = r["error_rate"], r["p95_ms"]
        print(f"  {r['service']:<16}{_fmt(e['baseline']):>6} -> {_fmt(e['peak']):>6} ({_fmt(e['change']):>6})      "
              f"{_fmt(p['baseline']):>7} -> {_fmt(p['peak']):>7} ({_fmt(p['change']):>7})")
    if amplified:
        print("\nAmplification (observed p95 increase / injected delay):")
        for a in amplified:
            print(f"  {a['service']:<16}{a['role']:<7} +{a['increase_ms']:>7.1f}ms  ~{a['ratio']:.1f}x"
                  f"{'  AMPLIFIED (> ' + str(AMPLIFIED_ABOVE) + 'x)' if a['amplified'] else ''}")
    if containers:
        print("\nContainer resources, baseline -> peak (reference only, not abort conditions):")
        for c in containers:
            cpu, mem = c["cpu_percent"], c["memory_mb"]
            print(f"  {c['container']:<16}cpu {_fmt(cpu['baseline'], '%'):>7} -> {_fmt(cpu['peak'], '%'):>7}    "
                  f"mem {_fmt(mem['baseline'], 'MB'):>8} -> {_fmt(mem['peak'], 'MB'):>8}")
    if recovery["recovered"]:
        secondary = (f"span metrics confirmed after {recovery['metrics_seconds']}s" if recovery.get("metrics_confirmed")
                     else "span metrics not yet back within thresholds (secondary check)")
        print(f"\nRecovery:   {recovery['seconds']}s after rollback by probes (primary); {secondary}")
    else:
        why = recovery.get("note") or f"probes not healthy within {guard.RECOVERY_TIMEOUT_S}s"
        print(f"\nRecovery:   did not recover ({why})")
    print(f"\nHypothesis: {plan['hypothesis']}\nVerdict:    {verdict['verdict'].upper()} - {verdict['explanation']}")
    for c in verdict.get("claims", []):
        print(f"  [{c['status']}] {c['claim']} - {c['evidence']}")
    if verdict.get("recommendation"):
        print(f"\nRecommended next move: {verdict['recommendation']}")
    print(line)


def _rounded(x):
    """Round floats for the LLM payload (error rates are in %, latencies in ms)."""
    if isinstance(x, float):
        return round(x, 2)
    if isinstance(x, dict):
        return {k: _rounded(v) for k, v in x.items()}
    return [_rounded(v) for v in x] if isinstance(x, list) else x


def amplification(plan, impact):
    """Observed p95 increase / injected delay for every service with data (network_delay only).
    A ratio above AMPLIFIED_ABOVE means the delay is paid more than once per request."""
    if plan.get("fault") != "network_delay" or not plan.get("magnitude"):
        return []
    out = []
    for r in impact:
        rise = r["p95_ms"]["change"]
        if rise is None:
            continue
        ratio = max(rise, 0) / plan["magnitude"]
        out.append({"service": r["service"], "role": "target" if r["service"] == plan["target"] else "caller",
                    "increase_ms": round(rise, 1), "delay_ms": plan["magnitude"], "ratio": round(ratio, 1),
                    "amplified": ratio > AMPLIFIED_ABOVE})
    return out


def _annotate(emit, text, tags, when):
    """Mark the Grafana charts. A failure is logged and ignored: it must never affect the experiment."""
    ok, detail = grafana.annotate(text, tags, when)
    emit(f"Grafana annotation {'created' if ok else 'FAILED (continuing)'}: {text} ({detail})")


def score_experiment(impact, recovery, availability, elapsed_s=None):
    """Deterministic score from measured evidence (no LLM): impact 0-100 (lower is better), resilience = 100 - impact.
    error_impact: worst error-rate rise in points / 100; latency_impact: worst p95 ratio, 5x baseline = full;
    availability_loss: share of failed probe requests; recovery_impact: time to recover / recovery timeout."""
    clamp = lambda v: max(0.0, min(1.0, v))
    rises = [r["error_rate"]["change"] for r in impact if r["error_rate"]["change"] is not None]
    ratios = [r["p95_ms"]["peak"] / r["p95_ms"]["baseline"] - 1 for r in impact
              if r["p95_ms"]["peak"] is not None and r["p95_ms"]["baseline"]]
    recovery_s = recovery["seconds"] if recovery.get("recovered") else guard.RECOVERY_TIMEOUT_S
    components = {"error_impact": clamp(max(rises, default=0) / 100),
                  "latency_impact": clamp(max(ratios, default=0) / 4),
                  "availability_loss": clamp((100 - availability) / 100) if availability is not None else 0.0,
                  "recovery_impact": clamp(recovery_s / guard.RECOVERY_TIMEOUT_S)}
    total = round(100 * sum(SCORE_WEIGHTS[k] * v for k, v in components.items()), 1)
    no_data = [r["service"] for r in impact if r["error_rate"]["peak"] is None and r["p95_ms"]["peak"] is None]
    limits = ([f"No data for: {', '.join(no_data)}"] if no_data else []) + (
        [f"Stopped after {elapsed_s:.0f}s, so the evidence covers only the start of the fault"] if elapsed_s is not None and elapsed_s < 15 else [])
    confidence = ("Low" if len(no_data) * 2 >= max(len(impact), 1) else "Medium" if limits else "High") + " confidence"
    return {"impact_score": total, "resilience_score": round(100 - total),
            "severity": next(label for bound, label in SEVERITY_BANDS if total < bound), "confidence": confidence,
            "availability_pct": None if availability is None else round(availability, 1),
            "components": {k: round(v, 3) for k, v in components.items()}, "limitations": limits}


def propose(goal=None):
    """Planner + policy: {plan, source, note, validation}. `goal` optionally steers the planner."""
    plan, source, note = propose_experiment(goal)
    return {"plan": plan, "source": source, "note": note, "validation": validate(plan)}


def execute(plan, source, validation, emit=print, on_phase=None, on_sample=None, on_watch=None, stop=None):
    """Inject, guard, always roll back, wait for recovery, judge, save. Returns the result dict
    ({"refused": reason} if nothing was injected).
    Hooks for the UI: on_phase(name), on_watch({service: {baseline, abort}}), on_sample(...), stop (Event)."""
    phase = on_phase or (lambda name: None)

    def report_sample(sample_phase):
        """Adapt the guard's telemetry callback to the legacy dashboard callback."""
        def callback(metrics, containers):
            if on_sample:
                on_sample(sample_phase, time.monotonic(), metrics, containers, {})
        return callback

    baseline = load_baseline()
    if baseline is None:
        emit("No baseline.json (run metrics.py first). Not injecting.")
        return {"refused": "no baseline.json"}
    services = guard.watched_services(plan, baseline, validation["blast_radius"])
    if not services:
        emit("None of the affected services have baseline thresholds; cannot guard. Not injecting.")
        return {"refused": "no baseline thresholds for the affected services"}
    thresholds = {s: FORCE_THRESHOLDS for s in services} if FORCE_ABORT_TEST else None
    try:  # never inject if the guard cannot see
        guard.live_metrics(services)
    except Exception as e:
        emit(f"Telemetry (Jaeger) is not reachable, so the guard would be blind: {e}. Not injecting.")
        return {"refused": "telemetry unreachable, the guard would be blind"}
    if baseline["flags_enabled"]:
        emit(f"Note: baseline was captured with flags on ({', '.join(baseline['flags_enabled'])}).")
    if FORCE_ABORT_TEST:
        emit("*** FORCE_ABORT_TEST: tiny thresholds, expect an automatic abort ***")
    emit(f"Watching: {', '.join(services)}")
    thr = thresholds or baseline["thresholds"]["services"]
    if on_watch:
        on_watch({s: {"baseline": {m: (baseline["services"][s][m] or {}).get("mean") for m in guard.METRICS},
                      "abort": {m: thr[s].get(m) for m in guard.METRICS}} for s in services})

    started = datetime.now(timezone.utc)
    g = {"aborted": True, "reason": "not started", "elapsed_s": 0, "timeline": [],
         "peaks": {s: {m: None for m in guard.METRICS} for s in services}}
    injected_at = None
    try:
        phase("injecting")
        active = inject.inject(plan)
        emit(f"FAULT ACTIVE: {plan['fault']} on {active['container_name']} (rollback: {active['rollback']})")
        injected_at = time.time()
        unit = f" ({plan['magnitude']} ms)" if plan["fault"] == "network_delay" else ""
        _annotate(emit, f"FAULT: {plan['fault']} on {plan['target']}{unit}", [plan["target"], plan["fault"]], injected_at)
        phase("monitoring")
        g = guard.watch(plan, baseline, services, thresholds=thresholds,
                        stop_event=stop, on_sample=report_sample("monitoring"))
    except KeyboardInterrupt:
        g["reason"] = "interrupted by user (Ctrl+C) during injection"
    except Exception as e:
        g["reason"] = f"error: {type(e).__name__}: {e}"
    finally:
        phase("rolling_back")
        rolled_back = _rollback(emit)
        if injected_at:
            _annotate(emit, f"rollback: {plan['target']} {'restored' if rolled_back else 'ROLLBACK FAILED'}",
                      [plan["target"], "rollback"], time.time())
    emit(f"{'ABORTED' if g['aborted'] else 'Fault window finished'}: {g['reason']}")

    recovery = {"recovered": False, "seconds": None, "metrics_confirmed": False, "metrics_seconds": None,
                "samples": [], "note": "rollback failed"}
    if rolled_back:
        phase("recovering")
        if stop is not None:
            stop.clear()  # STOP already did its job; pressing it again skips the recovery wait
        emit(f"Waiting up to {guard.RECOVERY_TIMEOUT_S}s for recovery...")
        recovery = guard.wait_for_recovery(plan, baseline, services,
                                           on_sample=report_sample("recovering"))
        recovery.setdefault("metrics_confirmed", None)
        recovery.setdefault("metrics_seconds", None)
        recovery.setdefault("samples", [])
    ended_at = time.time()
    phase("analyzing")
    impact = guard.impact_table(baseline, g["peaks"])
    containers = guard.container_table(baseline, g.get("container_peaks", {}))
    amplified = amplification(plan, impact)
    availability = guard.probe_availability(g.get("timeline", []))
    score = score_experiment(impact, recovery, availability, g["elapsed_s"] if g["timeline"] else None)
    measured = {"aborted": g["aborted"], "reason": g["reason"], "elapsed_s": g["elapsed_s"],
                "impact_baseline_to_peak": _rounded(impact), "amplification": amplified,
                "probe_availability_pct": score["availability_pct"],
                "recovery": {k: v for k, v in recovery.items() if k != "samples"},
                "forced_abort_test": FORCE_ABORT_TEST, "baseline_flags_enabled": baseline["flags_enabled"]}
    verdict = judge_hypothesis(plan, measured)

    RESULTS_DIR.mkdir(exist_ok=True)
    result_id = started.strftime("%Y%m%dT%H%M%SZ")
    result = {
        "id": result_id,
        "plan": {k: v for k, v in plan.items() if k != "ranking"}, "plan_source": source,
        "ranking": plan.get("ranking"), "validation": validation,
        "started_at": started.isoformat(timespec="seconds"),
        "baseline": {"captured_at": baseline["captured_at"], "flags_enabled": baseline["flags_enabled"]},
        "forced_abort_test": FORCE_ABORT_TEST, "guard": g, "rollback_verified": rolled_back,
        "recovery": recovery, "impact": impact, "amplification": amplified, "score": score,
        "container_resources": containers, "verdict": verdict,
        "grafana_window_ms": grafana.window_ms(injected_at, ended_at) if injected_at else None,
    }
    (RESULTS_DIR / f"{result_id}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    emit(f"Saved {RESULTS_DIR / (result_id + '.json')}")
    phase("done")
    return result


def run(plan, source, validation):
    """Terminal mode: execute and print the summary."""
    result = execute(plan, source, validation)
    if "refused" in result:
        return 1
    print_results(plan, result["guard"], result["rollback_verified"], result["recovery"], result["impact"],
                  result["container_resources"], result["amplification"], result["verdict"], result["score"])
    return 0 if result["rollback_verified"] else 2


def main():
    if not _recover_crashed_run():
        return 2
    proposal = propose()
    plan, source, validation = proposal["plan"], proposal["source"], proposal["validation"]
    print_summary(plan, source, proposal["note"], validation)
    if not validation["valid"]:
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
        "plan": plan, "source": source, "blast_radius": validation["blast_radius"],
        "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}, indent=2), encoding="utf-8")
    print(f"Approved - starting experiment (plan saved to {APPROVED_JSON})")
    return run(plan, source, validation)


if __name__ == "__main__":
    sys.exit(main())
