"""Deterministic analysis: completed experiments -> evidence index -> weaknesses, strengths and scores.

No LLM. Every finding lists the evidence ids that triggered it; analysis_agent.py investigates the top
weaknesses and report.py renders the result. `python analysis.py` prints the ranked weaknesses.
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx

import grafana
import planner
import guard
import ranker
import run_experiment as exp
from discovery import compute_stats, load_flows, load_graph
from guard import RECOVERY_TIMEOUT_S
from metrics import load_baseline
from policy import PROTECTED, blast_radius

RESULTS_DIR = exp.RESULTS_DIR
REPORTS_DIR = Path("reports")
DETERMINISTIC_JSON = REPORTS_DIR / "deterministic.json"
FORMAT_VERSION = 3   # bump when the analysis output changes shape, so stale caches are recomputed
JAEGER_UI = "http://localhost:8080/jaeger/ui"
USER_FACING = {"frontend", "frontend-proxy", "checkout"}  # where users feel errors and latency directly

# ---- detection thresholds (all rules read these) ----------------------------------------
AMPLIFIED_ABOVE = exp.AMPLIFIED_ABOVE  # latency_amplification: caller p95 rise > this x the injected delay
SILENT_P95_MS = 300        # silent_degradation: a caller p95 rise of at least this ...
SILENT_P95_RATIO = 2.0     # ... that is at least this many times its baseline p95 ...
SILENT_ERR_PP = 1.0        # ... while its error rate rose by less than this (percentage points)
HARD_DEP_ERR_PP = 5.0      # hard_dependency: container_stop raised a caller's error rate by at least this
OK_P95_MS = 100            # graceful_degradation_ok: every caller's p95 rose by less than this ...
OK_ERR_PP = 1.0            # ... and its error rate by less than this
MIN_STRENGTH_S = 30        # ... and the fault ran at least this long (a run stopped after 4 s proves nothing)
IMPACT_P95_MS = 100        # a service counts as impacted (for spread) above this p95 rise ...
IMPACT_ERR_PP = 1.0        # ... or this error-rate rise
SPREAD_MIN_HOPS = 2        # blast_radius_spread: an impacted service at least this many calls away
SLOW_RECOVERY_S = 60       # slow_recovery: recovery took longer than this, or never happened
NOT_EVIDENCE_NOTES = ("stopped by user", "interrupted", "rollback failed")  # recovery notes that are not a finding

# ---- severity (0-100) -------------------------------------------------------------------
# severity = 100 * pattern weight * (sum of component weights x component) * (conf_floor + (1 - conf_floor) * confidence)
SEVERITY_WEIGHTS = {
    "components": {"user_impact": 0.45, "criticality": 0.25, "spread": 0.15, "recovery": 0.15},
    # user impact: error-rate rise counts fully, latency adds up to this much; errors on non-user-facing services count half
    "user_errors": 1.0, "user_latency": 0.6, "non_user_facing": 0.5,
    "error_full_pp": 20.0,     # an error-rate rise of 20 points saturates the error part
    "latency_full_x": 9.0,     # p95 at 10x baseline (a rise of 9x) saturates the latency part
    "criticality": {"checkout_path": 0.4, "fan_in": 0.3, "journeys": 0.3},
    "pattern": {"hard_dependency": 1.0, "latency_amplification": 0.9, "silent_degradation": 0.9,
                "blast_radius_spread": 0.8, "slow_recovery": 0.8, "hypothesis_mismatch": 0.5},
    "conf_floor": 0.6,         # a single low-confidence run still keeps 60% of its severity
    "run_confidence": {"High": 1.0, "Medium": 0.7, "Low": 0.4},
}
SEVERITY_BANDS = ((70, "Critical"), (50, "High"), (30, "Medium"), (0, "Low"))   # lower bound -> label
SEVERITY_ORDER = ("Critical", "High", "Medium", "Low")
# Resilience: subtracted from a service's average experiment score per open weakness of this severity.
# Resilience: subtracted from a service's average run score per open weakness. Medium findings (often held at
# Medium only because their evidence is thin) still count, scaled by the finding's confidence; Low findings do not.
WEAKNESS_PENALTY = {"Critical": 15, "High": 8, "Medium": 5}
PENALTY_SCALED_BY_CONFIDENCE = ("Medium",)
MIN_RELIABLE_RUNS = 3   # fewer "ok" runs than this: the overall score is shown as provisional
WEAKNESS_PENALTY_CAP = 30   # related weaknesses often share one root cause; do not stack without limit
USER_STOPS = ("stopped by user", "interrupted by user")   # guard reasons of runs a person ended early

# ---- data quality: every result is "ok", "low_evidence" or "excluded" (files are never deleted) -------------------
EXCLUDED_JSON = RESULTS_DIR / "EXCLUDED.json"   # {"<result id>": "reason"}, tracked in git
RESULT_FILE = re.compile(r"^\d{8}T\d{6}Z$")      # result file stems; anything else in results/ is not a run
EXCLUDED_SOURCES = {"manual-test": "staged by a test harness (planner bypassed), not a planned experiment"}
MIN_TIMELINE_SAMPLES = 4      # fewer guard samples than this: the peak rests on too little data
RESTART_BEFORE_RUN = "restarts: restarted"   # recovery blocked by a restart the old guard compared with the baseline
LOW_EVIDENCE_CONFIDENCE = 0.5  # a low-evidence run counts this much towards a finding's confidence
LOW_EVIDENCE_MAX_SCORE = 49.9  # findings backed only by low-evidence runs stay below High (50)
TITLES = {"latency_amplification": "{caller} pays the delay on {target} several times over",
          "silent_degradation": "{caller} slows down silently when {target} degrades",
          "hard_dependency": "{caller} fails when {target} is down",
          "slow_recovery": "Slow recovery after faults on {target}",
          "blast_radius_spread": "Faults on {target} spread to services several calls away",
          "hypothesis_mismatch": "{target} behaved differently than predicted"}


# ---- loading ----------------------------------------------------------------------------

def load_excluded(path=None):
    """{result id: reason} from results/EXCLUDED.json; {} when it is missing or unreadable."""
    try:
        data = json.loads(Path(path or EXCLUDED_JSON).read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def result_files(results_dir=RESULTS_DIR):
    """The result files in results/ (EXCLUDED.json and other non-run files are skipped)."""
    return [p for p in sorted(Path(results_dir).glob("*.json")) if RESULT_FILE.match(p.stem)]


def _restart_before_run(d):
    """True for older results whose recovery was blocked by a restart that predates the run: their guard compared
    restarts with the baseline, not with the counts taken at experiment start (results now record restart_limits)."""
    return "restart_limits" not in d and RESTART_BEFORE_RUN in str(d["recovery"].get("note") or "")


def availability_of(d):
    """Probe availability during the fault: recomputed from the timeline's probes, else what the run recorded."""
    timeline = d["guard"].get("timeline") or []
    if any("probes" in t for t in timeline):
        return guard.probe_availability(timeline)
    return (d.get("score") or {}).get("availability_pct")


