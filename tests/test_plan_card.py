"""The plan card: the guard preview in /api/status and the card's text helpers (plan:pure block of static/index.html)."""
import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import guard

try:
    import quickjs
except ImportError:  # optional: the JS helpers are also exercised in the browser
    quickjs = None

INDEX = Path(__file__).resolve().parent.parent / "static" / "index.html"
PLAN = {"target": "shipping", "fault": "container_stop", "magnitude": 0, "duration_s": 60, "hypothesis": "h", "reasoning": "r"}
BASELINE = {"thresholds": {"services": {"shipping": {"error_rate": 5.0, "p95_ms": 160.0},
                                        "checkout": {"error_rate": 5.0, "p95_ms": 900.0}}}}


class GuardPreviewTest(unittest.TestCase):
    def test_preview_uses_the_guards_services_and_baseline_thresholds(self):
        with patch.object(app.metrics, "load_baseline", return_value=BASELINE), patch.object(app.exp, "FORCE_ABORT_TEST", False):
            g = app._guard_preview(PLAN, {"blast_radius": ["checkout", "frontend"]})
        self.assertEqual(g["services"], guard.watched_services(PLAN, BASELINE, ["checkout", "frontend"]))
        self.assertEqual(g["services"], ["shipping", "checkout"])          # frontend has no thresholds: not watched
        self.assertEqual(g["abort"]["checkout"], {"error_rate": 5.0, "p95_ms": 900.0})
        self.assertEqual((g["breaches_in_a_row"], g["recovery_timeout_s"]), (guard.CONSECUTIVE_BREACHES, guard.RECOVERY_TIMEOUT_S))
        self.assertEqual(g["max_duration_s"], min(60, guard.MAX_DURATION_S))
        self.assertEqual(g["rollback"], "start container")

    def test_forced_abort_test_shows_the_tiny_thresholds(self):
        with patch.object(app.metrics, "load_baseline", return_value=BASELINE), patch.object(app.exp, "FORCE_ABORT_TEST", True):
            g = app._guard_preview(PLAN, {"blast_radius": []})
        self.assertEqual(g["abort"]["shipping"], app.exp.FORCE_THRESHOLDS)

    def test_no_baseline_or_an_error_gives_no_preview(self):
        with patch.object(app.metrics, "load_baseline", return_value=None):
            self.assertIsNone(app._guard_preview(PLAN, {"blast_radius": []}))
        with patch.object(app.metrics, "load_baseline", side_effect=OSError("disk")):
            self.assertIsNone(app._guard_preview(PLAN, {"blast_radius": []}))

    def test_blank_state_has_no_preview(self):
        self.assertIsNone(app._blank()["guard_preview"])


@unittest.skipUnless(quickjs, "quickjs is not installed")
class PlanCardTextTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = INDEX.read_text(encoding="utf-8")
        pure = re.search(r"/\* plan:pure begin.*?\*/(.*?)/\* plan:pure end \*/", src, re.S).group(1)
        cls.ctx = quickjs.Context()
        cls.ctx.eval("const FAULT_LABELS = { container_stop: 'Container stop', network_delay: 'Network delay', traffic_spike: 'Traffic spike' };" + pure)

    def js(self, expr, *args):
        return json.loads(self.ctx.eval(f"JSON.stringify({expr}({', '.join(json.dumps(a) for a in args)}))"))

    def test_one_line_summary(self):
        self.assertEqual(self.js("planSummary", PLAN, {"blast_radius": ["checkout"]}), "Stop shipping for 60 s: does checkout fail gracefully?")
        delay = {**PLAN, "target": "product-catalog", "fault": "network_delay", "magnitude": 200}
        self.assertEqual(self.js("planSummary", delay, {"blast_radius": ["frontend"]}),
                         "Add 200 ms delay to product-catalog for 60 s: does the storefront slow down?")
        spike = {**PLAN, "target": "frontend", "fault": "traffic_spike", "duration_s": 20,
                 "parameters": {"baseline_users": 5, "peak_users": 8, "duration_seconds": 20}}
        self.assertEqual(self.js("planSummary", spike, {"blast_radius": []}),
                         "Raise traffic on frontend from 5 to 8 users for 20 s: does the storefront keep up?")

    def test_why_facts_come_from_the_ranker_and_the_agents_evidence(self):
        r = {"chosen": "a", "top_k": [{"candidate_id": "b", "why": "x"}, {"candidate_id": "a",
             "why": "service never tested; on checkout path; blast radius 6 | penalty: blast 6",
             "evidence": [{"tool": "search_source", "finding": "checkout retries payment: no"}]}]}
        self.assertEqual(self.js("whyFacts", r), ["service never tested", "on checkout path", "checkout retries payment: no (search source)"])
        self.assertEqual(self.js("whyFacts", None), [])

    def test_predictions_are_compact_rows(self):
        rows = self.js("predictionRows", {"predictions": [{"svc": "frontend", "metric": "p95_ms", "direction": "up", "min_factor": 3},
                                                          {"svc": "cart", "metric": "error_rate", "direction": "up", "min_factor": 1}]})
        self.assertEqual([(x["svc"], x["metric"], x["arrow"], x["factor"]) for x in rows],
                         [("frontend", "p95", "↑", "≥3×"), ("cart", "errors", "↑", "")])
        self.assertEqual(self.js("predictionRows", None), [])

    def test_abort_text_uses_only_preview_numbers(self):
        g = {"services": ["a", "b"], "abort": {"a": {"error_rate": 5, "p95_ms": 162.98}, "b": {"error_rate": 5, "p95_ms": 11071.5}},
             "breaches_in_a_row": 2, "max_duration_s": 60}
        self.assertEqual(self.js("abortText", g), "error rate above 5% or p95 above 163–11072 ms (each service’s baseline limit) "
                                                  "for 2 samples in a row; stops anyway at 60 s")
        self.assertEqual(self.js("abortText", None), "")


if __name__ == "__main__":
    unittest.main()
