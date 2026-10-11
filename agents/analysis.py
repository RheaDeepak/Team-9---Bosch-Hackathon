"""LangGraph evidence-analysis workflow with deterministic validation and fallback."""
import json
import statistics
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from operator import add
from pathlib import Path
from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel

import activity
import analysis as rules
from agents.checkpoint import sqlite_checkpointer
from agents.model import API_KEY_VAR, GeminiModels, load_api_key
from agents.models import AnalysisDecision
from agents.tools import (EdgeInput, PlanningToolbox, SearchInput, ServiceInput, ToolError,
                          compact, get_service_env, retry_evidence, search_source)
from discovery import CLIENT, SERVER, fetch_spans, load_graph

TOP_N = 5
MAX_TOOL_CALLS = 10
MAX_GRAPH_STEPS = 34
PROMPT_VERSION = "analysis-langgraph-v1"
LATEST_JSON = rules.REPORTS_DIR / "latest.json"
_MISSING = object()
TRACE_LIMIT = 100
PROMPT = (Path(__file__).parent / "prompts" / "analysis.md").read_text(encoding="utf-8")
ANALYSIS_TOOL_NAMES = frozenset({"get_weakness", "get_evidence", "get_experiment", "compare_runs",
                                 "search_source", "get_service_env", "retry_evidence", "get_trace_summary"})


class AnalysisState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    tool_calls_used: int
    evidence_trail: Annotated[list[dict], add]
    decision: dict
    validated: dict
    output: dict
    validation_error: str | None
    error: str
    repair_attempts: int
    model: str | None


class EvidenceInput(BaseModel):
    evidence_id: str


class ResultInput(BaseModel):
    result_id: str


class TraceInput(BaseModel):
    service: str
    start_ms: int
    end_ms: int


class WeaknessInput(BaseModel):
    id: str


def trace_summary(service, start_ms, end_ms, spans=None):
    start_ms, end_ms = int(start_ms), int(end_ms)
    if end_ms <= start_ms:
        raise ToolError("end_ms must be after start_ms")
    if spans is None:
        lookback = timedelta(milliseconds=max(0, time.time() * 1000 - start_ms) + 60_000)
        spans = fetch_spans(limits={service: TRACE_LIMIT}, lookback=lookback)
    lower, upper = start_ms * 1e6, end_ms * 1e6
    by_trace = defaultdict(list)
    for span in spans.values():
        if lower <= span["start"] <= upper:
            by_trace[span["trace"]].append(span)
    selected = {trace: rows for trace, rows in by_trace.items() if any(row["svc"] == service for row in rows)}
    if not selected:
        return {"service": service, "traces_found": 0,
                "note": "no traces in this window; do not infer what evicted traces contained"}
    rows = []
    for trace, spans_in_trace in selected.items():
        first = min(span["start"] for span in spans_in_trace)
        last = max(span["start"] + span["dur"] * 1e6 for span in spans_in_trace)
        rows.append({"trace_id": trace[:16], "duration_ms": round((last - first) / 1e6, 1),
                     "errors": sum(span["status"] == 2 for span in spans_in_trace), "spans": len(spans_in_trace)})
    children = defaultdict(list)
    for (trace, _), span in spans.items():
        children[(trace, span["parent"])].append(span)
    calls = Counter()
    for (trace, span_id), span in spans.items():
        if trace in selected and span["svc"] == service and span["kind"] == CLIENT:
            child_services = [child["svc"] for child in children[(trace, span_id)] if child["kind"] == SERVER]
            calls[child_services[0] if child_services else str(span["attrs"].get("server.address") or span["name"])] += 1
    durations = [row["duration_ms"] for row in rows]
    return {"service": service, "traces_found": len(rows), "p50_ms": round(statistics.median(durations), 1),
            "slowest": sorted(rows, key=lambda row: -row["duration_ms"])[:5],
            "erroring_traces": sum(row["errors"] > 0 for row in rows),
            "client_calls_per_callee": {key: {"calls": count, "per_trace": round(count / len(rows), 2)}
                                        for key, count in calls.most_common(8)}}


