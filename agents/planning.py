"""LangGraph planning workflow over immutable deterministic candidates."""
import json
import uuid
from collections import Counter
from operator import add
from pathlib import Path
from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

import activity
import ranker
from agents.checkpoint import sqlite_checkpointer
from agents.model import API_KEY_VAR, GeminiModels, load_api_key
from agents.models import PlanningDecision
from agents.tools import PlanningToolbox, compact, planning_tools
from discovery import load_flows, load_graph
from metrics import load_baseline
from policy import PROTECTED, blast_radius, validate

TOP_K = 3
MAX_TOOL_CALLS = 8
MAX_GRAPH_STEPS = 28
PROMPT_VERSION = "planning-langgraph-v1"
TRAFFIC_SPIKE_GOAL = "capacity"
METRICS = ("error_rate", "p95_ms")
DIRECTIONS = ("up", "down", "flat")
PROMPT = (Path(__file__).parent / "prompts" / "planning.md").read_text(encoding="utf-8")


class CapabilityUnavailable(ValueError):
    pass


class PlanningState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    tool_calls_used: int
    evidence: Annotated[list[dict], add]
    decision: dict
    result: dict
    error: str
    model: str | None


def top_k(ranking, k):
    seen, selected = set(), []
    for candidate in ranking:
        if candidate["target"] not in seen:
            seen.add(candidate["target"])
            selected.append(candidate)
    selected = selected[:k]
    for fault in sorted({candidate["fault"] for candidate in ranking}):
        if len(selected) < 2 or any(candidate["fault"] == fault for candidate in selected):
            continue
        extra = next((candidate for candidate in ranking
                      if candidate["fault"] == fault and candidate["target"] not in {row["target"] for row in selected}), None)
        extra = extra or next((candidate for candidate in ranking if candidate["fault"] == fault and candidate not in selected), None)
        if extra:
            counts = Counter(candidate["fault"] for candidate in selected)
            replace = next((index for index in range(len(selected) - 1, -1, -1)
                            if counts[selected[index]["fault"]] > 1), len(selected) - 1)
            selected[replace] = extra
    return selected


def _brief(candidates, graph, goal):
    rows = []
    for candidate in candidates:
        target = candidate["target"]
        rows.append({
            "id": candidate["id"], "risk": candidate["risk"], "info": candidate["info"],
            "priority": candidate["priority"], "why": candidate["why"], "outage": candidate["outage"],
            "top_factors": [f"{factor['label']} ({factor['contribution']:+.2f})" for factor in candidate["factors"][:4]],
            "callers": {source: ranker._mode(data) for source, _, data in graph.in_edges(target, data=True)},
            "blast_radius": blast_radius(graph, target),
        })
    return json.dumps({"operator_goal": goal, "candidates": rows, "protected_services": sorted(PROTECTED)}, indent=1)


def _default_story(candidate, graph):
    target, duration = candidate["target"], candidate["duration_s"]
    callers = sorted(source for source, _, edge in graph.in_edges(target, data=True) if ranker._mode(edge) == "sync")
    who = ", ".join(callers) or "no direct synchronous callers"
    if candidate["fault"] == "container_stop":
        hypothesis = (f"Stopping {target} for {duration}s makes calls to it fail: the error rate of its callers "
                      f"({who}) rises, and everything returns to baseline once {target} restarts.")
        predictions = [{"svc": service, "metric": "error_rate", "direction": "up", "min_factor": 2}
                       for service in callers]
    elif candidate["fault"] == "network_delay":
        hypothesis = (f"Adding {candidate['magnitude']}ms network delay to {target} for {duration}s raises p95 latency "
                      f"of {target}'s callers ({who}) and latency returns to baseline after rollback.")
        predictions = [{"svc": service, "metric": "p95_ms", "direction": "up", "min_factor": 1.5}
                       for service in callers]
    else:
        parameters = candidate["parameters"]
        hypothesis = (f"Increasing the fixed frontend journey load from {parameters['baseline_users']} to "
                      f"{parameters['peak_users']} users for {duration}s reveals saturation, then restores the original load.")
        predictions = [{"svc": target, "metric": "p95_ms", "direction": "up", "min_factor": 1.2}]
    return hypothesis, predictions or [{"svc": target, "metric": "error_rate", "direction": "flat", "min_factor": 1}]


def _plan(candidate, hypothesis, reasoning):
    plan = {"target": candidate["target"], "fault": candidate["fault"], "magnitude": candidate["magnitude"],
            "duration_s": candidate["duration_s"], "hypothesis": hypothesis, "reasoning": reasoning}
    plan.update({key: candidate[key] for key in ("parameters", "candidate_id", "capability_version") if key in candidate})
    return plan


