"""Analysis and scoring agent: investigates the top deterministic weaknesses and writes evidence-linked findings.

The LLM only reads (stored results, the demo's source and config, Jaeger). Code validates every finding:
each must cite existing evidence ids, may not raise severity by more than one level without a reason, and
every number in its root cause must appear in the cited evidence. Any failure falls back to the
deterministic findings (source "analysis_fallback"). Output is cached in reports/.
"""
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import analysis
import llm
import planner
import rank_agent
from discovery import CLIENT, SERVER, fetch_spans, load_graph
from rank_agent import AGENT_LOG, AgentLog, ToolError, tool_loop

TOP_N = 5
MAX_TOOL_CALLS = 10
MAX_LLM_CALLS = MAX_TOOL_CALLS + 2
REASK_LLM_CALLS = 3          # the single re-ask may still use a tool or two from the remaining budget
LATEST_JSON = analysis.REPORTS_DIR / "latest.json"
TRACE_LIMIT = 100            # traces fetched for get_trace_summary
REQUIRED = ("id", "title", "service", "pattern", "severity", "severity_score", "root_cause", "evidence_ids",
            "fix", "verify_by")

SYSTEM_PROMPT = f"""You are the analysis agent of a chaos-engineering tool for the OpenTelemetry Astronomy Shop.
Experiments have finished. Deterministic rules turned them into candidate weaknesses; you get the top ones.
Investigate them with the read-only tools and write findings a developer can act on.

For EACH weakness:
1. Explain the root cause in plain language. Cite evidence ids (e.g. "20261010T100023Z#impact.frontend.p95_ms").
   Every number you quote must come from the cited evidence (get_evidence shows it); code checks this.
2. Confirm or reject the rule's pattern. You may lower the severity (say why in agent_adjustment). You may not raise
   it by more than one level, and raising it at all needs a reason in agent_adjustment.
3. Check the call edge for a timeout, retry, fallback or circuit breaker: search_source on the CALLER's code
   (e.g. "timeout|deadline|WithTimeout|retry|backoff|circuit|fallback"), and search for the client CALL, not the
   bare service name (that mostly matches config): e.g. "client\\.getCart|getUserCart" or "client\\.getProduct".
   The frontend's gRPC clients live in src/frontend/gateways/rpc/<Service>.gateway.ts.
   get_service_env for config, retry_evidence / get_trace_summary for traces (Jaeger keeps only recent traces;
   if a tool finds none, say so and do not guess).
4. Recommend a SPECIFIC fix: the service, the call edge and the mechanism, e.g. "add a 300 ms deadline on
   frontend -> product-catalog GetProduct calls and fetch products in parallel". Put the file:line of the call site
   in fix_location, exactly as search_source returned it (other locations are discarded).
5. Say how to verify the fix: which experiment to re-run and what result proves it worked.

Budget: at most {MAX_TOOL_CALLS} tool calls in total. Then reply with JSON only (no prose, no code fences):
{{"findings": [{{"id": weakness id, "title": str, "service": str, "edge": str|null, "pattern": str,
   "severity": "Critical"|"High"|"Medium"|"Low", "severity_score": number 0-100, "root_cause": str,
   "evidence_ids": [str], "fix": str, "fix_location": str|null, "verify_by": str, "confidence": number 0-1,
   "agent_adjustment": str|null}}],
 "strengths": [str], "summary": str}}
One finding per weakness you were given, with the weakness id as "id"."""

TOOL_DECLS = [
    {"name": "get_weakness", "description": "One deterministic weakness: pattern, runs, evidence ids, severity parts.",
     "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "get_evidence", "description": "One evidence item (numbers as stored) by evidence id.",
     "parameters": {"type": "object", "properties": {"evidence_id": {"type": "string"}}, "required": ["evidence_id"]}},
    {"name": "get_experiment", "description": "Compact summary of one experiment result by result id.",
     "parameters": {"type": "object", "properties": {"result_id": {"type": "string"}}, "required": ["result_id"]}},
    {"name": "compare_runs", "description": "How the same target service behaved across faults and magnitudes.",
     "parameters": {"type": "object", "properties": {"service": {"type": "string"}}, "required": ["service"]}},
    *[d for d in rank_agent.TOOL_DECLS if d["name"] in ("search_source", "get_service_env", "retry_evidence")],
    {"name": "get_trace_summary",
     "description": "Slowest and erroring traces of a service in a time window (epoch ms), with client-call counts per "
                    "callee. Jaeger keeps only recent traces, so old windows may return nothing.",
     "parameters": {"type": "object", "properties": {"service": {"type": "string"}, "start_ms": {"type": "integer"},
                                                     "end_ms": {"type": "integer"}},
                    "required": ["service", "start_ms", "end_ms"]}},
]