def assess_quality(d, excluded=None):
    """(quality, reason): "excluded" runs never feed findings, strengths or scores; "low_evidence" runs may support a
    finding but lower its confidence and never carry a Critical/High finding alone."""
    excluded = load_excluded() if excluded is None else excluded
    if d["id"] in excluded:
        return "excluded", excluded[d["id"]]
    if d.get("forced_abort_test"):
        return "excluded", "forced abort test (fake thresholds)"
    if not d.get("rollback_verified"):
        return "excluded", "rollback not verified"
    if d.get("plan_source") in EXCLUDED_SOURCES:
        return "excluded", EXCLUDED_SOURCES[d["plan_source"]]
    reasons = []
    samples = len(d["guard"].get("timeline") or [])
    if samples < MIN_TIMELINE_SAMPLES:
        reasons.append(f"only {samples} guard sample{'' if samples == 1 else 's'} (need {MIN_TIMELINE_SAMPLES})")
    if d["score"].get("availability_pct") is None:
        reasons.append("availability not measured")
    if not exp.recovery_measured(d["recovery"], _restart_before_run(d)):
        reasons.append("recovery not measured (" + (str(d["recovery"].get("note") or "") or "restart from before the run") + ")")
    return ("low_evidence", "; ".join(reasons)) if reasons else ("ok", "")


def normalise_result(d, rid, excluded=None):
    """Bring older result shapes up to date (the same normalisation the UI's /api/results uses), re-score them the
    honest way (unmeasured components are left out, not counted as no damage) and flag their data quality.
    The files themselves are never changed."""
    d["id"] = rid
    d.setdefault("container_resources", [])
    d["amplification"] = exp.amplification(d["plan"], d["impact"])  # recomputed: older results have an older shape
    d["recovery"].setdefault("metrics_confirmed", None)
    d["recovery"].setdefault("recovered", False)
    recorded = d.get("score") or {}
    d["score"] = exp.score_experiment(d["impact"], d["recovery"], availability_of(d),
                                      d["guard"]["elapsed_s"] if d["guard"].get("timeline") else None,
                                      restart_before_run=_restart_before_run(d))
    if recorded.get("resilience_score") is not None and recorded["resilience_score"] != d["score"]["resilience_score"]:
        d["score"]["recorded_resilience_score"] = recorded["resilience_score"]   # what the file says, for reference
    d["verdict"].setdefault("recommendation", planner.remediation_for(d["plan"]))
    if not d.get("grafana_window_ms"):  # older results: derive the window from what was recorded
        start = datetime.fromisoformat(d["started_at"]).timestamp()
        d["grafana_window_ms"] = grafana.window_ms(start, start + d["guard"]["elapsed_s"] + (d["recovery"]["seconds"] or 60))
    d["quality"], d["quality_reason"] = assess_quality(d, excluded)
    return d


