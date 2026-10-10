import json
import unittest
from unittest.mock import patch

import llm
import planner
import run_experiment as exp


def row(service, err_base=0.0, err_peak=0.0, p95_base=10.0, p95_peak=10.0):
    ch = lambda b, p: None if b is None or p is None else p - b
    return {"service": service,
            "error_rate": {"baseline": err_base, "peak": err_peak, "change": ch(err_base, err_peak)},
            "p95_ms": {"baseline": p95_base, "peak": p95_peak, "change": ch(p95_base, p95_peak)}}


RECOVERED = {"recovered": True, "seconds": 0.0}
NETDELAY = {"target": "cart", "fault": "network_delay", "magnitude": 250}


class AmplificationTests(unittest.TestCase):
    def test_ratio_is_always_reported_and_flagged_above_1_5x(self):
        impact = [row("cart", p95_base=2, p95_peak=2), row("frontend", p95_base=30, p95_peak=30 + 475), row("checkout", p95_base=60, p95_peak=60 + 250)]
        by = {a["service"]: a for a in exp.amplification(NETDELAY, impact)}
        self.assertEqual((by["cart"]["ratio"], by["cart"]["amplified"], by["cart"]["role"]), (0.0, False, "target"))
        self.assertEqual((by["frontend"]["ratio"], by["frontend"]["amplified"], by["frontend"]["role"]), (1.9, True, "caller"))
        self.assertEqual((by["checkout"]["ratio"], by["checkout"]["amplified"]), (1.0, False))

    def test_exactly_1_5x_is_not_amplified_and_decreases_never_go_negative(self):
        a = {x["service"]: x for x in exp.amplification(NETDELAY, [row("a", p95_base=0, p95_peak=375), row("b", p95_base=100, p95_peak=50)])}
        self.assertFalse(a["a"]["amplified"])
        self.assertEqual(a["b"]["ratio"], 0.0)

    def test_services_without_data_are_skipped_and_other_faults_have_no_ratio(self):
        self.assertEqual(exp.amplification(NETDELAY, [row("a", p95_peak=None)]), [])
        self.assertEqual(exp.amplification({"fault": "container_stop", "magnitude": 0, "target": "cart"}, [row("a", p95_peak=999)]), [])


class ScoreTests(unittest.TestCase):
    def test_no_impact_scores_100(self):
        s = exp.score_experiment([row("a")], RECOVERED, 100.0, 60)
        self.assertEqual((s["resilience_score"], s["severity"], s["confidence"]), (100, "Low", "High confidence"))

    def test_weights_error_latency_availability_recovery(self):
        worst = exp.score_experiment([row("a", err_peak=100, p95_peak=1000)], {"recovered": False, "seconds": None}, 0.0, 60)
        self.assertEqual((worst["impact_score"], worst["resilience_score"], worst["severity"]), (100.0, 0, "Critical"))
        errors_only = exp.score_experiment([row("a", err_peak=50)], RECOVERED, 100.0, 60)
        self.assertEqual(errors_only["impact_score"], 17.5)          # 0.35 weight * 50%

    def test_severity_bands(self):
        cases = (({"err_peak": 50}, 17.5, "Low"),                         # 0.35 * 50
                 ({"err_peak": 100}, 35.0, "Moderate"),                    # 0.35 * 100
                 ({"err_peak": 100, "p95_peak": 1000}, 60.0, "High"))      # + 0.25 for a saturated latency rise
        for kwargs, impact, label in cases:
            s = exp.score_experiment([row("a", **kwargs)], RECOVERED, 100.0, 60)
            self.assertEqual((s["impact_score"], s["severity"]), (impact, label), s)

    def test_unrecovered_counts_as_the_full_recovery_timeout(self):
        s = exp.score_experiment([row("a")], {"recovered": False, "seconds": None}, 100.0, 60)
        self.assertEqual(s["components"]["recovery_impact"], 1.0)

    def test_missing_data_lowers_confidence_and_is_listed(self):
        impact = [row("a", err_peak=None, p95_peak=None), row("b", err_peak=None, p95_peak=None), row("c")]
        s = exp.score_experiment(impact, RECOVERED, 100.0, 60)
        self.assertEqual(s["confidence"], "Low confidence")
        self.assertIn("No data for: a, b", s["limitations"][0])
        short = exp.score_experiment([row("a")], RECOVERED, 100.0, 5)
        self.assertEqual(short["confidence"], "Medium confidence")