def _top_rows(candidates, decision=None):
    adjusted = {row.candidate_id: row for row in (decision.adjusted_ranking if decision else [])}
    rows = []
    for candidate in candidates:
        row = adjusted.get(candidate["id"])
        rows.append({"candidate_id": candidate["id"], "rank": candidate["rank"], "risk": candidate["risk"],
                     "info": candidate["info"], "priority": candidate["priority"], "why": candidate["why"],
                     "outage": candidate["outage"], "factors": candidate["factors"],
                     "adjusted_risk": row.adjusted_risk if row else None,
                     "adjusted_priority": row.adjusted_priority if row else None,
                     "evidence": [item.model_dump() for item in row.evidence] if row else [],
                     "reason": row.reason if row else None})
    return rows


def _fallback(candidates, graph, recorder, reason, evidence=(), model=None):
    best = candidates[0]
    hypothesis, predictions = _default_story(best, graph)
    plan = _plan(best, hypothesis, f"Ranker #1 (priority {best['priority']}): {best['why']}. "
                                  f"LLM investigation not used: {reason}")
    rows = _top_rows(candidates)
    rows[0]["evidence"] = list(evidence)[:10]
    output = {"source": "ranker_fallback", "note": reason, "model": model, "run_id": recorder.run_id,
              "tool_calls": len(evidence), "capped": len(evidence) >= MAX_TOOL_CALLS, "chosen": best["id"],
              "predictions": predictions, "top_k": rows, "plan": plan}
    recorder.emit("decision", f"ranker_fallback -> {best['id']}: {reason}", node="fallback", fallback=True,
                  detail=reason)
    recorder.finish("fallback", model=model, source=output["source"], note=reason, result=output, error=reason)
    return output


def _build_graph(candidates, graph, toolbox, tools, model_client, recorder, goal):
    tools_by_name = {tool.name: tool for tool in tools}

    def prepare(_state):
        recorder.emit("node", "Prepared deterministic candidate shortlist.", node="prepare_candidates", status="completed")
        return {"messages": [HumanMessage(content=_brief(candidates, graph, goal))],
                "tool_calls_used": 0, "evidence": []}

    def investigate(state):
        recorder.emit("node", "Asking the model what read-only evidence to inspect.", node="investigate", status="started")
        response, model_name = model_client.invoke_tools(PROMPT, state["messages"], tools)
        usage = getattr(response, "usage_metadata", None)
        recorder.emit("llm", f"answered by {model_name}", node="investigate", status="completed",
                      model=model_name, usage=usage)
        return {"messages": [response], "model": model_name}

    def route_after_investigate(state):
        calls = getattr(state["messages"][-1], "tool_calls", None) or []
        return "execute_tools" if calls and state.get("tool_calls_used", 0) < MAX_TOOL_CALLS else "finalize"

    def execute_tools(state):
        used = state.get("tool_calls_used", 0)
        replies, evidence = [], []
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
                evidence.append({"tool": name, "finding": f"{name}({json.dumps(args)}): {shown}"[:300]})
            replies.append(ToolMessage(content=json.dumps(result, default=str), tool_call_id=call_id or name or "unknown",
                                       name=name or "unknown"))
        return {"messages": replies, "tool_calls_used": used, "evidence": evidence}

    def route_after_tools(state):
        return "finalize" if state.get("tool_calls_used", 0) >= MAX_TOOL_CALLS else "investigate"

    def finalize(state):
        final_prompt = ("Select exactly one candidate from this immutable set: "
                        + json.dumps([candidate["id"] for candidate in candidates])
                        + ". Return the typed decision. adjusted_ranking may contain the candidates you assessed; "
                          "predictions must name services in the supplied topology.")
        try:
            decision, model_name = model_client.invoke_structured(
                PROMPT, [*state["messages"], HumanMessage(content=final_prompt)], PlanningDecision)
            recorder.emit("llm", f"structured decision answered by {model_name}", node="finalize",
                          status="completed", model=model_name)
            return {"decision": decision.model_dump(), "model": model_name}
        except Exception as exc:
            return {"error": f"structured planning failed: {type(exc).__name__}: {exc}"[:300]}

    def route_after_finalize(state):
        return "fallback" if state.get("error") else "validate_decision"

    def validate_decision(state):
        try:
            decision = PlanningDecision.model_validate(state["decision"])
            candidates_by_id = {candidate["id"]: candidate for candidate in candidates}
            if decision.candidate_id not in candidates_by_id:
                raise ValueError(f"candidate {decision.candidate_id!r} is not in the shortlist")
            unknown = [prediction.svc for prediction in decision.predictions if prediction.svc not in graph]
            if unknown:
                raise ValueError("predictions contain unknown services: " + ", ".join(unknown))
            candidate = candidates_by_id[decision.candidate_id]
            adjustment = next((row for row in decision.adjusted_ranking if row.candidate_id == candidate["id"]), None)
            reasoning = adjustment.reason if adjustment else candidate["why"]
            plan = _plan(candidate, decision.hypothesis, reasoning)
            check = validate(plan, graph)
            if not check["valid"]:
                raise ValueError("choice failed policy: " + "; ".join(check["reasons"]))
            result = {"chosen": candidate["id"], "plan": plan,
                      "predictions": [prediction.model_dump() for prediction in decision.predictions],
                      "top_k": _top_rows(candidates, decision)}
            return {"result": result}
        except Exception as exc:
            return {"error": f"invalid planning decision: {type(exc).__name__}: {exc}"[:300]}

    def route_after_validation(state):
        return "fallback" if state.get("error") else "complete"

    def fallback(state):
        return {"result": {"fallback_reason": state.get("error") or "planning graph ended without a result"}}

    def complete(state):
        return {}

    builder = StateGraph(PlanningState)
    builder.add_node("prepare_candidates", prepare)
    builder.add_node("investigate", investigate)
    builder.add_node("execute_tools", execute_tools)
    builder.add_node("finalize", finalize)
    builder.add_node("validate_decision", validate_decision)
    builder.add_node("fallback", fallback)
    builder.add_node("complete", complete)
    builder.add_edge(START, "prepare_candidates")
    builder.add_edge("prepare_candidates", "investigate")
    builder.add_conditional_edges("investigate", route_after_investigate)
    builder.add_conditional_edges("execute_tools", route_after_tools)
    builder.add_conditional_edges("finalize", route_after_finalize)
    builder.add_conditional_edges("validate_decision", route_after_validation)
    builder.add_edge("fallback", END)
    builder.add_edge("complete", END)
    return builder