def load_results(results_dir=RESULTS_DIR, excluded=None):
    """(every readable result oldest first, each with quality / quality_reason; [{id, reason}] unreadable files).
    Nothing is dropped here: analyse() keeps excluded runs out of findings and scores but still lists them."""
    excluded = load_excluded(Path(results_dir) / EXCLUDED_JSON.name) if excluded is None else excluded
    results, skipped = [], []
    for p in result_files(results_dir):
        try:
            results.append(normalise_result(json.loads(p.read_text(encoding="utf-8")), p.stem, excluded))
        except (OSError, ValueError, KeyError, TypeError) as e:
            skipped.append({"id": p.stem, "reason": f"unreadable: {type(e).__name__}"})
    return results, skipped


# ---- evidence ---------------------------------------------------------------------------

def grafana_url(window):
    return f"{grafana.GRAFANA_URL}/d/{grafana.DASHBOARD_UID}/{grafana.DASHBOARD_SLUG}?orgId=1&from={window[0]}&to={window[1]}"


def jaeger_url(service, window):
    return f"{JAEGER_UI}/search?service={service}&start={window[0] * 1000}&end={window[1] * 1000}&limit=20"


def _r(x, d=1):
    return round(x, d) if isinstance(x, float) else x


def build_evidence(r):
    """{evidence_id: item} for one result. Ids are "<result_id>#<path>" and never change."""
    rid, p, out = r["id"], r["plan"], {}

    def add(path, kind, label, value):
        out[f"{rid}#{path}"] = {"id": f"{rid}#{path}", "result_id": rid, "kind": kind, "label": label, "value": value}

    add("plan", "plan", f"{p['fault']} on {p['target']}" + (f" ({p['magnitude']} ms)" if p["fault"] == "network_delay" else ""),
        {"target": p["target"], "fault": p["fault"], "magnitude": p["magnitude"], "duration_s": p["duration_s"],
         "hypothesis": p.get("hypothesis"), "source": r.get("plan_source")})
    for row in r["impact"]:
        s = row["service"]
        for m, unit in (("error_rate", "%"), ("p95_ms", "ms")):
            v = row[m]
            add(f"impact.{s}.{m}", "impact", f"{s} {m.replace('_ms', '').replace('_', ' ')} ({unit})",
                {"baseline": _r(v["baseline"]), "peak": _r(v["peak"]), "change": _r(v["change"])})
    for a in r["amplification"]:
        add(f"amplification.{a['service']}", "amplification", f"{a['service']} p95 rise / injected delay",
            {k: a[k] for k in ("increase_ms", "delay_ms", "ratio", "amplified")})
    g = r["guard"]
    add("guard", "guard", "guard outcome", {"aborted": g["aborted"], "reason": g["reason"], "elapsed_s": g["elapsed_s"]})
    rec = r["recovery"]
    add("recovery", "recovery", "recovery after rollback",
        {"recovered": rec.get("recovered"), "seconds": rec.get("seconds"), "metrics_confirmed": rec.get("metrics_confirmed"),
         "note": rec.get("note"), "timeout_s": RECOVERY_TIMEOUT_S})
    for c in r["container_resources"]:
        add(f"container.{c['container']}", "container", f"{c['container']} cpu / memory (reference only)",
            {"cpu_percent": {k: _r(v) for k, v in c["cpu_percent"].items()},
             "memory_mb": {k: _r(v) for k, v in c["memory_mb"].items()}})
    v = r["verdict"]
    add("verdict", "verdict", "hypothesis verdict", {"verdict": v["verdict"], "explanation": v.get("explanation")})
    for i, c in enumerate(v.get("claims") or []):
        add(f"claim.{i}", "claim", f"claim: {c['claim'][:80]}", {k: c.get(k) for k in ("claim", "status", "evidence")})
    sc = r["score"]
    add("score", "score", "experiment score",
        {k: sc.get(k) for k in ("resilience_score", "impact_score", "severity", "confidence", "availability_pct", "limitations")})
    w = r["grafana_window_ms"]
    add("grafana", "link", "Grafana window", {"window_ms": w, "url": grafana_url(w)})
    add(f"jaeger.{p['target']}", "link", f"Jaeger traces of {p['target']} in the window", {"url": jaeger_url(p["target"], w)})
    if r.get("ranking"):
        rk = r["ranking"]
        add("ranking", "ranking", "why this experiment was chosen",
            {"source": rk.get("source"), "chosen": rk.get("chosen"),
             "evidence": [e for row in rk.get("top_k") or [] if row.get("candidate_id") == rk.get("chosen")
                          for e in row.get("evidence") or []][:5]})
    return out


