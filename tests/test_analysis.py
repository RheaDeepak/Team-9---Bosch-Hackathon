import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

import networkx as nx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analysis  # noqa: E402
import analysis_agent
import report

ROOT = Path(__file__).resolve().parents[1]
SYNC = {"mode": "sync", "type": "sync-grpc"}


def graph():
    """frontend-proxy -> frontend -> {cart, product-catalog, checkout}; checkout -> {cart, email}."""
    g = nx.DiGraph()
    for u, v in [("frontend-proxy", "frontend"), ("frontend", "cart"), ("frontend", "product-catalog"),
                 ("frontend", "checkout"), ("checkout", "cart"), ("checkout", "email")]:
        g.add_edge(u, v, **SYNC)
    return g


def baseline():
    stat = lambda m: {"mean": m, "std": 0.0, "max": m, "n": 20}
    svc = {"request_rate": stat(1.0), "error_rate": stat(0.0), "p95_ms": stat(30.0)}
    return {"services": {s: svc for s in ("frontend", "cart", "checkout", "email", "product-catalog")},
            "containers": {s: {"running": stat(1)} for s in ("frontend", "cart", "checkout", "email", "product-catalog")}}


def row(service, err=(0.0, 0.0), p95=(30.0, 30.0)):
    ch = lambda b, p: None if b is None or p is None else p - b
    return {"service": service, "error_rate": {"baseline": err[0], "peak": err[1], "change": ch(*err)},
            "p95_ms": {"baseline": p95[0], "peak": p95[1], "change": ch(*p95)}}


def result(rid, target, fault, impact, magnitude=0, aborted=False, reason="duration elapsed (60s)", elapsed=60.0,
           recovery=None, claims=()):
    d = {"plan": {"target": target, "fault": fault, "magnitude": magnitude, "duration_s": 60, "hypothesis": "h", "reasoning": "r"},
         "plan_source": "test", "started_at": "2026-10-10T10:00:00+00:00", "forced_abort_test": False,
         "guard": {"aborted": aborted, "reason": reason, "elapsed_s": elapsed}, "rollback_verified": True,
         "recovery": recovery or {"recovered": True, "seconds": 5.0}, "impact": impact,
         "verdict": {"verdict": "held", "explanation": "e", "claims": list(claims)}}
    return analysis.normalise_result(d, rid)


CART_DOWN = lambda rid: result(rid, "cart", "container_stop", [row("cart"), row("checkout", err=(0.0, 100.0))])
EMAIL_DOWN = result("20261010T120000Z", "email", "container_stop", [row("email"), row("checkout"), row("frontend", p95=(30, 40))])


def analyse(results):
    g = graph()
    return analysis.analyse(results, [], g, None, baseline(), {})


class RulesTest(unittest.TestCase):
    def test_delay_run_shows_amplification_and_silent_degradation(self):
        d = json.loads((ROOT / "results" / "20261010T100023Z.json").read_text(encoding="utf-8"))
        r = analysis.normalise_result(d, "20261010T100023Z")
        g = nx.DiGraph([("frontend-proxy", "frontend"), ("frontend", "product-catalog"), ("recommendation", "product-catalog"),
                        ("frontend", "recommendation"), ("checkout", "product-catalog")])
        found = {c["pattern"]: c for c in analysis.detect(r, g)}
        self.assertEqual(found["latency_amplification"]["caller"], "frontend")   # nearest amplified caller
        self.assertEqual(found["silent_degradation"]["caller"], "frontend")
        self.assertIn("20261010T100023Z#amplification.frontend", found["latency_amplification"]["evidence_ids"])
        self.assertIn("blast_radius_spread", found)                                  # frontend-proxy is 2 calls away

    def test_container_stop_with_caller_errors_is_a_hard_dependency(self):
        found = {c["pattern"]: c for c in analysis.detect(CART_DOWN("20261010T110000Z"), graph())}
        self.assertEqual(found["hard_dependency"]["edge"], "checkout->cart")

    def test_container_stop_without_caller_impact_is_a_strength(self):
        a = analyse([EMAIL_DOWN])
        self.assertEqual([s["id"] for s in a["strengths"]], ["strength:email:container_stop"])
        self.assertEqual(a["weaknesses"], [])

    def test_a_run_stopped_early_by_a_person_is_not_a_strength(self):
        early = result("20261010T130000Z", "email", "container_stop", [row("checkout")], aborted=True,
                       reason="stopped by user (STOP button)", elapsed=4.6)
        self.assertEqual(analyse([early])["strengths"], [])

    def test_same_weakness_in_two_runs_merges_with_higher_confidence(self):
        one = analyse([CART_DOWN("20261010T110000Z")])["weaknesses"]
        two = analyse([CART_DOWN("20261010T110000Z"), CART_DOWN("20261010T111000Z")])["weaknesses"]
        hard1 = [w for w in one if w["pattern"] == "hard_dependency"]
        hard2 = [w for w in two if w["pattern"] == "hard_dependency"]
        self.assertEqual((len(hard1), len(hard2)), (1, 1))
        self.assertEqual(hard2[0]["runs"], ["20261010T110000Z", "20261010T111000Z"])
        self.assertGreater(hard2[0]["confidence"], hard1[0]["confidence"])
        self.assertGreaterEqual(hard2[0]["severity_score"], hard1[0]["severity_score"])

    def test_numbers_check(self):
        item = {"value": {"baseline": 33.6, "peak": 15003.2, "change": 14969.6}}
        self.assertTrue(analysis.numbers_supported("p95 went from 33.6 to 15003 ms (see 20261010T095437Z#impact.x)", [item]))
        self.assertFalse(analysis.numbers_supported("p95 went up by 9000 ms", [item]))


