import json
import unittest
from unittest.mock import patch

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


PLAN_JSON = json.dumps({"target": "cart", "fault": "container_stop", "magnitude": 0, "duration_s": 60,
                        "hypothesis": "h", "reasoning": "r"})


class VerdictModel:
    def __init__(self, value):
        self.value = value

    def invoke_structured(self, system, messages, schema):
        if isinstance(self.value, Exception):
            raise self.value
        return schema.model_validate(self.value), "fake-model"


class PlannerTests(unittest.TestCase):
    def test_parse_plan_strips_code_fences_and_surrounding_prose(self):
        self.assertEqual(planner.parse_plan("~~~".replace("~", chr(96)) + "json\n" + PLAN_JSON + "\n~~~".replace("~", chr(96)))["target"], "cart")
        self.assertEqual(planner.parse_plan("Here you go: " + PLAN_JSON + " thanks")["fault"], "container_stop")

    def test_parse_plan_rejects_incomplete_or_non_json_replies(self):
        for bad in ("no json here", '{"target": "cart"}', "[1, 2]"):
            with self.assertRaises(ValueError):
                planner.parse_plan(bad)

    def graph_output(self):
        plan = json.loads(PLAN_JSON)
        return {
            "plan": plan,
            "source": "rank_agent",
            "note": "model fake-model, 0 tool calls",
            "model": "fake-model",
            "run_id": "abcd1234",
            "tool_calls": 0,
            "chosen": "cart:container_stop:0",
            "predictions": [{"svc": "cart", "metric": "error_rate", "direction": "up", "min_factor": 2}],
            "top_k": [],
        }

    def test_every_known_learning_goal_reaches_the_planning_graph(self):
        for goal, (_, hint) in planner.GOALS.items():
            with patch.object(planner, "run_agent", return_value=self.graph_output()) as run:
                plan, source, _ = planner.propose_experiment(goal)
            self.assertEqual(source, "rank_agent")
            self.assertEqual(plan["ranking"]["run_id"], "abcd1234")
            self.assertEqual(run.call_args.kwargs["goal"], hint)

    def test_unknown_goal_is_not_injected_into_the_prompt(self):
        with patch.object(planner, "run_agent", return_value=self.graph_output()) as run:
            planner.propose_experiment("rm -rf /")
        self.assertIsNone(run.call_args.kwargs["goal"])

    def test_capacity_goal_remains_a_hard_constraint(self):
        with patch.object(planner, "run_agent", return_value=self.graph_output()) as run:
            planner.propose_experiment(planner.TRAFFIC_SPIKE_GOAL)
        self.assertEqual(run.call_args.kwargs["goal"], "capacity")

    def test_early_planning_failure_uses_compatible_fallback_source(self):
        with patch.object(planner, "run_agent", side_effect=RuntimeError("ranker unavailable")):
            plan, source, note = planner.propose_experiment()
        self.assertEqual((source, plan["target"]), ("ranker_fallback", "cart"))
        self.assertIn("ranker unavailable", note)

    def test_typed_verdict_keeps_claims_and_recommendation(self):
        model = VerdictModel({
            "verdict": "partially held",
            "explanation": "Measured evidence was mixed.",
            "recommendation": "Add a timeout on cart calls.",
            "claims": [
                {"claim": "latency rises", "status": "supported", "evidence": "p95 increased"},
                {"claim": "errors stay flat", "status": "contradicted", "evidence": "errors increased"},
            ],
        })
        plan = json.loads(PLAN_JSON)
        verdict = planner.judge_hypothesis(plan, {"aborted": True}, model_client=model)
        self.assertEqual(verdict["verdict"], "partially held")
        self.assertEqual(verdict["model"], "fake-model")
        self.assertEqual(verdict["recommendation"], "Add a timeout on cart calls.")

    def test_bad_verdict_degrades_to_inconclusive(self):
        plan = json.loads(PLAN_JSON)
        verdict = planner.judge_hypothesis(plan, {}, model_client=VerdictModel(ValueError("bad schema")))
        self.assertEqual(verdict["verdict"], "inconclusive")
        self.assertIn("fallback", verdict["recommendation"].lower())


if __name__ == "__main__":
    unittest.main()
