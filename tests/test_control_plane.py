import time
import unittest

from control_plane import ExperimentManager, baseline_summary, score_experiment


PLAN = {
    "target": "cart",
    "fault": "container_stop",
    "magnitude": 0,
    "duration_s": 0.03,
    "hypothesis": "Cart errors increase and the service recovers.",
    "reasoning": "Cart is on the checkout path.",
}


def snapshot(error_rate=0.0, p95=50.0, throughput=20.0):
    return {
        "services": {
            "frontend": {
                "error_rate": error_rate,
                "p95_ms": p95,
                "request_rate": throughput,
            }
        },
        "containers": {"cart": {"running": 1}},
    }


def baseline():
    def metric(mean):
        return {"mean": mean, "std": 0, "max": mean, "n": 1}

    return {
        "services": {
            "frontend": {
                "error_rate": metric(0.5),
                "p95_ms": metric(50.0),
                "request_rate": metric(20.0),
            }
        },
        "containers": {"cart": {"cpu_percent": metric(1.0), "memory_mb": metric(20.0)}},
        "thresholds": {
            "services": {"frontend": {"error_rate": 5.5, "p95_ms": 150.0}},
            "containers": {"cart": {"restarts": 0, "running": 1}},
        },
    }


def guarded_result(*, aborted=False, reason="duration elapsed"):
    return {
        "aborted": aborted,
        "reason": reason,
        "elapsed_s": 0.03,
        "peaks": {"frontend": {"error_rate": 20.0, "p95_ms": 250.0}},
        "container_peaks": {"cart": {"cpu_percent": 2.0, "memory_mb": 22.0}},
        "timeline": [],
    }