def run_agent(k=TOP_K, api_key=None, model_client=None, graph=None, baseline=None, flows=None, history=None,
              log_path=None, goal=None, traffic_capability=None, checkpointer_path=None, **_compat):
    if graph is None:
        graph, _ = load_graph()
    baseline = load_baseline() if baseline is None else baseline
    flows = load_flows() if flows is None else flows
    history = ranker.load_history() if history is None else history
    ranking = ranker.rank(graph, baseline, flows, history, traffic_capability=traffic_capability)
    if goal == TRAFFIC_SPIKE_GOAL:
        ranking = [candidate for candidate in ranking if candidate["fault"] == "traffic_spike"]
        if not ranking:
            raise CapabilityUnavailable("Traffic-spike capability is not ready for a capacity experiment")
        candidates = ranking[:1]
    else:
        candidates = top_k(ranking, k)
    if not candidates:
        raise ValueError("ranker produced no valid candidates")

    recorder = activity.Recorder(uuid.uuid4().hex[:8], mode="planning", prompt_version=PROMPT_VERSION)
    recorder.emit("thought", "investigating top-%d: %s" %
                  (len(candidates), ", ".join(candidate["id"] for candidate in candidates)),
                  node="prepare_candidates", detail=json.dumps(candidates, indent=1, default=str)[:6000])
    key = api_key or load_api_key()
    if model_client is None and not key:
        return _fallback(candidates, graph, recorder, f"{API_KEY_VAR} is not set")
    model_client = model_client or GeminiModels(key)
    toolbox = PlanningToolbox(graph, baseline, history, ranking)
    tools = planning_tools(toolbox)
    builder = _build_graph(candidates, graph, toolbox, tools, model_client, recorder, goal)
    try:
        path = checkpointer_path or None
        kwargs = {"path": Path(path)} if path else {}
        with sqlite_checkpointer(**kwargs) as checkpointer:
            workflow = builder.compile(checkpointer=checkpointer)
            state = workflow.invoke({}, {"configurable": {"thread_id": recorder.run_id},
                                         "recursion_limit": MAX_GRAPH_STEPS})
    except Exception as exc:
        return _fallback(candidates, graph, recorder,
                         f"LangGraph planning failed: {type(exc).__name__}: {exc}"[:300])
    if state.get("error") or state.get("result", {}).get("fallback_reason"):
        reason = state.get("error") or state["result"]["fallback_reason"]
        return _fallback(candidates, graph, recorder, reason, state.get("evidence", []), state.get("model"))
    result = state["result"]
    output = {"source": "rank_agent", "note": f"model {state.get('model')}, {state.get('tool_calls_used', 0)} tool calls",
              "model": state.get("model"), "capped": state.get("tool_calls_used", 0) >= MAX_TOOL_CALLS,
              "run_id": recorder.run_id, "tool_calls": state.get("tool_calls_used", 0), **result}
    recorder.emit("decision", f"chose {result['chosen']} after {output['tool_calls']} tool calls",
                  node="validate_decision", fallback=False, detail=json.dumps(result, indent=1, default=str))
    recorder.finish("completed", model=output["model"], source=output["source"], note=output["note"], result=output)
    return output