NUMBER = re.compile(r"(?<![A-Za-z0-9_.])-?\d+(?:\.\d+)?")
EVIDENCE_ID = re.compile(r"\d{8}T\d{6}Z#[\w.\-]+")


def numbers_in(text):
    """Numbers quoted in a text, ignoring evidence ids and digits glued to words (p95, gRPC2)."""
    return [float(x) for x in NUMBER.findall(EVIDENCE_ID.sub(" ", str(text)))]


def numbers_supported(text, evidence_items):
    """True if every number in `text` appears in the cited evidence (rounding allowed)."""
    known = [n for item in evidence_items for n in numbers_in(json.dumps(item["value"]))]
    return all(any(abs(n - k) <= max(0.5, 0.02 * abs(k)) or abs(abs(n) - abs(k)) <= 0.5 for k in known)
               for n in numbers_in(text))


# ---- rules ------------------------------------------------------------------------------

def _hops(g, src, dst):
    try:
        return nx.shortest_path_length(g, src, dst)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None


def _nearest(g, target, services):
    """The service closest to the target (fewest calls away), ties by name."""
    return min(services, key=lambda s: (_hops(g, s, target) or 99, s)) if services else None


def _edge(g, caller, target):
    return f"{caller}->{target}" if g.has_edge(caller, target) else f"{caller}->...->{target}"


def _cand(pattern, r, caller, ev, g, why):
    t = r["plan"]["target"]
    return {"pattern": pattern, "target": t, "caller": caller, "edge": _edge(g, caller, t) if caller else None,
            "result_id": r["id"], "fault": r["plan"]["fault"], "magnitude": r["plan"]["magnitude"],
            "evidence_ids": [f"{r['id']}#{e}" for e in ev], "why": why}


def _impacted(row):
    e, p = row["error_rate"]["change"], row["p95_ms"]["change"]
    return (e is not None and e >= IMPACT_ERR_PP) or (p is not None and p >= IMPACT_P95_MS)