class AnalysisToolbox(PlanningToolbox):
    def __init__(self, graph, data, results):
        super().__init__(graph, {}, [], [])
        self.data, self.results = data, {result["id"]: result for result in results}
        self.weaknesses = {weakness["id"]: weakness for weakness in data["weaknesses"]}
        self.seen_locations = set()

    def search_source(self, service, pattern):
        self._service(service)
        output = search_source(service, pattern)
        self.seen_locations |= {match["at"] for match in output["matches"]}
        return output

    def get_service_env(self, service):
        self._service(service)
        return get_service_env(service)

    def retry_evidence(self, caller, callee):
        return retry_evidence(self._service(caller), self._service(callee))

    def get_weakness(self, id):
        weakness = self.weaknesses.get(id)
        if weakness is None:
            raise ToolError(f"unknown weakness {id!r}")
        return {key: value for key, value in weakness.items() if key != "per_run"} | {
            "evidence_ids": weakness["evidence_ids"][:25]}

    def get_evidence(self, evidence_id):
        item = self.data["evidence"].get(evidence_id)
        if item is None:
            raise ToolError(f"unknown evidence id {evidence_id!r}")
        return item

    def get_experiment(self, result_id):
        result = self.results.get(result_id)
        if result is None:
            raise ToolError(f"unknown result {result_id!r}")
        rounded = lambda value: round(value, 1) if isinstance(value, float) else value
        return {"id": result["id"],
                "plan": {key: result["plan"][key] for key in ("target", "fault", "magnitude", "duration_s", "hypothesis")},
                "guard": {key: result["guard"][key] for key in ("aborted", "reason", "elapsed_s")},
                "recovery": {key: result["recovery"].get(key) for key in ("recovered", "seconds", "note")},
                "score": {key: result["score"].get(key) for key in ("resilience_score", "severity", "confidence", "limitations")},
                "impact": {item["service"]: {metric: {key: rounded(item[metric][key]) for key in ("baseline", "peak", "change")}
                                               for metric in ("error_rate", "p95_ms")} for item in result["impact"]},
                "amplification": result["amplification"],
                "verdict": {"verdict": result["verdict"]["verdict"], "claims": result["verdict"].get("claims", [])[:6]},
                "grafana_window_ms": result["grafana_window_ms"], "evidence_prefix": f"{result['id']}#"}

    def compare_runs(self, service):
        service = self._service(service)
        rows = []
        for result in self.results.values():
            if result["plan"]["target"] != service:
                continue
            callers = [item for item in result["impact"] if item["service"] != service]
            worst = lambda metric: max((item[metric]["change"] for item in callers
                                        if item[metric]["change"] is not None), default=None)
            rows.append({"id": result["id"], "fault": result["plan"]["fault"],
                         "magnitude": result["plan"]["magnitude"], "aborted": result["guard"]["aborted"],
                         "elapsed_s": result["guard"]["elapsed_s"],
                         "worst_caller_p95_change_ms": worst("p95_ms"),
                         "worst_caller_error_change_pp": worst("error_rate"),
                         "max_amplification": max((item["ratio"] for item in result["amplification"]), default=None),
                         "recovered": result["recovery"].get("recovered"),
                         "recovery_s": result["recovery"].get("seconds"),
                         "score": result["score"]["resilience_score"]})
        if not rows:
            raise ToolError(f"no experiments on {service!r}")
        return {"service": service, "runs": rows[:20]}

    def get_trace_summary(self, service, start_ms, end_ms):
        allowed = any(start_ms >= window[0] and end_ms <= window[1]
                      for window in (result["grafana_window_ms"] for result in self.results.values()))
        if not allowed:
            raise ToolError("trace window must be inside a stored experiment window")
        return trace_summary(self._service(service), start_ms, end_ms)

    def call(self, name, args):
        function = getattr(self, name, None)
        if name not in ANALYSIS_TOOL_NAMES or function is None:
            return {"error": f"unknown tool {name!r}"}
        try:
            return function(**(args or {}))
        except ToolError as exc:
            return {"error": str(exc)}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"[:300]}