class SeverityTest(unittest.TestCase):
    def test_bands_are_ordered(self):
        labels = [analysis.severity_label(x) for x in (95, 70, 69.9, 50, 49.9, 30, 29.9, 0)]
        self.assertEqual(labels, ["Critical", "Critical", "High", "High", "Medium", "Medium", "Low", "Low"])
        self.assertEqual(analysis.SEVERITY_ORDER, ("Critical", "High", "Medium", "Low"))

    def test_weights_change_the_order(self):
        # cart: on the checkout path, 100% caller errors. product-catalog: a small latency-only problem.
        runs = [CART_DOWN("20261010T110000Z"),
                result("20261010T120000Z", "product-catalog", "network_delay", [row("product-catalog"), row("frontend", p95=(30, 400))],
                       magnitude=100)]
        by_user = copy.deepcopy(analysis.SEVERITY_WEIGHTS)
        by_user["components"] = {"user_impact": 1.0, "criticality": 0.0, "spread": 0.0, "recovery": 0.0}
        by_spread = copy.deepcopy(analysis.SEVERITY_WEIGHTS)
        by_spread["components"] = {"user_impact": 0.0, "criticality": 0.0, "spread": 1.0, "recovery": 0.0}
        first = lambda w: analysis.analyse(runs, [], graph(), None, baseline(), {}, weights=w)["weaknesses"][0]["service"]
        self.assertEqual(first(by_user), "cart")                 # 100 error points beat a latency rise
        self.assertEqual(first(by_spread), "product-catalog")    # cart's blast radius is larger, so its spread share is smaller


class ScoresTest(unittest.TestCase):
    def test_untested_service_is_not_tested_never_100(self):
        s = analyse([EMAIL_DOWN])["scores"]["services"]
        self.assertEqual((s["product-catalog"]["score"], s["product-catalog"]["status"]), (None, "not tested"))
        self.assertIsNotNone(s["email"]["score"])

    def test_only_early_stopped_runs_give_insufficient_data(self):
        early = result("20261010T130000Z", "checkout", "container_stop", [row("frontend")], aborted=True,
                       reason="stopped by user (STOP button)", elapsed=4.6)
        s = analyse([early])["scores"]
        self.assertEqual(s["services"]["checkout"]["status"], "insufficient data")
        self.assertIsNone(s["overall"]["score"])


class FakeModels:
    def __init__(self, tool_responses=(), decisions=()):
        self.tool_responses = iter(tool_responses)
        self.decisions = iter(decisions)

    def invoke_tools(self, system, messages, tools):
        value = next(self.tool_responses)
        if isinstance(value, Exception):
            raise value
        return value, "fake-model"

    def invoke_structured(self, system, messages, schema):
        value = next(self.decisions)
        if isinstance(value, Exception):
            raise value
        return schema.model_validate(value), "fake-model"


def ready_message():
    from langchain_core.messages import AIMessage
    return AIMessage(content="ready")


def tool_message(*calls):
    from langchain_core.messages import AIMessage
    return AIMessage(content="", tool_calls=[
        {"name": name, "args": args, "id": f"call-{index}", "type": "tool_call"}
        for index, (name, args) in enumerate(calls, 1)
    ])


