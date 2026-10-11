"""The guard preview in /api/status (what the guard will watch and when it aborts, for a proposed plan)."""
import unittest
from unittest.mock import patch

import app
import guard

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


if __name__ == "__main__":
    unittest.main()
