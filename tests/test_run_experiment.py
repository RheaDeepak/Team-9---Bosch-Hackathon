import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import run_experiment


PLAN = {
    "target": "cart",
    "fault": "container_stop",
    "magnitude": 0,
    "duration_s": 1,
    "hypothesis": "Cart recovers after a bounded fault.",
    "reasoning": "Exercise the checkout dependency.",
}

BASELINE = {
    "captured_at": "2026-10-11T00:00:00+00:00",
    "flags_enabled": [],
    "services": {"frontend": {"error_rate": {"mean": 0.0}, "p95_ms": {"mean": 50.0}}},
    "containers": {"cart": {"cpu_percent": {"mean": 1.0}, "memory_mb": {"mean": 20.0}}},
    "thresholds": {
        "services": {"frontend": {"error_rate": 5.0, "p95_ms": 150.0}},
        "containers": {"cart": {"restarts": 0}},
    },
}


class ExecuteContractTests(unittest.TestCase):
    def test_execute_adapts_legacy_dashboard_callback_to_guard_api(self):
        samples = []
        monitored = {"aborted": False, "reason": "duration elapsed", "elapsed_s": 1,
                     "peaks": {"frontend": {"error_rate": 0.0, "p95_ms": 50.0}},
                     "container_peaks": {}, "timeline": []}
        metrics = {"frontend": {"n": 1, "errors": 0, "error_rate": 0.0, "p95_ms": 50.0}}
        containers = {"cart": {"running": True, "cpu_percent": 1.0, "memory_mb": 20.0}}

        def watch(*args, **kwargs):
            self.assertEqual(kwargs["stop_event"], stop)
            kwargs["on_sample"](metrics, containers)
            return monitored

        def recover(*args, **kwargs):
            kwargs["on_sample"](metrics, containers)
            return {"recovered": True, "seconds": 1}

        stop = threading.Event()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(run_experiment, "RESULTS_DIR", Path(directory)), \
                patch.object(run_experiment, "load_baseline", return_value=BASELINE), \
                patch.object(run_experiment.guard, "watched_services", return_value=["frontend"]), \
                patch.object(run_experiment.guard, "live_metrics", return_value=metrics), \
                patch.object(run_experiment.guard, "watch", side_effect=watch), \
                patch.object(run_experiment.guard, "wait_for_recovery", side_effect=recover), \
                patch.object(run_experiment.guard, "impact_table", return_value=[]), \
                patch.object(run_experiment.guard, "container_table", return_value=[]), \
                patch.object(run_experiment.inject, "inject", return_value={"container_name": "cart", "rollback": "start"}), \
                patch.object(run_experiment.inject, "rollback", return_value=True), \
                patch.object(run_experiment.grafana, "annotate", return_value=(True, "ok")), \
                patch.object(run_experiment, "judge_hypothesis", return_value={"verdict": "held", "explanation": "recovered"}):
            result = run_experiment.execute(
                PLAN, "fallback", {"blast_radius": ["frontend"]}, stop=stop,
                on_sample=lambda *sample: samples.append(sample),
            )

        self.assertEqual([sample[0] for sample in samples], ["monitoring", "recovering"])
        self.assertTrue(result["recovery"]["recovered"])
        self.assertIsNone(result["recovery"]["metrics_confirmed"])


if __name__ == "__main__":
    unittest.main()