class AnalysisGraphTest(unittest.TestCase):
    def setUp(self):
        from unittest import mock

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.results = [CART_DOWN("20261010T110000Z"), EMAIL_DOWN]
        self.data = analyse(self.results)
        self.top = analysis_agent.pick_top(self.data["weaknesses"])
        patcher = mock.patch.object(__import__("activity"), "SESSIONS_DIR", self.directory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def decision(self, **change):
        findings = []
        for weakness in self.top:
            finding = {
                "id": weakness["id"],
                "title": weakness["title"],
                "service": weakness["service"],
                "edge": weakness["edge"],
                "pattern": weakness["pattern"],
                "severity": weakness["severity"],
                "severity_score": weakness["severity_score"],
                "root_cause": weakness["detail"],
                "evidence_ids": [weakness["evidence_ids"][0]],
                "fix": weakness["fix"],
                "fix_location": None,
                "verify_by": weakness["verify_by"],
                "confidence": weakness["confidence"],
                "agent_adjustment": None,
            }
            if weakness is self.top[0]:
                finding.update(change)
            findings.append(finding)
        return {"findings": findings, "strengths": [], "summary": "Evidence-backed weaknesses."}

    def run_agent(self, fake):
        return analysis_agent.run_analysis(
            model_client=fake,
            data=self.data,
            results=self.results,
            graph=graph(),
            write=False,
            checkpointer_path=self.directory / "agent_runs.sqlite3",
        )

    def test_tool_route_and_valid_findings(self):
        fake = FakeModels(
            [tool_message(("get_evidence", {"evidence_id": self.top[0]["evidence_ids"][0]})), ready_message()],
            [self.decision()],
        )
        output = self.run_agent(fake)
        self.assertEqual((output["source"], output["tool_calls"]), ("analysis_agent", 1))
        self.assertEqual({finding["source"] for finding in output["findings"]}, {"agent"})

    def test_validation_error_is_repaired_once(self):
        bad = self.decision(evidence_ids=["unknown#evidence"])
        output = self.run_agent(FakeModels([ready_message()], [bad, self.decision()]))
        self.assertEqual(output["source"], "analysis_agent")

    def test_second_invalid_answer_falls_back(self):
        bad = self.decision(evidence_ids=["unknown#evidence"])
        output = self.run_agent(FakeModels([ready_message()], [bad, bad]))
        self.assertEqual(output["source"], "analysis_fallback")
        self.assertIn("unknown evidence", output["note"])
        self.assertEqual({finding["source"] for finding in output["findings"]}, {"deterministic"})

    def test_provider_and_structured_output_failures_fall_back(self):
        for fake in (
            FakeModels([RuntimeError("provider unavailable")], []),
            FakeModels([ready_message()], [ValueError("malformed output")]),
        ):
            self.assertEqual(self.run_agent(fake)["source"], "analysis_fallback")

    def test_severity_change_requires_grounded_adjustment(self):
        current = self.top[0]["severity"]
        replacement = "Critical" if current not in ("Critical", "High") else "Low"
        bad = self.decision(severity=replacement)
        output = self.run_agent(FakeModels([ready_message()], [bad, bad]))
        self.assertEqual(output["source"], "analysis_fallback")
        self.assertIn("severity", output["note"])

    def test_unsupported_number_is_replaced_and_unverified_location_is_dropped(self):
        decision = self.decision(
            root_cause="Latency rose by 999999 milliseconds.",
            fix_location="src/checkout/invented.go:999",
        )
        output = self.run_agent(FakeModels([ready_message()], [decision]))
        finding = next(item for item in output["findings"] if item["id"] == self.top[0]["id"])
        self.assertTrue(finding["root_cause_replaced"])
        self.assertTrue(finding["fix_location_dropped"])
        self.assertIsNone(finding["fix_location"])

    def test_multiple_calls_share_ten_call_budget(self):
        evidence_id = self.top[0]["evidence_ids"][0]
        calls = [("get_evidence", {"evidence_id": evidence_id}) for _ in range(14)]
        output = self.run_agent(FakeModels([tool_message(*calls)], [self.decision()]))
        self.assertEqual(output["tool_calls"], analysis_agent.MAX_TOOL_CALLS)

    def test_trace_queries_are_limited_to_stored_experiment_windows(self):
        toolbox = analysis_agent.Toolbox(graph(), self.data, self.results)
        result = toolbox.call("get_trace_summary", {"service": "checkout", "start_ms": 1, "end_ms": 2})
        self.assertIn("stored experiment window", result["error"])

    def test_no_weaknesses_uses_deterministic_output_without_model(self):
        empty = analyse([])
        output = analysis_agent.run_analysis(
            model_client=FakeModels([], []), data=empty, results=[], graph=graph(), write=False,
            checkpointer_path=self.directory / "empty.sqlite3",
        )
        self.assertEqual(output["source"], "analysis_fallback")
        self.assertEqual(output["findings"], [])


class OutputCoverageTest(unittest.TestCase):
    def test_summary_and_coverage_remain_compatible(self):
        one = analyse([CART_DOWN("20261010T110000Z")])
        output = analysis_agent.build_output(one, "analysis_fallback", "test")
        self.assertEqual(output["source"], "analysis_fallback")
        self.assertEqual(output["results_used"], ["20261010T110000Z"])
        self.assertTrue(analysis_agent.coverage(one, output)["fresh"])

    def test_report_renderer_accepts_graph_output(self):
        data = analyse([CART_DOWN("20261010T110000Z")])
        output = analysis_agent.build_output(data, "analysis_fallback", "test")
        markdown = report.render_md(output)
        self.assertIn("Resilience", markdown)
        self.assertIn("Evidence", markdown)


if __name__ == "__main__":
    unittest.main()