def detect(r, g):
    """Candidate findings (weaknesses and strengths) for one result, each with its evidence ids."""
    p, t = r["plan"], r["plan"]["target"]
    rows = {row["service"]: row for row in r["impact"] if row["service"] != t}
    out = []

    amp = [a for a in r["amplification"] if a["role"] == "caller" and a["amplified"]]
    if amp:
        c = _nearest(g, t, [a["service"] for a in amp])
        a = next(x for x in amp if x["service"] == c)
        out.append(_cand("latency_amplification", r, c, ["plan", *(f"amplification.{x['service']}" for x in amp),
                                                         f"impact.{c}.p95_ms"], g,
                         f"Injecting {a['delay_ms']} ms on {t} raised {c} p95 by {a['increase_ms']} ms, "
                         f"{a['ratio']}x the delay: the delay is paid more than once per request, which points to "
                         f"sequential calls without a deadline."))

    silent = []
    for s, row in rows.items():
        pc, pb, ec = row["p95_ms"]["change"], row["p95_ms"]["baseline"], row["error_rate"]["change"]
        if pc is not None and ec is not None and pc >= SILENT_P95_MS and pb and (pb + pc) / pb >= SILENT_P95_RATIO \
                and ec < SILENT_ERR_PP:
            silent.append(s)
    if silent:
        c = _nearest(g, t, silent)
        row = rows[c]
        out.append(_cand("silent_degradation", r, c, ["plan", f"impact.{c}.p95_ms", f"impact.{c}.error_rate"], g,
                         f"{c} p95 went from {_r(row['p95_ms']['baseline'])} to {_r(row['p95_ms']['peak'])} ms "
                         f"while its error rate changed by {_r(row['error_rate']['change'])} points: requests got slower "
                         f"instead of failing fast, so no error-based alert would fire."))

    if p["fault"] == "container_stop":
        hard = [s for s, row in rows.items() if (row["error_rate"]["change"] or 0) >= HARD_DEP_ERR_PP]
        if hard:
            c = _nearest(g, t, hard)
            e = rows[c]["error_rate"]
            out.append(_cand("hard_dependency", r, c, ["plan", f"impact.{c}.error_rate", "guard"], g,
                             f"Stopping {t} raised {c} error rate from {_r(e['baseline'])}% to {_r(e['peak'])}%: "
                             f"{c} has no fallback or circuit breaker for {t}."))

    with_data = {s: row for s, row in rows.items()
                 if row["error_rate"]["change"] is not None or row["p95_ms"]["change"] is not None}
    ran_long = not r["guard"]["aborted"] or r["guard"]["elapsed_s"] >= MIN_STRENGTH_S
    if ran_long and with_data and all((row["error_rate"]["change"] or 0) < OK_ERR_PP and (row["p95_ms"]["change"] or 0) < OK_P95_MS
                                      for row in with_data.values()):
        worst = max(with_data, key=lambda s: with_data[s]["p95_ms"]["change"] or 0)
        out.append({**_cand("graceful_degradation_ok", r, None, ["plan", "guard", *(f"impact.{s}.p95_ms" for s in with_data),
                                                                 *(f"impact.{s}.error_rate" for s in with_data)], g,
                            f"During {p['fault']} on {t} for {r['guard']['elapsed_s']} s no caller degraded: the largest "
                            f"p95 rise was {_r(with_data[worst]['p95_ms']['change'])} ms ({worst}) and no error rate rose."),
                    "strength": True})

    rec = r["recovery"]
    note = str(rec.get("note") or "")
    if not any(x in note for x in NOT_EVIDENCE_NOTES):
        if rec.get("recovered") and (rec.get("seconds") or 0) > SLOW_RECOVERY_S:
            out.append(_cand("slow_recovery", r, None, ["plan", "recovery"], g,
                             f"After rollback the system took {rec['seconds']} s to recover."))
        elif not rec.get("recovered"):
            out.append(_cand("slow_recovery", r, None, ["plan", "recovery"], g,
                             f"The system did not recover within {RECOVERY_TIMEOUT_S} s after rollback."))

    far = [s for s, row in rows.items() if _impacted(row) and (_hops(g, s, t) or 0) >= SPREAD_MIN_HOPS]
    if far:
        out.append(_cand("blast_radius_spread", r, None, ["plan", *(f"impact.{s}.p95_ms" for s in far),
                                                          *(f"impact.{s}.error_rate" for s in far)], g,
                         f"The fault on {t} reached {', '.join(sorted(far))}, which "
                         f"{'does' if len(far) == 1 else 'do'} not call {t} directly."))

    claims = [i for i, c in enumerate(r["verdict"].get("claims") or []) if c.get("status") == "contradicted"]
    if claims:
        first = r["verdict"]["claims"][claims[0]]
        out.append(_cand("hypothesis_mismatch", r, None, ["plan", *(f"claim.{i}" for i in claims)], g,
                         f"Contradicted claim: \"{first['claim']}\" ({first['evidence']})."))
    return out


# ---- grouping, severity, scores ---------------------------------------------------------

def _clamp(x):
    return max(0.0, min(1.0, x))


def criticality(g, stats, baseline, flows, history):
    """{service: 0..1} from the ranker's features: on checkout path, fan-in, user journeys."""
    w = SEVERITY_WEIGHTS["criticality"]
    feats = {s: ranker._target_features(g, stats, baseline, flows, history, s)
             for s in g if s not in PROTECTED}
    top = lambda k: max([f[k] for f in feats.values()] + [1])
    return {s: round(w["checkout_path"] * f["checkout_path"] + w["fan_in"] * f["fan_in"] / top("fan_in")
                     + w["journeys"] * f["journeys"] / top("journeys"), 3) for s, f in feats.items()}


def _user_impact(r):
    sw = SEVERITY_WEIGHTS
    err = lat = 0.0
    for row in r["impact"]:
        scale = 1.0 if row["service"] in USER_FACING else sw["non_user_facing"]
        e, pb, pp = row["error_rate"]["change"], row["p95_ms"]["baseline"], row["p95_ms"]["peak"]
        if e is not None:
            err = max(err, scale * e / sw["error_full_pp"])
        if row["service"] in USER_FACING and pb and pp is not None:
            lat = max(lat, (pp / pb - 1) / sw["latency_full_x"])
    return min(1.0, sw["user_errors"] * _clamp(err) + sw["user_latency"] * _clamp(lat))


def _recovery_cost(r):
    rec = r["recovery"]
    if any(x in str(rec.get("note") or "") for x in NOT_EVIDENCE_NOTES):
        return 0.0
    return 1.0 if not rec.get("recovered") else _clamp((rec.get("seconds") or 0) / RECOVERY_TIMEOUT_S)