# ---- read-only tools -------------------------------------------------------------------

def trace_summary(service, start_ms, end_ms, spans=None):
    """Slowest / erroring traces of `service` within [start_ms, end_ms] and its client calls per callee."""
    start_ms, end_ms = int(start_ms), int(end_ms)
    if end_ms <= start_ms:
        raise ToolError("end_ms must be after start_ms")
    if spans is None:
        lookback = timedelta(milliseconds=max(0, time.time() * 1000 - start_ms) + 60_000)
        spans = fetch_spans(limits={service: TRACE_LIMIT}, lookback=lookback)
    lo, hi = start_ms * 1e6, end_ms * 1e6
    by_trace = defaultdict(list)
    for s in spans.values():
        if lo <= s["start"] <= hi:
            by_trace[s["trace"]].append(s)
    mine = {t: ss for t, ss in by_trace.items() if any(s["svc"] == service for s in ss)}
    if not mine:
        return {"service": service, "traces_found": 0,
                "note": "no traces of this service in the window; Jaeger keeps only recent traces in memory, so "
                        "this window has probably been evicted. Do not guess what they showed."}
    rows = []
    for t, ss in mine.items():
        first, last = min(s["start"] for s in ss), max(s["start"] + s["dur"] * 1e6 for s in ss)
        rows.append({"trace_id": t[:16], "duration_ms": round((last - first) / 1e6, 1),
                     "errors": sum(s["status"] == 2 for s in ss), "spans": len(ss)})
    children = defaultdict(list)
    for (tid, _), s in spans.items():
        children[(tid, s["parent"])].append(s)
    calls = Counter()
    for (tid, sid), s in spans.items():
        if tid in mine and s["svc"] == service and s["kind"] == CLIENT:
            kids = [k["svc"] for k in children[(tid, sid)] if k["kind"] == SERVER]
            calls[kids[0] if kids else str(s["attrs"].get("server.address") or s["name"])] += 1
    durs = [r["duration_ms"] for r in rows]
    return {"service": service, "traces_found": len(rows), "p50_ms": round(statistics.median(durs), 1),
            "slowest": sorted(rows, key=lambda r: -r["duration_ms"])[:5],
            "erroring_traces": sum(r["errors"] > 0 for r in rows),
            "client_calls_per_callee": {k: {"calls": v, "per_trace": round(v / len(rows), 2)}
                                        for k, v in calls.most_common(8)}}


class AnalysisToolbox(rank_agent.Toolbox):
    """The analysis tools; search_source, get_service_env and retry_evidence are inherited from rank_agent."""
    decls = TOOL_DECLS

    def __init__(self, g, data, results):
        super().__init__(g, {}, [], [])
        self.data, self.results = data, {r["id"]: r for r in results}
        self.weak = {w["id"]: w for w in data["weaknesses"]}
        self.seen_locations = set()   # file:line results of search_source; the only allowed fix_location values

    def search_source(self, service, pattern):
        out = super().search_source(service, pattern)
        self.seen_locations |= {m["at"] for m in out["matches"]}
        return out

    def get_weakness(self, id):
        w = self.weak.get(id)
        if w is None:
            raise ToolError(f"unknown weakness {id!r}")
        return {k: v for k, v in w.items() if k != "per_run"} | {"evidence_ids": w["evidence_ids"][:25]}

    def get_evidence(self, evidence_id):
        item = self.data["evidence"].get(evidence_id)
        if item is None:
            raise ToolError(f"unknown evidence id {evidence_id!r}")
        return item

    def get_experiment(self, result_id):
        r = self.results.get(result_id)
        if r is None:
            raise ToolError(f"unknown result {result_id!r}")
        rnd = lambda x: round(x, 1) if isinstance(x, float) else x
        return {"id": r["id"], "plan": {k: r["plan"][k] for k in ("target", "fault", "magnitude", "duration_s", "hypothesis")},
                "guard": {k: r["guard"][k] for k in ("aborted", "reason", "elapsed_s")},
                "recovery": {k: r["recovery"].get(k) for k in ("recovered", "seconds", "note")},
                "score": {k: r["score"].get(k) for k in ("resilience_score", "severity", "confidence", "limitations")},
                "impact": {i["service"]: {m: {k: rnd(i[m][k]) for k in ("baseline", "peak", "change")}
                                          for m in ("error_rate", "p95_ms")} for i in r["impact"]},
                "amplification": r["amplification"],
                "verdict": {"verdict": r["verdict"]["verdict"], "claims": r["verdict"].get("claims", [])[:6]},
                "grafana_window_ms": r["grafana_window_ms"], "evidence_prefix": f"{r['id']}#"}

    def compare_runs(self, service):
        rows = []
        for r in self.results.values():
            if r["plan"]["target"] != service:
                continue
            callers = [i for i in r["impact"] if i["service"] != service]
            worst = lambda m: max((i[m]["change"] for i in callers if i[m]["change"] is not None), default=None)
            rows.append({"id": r["id"], "fault": r["plan"]["fault"], "magnitude": r["plan"]["magnitude"],
                         "aborted": r["guard"]["aborted"], "elapsed_s": r["guard"]["elapsed_s"],
                         "worst_caller_p95_change_ms": worst("p95_ms") and round(worst("p95_ms"), 1),
                         "worst_caller_error_change_pp": worst("error_rate") and round(worst("error_rate"), 1),
                         "max_amplification": max((a["ratio"] for a in r["amplification"]), default=None),
                         "recovered": r["recovery"].get("recovered"), "recovery_s": r["recovery"].get("seconds"),
                         "score": r["score"]["resilience_score"]})
        if not rows:
            raise ToolError(f"no experiments on {service!r}")
        return {"service": service, "runs": sorted(rows, key=lambda x: (x["fault"], x["magnitude"], x["id"]))[:20]}

    def get_trace_summary(self, service, start_ms, end_ms):
        return trace_summary(self._service(service), start_ms, end_ms)