class ControlPlaneTests(unittest.TestCase):
    def test_ai_can_explore_without_an_operator_goal(self):
        received = []
        manager = ExperimentManager(planner=lambda goal: received.append(goal) or (dict(PLAN), "fallback", "test"))
        proposal = manager.propose()
        self.assertEqual(received, [None])
        self.assertIsNone(proposal["goal"])

    def test_baseline_summary_preserves_missing_data(self):
        self.assertEqual(
            baseline_summary({"services": {"frontend": {}}}),
            {"availability": None, "error_rate": None, "p95_latency_ms": None, "throughput_rps": None},
        )

    def test_scoring_uses_prd_formula_and_bounds(self):
        result = score_experiment(
            {"availability": 99.5, "error_rate": 0.5, "p95_latency_ms": 50.0, "throughput_rps": 20.0},
            {"availability": 80.0, "error_rate": 20.0, "p95_latency_ms": 250.0, "throughput_rps": 12.0},
            recovery_seconds=30,
        )
        self.assertEqual(result["severity"], "High")
        self.assertGreaterEqual(result["resilience_score"], 0)
        self.assertLessEqual(result["resilience_score"], 100)

    def test_full_run_injects_rolls_back_and_reports(self):
        actions = []
        snapshots = iter([snapshot(), snapshot(20, 250, 12), snapshot(1, 55, 19)])
        manager = ExperimentManager(
            planner=lambda goal: (dict(PLAN), "fallback", "test"),
            validator=lambda plan: {"valid": True, "reasons": [], "blast_radius": ["frontend"]},
            injector=lambda plan, experiment_id: actions.append(("inject", plan["target"])) or {"target": plan["target"], "fault": plan["fault"]},
            rollback_fn=lambda active: actions.append(("rollback", active["target"])) or True,
            snapshotter=lambda: next(snapshots, snapshot(1, 55, 19)),
            baseline_loader=baseline,
            judge=lambda plan, results: {"verdict": "held", "explanation": "Cart failed and recovered."},
            telemetry_probe=lambda services: snapshot()["services"],
            guard_watcher=lambda plan, measured, services, **kwargs: guarded_result(),
            recovery_waiter=lambda plan, measured, services, **kwargs: {"recovered": True, "seconds": 0.01},
            observation_interval_s=0.01,
        )
        experiment = manager.propose("test graceful degradation")
        self.assertTrue(manager.validate(experiment["id"])["allowed"])
        manager.approve(experiment["id"], "judge-demo")
        finished = self._wait_for_terminal(manager, experiment["id"])
        self.assertEqual(finished["state"], "completed")
        self.assertEqual(actions, [("inject", "cart"), ("rollback", "cart")])
        self.assertIsNotNone(finished["report"])
        self.assertEqual(finished["report"]["ai_interpretation"]["verdict"], "held")

    def test_abort_still_rolls_back(self):
        plan = {**PLAN, "duration_s": 2}
        actions = []

        def wait_for_abort(candidate, measured, services, *, stop, on_sample):
            stop.wait(1)
            return guarded_result(aborted=True, reason="operator requested emergency stop")

        manager = ExperimentManager(
            planner=lambda goal: (plan, "fallback", "test"),
            validator=lambda candidate: {"valid": True, "reasons": [], "blast_radius": ["frontend"]},
            injector=lambda candidate, experiment_id: actions.append("inject") or {"target": "cart", "fault": "container_stop"},
            rollback_fn=lambda active: actions.append("rollback") or True,
            snapshotter=lambda: snapshot(4, 80, 16),
            baseline_loader=baseline,
            judge=lambda candidate, results: {"verdict": "inconclusive", "explanation": "aborted"},
            telemetry_probe=lambda services: snapshot()["services"],
            guard_watcher=wait_for_abort,
            recovery_waiter=lambda candidate, measured, services, **kwargs: {"recovered": True, "seconds": 0.01},
            observation_interval_s=0.01,
        )
        experiment = manager.propose("test abort")
        manager.validate(experiment["id"])
        manager.approve(experiment["id"], "judge-demo")
        deadline = time.time() + 1
        while manager.get(experiment["id"])["state"] != "observing" and time.time() < deadline:
            time.sleep(0.005)
        manager.abort(experiment["id"], "operator pressed STOP")
        finished = self._wait_for_terminal(manager, experiment["id"])
        self.assertEqual(finished["state"], "aborted")
        self.assertEqual(actions, ["inject", "rollback"])

    def test_active_verification_exception_still_rolls_back(self):
        traffic_plan = {
            "target": "frontend",
            "fault": "traffic_spike",
            "magnitude": 8,
            "duration_s": 20,
            "hypothesis": "Traffic rises and returns to baseline.",
            "reasoning": "Exercise the existing load path.",
            "parameters": {
                "baseline_users": 5,
                "peak_users": 8,
                "spawn_rate_per_second": 2,
                "duration_seconds": 20,
                "journey_profile": "astronomy-shop-default",
            },
        }
        actions = []
        manager = ExperimentManager(
            planner=lambda goal: (traffic_plan, "deterministic-capability", "test"),
            validator=lambda candidate: {"valid": True, "reasons": [], "blast_radius": ["frontend"]},
            injector=lambda candidate, experiment_id: actions.append("inject") or {"target": "frontend", "fault": "traffic_spike"},
            active_verifier=lambda handle: (_ for _ in ()).throw(RuntimeError("active verification failed")),
            rollback_fn=lambda active: actions.append("rollback") or True,
            recovery_verifier=lambda active: {"verified": True},
            baseline_loader=baseline,
            telemetry_probe=lambda services: snapshot()["services"],
        )
        experiment = manager.propose("capacity")
        manager.validate(experiment["id"])
        manager.approve(experiment["id"], "judge-demo")
        finished = self._wait_for_terminal(manager, experiment["id"])
        self.assertEqual(finished["state"], "failed")
        self.assertEqual(actions, ["inject", "rollback"])
        self.assertIn("active verification failed", finished["error"])

    def test_traffic_spike_report_keeps_executor_evidence(self):
        traffic_plan = {
            "target": "frontend",
            "fault": "traffic_spike",
            "magnitude": 8,
            "duration_s": 20,
            "hypothesis": "Traffic rises and returns to baseline.",
            "reasoning": "Exercise the existing load path.",
            "parameters": {
                "baseline_users": 5,
                "peak_users": 8,
                "spawn_rate_per_second": 2,
                "duration_seconds": 20,
                "journey_profile": "astronomy-shop-default",
            },
        }
        manager = ExperimentManager(
            planner=lambda goal: (traffic_plan, "deterministic-capability", "tested"),
            validator=lambda candidate: {"valid": True, "reasons": [], "blast_radius": ["frontend"]},
            injector=lambda candidate, experiment_id: {"target": "frontend", "fault": "traffic_spike"},
            active_verifier=lambda handle: {"verified": True, "expected_users": 8, "observed_users": 8},
            rollback_fn=lambda active: True,
            recovery_verifier=lambda active: {"verified": True, "expected_users": 5, "observed_users": 5},
            baseline_loader=baseline,
            telemetry_probe=lambda services: snapshot()["services"],
            guard_watcher=lambda candidate, measured, services, **kwargs: guarded_result(),
            recovery_waiter=lambda candidate, measured, services, **kwargs: {"recovered": True, "seconds": 0.01},
            judge=lambda candidate, results: {"verdict": "held", "explanation": "bounded load observed"},
        )
        experiment = manager.propose("capacity")
        manager.validate(experiment["id"])
        manager.approve(experiment["id"], "judge-demo")
        finished = self._wait_for_terminal(manager, experiment["id"])
        self.assertEqual(finished["state"], "completed")
        self.assertTrue(finished["report"]["fault_evidence"]["verified"])
        self.assertTrue(finished["report"]["fault_recovery"]["verified"])
        self.assertIn("Locust users", [item["label"] for item in finished["report"]["evidence"]])

    @staticmethod
    def _wait_for_terminal(manager, experiment_id):
        deadline = time.time() + 2
        while time.time() < deadline:
            experiment = manager.get(experiment_id)
            if experiment["state"] in {"completed", "aborted", "failed"}:
                return experiment
            time.sleep(0.01)
        raise AssertionError("experiment did not reach a terminal state")


if __name__ == "__main__":
    unittest.main()
