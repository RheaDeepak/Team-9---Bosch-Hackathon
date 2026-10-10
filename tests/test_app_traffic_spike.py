import unittest
from unittest.mock import patch

import app
from fastapi import HTTPException


READY = {
    "failure_type": "traffic_spike",
    "capability_version": "traffic-spike-v1",
    "executor_ready": True,
    "rollback_ready": True,
    "ready": True,
    "integration_test_evidence": "tests/test_traffic_spike_integration.py",
    "detail": "Locust running with 5 users",
}


class TrafficSpikeFrontendApiTests(unittest.TestCase):
    def test_capacity_goal_is_exposed_only_when_the_capability_is_selectable(self):
        with patch.object(app.traffic_spike, "capability", return_value=dict(READY)):
            self.assertIn(app.planner.TRAFFIC_SPIKE_GOAL, app.api_goals())
            self.assertTrue(app.api_fault_capabilities()["ready"])

        unavailable = {**READY, "rollback_ready": False, "detail": "rollback not ready"}
        with patch.object(app.traffic_spike, "capability", return_value=unavailable):
            self.assertNotIn(app.planner.TRAFFIC_SPIKE_GOAL, app.api_goals())
            self.assertFalse(app.api_fault_capabilities()["ready"])

    def test_approval_rechecks_traffic_capability_server_side(self):
        plan = {
            "target": "frontend",
            "fault": "traffic_spike",
            "magnitude": 8,
            "duration_s": 20,
            "hypothesis": "bounded load",
            "reasoning": "capacity check",
        }
        with app.LOCK:
            previous = dict(app.STATE)
            app.STATE.update(app._blank(), phase="proposed", plan=plan, source="deterministic-capability")
        unavailable = {**READY, "ready": False, "detail": "Locust is stopped"}
        try:
            with patch.object(app, "validate", return_value={"valid": True, "reasons": [], "blast_radius": ["frontend"]}), \
                    patch.object(app.traffic_spike, "capability", return_value=unavailable), \
                    self.assertRaises(HTTPException) as raised:
                app.api_approve()
            self.assertEqual(raised.exception.status_code, 409)
            self.assertIn("Locust is stopped", raised.exception.detail)
        finally:
            with app.LOCK:
                app.STATE.clear()
                app.STATE.update(previous)


if __name__ == "__main__":
    unittest.main()