# ---- validation ------------------------------------------------------------------------

def check_answer(obj, top, data, seen_locations=frozenset()):
    """Validate the LLM's JSON. Raises ValueError (-> re-ask once, then fall back); returns cleaned findings."""
    by_id = {w["id"]: w for w in top}
    findings = obj.get("findings")
    if not isinstance(findings, list) or not findings:
        raise ValueError("findings missing")
    out = {}
    for f in findings:
        if not isinstance(f, dict):
            raise ValueError("a finding is not an object")
        missing = [k for k in REQUIRED if f.get(k) in (None, "", [])]
        if missing:
            raise ValueError(f"finding {f.get('id')!r} is missing {', '.join(missing)}")
        w = by_id.get(f["id"])
        if w is None:
            raise ValueError(f"finding id {f['id']!r} is not one of the weaknesses given")
        ids = [str(e) for e in f["evidence_ids"]] if isinstance(f["evidence_ids"], list) else []
        unknown = [e for e in ids if e not in data["evidence"]]
        if not ids or unknown:
            raise ValueError(f"finding {f['id']!r} cites unknown evidence ids: {', '.join(unknown[:5]) or '(none)'}")
        if f["severity"] not in analysis.SEVERITY_ORDER:
            raise ValueError(f"finding {f['id']!r}: bad severity {f['severity']!r}")
        old, new = analysis.SEVERITY_ORDER.index(w["severity"]), analysis.SEVERITY_ORDER.index(f["severity"])
        reason = str(f.get("agent_adjustment") or "").strip()
        if new < old - 1:
            raise ValueError(f"finding {f['id']!r} raises severity by more than one level ({w['severity']} -> {f['severity']})")
        if new != old and not reason:
            raise ValueError(f"finding {f['id']!r} changes severity without a reason in agent_adjustment")
        score = f["severity_score"]
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= score <= 100:
            raise ValueError(f"finding {f['id']!r}: severity_score must be 0-100")
        cited = [data["evidence"][e] for e in ids]
        root = str(f["root_cause"]).strip()
        replaced = not analysis.numbers_supported(root, cited)
        conf = f.get("confidence")
        loc = str(f.get("fix_location") or "").strip()
        loc_ok = any(seen in loc for seen in seen_locations)   # must quote a search_source result
        out[w["id"]] = {
            "id": w["id"], "title": str(f["title"])[:160], "service": w["service"], "edge": w["edge"],
            "pattern": w["pattern"], "severity": f["severity"], "severity_score": round(float(score), 1),
            "deterministic_severity": w["severity"],
            "root_cause": w["detail"] if replaced else root[:1200], "root_cause_replaced": replaced,
            "evidence_ids": ids, "fix": str(f["fix"])[:800], "fix_location": loc[:200] if loc_ok else None,
            "fix_location_dropped": bool(loc) and not loc_ok,
            "verify_by": str(f["verify_by"])[:600],
            "confidence": round(float(conf), 2) if isinstance(conf, (int, float)) and 0 <= conf <= 1 else w["confidence"],
            "agent_adjustment": reason or None, "runs": w["runs"], "source": "agent"}
    summary = obj.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary missing")
    notes = [str(s)[:300] for s in obj.get("strengths") or [] if isinstance(s, (str, int, float))]
    return {"findings": out, "summary": summary.strip()[:1500], "strength_notes": notes}


