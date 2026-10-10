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


def reply(text):
    return {"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}


class AnalysisAgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "agent_log.jsonl"
        self.results = [CART_DOWN("20261010T110000Z"), EMAIL_DOWN]
        self.data = analyse(self.results)
        self.top = self.data["weaknesses"][0]

    def run_agent(self, *texts):
        it = iter(texts)
        post = lambda key, body, models: (reply(next(it)), "fake-model")
        return analysis_agent.run_analysis(api_key="k", post=post, data=self.data, results=self.results, graph=graph(),
                                           log_path=self.log, write=False)

    def answer(self, **change):
        w = self.top
        f = {"id": w["id"], "title": "Checkout fails when cart is down", "service": w["service"], "edge": w["edge"],
             "pattern": w["pattern"], "severity": w["severity"], "severity_score": w["severity_score"],
             "root_cause": "checkout error rate went from 0.0% to 100.0% when cart stopped.",
             "evidence_ids": ["20261010T110000Z#impact.checkout.error_rate"], "fix": "Add a fallback on checkout -> cart.",
             "fix_location": None, "verify_by": "Re-run container_stop on cart.", "confidence": 0.9, "agent_adjustment": None}
        f.update(change)
        return json.dumps({"findings": [f], "strengths": [], "summary": "One hard dependency."})

    def modes(self):
        return {json.loads(line)["mode"] for line in self.log.read_text().splitlines()}

    def test_garbage_twice_falls_back(self):
        out = self.run_agent("not json at all", "still not json")
        self.assertEqual(out["source"], "analysis_fallback")
        self.assertEqual({f["source"] for f in out["findings"]}, {"deterministic"})
        self.assertEqual(self.modes(), {"analysis"})

    def test_unknown_evidence_id_is_reasked_then_falls_back(self):
        bad = self.answer(evidence_ids=["20261010T110000Z#impact.nowhere.p95_ms"])
        out = self.run_agent(bad, bad)
        self.assertEqual(out["source"], "analysis_fallback")
        self.assertIn("unknown evidence", out["note"])

    def test_reask_once_recovers(self):
        out = self.run_agent(self.answer(evidence_ids=["bogus"]), self.answer())
        self.assertEqual(out["source"], "analysis_agent")
        top = next(f for f in out["findings"] if f["id"] == self.top["id"])
        self.assertEqual((top["source"], top["title"]), ("agent", "Checkout fails when cart is down"))

    def test_invented_numbers_are_replaced_by_the_rule_text(self):
        out = self.run_agent(self.answer(root_cause="checkout error rate hit 73.0% within 12 s."))
        top = next(f for f in out["findings"] if f["id"] == self.top["id"])
        self.assertTrue(top["root_cause_replaced"])
        self.assertEqual(top["root_cause"], self.top["detail"])

    def test_severity_cannot_jump_two_levels_or_change_silently(self):
        order = analysis.SEVERITY_ORDER
        i = order.index(self.top["severity"])
        if i >= 2:
            jump = self.answer(severity=order[i - 2], agent_adjustment="worse than it looks")
            self.assertEqual(self.run_agent(jump, jump)["source"], "analysis_fallback")
        if i + 1 < len(order):
            silent = self.answer(severity=order[i + 1])
            self.assertEqual(self.run_agent(silent, silent)["source"], "analysis_fallback")
            ok = self.answer(severity=order[i + 1], agent_adjustment="checkout already retries once")
            self.assertEqual(self.run_agent(ok)["source"], "analysis_agent")

    def test_fix_location_must_come_from_search_source(self):
        out = self.run_agent(self.answer(fix_location="src/checkout/main.go:999"))
        top = next(f for f in out["findings"] if f["id"] == self.top["id"])
        self.assertIsNone(top["fix_location"])
        self.assertTrue(top["fix_location_dropped"])


class ReportTest(unittest.TestCase):
    def test_renders_with_zero_and_several_findings(self):
        empty = analysis_agent.build_output(analyse([]), "analysis_fallback", "test")
        slow = result("20261010T120000Z", "product-catalog", "network_delay",
                      [row("product-catalog"), row("frontend", p95=(30, 1030))], magnitude=200)
        many = analysis_agent.build_output(analyse([CART_DOWN("20261010T110000Z"), EMAIL_DOWN, slow]), "analysis_fallback", "test")
        self.assertIn("No weaknesses were found", report.render_md(empty))
        self.assertIn("No weaknesses were found", report.render_html(empty))
        md, page = report.render_md(many), report.render_html(many)
        self.assertGreater(len(many["findings"]), 1)
        for f in many["findings"]:
            self.assertIn(f["title"], md)
        self.assertIn("Callers kept working during container_stop on email", page)
        self.assertNotIn("<script", page)                      # self-contained, no external assets
        self.assertNotIn("src=", page)
        self.assertIn("not tested", md)


if __name__ == "__main__":
    unittest.main()