def severity_label(score):
    return next(label for bound, label in SEVERITY_BANDS if score >= bound)


def group(cands, by_id, g, crit, weights=SEVERITY_WEIGHTS):
    """Merge candidates with the same (pattern, target, caller) across runs into one weakness and score it."""
    groups = {}
    for c in cands:
        groups.setdefault((c["pattern"], c["target"], c["caller"]), []).append(c)
    out = []
    for (pattern, target, caller), cs in groups.items():
        runs = [by_id[c["result_id"]] for c in cs]
        br = max(1, len(blast_radius(g, target)))
        comp = {"user_impact": max(_user_impact(r) for r in runs),
                "criticality": crit.get(target, 0.0),
                "spread": max(_clamp(sum(_impacted(row) for row in r["impact"] if row["service"] != target) / br) for r in runs),
                "recovery": max(_recovery_cost(r) for r in runs)}
        run_conf = [weights["run_confidence"].get(r["score"]["confidence"].split()[0], 0.4)
                    * (LOW_EVIDENCE_CONFIDENCE if r.get("quality") == "low_evidence" else 1.0) for r in runs]
        confidence = round(min(1.0, sum(run_conf) / len(run_conf) * (0.6 + 0.2 * len(runs))), 2)
        base = sum(weights["components"][k] * v for k, v in comp.items())
        score = round(100 * weights["pattern"].get(pattern, 1.0) * base
                      * (weights["conf_floor"] + (1 - weights["conf_floor"]) * confidence), 1)
        low = sorted({r["id"] for r in runs if r.get("quality") == "low_evidence"})
        capped = bool(low) and len(low) == len({r["id"] for r in runs}) and score > LOW_EVIDENCE_MAX_SCORE
        if capped:   # low-evidence runs alone never carry a Critical/High finding
            score = LOW_EVIDENCE_MAX_SCORE
        first = cs[0]
        out.append({
            "id": f"{pattern}:{target}:{caller or '-'}", "pattern": pattern, "service": target, "caller": caller,
            "edge": first["edge"], "title": TITLES[pattern].format(caller=caller, target=target),
            "runs": sorted({c["result_id"] for c in cs}),
            "faults": sorted({f"{c['fault']}" + (f" {c['magnitude']}ms" if c["fault"] == "network_delay" else "") for c in cs}),
            "evidence_ids": [e for c in cs for e in c["evidence_ids"]],
            "detail": (first["why"] if len(cs) == 1 else f"{first['why']} Seen in {len(cs)} runs.")
                      + (" Severity held below High: every supporting run is low evidence." if capped else ""),
            "per_run": [{"result_id": c["result_id"], "why": c["why"]} for c in cs],
            "components": {k: round(v, 3) for k, v in comp.items()}, "confidence": confidence,
            "low_evidence_runs": low, "capped_low_evidence": capped,
            "severity_score": score, "severity": severity_label(score),
            "fix": planner.remediation_for({"target": target, "fault": first["fault"]}),
            "fix_location": None, "verify_by": verify_by(pattern, first, target, caller)})
    out.sort(key=lambda w: (-w["severity_score"], w["id"]))
    return out


def verify_by(pattern, c, target, caller):
    rerun = f"Re-run {c['fault']} on {target}" + (f" at {c['magnitude']} ms" if c["fault"] == "network_delay" else "")
    goal = {"latency_amplification": f"{caller} p95 rises by at most {AMPLIFIED_ABOVE}x the injected delay",
            "silent_degradation": f"{caller} fails fast or stays within {SILENT_P95_MS} ms of its baseline p95",
            "hard_dependency": f"{caller} error rate rises by less than {HARD_DEP_ERR_PP} points",
            "slow_recovery": f"the system recovers within {SLOW_RECOVERY_S} s of rollback",
            "blast_radius_spread": f"services two or more calls away stay within {IMPACT_P95_MS} ms p95 "
                                   f"and {IMPACT_ERR_PP} error points of baseline",
            "hypothesis_mismatch": "the result matches the (updated) hypothesis claim by claim"}[pattern]
    return f"{rerun}; the fix works if {goal}."


def strengths_of(cands, weaknesses):
    """Strengths grouped by (target, fault). Dropped if a weakness was seen for the same target and fault."""
    weak = {(w["service"], f.split()[0]) for w in weaknesses for f in w["faults"] if w["pattern"] != "hypothesis_mismatch"}
    groups = {}
    for c in cands:
        groups.setdefault((c["target"], c["fault"]), []).append(c)
    out = []
    for (t, fault), cs in sorted(groups.items()):
        if (t, fault) in weak:
            continue
        out.append({"id": f"strength:{t}:{fault}", "service": t, "fault": fault,
                    "title": f"Callers kept working during {fault} on {t}", "runs": sorted({c["result_id"] for c in cs}),
                    "evidence_ids": [e for c in cs for e in c["evidence_ids"]], "detail": cs[-1]["why"]})
    return out


