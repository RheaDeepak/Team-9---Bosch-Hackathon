import unittest
import time
from unittest.mock import patch

import guard


class RecoveryGuardTests(unittest.TestCase):
    def test_live_metrics_exposes_request_rate_for_frontend_charts(self):
        now = time.time() * 1e9
        spans = {
            "one": {"svc": "frontend", "kind": guard.SERVER, "start": now, "dur": 40.0, "status": 0},
            "two": {"svc": "frontend", "kind": guard.SERVER, "start": now, "dur": 60.0, "status": 0},
        }
        with patch.object(guard, "fetch_spans", return_value=spans):
            metrics = guard.live_metrics(["frontend"], window_s=10)

        self.assertEqual(metrics["frontend"]["request_rate"], 0.2)

    def test_recovery_checks_target_container_even_when_target_has_no_span_threshold(self):
        baseline = {
            "thresholds": {
                "services": {"frontend": {"error_rate": 5.0, "p95_ms": 150.0}},
                "containers": {"cart": {"restarts": 0}},
            }
        }
        metrics = {"frontend": {"n": 10, "errors": 0, "error_rate": 0.0, "p95_ms": 50.0}}
        containers = {"cart": {"running": 1, "restarts": 0, "cpu_percent": 1.0, "memory_mb": 20.0}}
        required_targets = []

        def sample(services, window_s, required_containers=()):
            required_targets.extend(required_containers)
            return metrics, containers

        progress = []
        with patch.object(guard, "_sample", side_effect=sample), \
                patch.object(guard, "POLL_S", 0), \
                patch.object(guard, "RECOVERY_SAMPLES", 2), \
                patch.object(guard, "RECOVERY_TIMEOUT_S", 1):
            result = guard.wait_for_recovery(
                {"target": "cart"},
                baseline,
                ["frontend"],
                on_progress=progress.append,
            )

        self.assertTrue(result["recovered"])
        self.assertEqual(required_targets, ["cart", "cart"])
        self.assertEqual(progress[0]["healthy_samples"], 1)


if __name__ == "__main__":
    unittest.main()