# ---- output ----------------------------------------------------------------------------

def deterministic_finding(w):
    return {"id": w["id"], "title": w["title"], "service": w["service"], "edge": w["edge"], "pattern": w["pattern"],
            "severity": w["severity"], "severity_score": w["severity_score"], "deterministic_severity": w["severity"],
            "root_cause": w["detail"], "root_cause_replaced": False, "evidence_ids": w["evidence_ids"],
            "fix": w["fix"], "fix_location": w["fix_location"], "fix_location_dropped": False, "verify_by": w["verify_by"],
            "confidence": w["confidence"], "agent_adjustment": None, "runs": w["runs"], "source": "deterministic"}


def deterministic_summary(data):
    ws, ov = data["weaknesses"], data["scores"]["overall"]
    counts = Counter(w["severity"] for w in ws)
    parts = [f"{counts[s]} {s}" for s in analysis.SEVERITY_ORDER if counts[s]]
    head = f"{len(ws)} weaknesses ({', '.join(parts)})" if ws else "No weaknesses found"
    top = f"; the most severe: {ws[0]['title']} ({ws[0]['severity']})" if ws else ""
    score = (f" Overall resilience {ov['score']}/100 over {ov['tested']} tested services." if ov["score"] is not None
             else " No service has a resilience score yet.")
    return f"{head}{top}. {len(data['strengths'])} strengths.{score}"


def build_output(data, source, note, agent=None, model=None, run_id=None, tool_calls=0):
    """Same shape for the agent and the fallback: findings ranked by severity, plus strengths and scores."""
    agent = agent or {}
    findings = [agent.get("findings", {}).get(w["id"]) or deterministic_finding(w) for w in data["weaknesses"]]
    findings.sort(key=lambda f: (analysis.SEVERITY_ORDER.index(f["severity"]), -f["severity_score"], f["id"]))
    cited = {e for f in findings for e in f["evidence_ids"]} | {e for s in data["strengths"] for e in s["evidence_ids"]}
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": source, "note": note,
            "model": model, "run_id": run_id, "tool_calls": tool_calls, "results_used": data["results_used"],
            "skipped": data["skipped"], "findings": findings, "strengths": data["strengths"],
            "strength_notes": agent.get("strength_notes", []),
            "summary": agent.get("summary") or deterministic_summary(data), "scores": data["scores"],
            "experiments": data["experiments"], "evidence": {e: data["evidence"][e] for e in sorted(cited)},
            "thresholds": data["thresholds"], "severity_weights": data["severity_weights"]}


def save(output):
    analysis.REPORTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = analysis.REPORTS_DIR / f"analysis_{ts}.json"
    analysis.write_json(path, output)
    analysis.write_json(LATEST_JSON, output)
    return path