def measures_resilience(r):
    """False for runs a person stopped before MIN_STRENGTH_S: they end before the fault could show anything."""
    g = r["guard"]
    return not (g["aborted"] and any(x in g["reason"] for x in USER_STOPS) and g["elapsed_s"] < MIN_STRENGTH_S)


def resilience_scores(results, weaknesses, services, crit):
    """Per service: mean experiment resilience score minus open-weakness penalties (Medium scaled by confidence).
    Overall: criticality-weighted mean over tested services. Untested services are "not tested"."""
    per = {}
    for s in sorted(services):
        tried = [r for r in results if r["plan"]["target"] == s]
        runs = [r for r in tried if measures_resilience(r)]
        if not tried:
            per[s] = {"score": None, "status": "not tested", "criticality": crit.get(s, 0.0),
                      "explain": f"{s} has not been tested yet."}
            continue
        if not runs:
            per[s] = {"score": None, "status": "insufficient data", "criticality": crit.get(s, 0.0),
                      "explain": f"{len(tried)} run(s) on {s}, all stopped by a person before {MIN_STRENGTH_S} s."}
            continue
        scores = [r["score"]["resilience_score"] for r in runs]
        open_w = [w for w in weaknesses if w["service"] == s and w["severity"] in WEAKNESS_PENALTY]
        penalty = round(min(WEAKNESS_PENALTY_CAP, sum(
            WEAKNESS_PENALTY[w["severity"]] * (w["confidence"] if w["severity"] in PENALTY_SCALED_BY_CONFIDENCE else 1.0)
            for w in open_w)), 1)
        mean = sum(scores) / len(scores)
        score = max(0, round(mean - penalty))
        per[s] = {"score": score, "status": "tested", "runs": len(runs), "mean_run_score": round(mean, 1),
                  "penalty": penalty, "open_weaknesses": [w["id"] for w in open_w], "criticality": crit.get(s, 0.0),
                  "explain": f"mean of {len(runs)} run scores ({round(mean, 1)}) minus {penalty} for "
                             f"{len(open_w)} open weaknesses (Critical {WEAKNESS_PENALTY['Critical']}, High "
                             f"{WEAKNESS_PENALTY['High']}, Medium {WEAKNESS_PENALTY['Medium']} x confidence; cap "
                             f"{WEAKNESS_PENALTY_CAP}) = {score}"}
    tested = {s: v for s, v in per.items() if v["score"] is not None}
    if tested:
        weights = {s: 1 + v["criticality"] for s, v in tested.items()}
        overall = round(sum(weights[s] * v["score"] for s, v in tested.items()) / sum(weights.values()))
        explain = (f"criticality-weighted mean of {len(tested)} tested services (weight 1 + criticality); "
                   f"{len(per) - len(tested)} services not tested yet and not included")
    else:
        overall, explain = None, "no service has been tested yet"
    reliable = sum(r.get("quality", "ok") == "ok" for r in results)
    provisional = overall is not None and reliable < MIN_RELIABLE_RUNS
    return {"overall": {"score": overall, "tested": len(tested), "services": len(per), "explain": explain,
                        "reliable_runs": reliable, "provisional": provisional,
                        "provisional_note": f"provisional: based on {reliable} reliable runs" if provisional else None},
            "services": per}


# ---- entry points -----------------------------------------------------------------------