def analysis_tools(toolbox):
    specs = [
        ("get_weakness", "Get one deterministic weakness and its evidence IDs.", WeaknessInput),
        ("get_evidence", "Get one immutable evidence item by ID.", EvidenceInput),
        ("get_experiment", "Get a compact completed-experiment summary.", ResultInput),
        ("compare_runs", "Compare completed experiments for the same target service.", ServiceInput),
        ("search_source", "Search one service's source for resilience mechanisms and call sites.", SearchInput),
        ("get_service_env", "Get non-secret resilience and resource settings from Compose.", ServiceInput),
        ("retry_evidence", "Inspect recent traces for repeated calls and fixed client timeouts.", EdgeInput),
        ("get_trace_summary", "Inspect slow and erroring traces within an experiment window.", TraceInput),
    ]
    return [StructuredTool.from_function(name=name, description=description, args_schema=schema,
                                         func=lambda _name=name, **kwargs: toolbox.call(_name, kwargs))
            for name, description, schema in specs]


def pick_top(weaknesses, n=TOP_N):
    seen, selected = set(), []
    for weakness in weaknesses:
        key = (weakness["service"], weakness["pattern"])
        if key not in seen:
            seen.add(key)
            selected.append(weakness)
    return selected[:n]


def _brief(top, data):
    weaknesses = [{"id": weakness["id"], "pattern": weakness["pattern"], "service": weakness["service"],
                   "edge": weakness["edge"], "severity": weakness["severity"],
                   "severity_score": weakness["severity_score"], "confidence": weakness["confidence"],
                   "faults": weakness["faults"], "runs": weakness["runs"][:6], "rule_says": weakness["detail"],
                   "evidence_ids": weakness["evidence_ids"][:10]} for weakness in top]
    return json.dumps({"weaknesses": weaknesses, "strengths": [item["title"] for item in data["strengths"]],
                       "overall_resilience": data["scores"]["overall"]}, indent=1)


def check_answer(decision, top, data, seen_locations=frozenset()):
    by_id = {weakness["id"]: weakness for weakness in top}
    output = {}
    for finding in decision.findings:
        weakness = by_id.get(finding.id)
        if weakness is None:
            raise ValueError(f"finding id {finding.id!r} is not one of the supplied weaknesses")
        unknown = [evidence_id for evidence_id in finding.evidence_ids if evidence_id not in data["evidence"]]
        if unknown:
            raise ValueError(f"finding {finding.id!r} cites unknown evidence: {', '.join(unknown[:5])}")
        old = rules.SEVERITY_ORDER.index(weakness["severity"])
        new = rules.SEVERITY_ORDER.index(finding.severity)
        adjustment = (finding.agent_adjustment or "").strip()
        if new < old - 1:
            raise ValueError(f"finding {finding.id!r} raises severity by more than one level")
        if new != old and not adjustment:
            raise ValueError(f"finding {finding.id!r} changes severity without agent_adjustment")
        cited = [data["evidence"][evidence_id] for evidence_id in finding.evidence_ids]
        unsupported_numbers = not rules.numbers_supported(finding.root_cause, cited)
        location = (finding.fix_location or "").strip()
        location_ok = location in seen_locations
        output[weakness["id"]] = {
            "id": weakness["id"], "title": finding.title[:160], "service": weakness["service"],
            "edge": weakness["edge"], "pattern": weakness["pattern"], "severity": finding.severity,
            "severity_score": round(finding.severity_score, 1), "deterministic_severity": weakness["severity"],
            "root_cause": weakness["detail"] if unsupported_numbers else finding.root_cause[:1200],
            "root_cause_replaced": unsupported_numbers, "evidence_ids": finding.evidence_ids,
            "fix": finding.fix[:800], "fix_location": location[:200] if location_ok else None,
            "fix_location_dropped": bool(location) and not location_ok, "verify_by": finding.verify_by[:600],
            "confidence": round(finding.confidence, 2), "agent_adjustment": adjustment or None,
            "runs": weakness["runs"], "source": "agent",
        }
    missing = [weakness["id"] for weakness in top if weakness["id"] not in output]
    if missing:
        raise ValueError("missing findings for: " + ", ".join(missing))
    return {"findings": output, "summary": decision.summary[:1500],
            "strength_notes": [note[:300] for note in decision.strengths]}


