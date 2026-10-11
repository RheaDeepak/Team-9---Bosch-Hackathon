"""Data quality: excluded / low-evidence runs, honest scores, and how both reach findings, scores and the ranker."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixtures import baseline as rank_baseline, flows as rank_flows, graph as rank_graph, run  # noqa: E402
from test_analysis import CART_DOWN, baseline, graph, row  # noqa: E402

import analysis  # noqa: E402
import analysis_agent  # noqa: E402
from agents.models import AnalysisDecision  # noqa: E402
import guard  # noqa: E402
import ranker  # noqa: E402
import report  # noqa: E402
import run_experiment as exp  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUTAGE_RUNS = ("20261010T210938Z", "20261010T212947Z", "20261010T213637Z")
RECOVERED = {"recovered": True, "seconds": 0.0}


def sampled(d, n=6, ok=True):
    """Give a fixture result n guard samples with probes, re-scored and re-assessed like a real recent run."""
    d = copy.deepcopy(d)
    d["guard"]["timeline"] = [{"t": 5.0 * i, "metrics": {}, "breaches": {},
                               "probes": {"products": {"ok": ok, "ms": 12}, "cart": {"ok": True, "ms": 9}}} for i in range(n)]
    d.pop("score", None)
    return analysis.normalise_result(d, d["id"], excluded={})


class QualityFlagsTest(unittest.TestCase):
    def test_the_outage_runs_are_excluded_with_their_reason(self):
        results, _ = analysis.load_results(ROOT / "results")
        by_id = {r["id"]: r for r in results}
        for rid in OUTAGE_RUNS:
            self.assertEqual(by_id[rid]["quality"], "excluded", rid)
            self.assertIn("product-catalog memory outage", by_id[rid]["quality_reason"])
        self.assertNotIn("EXCLUDED", by_id)                                        # the list itself is not a run

    def test_two_samples_are_low_evidence_and_enough_samples_are_ok(self):
        self.assertEqual(sampled(CART_DOWN("20261010T110000Z"), n=6)["quality"], "ok")
        two = sampled(CART_DOWN("20261010T110000Z"), n=2)
        self.assertEqual(two["quality"], "low_evidence")
        self.assertIn("only 2 guard samples", two["quality_reason"])

    def test_other_exclusion_and_low_evidence_rules(self):
        base = sampled(CART_DOWN("20261010T110000Z"))
        variants = {
            "forced abort test": dict(forced_abort_test=True), "rollback not verified": dict(rollback_verified=False),
            "test harness": dict(plan_source="manual-test")}
        for reason, change in variants.items():
            d = analysis.normalise_result({**copy.deepcopy(base), **change}, base["id"], excluded={})
            self.assertEqual((d["quality"], reason in d["quality_reason"]), ("excluded", True), reason)
        listed = analysis.normalise_result(copy.deepcopy(base), base["id"], excluded={base["id"]: "manual note"})
        self.assertEqual((listed["quality"], listed["quality_reason"]), ("excluded", "manual note"))
        stopped = copy.deepcopy(base)
        stopped["recovery"] = {"recovered": False, "seconds": None, "note": "recovery wait stopped by user"}
        d = analysis.normalise_result(stopped, base["id"], excluded={})
        self.assertEqual(d["quality"], "low_evidence")
        self.assertIn("recovery not measured", d["quality_reason"])

    def test_a_restart_before_the_run_is_not_a_real_non_recovery(self):
        old = sampled(CART_DOWN("20261010T110000Z"))
        old["recovery"] = {"recovered": False, "seconds": None, "note": "not within thresholds after 180s (checkout restarts: restarted (1))"}
        old.pop("restart_limits", None)                                          # older result: limits came from the baseline
        d = analysis.normalise_result(old, old["id"], excluded={})
        self.assertEqual(d["quality"], "low_evidence")
        self.assertIsNone(d["score"]["components"]["recovery_impact"])
        new = copy.deepcopy(old)
        new["restart_limits"] = {"checkout": 0}                                  # newer result: a restart during the run is real
        self.assertEqual(analysis.normalise_result(new, new["id"], excluded={})["score"]["components"]["recovery_impact"], 1.0)

    def test_restarts_are_compared_with_the_counts_at_experiment_start(self):
        metrics = {"checkout": {"n": 10, "errors": 0, "error_rate": 0.0, "p95_ms": 50.0}}
        containers = {"checkout": {"running": 1, "restarts": 4, "cpu_percent": 1.0, "memory_mb": 10.0}}
        thr = {"checkout": {"error_rate": 5.0, "p95_ms": 150.0}}
        self.assertEqual(guard._breaches(metrics, containers, thr, {"checkout": 4}, None), {})            # counts at start
        self.assertIn("checkout restarts", guard._breaches(metrics, containers, thr, {"checkout": 0}, None))  # older baseline


class HonestScoreTest(unittest.TestCase):
    def test_missing_availability_renormalises_and_says_so(self):
        measured = exp.score_experiment([row("a", err=(0, 50))], RECOVERED, 100.0, 60)
        missing = exp.score_experiment([row("a", err=(0, 50))], RECOVERED, None, 60)
        self.assertEqual(measured["impact_score"], 17.5)                           # 0.35 x 50%: availability counted as measured
        self.assertEqual(missing["impact_score"], round(100 * 0.35 * 0.5 / 0.8, 1))  # weights renormalised over the other three
        self.assertGreater(missing["weights"]["error_impact"], exp.SCORE_WEIGHTS["error_impact"])
        self.assertNotIn("availability_loss", missing["weights"])
        self.assertIsNone(missing["components"]["availability_loss"])
        self.assertTrue(any("availability not measured" in l for l in missing["limitations"]))
        self.assertAlmostEqual(sum(missing["weights"].values()), 1.0, places=2)

    def test_unmeasured_recovery_renormalises_too(self):
        s = exp.score_experiment([row("a")], {"recovered": False, "seconds": None, "note": "recovery wait stopped by user"}, 100.0, 60)
        self.assertIsNone(s["components"]["recovery_impact"])
        self.assertEqual(s["impact_score"], 0.0)                                  # not "180 s without recovery"
        self.assertTrue(any("recovery not measured" in l for l in s["limitations"]))
        real = exp.score_experiment([row("a")], {"recovered": False, "seconds": None}, 100.0, 60)
        self.assertEqual(real["components"]["recovery_impact"], 1.0)              # a real non-recovery still counts in full


class EvidenceInFindingsTest(unittest.TestCase):
    def analyse(self, results):
        return analysis.analyse(results, [], graph(), None, baseline(), {})

    def test_excluded_runs_feed_no_finding_score_or_evidence(self):
        good = sampled(CART_DOWN("20261010T110000Z"))
        bad = analysis.normalise_result(copy.deepcopy(good), "20261010T120000Z", excluded={"20261010T120000Z": "outage"})
        bad["id"] = "20261010T120000Z"
        a = self.analyse([bad])
        self.assertEqual((a["weaknesses"], a["strengths"]), ([], []))
        self.assertEqual(a["scores"]["services"]["cart"]["status"], "not tested")
        self.assertFalse(any(e.startswith("20261010T120000Z#") for e in a["evidence"]))
        self.assertEqual([x["id"] for x in a["excluded"]], ["20261010T120000Z"])
        self.assertEqual(a["experiments"][0]["quality"], "excluded")              # still listed, never used
        both = self.analyse([good, bad])
        self.assertTrue(all("20261010T120000Z" not in w["runs"] for w in both["weaknesses"]))
        self.assertEqual(both["scores"]["services"]["cart"]["runs"], 1)

    def test_low_evidence_alone_never_makes_a_critical_or_high_finding(self):
        strong = [sampled(CART_DOWN(f"20261010T11{i}000Z"), n=6) for i in range(2)]
        weak = [sampled(CART_DOWN(f"20261010T11{i}000Z"), n=2) for i in range(2)]
        hard = lambda a: next(w for w in a["weaknesses"] if w["pattern"] == "hard_dependency")
        with_ok, only_low = hard(self.analyse(strong)), hard(self.analyse(weak))
        self.assertIn(with_ok["severity"], ("Critical", "High"))
        self.assertNotIn(only_low["severity"], ("Critical", "High"))
        self.assertTrue(only_low["capped_low_evidence"])
        self.assertEqual(only_low["low_evidence_runs"], sorted(w["id"] for w in weak))
        self.assertLess(only_low["confidence"], with_ok["confidence"])
        mixed = hard(self.analyse([strong[0], weak[1]]))                         # one ok run is enough to carry it
        self.assertFalse(mixed["capped_low_evidence"])

    def test_the_agent_cannot_raise_a_low_evidence_finding_to_high(self):
        data = self.analyse([sampled(CART_DOWN(f"20261010T11{i}000Z"), n=2) for i in range(2)])
        w = next(x for x in data["weaknesses"] if x["capped_low_evidence"])
        ev = w["evidence_ids"][:1]
        answer = {"findings": [{"id": w["id"], "title": "t", "service": w["service"], "pattern": w["pattern"],
                                "severity": "High", "severity_score": 60, "root_cause": "checkout fails",
                                "evidence_ids": ev, "fix": "f", "verify_by": "v", "confidence": 0.5,
                                "agent_adjustment": "looks bad"}],
                  "summary": "s", "strengths": []}
        answer = AnalysisDecision.model_validate(answer)   # the LangGraph agent's structured output
        with self.assertRaisesRegex(ValueError, "low-evidence"):
            analysis_agent.check_answer(answer, [w], data)


class PenaltyAndProvisionalTest(unittest.TestCase):
    def analyse(self, results):
        return analysis.analyse(results, [], graph(), None, baseline(), {})

    def test_medium_findings_still_lower_the_score_by_confidence(self):
        a = self.analyse([sampled(CART_DOWN(f"20261010T11{i}000Z"), n=2) for i in range(2)])   # low evidence only
        cart = [w for w in a["weaknesses"] if w["service"] == "cart"]
        self.assertTrue(cart and all(w["severity"] in ("Medium", "Low") for w in cart))
        expected = round(min(analysis.WEAKNESS_PENALTY_CAP, sum(
            analysis.WEAKNESS_PENALTY["Medium"] * w["confidence"] for w in cart if w["severity"] == "Medium")), 1)
        s = a["scores"]["services"]["cart"]
        self.assertGreater(expected, 0)
        self.assertEqual(s["penalty"], expected)
        self.assertEqual(s["score"], max(0, round(s["mean_run_score"] - expected)))
        self.assertIn("Medium 5 x confidence", s["explain"])
        self.assertNotIn("Low", analysis.WEAKNESS_PENALTY)                       # Low findings cost nothing
        self.assertEqual(analysis.WEAKNESS_PENALTY_CAP, 30)

    def test_overall_score_is_provisional_with_fewer_than_3_reliable_runs(self):
        few = self.analyse([sampled(CART_DOWN(f"20261010T11{i}000Z"), n=6) for i in range(2)])["scores"]["overall"]
        self.assertTrue(few["provisional"])
        self.assertEqual((few["reliable_runs"], few["provisional_note"]), (2, "provisional: based on 2 reliable runs"))
        many = self.analyse([sampled(CART_DOWN(f"20261010T11{i}000Z"), n=6) for i in range(3)])["scores"]["overall"]
        self.assertEqual((many["provisional"], many["provisional_note"]), (False, None))
        out = analysis_agent.build_output(self.analyse([sampled(CART_DOWN("20261010T110000Z"))]), "analysis_fallback", "t")
        self.assertIn("provisional: based on 1 reliable runs", report.render_md(out))
        self.assertIn("provisional: based on 1 reliable runs", report.render_html(out))
        self.assertIn("provisional: based on 1 reliable runs", out["summary"])


class RankerAfterAbortTest(unittest.TestCase):
    def test_after_an_abort_the_lowest_magnitude_is_preferred(self):
        for aborted_at in (200, 1000):
            history = [run("pay", "network_delay", aborted_at, verdict="inconclusive", guard_aborted=True)]
            ranking = ranker.rank(rank_graph(), rank_baseline(), rank_flows(), history)
            delays = sorted((c for c in ranking if c["target"] == "pay" and c["fault"] == "network_delay"), key=lambda c: c["rank"])
            self.assertEqual(delays[0]["magnitude"], 200, aborted_at)
            score = {c["magnitude"]: c["features"]["prior_abort"]["score"] for c in delays}
            self.assertEqual(score[500], 1.0)
            self.assertLess(score[200], 1.0)

    def test_guard_thresholds_are_unchanged(self):
        self.assertEqual((guard.CONSECUTIVE_BREACHES, guard.MIN_FAILED_REQUESTS), (2, 3))


class RankerBothBehavioursTest(unittest.TestCase):
    """fault diversity (one row per service and fault, coverage, fair stop penalties, entry points, unavailable
    traffic_spike) and the lowest-magnitude preference after a guard abort hold together."""

    def test_rows_coverage_stop_penalties_and_lowest_after_abort_at_once(self):
        history = [run("pay", "network_delay", 200, verdict="inconclusive", guard_aborted=True)] + \
                  [run(t, "network_delay", 500) for t in ("checkout", "worker")]
        g, base = rank_graph(), rank_baseline()
        g.add_edge("load-generator", "frontend", mode="sync", type="sync-http")
        g.add_edge("frontend", "checkout", mode="sync", type="sync-http")
        base["services"]["frontend"] = dict(base["services"]["web"])
        base["containers"]["frontend"] = {"running": {"mean": 1}}
        cap = {"ready": False, "runtime_ready": False, "executor_ready": True, "rollback_ready": True,
               "integration_test_evidence": "x", "detail": "Unavailable: ConnectionError"}
        ranking = ranker.rank(g, base, rank_flows(), history, traffic_capability=cap)
        rows = [c for c in ranking if "variant_of" not in c]
        keys = [(c["target"], c["fault"]) for c in rows]
        self.assertEqual(len(keys), len(set(keys)))                                       # one row per (service, fault)
        pay = next(c for c in rows if (c["target"], c["fault"]) == ("pay", "network_delay"))
        self.assertEqual(pay["magnitude"], 200)                                           # lowest after the abort leads ...
        self.assertEqual(pay["features"]["prior_abort"]["score"], ranker.LOWEST_AFTER_ABORT_SHARE)
        self.assertTrue(all(ranking[[c["id"] for c in ranking].index(v)]["features"]["prior_abort"]["score"] == 1.0
                            for v in pay["variants"]))                                    # ... higher magnitudes keep the full penalty
        self.assertIn("lowest magnitude preferred", pay["why"] + str([f["label"] for f in pay["factors"]]))
        self.assertIn("container_stop", [c["fault"] for c in rows[:5]])                   # coverage: only delays were run
        self.assertEqual(next(c for c in rows if c["fault"] == "container_stop")["features"]["coverage"]["score"], 1.0)
        queue_stop = next(c for c in ranking if c["id"] == "queue:container_stop:0")
        self.assertEqual((queue_stop["outage"], queue_stop["stop_reach"]), (0.0, 0.0))  # async-only leaf: no outage
        entry = {c["target"] for c in ranking if c["features"]["entry_point"]["raw"]}
        self.assertEqual(entry, {"web", "frontend"})                                      # called by the load generator
        spike = next(c for c in ranking if c["fault"] == "traffic_spike")
        self.assertTrue(spike["unavailable"])
        self.assertEqual(rows[-1]["id"], spike["id"])                                     # listed last, never ahead of a runnable row


class ResultFilesTest(unittest.TestCase):
    def test_excluded_json_is_not_a_run_anywhere(self):
        stems = [p.stem for p in analysis.result_files(ROOT / "results")]
        self.assertNotIn("EXCLUDED", stems)
        self.assertTrue(all(analysis.RESULT_FILE.match(s) for s in stems))
        self.assertNotIn("EXCLUDED", {h["file"].split(".")[0] for h in ranker.load_history(ROOT / "results")})


if __name__ == "__main__":
    unittest.main()