def analyse(results=None, skipped=(), g=None, stats=None, baseline=None, flows=None, weights=SEVERITY_WEIGHTS):
    """Full deterministic analysis. Arguments default to the files on disk."""
    if results is None:
        results, skipped = load_results()
    everything = list(results)
    excluded_runs = [r for r in everything if r.get("quality") == "excluded"]
    results = [r for r in everything if r.get("quality") != "excluded"]   # excluded runs feed no finding or score
    if g is None:
        g, stats = load_graph()
    stats = stats or compute_stats(g)
    baseline = load_baseline() if baseline is None else baseline
    flows = load_flows() if flows is None else flows
    by_id = {r["id"]: r for r in results}
    crit = criticality(g, stats, baseline or {}, flows, [])  # history only feeds recovery, not criticality
    evidence = {k: v for r in results for k, v in build_evidence(r).items()}
    cands = [c for r in results for c in detect(r, g)]
    weaknesses = group([c for c in cands if not c.get("strength")], by_id, g, crit, weights)
    strengths = strengths_of([c for c in cands if c.get("strength")], weaknesses)
    services = set(ranker.candidate_targets(g, baseline)) if baseline else set()
    services |= {r["plan"]["target"] for r in results}
    return {
        "version": FORMAT_VERSION, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "results_used": [r["id"] for r in results],
        "skipped": list(skipped) + [{"id": r["id"], "reason": "excluded: " + r["quality_reason"]} for r in excluded_runs],
        "excluded": [{"id": r["id"], "target": r["plan"]["target"], "fault": r["plan"]["fault"], "reason": r["quality_reason"]}
                     for r in excluded_runs],
        "quality_counts": {q: sum(r.get("quality", "ok") == q for r in everything) for q in ("ok", "low_evidence", "excluded")},
        "excluded_map": load_excluded(),
        "experiments": [{"id": r["id"], "target": r["plan"]["target"], "fault": r["plan"]["fault"],
                         "magnitude": r["plan"]["magnitude"],
                         "outcome": ("aborted: " if r["guard"]["aborted"] else "completed: ") + r["guard"]["reason"],
                         "recovered": r["recovery"].get("recovered"), "recovery_s": r["recovery"].get("seconds"),
                         "score": r["score"]["resilience_score"], "verdict": r["verdict"]["verdict"],
                         "quality": r.get("quality", "ok"), "quality_reason": r.get("quality_reason", ""),
                         "grafana_url": grafana_url(r["grafana_window_ms"]),
                         "jaeger_url": jaeger_url(r["plan"]["target"], r["grafana_window_ms"])} for r in everything],
        "weaknesses": weaknesses, "strengths": strengths, "evidence": evidence,
        "scores": resilience_scores(results, weaknesses, services, crit),
        "thresholds": {k: v for k, v in globals().items() if k.isupper() and isinstance(v, (int, float)) and not isinstance(v, bool)},
        "severity_weights": weights}


def write_json(path, obj):
    """Write via a temp file and rename, so a reader (the web UI) never sees a half-written file."""
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    tmp.replace(path)


def refresh(path=DETERMINISTIC_JSON):
    """Recompute and save the deterministic analysis (cheap: no LLM, no network)."""
    a = analyse()
    write_json(path, a)
    return a


def current(path=DETERMINISTIC_JSON, results_dir=RESULTS_DIR):
    """The saved deterministic analysis if it covers exactly the result files on disk, else a fresh one."""
    try:
        a = json.loads(path.read_text(encoding="utf-8"))
        seen = set(a["results_used"]) | {s["id"] for s in a["skipped"]}
        if (a.get("version") == FORMAT_VERSION and seen == {p.stem for p in result_files(results_dir)}
                and a.get("excluded_map") == load_excluded(Path(results_dir) / EXCLUDED_JSON.name)):
            return a
    except (OSError, ValueError, KeyError):
        pass
    return refresh(path)


def print_table(a):
    ov = a["scores"]["overall"]
    print(f"{len(a['results_used'])} results analysed, {len(a['skipped'])} skipped "
          f"({', '.join(sorted({s['reason'] for s in a['skipped']})) or 'none'})")
    print(f"Overall resilience: {ov['score'] if ov['score'] is not None else 'not tested'} ({ov['explain']})\n")
    print(f"{'#':>2}  {'severity':<9}{'score':>6}{'conf':>6}  {'pattern':<23}{'edge / service':<34}{'runs':>4}  detail")
    for i, w in enumerate(a["weaknesses"], 1):
        print(f"{i:>2}  {w['severity']:<9}{w['severity_score']:>6}{w['confidence']:>6}  {w['pattern']:<23}"
              f"{w['edge'] or w['service']:<34}{len(w['runs']):>4}  {w['detail'][:110]}")
    print("\nStrengths:")
    for s in a["strengths"] or [{"title": "none found", "runs": [], "detail": ""}]:
        print(f"  + {s['title']} ({len(s['runs'])} runs) {s['detail'][:110]}")
    print("\nPer-service resilience:")
    for s, v in sorted(a["scores"]["services"].items(), key=lambda kv: (kv[1]["score"] is None, kv[1]["score"] or 0)):
        print(f"  {s:<18}{v['status'] if v['score'] is None else v['score']:>17}   {v['explain']}")


if __name__ == "__main__":
    try:
        analysis = refresh()
    except (OSError, ValueError, KeyError) as e:
        print(f"Cannot analyse: {e}")
        sys.exit(1)
    print_table(analysis)
    print(f"\nSaved {DETERMINISTIC_JSON}")
