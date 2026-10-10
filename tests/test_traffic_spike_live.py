import os
import tempfile
import time
import unittest
from pathlib import Path

import inject as inject_module
from traffic_spike import LocustController, MAX_PEAK_USERS, TrafficSpikeExecutor


@unittest.skipUnless(os.getenv("TRAFFIC_SPIKE_LIVE_TEST") == "1", "set TRAFFIC_SPIKE_LIVE_TEST=1 for the bounded live check")
class TrafficSpikeLiveTests(unittest.TestCase):
    def test_bounded_spike_restores_running_deployment(self):
        controller = LocustController(os.getenv("LOCUST_URL", "http://localhost:8080/loadgen"))
        original = controller.capture_state()
        controller.validate_supported_runtime(original)
        self.assertTrue(original["running"])
        self.assertGreaterEqual(original["user_count"], 1)
        self.assertLess(original["user_count"], MAX_PEAK_USERS)
        peak_users = original["user_count"] + 1
        duration_seconds = 10
        plan = {
            "target": "frontend",
            "fault": "traffic_spike",
            "magnitude": peak_users,
            "duration_s": duration_seconds,
            "parameters": {
                "baseline_users": original["user_count"],
                "peak_users": peak_users,
                "spawn_rate_per_second": 1,
                "duration_seconds": duration_seconds,
                "journey_profile": "astronomy-shop-default",
            },
        }
        executor = TrafficSpikeExecutor(controller)
        handle = None
        original_active_file = inject_module.ACTIVE_FILE
        with tempfile.TemporaryDirectory() as directory:
            inject_module.ACTIVE_FILE = Path(directory) / "active_experiment.json"
            try:
                handle = inject_module.inject(plan, "exp-live-gate", traffic_executor=executor)
                evidence = inject_module.verify_active(handle, traffic_executor=executor)
                self.assertTrue(evidence["verified"])
                time.sleep(duration_seconds)
            finally:
                self.assertTrue(inject_module.rollback(handle, traffic_executor=executor))
                inject_module.ACTIVE_FILE = original_active_file
        recovery = inject_module.verify_recovered(handle, traffic_executor=executor)
        self.assertTrue(recovery["verified"])
        self.assertEqual(recovery["observed_users"], original["user_count"])


if __name__ == "__main__":
    unittest.main()
