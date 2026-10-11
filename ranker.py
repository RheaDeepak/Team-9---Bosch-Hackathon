"""Experiment ranker: score every allowed (target, fault, magnitude) by risk and informativeness.

Deterministic and transparent: every feature keeps its raw value next to a 0..1 score, and every
candidate gets a one-line "why". No LLM here; rank_agent.py investigates the top few.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from discovery import compute_stats, load_flows, load_graph
from guard import RECOVERY_TIMEOUT_S
from metrics import LATENCY_FLOOR_MS, load_baseline
from policy import ALLOWED_FAULTS, MAX_NETWORK_DELAY_MS, PROTECTED, blast_radius, validate
from traffic_spike import FAULT_TYPE as TRAFFIC_SPIKE, MAX_PEAK_USERS, candidate_plan as traffic_spike_candidate_plan, capability as traffic_spike_capability

RESULTS_DIR = Path("results")
RANKINGS_DIR = Path("rankings")
DEFAULT_DURATION_S = 60
MAGNITUDES = {"container_stop": (0,), "network_delay": (200, 500, 1000)}

# Every tunable number in one place. Within "risk" and "info" the weights sum to 1, so both scores are 0..1.
WEIGHTS = {
    # priority = risk * risk_score + info * info_score - safety * (blast share x severity) - prior_abort * (aborted before)
    #            - outage * (total outage / entry point)
    "priority": {"risk": 0.55, "info": 0.45, "safety": 0.20, "prior_abort": 0.15, "outage": 0.25},
    # risk: likely user impact if the service is weak
    "risk": {"fan_in": 0.15, "blast_radius": 0.20, "checkout_path": 0.20, "journeys": 0.15, "traffic": 0.20,
             "recovery": 0.10},
    # informativeness: how much we would learn
    "info": {"novelty": 0.40, "escalation": 0.15, "clarity": 0.15, "uncertainty": 0.20, "partial": 0.10},
    # risk multipliers: an async-only (or uncalled) dependency keeps this share of its risk ...
    "async_floor": 0.3,
    # ... and the mildest fault keeps this share (container_stop / max delay = full severity)
    "severity_floor": 0.5,
}
# How sure we are of the outcome given the last verdict on the same service x fault (high = worth testing).
UNCERTAINTY = {None: 0.6, "inconclusive": 0.8, "not held": 0.5, "held": 0.2}
NOVELTY = {"untested": 1.0, "other_fault": 0.7, "other_magnitude": 0.4, "exact_repeat": 0.0}
NOISE_CV = 0.5        # p95 std / max(p95, LATENCY_FLOOR_MS) at which a service counts as fully noisy
NOISE_ERR_PP = 1.0    # error-rate std (percentage points) at which a service counts as fully noisy
UNKNOWN_NOISE = 0.5   # no baseline numbers (e.g. kafka, databases): neither clear nor noisy
OUTAGE_JOURNEY_SHARE = 0.5  # stopping a service on more than this share of the user journeys starts to look like an outage
PENALTY_NOTE = 0.05     # penalties at least this large are named in the why line
# After the guard aborted a (service, fault), its lowest magnitude keeps only this share of the prior-abort penalty and
# every higher magnitude keeps all of it: the mildest step runs longest, so the next run collects enough samples.
# The guard's thresholds and abort rules are not touched.
LOWEST_AFTER_ABORT_SHARE = 0.3
GUARD_ABORT = "threshold breached"  # guard reasons that mean the system really degraded


def _mean(m, key):
    return ((m or {}).get(key) or {}).get("mean")


def _mode(edge):
    return edge.get("mode") or ("async" if edge.get("type") == "async-messaging" else "sync")


def load_history(results_dir=RESULTS_DIR):
    """Past experiments from results/*.json, oldest first. Forced kill-switch tests are skipped:
    they ran with fake thresholds and say nothing about the system."""
    runs = []
    for p in sorted(Path(results_dir).glob("*.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            plan = r["plan"]
        except (OSError, ValueError, KeyError):
            continue
        if r.get("forced_abort_test"):
            continue
        g, rec = r.get("guard") or {}, r.get("recovery") or {}
        runs.append({"file": p.name, "target": plan["target"], "fault": plan["fault"], "magnitude": plan["magnitude"],
                     "verdict": (r.get("verdict") or {}).get("verdict"),
                     "guard_aborted": bool(g.get("aborted")) and str(g.get("reason", "")).startswith(GUARD_ABORT),
                     "reason": str(g.get("reason", ""))[:120], "recovery_s": rec.get("seconds")})
    return runs


def candidate_targets(g, baseline):
    """Graph services that are not protected and had a running container in the baseline."""
    containers = (baseline or {}).get("containers", {})
    return [t for t in sorted(g) if t not in PROTECTED and (_mean(containers.get(t), "running") or 0) > 0]


def generate_candidates(g, baseline, duration_s=DEFAULT_DURATION_S, traffic_capability=None):
    """Every (target, fault, magnitude) on the ladder that passes policy.validate()."""
    out = []
    for t in candidate_targets(g, baseline):
        for fault in ALLOWED_FAULTS:
            for m in MAGNITUDES.get(fault, ()):
                plan = {"target": t, "fault": fault, "magnitude": m, "duration_s": duration_s,
                        "hypothesis": "(ranker candidate)", "reasoning": "(ranker candidate)"}
                if validate(plan, g)["valid"]:
                    out.append(plan)
    traffic = traffic_spike_candidate_plan(traffic_capability or {})
    if traffic and traffic["target"] in g and validate(traffic, g)["valid"]:
        out.append(traffic)
    return out


def _target_features(g, stats, baseline, flows, history, t):
    """Raw per-service features (shared by all faults on that service)."""
    services = baseline.get("services", {})
    callers = [(c, _mode(g[c][t])) for c in g.predecessors(t)]
    sync = sum(m == "sync" for _, m in callers)
    rate, via = _mean(services.get(t), "request_rate"), None
    if rate is None:  # not instrumented (kafka, databases): estimate from what its callers handle
        rate = sum(_mean(services.get(c), "request_rate") or 0 for c, _ in callers)
        via = "callers"
    m = services.get(t) or {}
    p95, p95_std = _mean(m, "p95_ms"), (m.get("p95_ms") or {}).get("std")
    err, err_std = _mean(m, "error_rate"), (m.get("error_rate") or {}).get("std")
    if p95 is None and err is None:
        noise = UNKNOWN_NOISE
    else:
        cv = (p95_std or 0) / max(p95 or 0, LATENCY_FLOOR_MS)  # +-2ms on a 3ms service is not noise
        noise = min(1.0, max(cv / NOISE_CV, (err_std or 0) / NOISE_ERR_PP))
    entry = not callers or all(c in PROTECTED for c, _ in callers)  # only the edge proxy (or nobody) calls it: users hit it directly
    journeys = sum(any(t in (s["from"], s["to"]) for s in f["steps"]) for f in flows.values())
    past = [r for r in history if r["target"] == t]
    recoveries = [r["recovery_s"] for r in past if r["recovery_s"] is not None]
    return {"fan_in": stats["fan_in"].get(t, g.in_degree(t)), "sync_callers": sync, "async_callers": len(callers) - sync,
            "blast_radius": len(blast_radius(g, t)), "checkout_path": t in stats["checkout_path"],
            "journeys": journeys, "journey_share": journeys / max(len(flows), 1), "entry_point": entry,
            "traffic": rate, "traffic_via": via, "baseline_p95": p95, "baseline_error_rate": err, "noise": noise,
            "recovery": max(recoveries) if recoveries else None}


def _history_features(history, t, fault, magnitude):
    same = [r for r in history if r["target"] == t and r["fault"] == fault]
    exact = [r for r in same if r["magnitude"] == magnitude]
    if exact:
        novelty = "exact_repeat"
    elif same:
        novelty = "other_magnitude"
    elif any(r["target"] == t for r in history):
        novelty = "other_fault"
    else:
        novelty = "untested"
    smaller = [r for r in same if r["magnitude"] < magnitude]
    # A smaller step held cleanly -> try bigger. A smaller (or this) step tripped the guard -> don't push.
    aborted_at_or_below = [r for r in same if r["magnitude"] <= magnitude and r["guard_aborted"]]
    aborted_any = [r for r in same if r["guard_aborted"]]
    held_below = [r for r in smaller if r["verdict"] == "held" and not r["guard_aborted"]]
    return {"tested": [f"{r['fault']}@{r['magnitude']} {r['verdict']}" for r in same],
            "novelty": novelty, "repeats": len(exact),
            "escalation": max((r["magnitude"] for r in held_below), default=None) if not aborted_at_or_below else None,
            "last_verdict": same[-1]["verdict"] if same else None,
            "prior_abort": max((r["magnitude"] for r in aborted_at_or_below), default=None),
            "aborted_at": max((r["magnitude"] for r in aborted_any), default=None)}


def _severity(fault, magnitude):
    if fault == "container_stop":
        return 1.0
    cap = MAX_PEAK_USERS if fault == TRAFFIC_SPIKE else MAX_NETWORK_DELAY_MS
    return min(1.0, magnitude / cap)


def _outage(f, fault, sev, sync_share):
    """0..1: how close this fault is to taking the whole app down. A stopped entry point (frontend) is a total outage;
    a stopped service that most journeys call synchronously is almost one; a delay at the front door is half of one."""
    if fault == "container_stop":
        spread = max(0.0, (f["journey_share"] - OUTAGE_JOURNEY_SHARE) / (1 - OUTAGE_JOURNEY_SHARE)) * (0.5 + 0.5 * sync_share)
        return 1.0 if f["entry_point"] else min(1.0, spread)
    if fault == TRAFFIC_SPIKE:
        return 0.25 * sev if f["entry_point"] else 0.0
    return 0.5 * sev if f["entry_point"] else 0.0


def _partial(fault, sev, sync_share, outage):
    """0..1: how likely the outcome is partial degradation (slow, some errors, maybe graceful) rather than all-or-nothing.
    Those experiments teach the most about fallbacks, so they are rewarded; an outage leaves nothing to learn."""
    if fault == TRAFFIC_SPIKE:
        shape = 0.9
    else:
        shape = 0.6 + 0.4 * (1 - sev) if fault == "network_delay" else 0.4 + 0.6 * (1 - sync_share)
    return (1 - outage) * shape


def _labels(f, c):
    """Human text for each feature, used in the "why" line."""
    calls = ("async only" if f["fan_in"] and not f["sync_callers"]
             else f"{f['sync_callers']} sync, {f['async_callers']} async")
    tested = {"untested": "service never tested", "other_fault": f"untested for {c['fault']}",
              "other_magnitude": f"{c['fault']} tried at other magnitudes",
              "exact_repeat": f"exact repeat (x{c['history']['repeats']})"}
    traffic = "-" if f["traffic"] is None else f"{f['traffic']:.2f} req/s" + (" via callers" if f["traffic_via"] else "")
    h = c["history"]
    return {
        "fan_in": f"fan-in {f['fan_in']} ({calls})", "blast_radius": f"blast radius {f['blast_radius']}",
        "checkout_path": "on checkout path", "journeys": f"in {f['journeys']} user journeys",
        "traffic": f"traffic {traffic}", "recovery": f"past recovery {f['recovery']}s",
        "novelty": tested[h["novelty"]],
        "escalation": f"escalate: {h['escalation']}{' users' if c['fault'] == TRAFFIC_SPIKE else 'ms'} held",
        "clarity": "no baseline metrics" if f["baseline_p95"] is None else f"clean baseline (noise {f['noise']:.2f})",
        "uncertainty": "no prior verdict" if h["last_verdict"] is None else f"last verdict {h['last_verdict']}",
        "safety": f"blast {f['blast_radius']} at severity {c['severity']:.1f}",
        "outage": ("entry point: users hit it directly" if f["entry_point"]
                   else f"stops {f['journeys']} user journeys (near-total outage)"),
        "partial": "likely partial degradation",
        "prior_abort": (f"guard aborted at {h['aborted_at']}{' users' if c['fault'] == TRAFFIC_SPIKE else 'ms'}"
                        + ("; lowest magnitude preferred so the run lasts" if c["magnitude"] == min(MAGNITUDES.get(c["fault"], (c["magnitude"],))) else "")
                        if c["fault"] in {"network_delay", TRAFFIC_SPIKE} and h["aborted_at"] is not None
                        else "guard aborted before"),
    }


def rank(g, baseline, flows, history, stats=None, weights=WEIGHTS, duration_s=DEFAULT_DURATION_S, traffic_capability=None):
    """Score and sort all candidates (best first). Returns a list of candidate dicts."""
    if not baseline:
        raise ValueError("no baseline: run metrics.py first")
    stats = stats or compute_stats(g)
    wp, wr, wi = weights["priority"], weights["risk"], weights["info"]
    plans = generate_candidates(g, baseline, duration_s, traffic_capability)
    per_target = {t: _target_features(g, stats, baseline, flows, history, t) for t in {p["target"] for p in plans}}
    peak = lambda k: max([f[k] or 0 for f in per_target.values()] + [1e-9])
    norm = {k: peak(k) for k in ("fan_in", "blast_radius", "journeys", "traffic", "baseline_p95")}

    out = []
    for p in plans:
        f, h = per_target[p["target"]], _history_features(history, p["target"], p["fault"], p["magnitude"])
        sev = _severity(p["fault"], p["magnitude"])
        callers = f["sync_callers"] + f["async_callers"]
        sync_share = f["sync_callers"] / callers if callers else 0.0
        coupling = weights["async_floor"] + (1 - weights["async_floor"]) * sync_share
        sev_factor = weights["severity_floor"] + (1 - weights["severity_floor"]) * sev
        outage = _outage(f, p["fault"], sev, sync_share)
        score = {
            "fan_in": f["fan_in"] / norm["fan_in"], "blast_radius": f["blast_radius"] / norm["blast_radius"],
            "checkout_path": float(f["checkout_path"]), "journeys": f["journeys"] / norm["journeys"],
            "traffic": (f["traffic"] or 0) / norm["traffic"],
            "recovery": min(1.0, (f["recovery"] or 0) / RECOVERY_TIMEOUT_S),
            "partial": _partial(p["fault"], sev, sync_share, outage),
            "novelty": NOVELTY[h["novelty"]], "escalation": 1.0 if h["escalation"] is not None else 0.0,
            "clarity": 1 - f["noise"], "uncertainty": UNCERTAINTY.get(h["last_verdict"], 0.6),
        }
        risk = sum(wr[k] * score[k] for k in wr) * coupling * sev_factor
        info = sum(wi[k] * score[k] for k in wi)
        safety = score["blast_radius"] * sev
        ladder = MAGNITUDES.get(p["fault"], ())
        if h["aborted_at"] is None or len(ladder) < 2:
            prior_abort = 1.0 if h["prior_abort"] is not None else 0.0
        elif p["magnitude"] == min(ladder):   # the guard aborted this pair before: steer to its lowest magnitude ...
            prior_abort = LOWEST_AFTER_ABORT_SHARE if h["prior_abort"] is not None else 0.0
        else:                                 # ... and away from every higher one
            prior_abort = 1.0
        priority = (wp["risk"] * risk + wp["info"] * info - wp["safety"] * safety - wp["prior_abort"] * prior_abort
                    - wp.get("outage", 0.0) * outage)

        # Each feature's share of the priority, for the "why" line.
        contrib = {k: wp["risk"] * wr[k] * score[k] * coupling * sev_factor for k in wr}
        contrib |= {k: wp["info"] * wi[k] * score[k] for k in wi}
        contrib |= {"safety": -wp["safety"] * safety, "prior_abort": -wp["prior_abort"] * prior_abort,
                    "outage": -wp.get("outage", 0.0) * outage}
        cand = {"id": f"{p['target']}:{p['fault']}:{p['magnitude']}",
                 **{k: p[k] for k in ("target", "fault", "magnitude", "duration_s")},
                 "severity": round(sev, 2), "outage": round(outage, 3), "history": h}
        cand.update({k: p[k] for k in ("parameters", "candidate_id", "capability_version") if k in p})
        labels = _labels(f, cand)
        top = sorted((k for k in contrib if contrib[k] > 1e-6), key=lambda k: -contrib[k])[:3]
        penalties = [labels[k] for k in ("safety", "prior_abort", "outage") if -contrib[k] >= PENALTY_NOTE]
        cand.update(
            risk=round(risk, 3), info=round(info, 3), priority=round(priority, 3),
            why="; ".join(labels[k] for k in top) + (f" | penalty: {', '.join(penalties)}" if penalties else "")
                + (f" | {labels['novelty']}" if h["novelty"] == "exact_repeat" else ""),
            factors=sorted(
                ({"name": k, "kind": "risk" if k in wr else "info" if k in wi else "penalty",
                  "score": round(score.get(k, {"safety": safety, "prior_abort": prior_abort, "outage": outage}.get(k, 0)), 3),
                  "contribution": round(contrib[k], 3), "label": labels[k]} for k in contrib),
                key=lambda x: -abs(x["contribution"])),
            features={
                "fan_in": {"raw": f["fan_in"], "score": round(score["fan_in"], 3)},
                "sync_callers": {"raw": f["sync_callers"], "score": round(sync_share, 3)},
                "async_callers": {"raw": f["async_callers"], "score": round(1 - sync_share if callers else 0, 3)},
                "sync_coupling": {"raw": round(sync_share, 3), "score": round(coupling, 3)},
                "blast_radius": {"raw": f["blast_radius"], "score": round(score["blast_radius"], 3)},
                "checkout_path": {"raw": f["checkout_path"], "score": score["checkout_path"]},
                "journeys": {"raw": f["journeys"], "score": round(score["journeys"], 3)},
                "traffic": {"raw": None if f["traffic"] is None else round(f["traffic"], 3),
                            "score": round(score["traffic"], 3), "via": f["traffic_via"]},
                "baseline_p95": {"raw": None if f["baseline_p95"] is None else round(f["baseline_p95"], 2),
                                 "score": round((f["baseline_p95"] or 0) / norm["baseline_p95"], 3)},
                "baseline_error_rate": {"raw": None if f["baseline_error_rate"] is None else round(f["baseline_error_rate"], 2),
                                        "score": round(min(1.0, (f["baseline_error_rate"] or 0) / 5), 3)},
                "noise": {"raw": round(f["noise"], 3), "score": round(f["noise"], 3)},
                "recovery": {"raw": f["recovery"], "score": round(score["recovery"], 3)},
                "tested": {"raw": h["tested"], "score": float(bool(h["tested"]))},
                "novelty": {"raw": h["novelty"], "score": score["novelty"]},
                "escalation": {"raw": h["escalation"], "score": score["escalation"]},
                "uncertainty": {"raw": h["last_verdict"], "score": score["uncertainty"]},
                "prior_abort": {"raw": h["prior_abort"], "score": prior_abort},
                "severity": {"raw": p["magnitude"], "score": round(sev, 3)},
                "entry_point": {"raw": f["entry_point"], "score": float(f["entry_point"])},
                "outage": {"raw": round(outage, 3), "score": round(outage, 3)},
                "partial": {"raw": round(score["partial"], 3), "score": round(score["partial"], 3)},
            })
        out.append(cand)
    out.sort(key=lambda c: (-c["priority"], c["id"]))
    for i, c in enumerate(out, 1):
        c["rank"] = i
    return out


def rank_from_files(weights=WEIGHTS):
    """Rank using dependency_map.json, baseline.json and results/*.json. Returns (ranking, graph)."""
    g, stats = load_graph()
    return rank(g, load_baseline(), load_flows(), load_history(), stats, weights,
                traffic_capability=traffic_spike_capability()), g


def print_table(ranking, top):
    print(f"{'#':>3}  {'target':<16}{'fault':<15}{'mag':>5}{'risk':>7}{'info':>7}{'prio':>7}  why")
    for c in ranking[:top]:
        print(f"{c['rank']:>3}  {c['target']:<16}{c['fault']:<15}{c['magnitude']:>5}"
              f"{c['risk']:>7.3f}{c['info']:>7.3f}{c['priority']:>7.3f}  {c['why']}")


def save_ranking(ranking, weights=WEIGHTS, directory=RANKINGS_DIR):
    now = datetime.now(timezone.utc)
    directory.mkdir(exist_ok=True)
    path = directory / f"{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps({"generated_at": now.isoformat(timespec="seconds"), "weights": weights,
                                "candidates": ranking}, indent=2), encoding="utf-8")
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description="Rank chaos experiments by risk and informativeness.")
    ap.add_argument("--top", type=int, default=10, help="rows to print (default 10)")
    ap.add_argument("--json", action="store_true", help="save the full ranking to rankings/<UTC timestamp>.json")
    args = ap.parse_args(argv)
    try:
        ranking, _ = rank_from_files()
    except (OSError, ValueError, KeyError) as e:
        print(f"Cannot rank: {e}")
        return 1
    print(f"{len(ranking)} candidates (protected and stopped services excluded, all pass policy)\n")
    print_table(ranking, args.top)
    if args.json:
        print(f"\nSaved {save_ranking(ranking)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