PLAN_JSON = json.dumps({"target": "cart", "fault": "container_stop", "magnitude": 0, "duration_s": 60, "hypothesis": "h", "reasoning": "r"})


class PlannerTests(unittest.TestCase):
    def test_parse_plan_strips_code_fences_and_surrounding_prose(self):
        self.assertEqual(planner.parse_plan("```json\n" + PLAN_JSON + "\n```")["target"], "cart")
        self.assertEqual(planner.parse_plan("Here you go: " + PLAN_JSON + " thanks")["fault"], "container_stop")

    def test_parse_plan_rejects_incomplete_or_non_json_replies(self):
        for bad in ("no json here", '{"target": "cart"}', "[1, 2]"):
            with self.assertRaises(ValueError):
                planner.parse_plan(bad)

    def propose(self, goal):
        seen = {}

        def fake_llm(key, context, system=planner.SYSTEM_PROMPT):
            seen["context"] = context
            return PLAN_JSON, "fake-model", []

        with patch.dict("os.environ", {"LLM_API_KEY": "k"}), patch.object(llm, "load_dotenv"), \
                patch.object(planner, "load_graph", return_value=(None, None)), patch.object(planner, "load_baseline", return_value={}), \
                patch.object(planner, "load_flows", return_value={}), patch.object(planner, "build_context", return_value={"candidate_targets": ["cart"]}), \
                patch.object(planner, "_call_llm", side_effect=fake_llm):
            plan, source, note = planner.propose_experiment(goal)
        return plan, source, note, seen["context"]

    def test_goal_is_passed_to_the_model_as_a_hint_only_when_valid(self):
        _, source, _, ctx = self.propose("timeout_behavior")
        self.assertEqual(source, "llm")
        self.assertIn("timeout and retry", ctx["optional_operator_goal"])
        self.assertNotIn("optional_operator_goal", self.propose(None)[3])
        self.assertNotIn("optional_operator_goal", self.propose("rm -rf /")[3])

    def test_no_key_falls_back_and_says_so(self):
        with patch.dict("os.environ", {"LLM_API_KEY": ""}), patch.object(llm, "load_dotenv"):
            plan, source, note = planner.propose_experiment()
        self.assertEqual((source, plan["target"]), ("fallback", "cart"))
        self.assertIn("not set", note)

    def judge(self, reply):
        plan = {"target": "cart", "fault": "network_delay", "magnitude": 250, "duration_s": 60, "hypothesis": "h", "reasoning": "r"}
        with patch.dict("os.environ", {"LLM_API_KEY": "k"}), patch.object(llm, "load_dotenv"), \
                patch.object(planner, "_call_llm", return_value=(reply, "fake-model", ["m1: HTTP 429 RESOURCE_EXHAUSTED"])):
            return planner.judge_hypothesis(plan, {"aborted": True})

    def test_verdict_keeps_claims_and_the_recommendation(self):
        v = self.judge(json.dumps({"verdict": "partially held", "explanation": "e", "recommendation": "Add a timeout on cart calls.",
                                   "claims": [{"claim": "c1", "status": "supported", "evidence": "x"}, {"claim": "c2", "status": "bogus", "evidence": "y"}]}))
        self.assertEqual(v["verdict"], "partially held")
        self.assertEqual([c["status"] for c in v["claims"]], ["supported", "no data"])   # unknown status is not trusted
        self.assertEqual(v["recommendation"], "Add a timeout on cart calls.")
        self.assertEqual(v["skipped_models"], ["m1: HTTP 429 RESOURCE_EXHAUSTED"])

    def test_bad_verdict_degrades_to_inconclusive_with_a_template_recommendation(self):
        v = self.judge(json.dumps({"verdict": "totally fine", "explanation": "e"}))
        self.assertEqual(v["verdict"], "inconclusive")
        self.assertIn("timeout", v["recommendation"])

    def test_missing_recommendation_falls_back_to_the_template(self):
        v = self.judge(json.dumps({"verdict": "held", "explanation": "e", "claims": []}))
        self.assertIn("circuit breaker", v["recommendation"])


if __name__ == "__main__":
    unittest.main()