def deterministic_finding(weakness):
    return {"id": weakness["id"], "title": weakness["title"], "service": weakness["service"],
            "edge": weakness["edge"], "pattern": weakness["pattern"], "severity": weakness["severity"],
            "severity_score": weakness["severity_score"], "deterministic_severity": weakness["severity"],
            "root_cause": weakness["detail"], "root_cause_replaced": False,
            "evidence_ids": weakness["evidence_ids"], "fix": weakness["fix"],
            "fix_location": weakness["fix_location"], "fix_location_dropped": False,
            "verify_by": weakness["verify_by"], "confidence": weakness["confidence"],
            "agent_adjustment": None, "runs": weakness["runs"], "source": "deterministic"}


def deterministic_summary(data):
    weaknesses, overall = data["weaknesses"], data["scores"]["overall"]
    counts = Counter(weakness["severity"] for weakness in weaknesses)
    parts = [f"{counts[severity]} {severity}" for severity in rules.SEVERITY_ORDER if counts[severity]]
    head = f"{len(weaknesses)} weaknesses ({', '.join(parts)})" if weaknesses else "No weaknesses found"
    top = f"; the most severe: {weaknesses[0]['title']} ({weaknesses[0]['severity']})" if weaknesses else ""
    score = (f" Overall resilience {overall['score']}/100 over {overall['tested']} tested services."
             if overall["score"] is not None else " No service has a resilience score yet.")
    return f"{head}{top}. {len(data['strengths'])} strengths.{score}"


def build_output(data, source, note, agent=None, model=None, run_id=None, tool_calls=0):
    agent = agent or {}
    findings = [agent.get("findings", {}).get(weakness["id"]) or deterministic_finding(weakness)
                for weakness in data["weaknesses"]]
    findings.sort(key=lambda finding: (rules.SEVERITY_ORDER.index(finding["severity"]),
                                       -finding["severity_score"], finding["id"]))
    cited = {evidence_id for finding in findings for evidence_id in finding["evidence_ids"]}
    cited |= {evidence_id for strength in data["strengths"] for evidence_id in strength["evidence_ids"]}
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": source,
            "note": note, "model": model, "run_id": run_id, "tool_calls": tool_calls,
            "results_used": data["results_used"], "skipped": data["skipped"], "findings": findings,
            "strengths": data["strengths"], "strength_notes": agent.get("strength_notes", []),
            "summary": agent.get("summary") or deterministic_summary(data), "scores": data["scores"],
            "experiments": data["experiments"],
            "evidence": {evidence_id: data["evidence"][evidence_id] for evidence_id in sorted(cited)},
            "thresholds": data["thresholds"], "severity_weights": data["severity_weights"]}


def save(output):
    rules.REPORTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = rules.REPORTS_DIR / f"analysis_{timestamp}.json"
    rules.write_json(path, output)
    rules.write_json(LATEST_JSON, output)
    return path


