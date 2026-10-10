import copy
import unittest
from unittest.mock import patch

import networkx as nx
from pydantic import ValidationError

from policy import validate
from planner import SYSTEM_PROMPT, propose_experiment
from traffic_spike import (
    EXPECTED_USER_WEIGHTS,
    TrafficSpikeExecutor,
    TrafficSpikeParameters,
)


def plan(**overrides):
    item = {
        "target": "frontend",
        "fault": "traffic_spike",
        "magnitude": 8,
        "duration_s": 20,
        "hypothesis": "Bounded extra traffic reveals frontend saturation and returns to baseline.",
        "reasoning": "Frontend is the fixed ingress target for the existing load generator.",
        "parameters": {
            "baseline_users": 5,
            "peak_users": 8,
            "spawn_rate_per_second": 2,
            "duration_seconds": 20,
            "journey_profile": "astronomy-shop-default",
        },
    }
    item.update(overrides)
    return item


class FakeController:
    def __init__(self, *, fail_set=False):
        self.state = {
            "controller_service": "load-generator",
            "controller_url": "http://locust.test",
            "state": "running",
            "running": True,
            "user_count": 5,
            "spawn_rate_per_second": 1.0,
            "current_rps": 2.0,
            "journey_profile": "astronomy-shop-default",
            "locust_profile": None,
            "locust_version": "2.46.6",
            "user_class_weights": EXPECTED_USER_WEIGHTS,
        }
        self.fail_set = fail_set
        self.set_calls = []

    def capture_state(self):
        return copy.deepcopy(self.state)

    @staticmethod
    def validate_supported_runtime(state):
        if state["user_class_weights"] != EXPECTED_USER_WEIGHTS:
            raise RuntimeError("unsupported weights")

    def set_load(self, users, spawn_rate):
        self.set_calls.append((users, spawn_rate))
        if self.fail_set:
            raise RuntimeError("injection failed")
        self.state.update(
            state="running",
            running=True,
            user_count=users,
            spawn_rate_per_second=float(spawn_rate),
            current_rps=float(users),
        )

    def stop(self):
        self.state.update(state="stopped", running=False, user_count=0, current_rps=0.0)

    def wait_for_state(self, *, running, users):
        if self.state["running"] != running or self.state["user_count"] != users:
            raise RuntimeError("state did not converge")
        return self.capture_state()


class TrafficSpikeTests(unittest.TestCase):
    def test_parameters_are_strict_bounded_and_forbid_raw_fields(self):
        valid = TrafficSpikeParameters.model_validate(plan()["parameters"])
        self.assertEqual(valid.peak_users, 8)
        for changes in (
            {"peak_users": 11},
            {"spawn_rate_per_second": 3},
            {"duration_seconds": 31},
            {"peak_users": 5},
            {"peak_users": True},
            {"url": "http://untrusted"},
        ):
            candidate = {**plan()["parameters"], **changes}
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                TrafficSpikeParameters.model_validate(candidate)

    def test_policy_restricts_target_and_duplicate_fields(self):
        graph = nx.DiGraph()
        graph.add_nodes_from(["frontend", "cart"])
        self.assertTrue(validate(plan(), graph)["valid"])
        self.assertFalse(validate(plan(target="cart"), graph)["valid"])
        self.assertFalse(validate(plan(magnitude=7), graph)["valid"])
        self.assertFalse(validate(plan(duration_s=19), graph)["valid"])

    def test_original_state_is_persisted_before_mutation(self):
        controller = FakeController(fail_set=True)
        persisted = []
        with self.assertRaisesRegex(RuntimeError, "injection failed"):
            TrafficSpikeExecutor(controller).inject(plan(), "exp-test", lambda item: persisted.append(copy.deepcopy(item)))
        self.assertEqual(persisted[0]["status"], "prepared")
        self.assertEqual(persisted[0]["original_state"]["user_count"], 5)
        self.assertIsNone(persisted[0]["applied_state"])

    def test_active_verification_and_idempotent_rollback(self):
        controller = FakeController()
        persisted = []
        executor = TrafficSpikeExecutor(controller)
        handle = executor.inject(plan(), "exp-test", lambda item: persisted.append(copy.deepcopy(item)))
        active = executor.verify_active(handle, lambda item: persisted.append(copy.deepcopy(item)))
        self.assertTrue(active["verified"])
        self.assertEqual(active["observed_users"], 8)
        self.assertTrue(executor.rollback(handle, lambda item: persisted.append(copy.deepcopy(item))))
        self.assertEqual(controller.state["user_count"], 5)
        set_count = len(controller.set_calls)
        self.assertTrue(executor.rollback(handle, lambda item: persisted.append(copy.deepcopy(item))))
        self.assertEqual(len(controller.set_calls), set_count)
        self.assertTrue(executor.verify_recovered(handle)["verified"])

    def test_candidate_is_deterministic_and_requires_ready_capability(self):
        self.assertNotIn("traffic_spike", SYSTEM_PROMPT)
        ready = {
            "ready": True,
            "executor_ready": True,
            "rollback_ready": True,
            "integration_test_evidence": "tests/test_traffic_spike_integration.py",
            "detail": "tested",
            "capability_version": "traffic-spike-v1",
            "current_state": {"user_count": 5},
        }
        with patch("planner.traffic_spike_capability", return_value=ready):
            candidate, source, _ = propose_experiment("capacity")
        self.assertEqual(source, "deterministic-capability")
        self.assertEqual(candidate["fault"], "traffic_spike")
        self.assertEqual(candidate["parameters"]["baseline_users"], 5)
        with patch("planner.traffic_spike_capability", return_value={"ready": False, "detail": "not tested"}):
            fallback, source, note = propose_experiment("capacity")
        self.assertEqual(source, "fallback")
        self.assertNotEqual(fallback["fault"], "traffic_spike")
        self.assertIn("not ready", note)


if __name__ == "__main__":
    unittest.main()