def load_latest():
    try:
        return json.loads(LATEST_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def coverage(data=None, latest=None):
    """How the last agent analysis (reports/latest.json) relates to the results on disk:
    {analysed, fresh, stale, covered_runs, new_runs, generated_at, run_id}. Fresh = it covers exactly the
    current results; stale = there are results it has not seen."""
    data = data or analysis.current()
    latest = latest if latest is not None else load_latest()
    covered = list((latest or {}).get("results_used") or [])
    new = [r for r in data["results_used"] if r not in set(covered)]
    return {"analysed": bool(latest), "fresh": bool(latest) and covered == data["results_used"],
            "stale": bool(new), "covered_runs": covered, "new_runs": new,
            "generated_at": (latest or {}).get("generated_at"), "run_id": (latest or {}).get("run_id")}


def latest_or_deterministic(data=None, latest=None):
    """The cached agent findings if they cover the current results (deterministic parts recomputed fresh),
    else a fresh deterministic analysis (no LLM)."""
    data = data or analysis.current()
    latest = latest if latest is not None else load_latest()
    if coverage(data, latest)["fresh"]:
        agent = {"findings": {f["id"]: f for f in latest["findings"] if f.get("source") == "agent"},
                 "summary": latest.get("summary"), "strength_notes": latest.get("strength_notes", [])}
        out = build_output(data, latest["source"], latest.get("note"), agent, latest.get("model"),
                           latest.get("run_id"), latest.get("tool_calls", 0))
        out["generated_at"] = latest["generated_at"]
        return out
    return build_output(data, "analysis_fallback", "deterministic analysis (no agent run for the current results)")


# ---- the agent -------------------------------------------------------------------------

def _brief(top, data):
    rows = [{"id": w["id"], "pattern": w["pattern"], "service": w["service"], "edge": w["edge"],
             "severity": w["severity"], "severity_score": w["severity_score"], "confidence": w["confidence"],
             "faults": w["faults"], "runs": w["runs"][:6], "rule_says": w["detail"], "evidence_ids": w["evidence_ids"][:10]}
            for w in top]
    return ("Weaknesses to investigate (deterministic rules, ranked by severity):\n" + json.dumps(rows, indent=1)
            + "\nStrengths found by the rules: " + json.dumps([s["title"] for s in data["strengths"]])
            + f"\nOverall resilience score: {data['scores']['overall']}")


def pick_top(weaknesses, n=TOP_N):
    """The n most severe weaknesses, one per (service, pattern): the same pattern seen from another caller is
    usually the same root cause, and the tool budget is better spent on a different problem."""
    seen, out = set(), []
    for w in weaknesses:
        if (w["service"], w["pattern"]) not in seen:
            seen.add((w["service"], w["pattern"]))
            out.append(w)
    return out[:n]


def run_analysis(api_key=None, post=None, data=None, results=None, graph=None, log_path=AGENT_LOG, write=True):
    """Investigate the top weaknesses and return the analysis output (also cached in reports/). Never raises
    for LLM problems: those fall back to the deterministic findings."""
    if results is None:
        results, skipped = analysis.load_results()
        data = data or analysis.analyse(results, skipped)
    data = data or analysis.analyse(results)
    graph = graph if graph is not None else load_graph()[0]
    log = AgentLog(log_path, mode="analysis")
    top = pick_top(data["weaknesses"])
    log("thought", f"investigating top-{len(top)}: " + ", ".join(f"{w['id']} ({w['severity']})" for w in top))

    def finish(out):
        if write:
            save(out)
        return out

    def fallback(reason, used=0, model=None):
        log("decision", f"analysis_fallback: {reason}", fallback=True, detail=reason)
        return finish(build_output(data, "analysis_fallback", reason, model=model, run_id=log.run_id, tool_calls=used))

    if not top:
        return fallback("no weaknesses to investigate")
    api_key = api_key or llm.load_api_key()
    if not api_key:
        return fallback(f"{llm.API_KEY_VAR} is not set")
    post = post or llm.post
    toolbox = AnalysisToolbox(graph, data, results)
    contents = [{"role": "user", "parts": [{"text": _brief(top, data)}]}]
    used, model = 0, None
    try:
        with llm.listening(lambda kind, text: log(kind, text)):  # model answers and 429 waits show up in the feed
            text, model, used = tool_loop(api_key, SYSTEM_PROMPT, contents, TOOL_DECLS, toolbox, log, post,
                                          MAX_TOOL_CALLS, MAX_LLM_CALLS)
            try:
                answer = check_answer(planner._parse_json(text), top, data, toolbox.seen_locations)
            except ValueError as e:  # re-ask once with the reason
                log("thought", f"answer rejected, asking once more: {e}")
                contents.append({"role": "user", "parts": [{"text": f"Your answer was rejected: {e}. "
                                                                     "Reply again with the corrected JSON only."}]})
                text, model, used = tool_loop(api_key, SYSTEM_PROMPT, contents, TOOL_DECLS, toolbox, log, post,
                                              MAX_TOOL_CALLS, REASK_LLM_CALLS, used=used, model=model)
                answer = check_answer(planner._parse_json(text), top, data, toolbox.seen_locations)
    except Exception as e:  # LLM/network failure or invalid output (twice)
        return fallback(f"LLM analysis failed: {type(e).__name__}: {e}"[:300], used, model)
    replaced = [f for f in answer["findings"].values() if f["root_cause_replaced"]]
    log("decision", f"{len(answer['findings'])} findings from the agent after {used} tool calls"
                    + (f"; {len(replaced)} root causes replaced by the rule text (numbers not in evidence)" if replaced else ""),
        fallback=False)
    return finish(build_output(data, "analysis_agent", f"model {model}, {used} tool calls", answer, model,
                               log.run_id, used))


if __name__ == "__main__":
    out = run_analysis()
    print(f"{out['source']}: {out['note']}\n{out['summary']}\n")
    for f in out["findings"][:10]:
        print(f"[{f['severity']:<8}] {f['title']}  ({f['source']})\n    {f['root_cause'][:200]}\n    fix: {f['fix'][:200]}")
    print(f"\nSaved {LATEST_JSON}")
    sys.exit(0)