def load_latest():
    try:
        return json.loads(LATEST_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def coverage(data=None, latest=_MISSING):
    data = data or rules.current()
    latest = load_latest() if latest is _MISSING else latest
    covered = list((latest or {}).get("results_used") or [])
    new = [result for result in data["results_used"] if result not in set(covered)]
    return {"analysed": bool(latest), "fresh": bool(latest) and covered == data["results_used"],
            "stale": bool(new), "covered_runs": covered, "new_runs": new,
            "generated_at": (latest or {}).get("generated_at"), "run_id": (latest or {}).get("run_id")}


def latest_or_deterministic(data=None, latest=_MISSING):
    data = data or rules.current()
    latest = load_latest() if latest is _MISSING else latest
    if coverage(data, latest)["fresh"]:
        agent = {"findings": {finding["id"]: finding for finding in latest["findings"]
                               if finding.get("source") == "agent"},
                 "summary": latest.get("summary"), "strength_notes": latest.get("strength_notes", [])}
        output = build_output(data, latest["source"], latest.get("note"), agent, latest.get("model"),
                              latest.get("run_id"), latest.get("tool_calls", 0))
        output["generated_at"] = latest["generated_at"]
        return output
    return build_output(data, "analysis_fallback", "deterministic analysis (no agent run for the current results)")


def _build_graph(top, data, toolbox, tools, model_client, recorder, write):
    tools_by_name = {tool.name: tool for tool in tools}

    def prepare(_state):
        recorder.emit("node", "Prepared deterministic weaknesses.", node="prepare_weaknesses", status="completed")
        return {"messages": [HumanMessage(content=_brief(top, data))], "tool_calls_used": 0,
                "evidence_trail": [], "repair_attempts": 0}

    def investigate(state):
        response, model_name = model_client.invoke_tools(PROMPT, state["messages"], tools)
        recorder.emit("llm", f"answered by {model_name}", node="investigate", status="completed",
                      model=model_name, usage=getattr(response, "usage_metadata", None))
        return {"messages": [response], "model": model_name}

    def route_after_investigate(state):
        calls = getattr(state["messages"][-1], "tool_calls", None) or []
        return "execute_tools" if calls and state.get("tool_calls_used", 0) < MAX_TOOL_CALLS else "finalize"

    def execute_tools(state):
        used, replies, trail = state.get("tool_calls_used", 0), [], []
        for call in state["messages"][-1].tool_calls:
            name, args, call_id = call.get("name"), call.get("args") or {}, call.get("id")
            if used >= MAX_TOOL_CALLS:
                result = {"error": "tool budget exhausted"}
            else:
                used += 1
                recorder.emit("tool_call", f"{name}({json.dumps(args, default=str)})", node="execute_tools", tool=name,
                              args=args, call=f"{used}/{MAX_TOOL_CALLS}")
                if name not in tools_by_name:
                    result = {"error": f"unknown tool {name!r}"}
                else:
                    try:
                        result = compact(tools_by_name[name].invoke(args))
                    except Exception as exc:
                        result = {"error": f"{type(exc).__name__}: {exc}"[:300]}
                shown = json.dumps(result, default=str)
                recorder.emit("tool_result", shown, node="execute_tools", tool=name,
                              detail=json.dumps(result, indent=1, default=str))
                trail.append({"tool": name, "finding": shown[:300]})
            replies.append(ToolMessage(content=json.dumps(result, default=str), tool_call_id=call_id or name or "unknown",
                                       name=name or "unknown"))
        return {"messages": replies, "tool_calls_used": used, "evidence_trail": trail}

    def route_after_tools(state):
        return "finalize" if state.get("tool_calls_used", 0) >= MAX_TOOL_CALLS else "investigate"

    def finalize(state):
        instruction = ("Return one typed finding for every supplied weakness ID. Use only evidence IDs returned in "
                       "the input or tools. If a source search did not return a file:line, set fix_location to null.")
        try:
            decision, model_name = model_client.invoke_structured(
                PROMPT, [*state["messages"], HumanMessage(content=instruction)], AnalysisDecision)
            recorder.emit("llm", f"structured analysis answered by {model_name}", node="finalize",
                          status="completed", model=model_name)
            return {"decision": decision.model_dump(), "model": model_name, "validation_error": None}
        except Exception as exc:
            return {"error": f"structured analysis failed: {type(exc).__name__}: {exc}"[:300]}

    def route_after_finalize(state):
        return "fallback" if state.get("error") else "validate_findings"

    def validate_findings(state):
        try:
            decision = AnalysisDecision.model_validate(state["decision"])
            validated = check_answer(decision, top, data, toolbox.seen_locations)
            return {"validated": validated, "validation_error": None}
        except Exception as exc:
            message = f"invalid analysis decision: {type(exc).__name__}: {exc}"[:500]
            recorder.emit("thought", f"answer rejected: {message}", node="validate_findings")
            return {"validation_error": message}

    def route_after_validation(state):
        if not state.get("validation_error"):
            return "persist_report"
        return "repair_once" if state.get("repair_attempts", 0) == 0 else "fallback"

    def repair_once(state):
        return {"messages": [HumanMessage(content=f"Your typed answer was rejected: {state['validation_error']}. Correct it once.")],
                "repair_attempts": 1, "error": ""}

    def persist_report(state):
        output = build_output(data, "analysis_agent",
                              f"model {state.get('model')}, {state.get('tool_calls_used', 0)} tool calls",
                              state["validated"], state.get("model"), recorder.run_id,
                              state.get("tool_calls_used", 0))
        if write:
            save(output)
        return {"output": output}

    def fallback(state):
        reason = state.get("error") or state.get("validation_error") or "analysis graph ended without valid findings"
        output = build_output(data, "analysis_fallback", reason, model=state.get("model"),
                              run_id=recorder.run_id, tool_calls=state.get("tool_calls_used", 0))
        if write:
            save(output)
        return {"output": output}

    builder = StateGraph(AnalysisState)
    for name, function in (("prepare_weaknesses", prepare), ("investigate", investigate),
                           ("execute_tools", execute_tools), ("finalize", finalize),
                           ("validate_findings", validate_findings), ("repair_once", repair_once),
                           ("persist_report", persist_report), ("fallback", fallback)):
        builder.add_node(name, function)
    builder.add_edge(START, "prepare_weaknesses")
    builder.add_edge("prepare_weaknesses", "investigate")
    builder.add_conditional_edges("investigate", route_after_investigate)
    builder.add_conditional_edges("execute_tools", route_after_tools)
    builder.add_conditional_edges("finalize", route_after_finalize)
    builder.add_conditional_edges("validate_findings", route_after_validation)
    builder.add_edge("repair_once", "finalize")
    builder.add_edge("persist_report", END)
    builder.add_edge("fallback", END)
    return builder


def run_analysis(api_key=None, model_client=None, data=None, results=None, graph=None, log_path=None,
                 write=True, checkpointer_path=None, **_compat):
    if results is None:
        results, skipped = rules.load_results()
        data = data or rules.analyse(results, skipped)
    data = data or rules.analyse(results)
    graph = graph if graph is not None else load_graph()[0]
    top = pick_top(data["weaknesses"])
    recorder = activity.Recorder(uuid.uuid4().hex[:8], mode="analysis", prompt_version=PROMPT_VERSION)
    recorder.emit("thought", f"investigating top-{len(top)}: " + ", ".join(item["id"] for item in top),
                  node="prepare_weaknesses")

    def deterministic(reason):
        output = build_output(data, "analysis_fallback", reason, run_id=recorder.run_id)
        if write:
            save(output)
        recorder.emit("decision", f"analysis_fallback: {reason}", node="fallback", fallback=True, detail=reason)
        recorder.finish("fallback", source=output["source"], note=reason, result=output, error=reason)
        return output

    if not top:
        return deterministic("no weaknesses to investigate")
    key = api_key or load_api_key()
    if model_client is None and not key:
        return deterministic(f"{API_KEY_VAR} is not set")
    model_client = model_client or GeminiModels(key)
    toolbox = AnalysisToolbox(graph, data, results)
    tools = analysis_tools(toolbox)
    builder = _build_graph(top, data, toolbox, tools, model_client, recorder, write)
    try:
        kwargs = {"path": Path(checkpointer_path)} if checkpointer_path else {}
        with sqlite_checkpointer(**kwargs) as checkpointer:
            workflow = builder.compile(checkpointer=checkpointer)
            state = workflow.invoke({}, {"configurable": {"thread_id": recorder.run_id},
                                         "recursion_limit": MAX_GRAPH_STEPS})
    except Exception as exc:
        return deterministic(f"LangGraph analysis failed: {type(exc).__name__}: {exc}"[:300])
    output = state["output"]
    fallback = output["source"] != "analysis_agent"
    recorder.emit("decision", (f"analysis_fallback: {output['note']}" if fallback else
                                f"{len(state.get('validated', {}).get('findings', {}))} findings from the agent"),
                  node="fallback" if fallback else "persist_report", fallback=fallback)
    recorder.finish("fallback" if fallback else "completed", model=output.get("model"),
                    source=output["source"], note=output["note"], result=output,
                    error=output["note"] if fallback else None)
    return output
