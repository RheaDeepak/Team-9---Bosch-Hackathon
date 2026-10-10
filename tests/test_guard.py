import time
import unittest
from unittest.mock import patch

import guard

THR = {"cart": {"error_rate": 5.0, "p95_ms": 100.0}, "flagd": {"error_rate": 5.0, "p95_ms": 5.0}}


def metric(n=40, errors=0, p95=10.0):
    return {"n": n, "errors": errors, "error_rate": 100 * errors / n if n else None, "p95_ms": p95}


def span(svc, start, dur=10.0, status=0, url="", kind=2):
    return {"svc": svc, "kind": kind, "start": int(start * 1e9), "dur": dur, "status": status,
            "attrs": {"http.url": url} if url else {}}


class BreachRuleTests(unittest.TestCase):
    def test_error_rate_needs_a_minimum_number_of_failed_requests(self):
        two = {"cart": metric(errors=2, p95=10)}          # 5.0% would not exceed; use a high rate with few failures
        two["cart"]["error_rate"] = 50.0
        self.assertEqual(guard._breaches(two, {}, THR, {}, None), {})
        three = {"cart": metric(errors=3, p95=10)}
        three["cart"]["error_rate"] = 50.0
        self.assertIn("cart error_rate", guard._breaches(three, {}, THR, {}, None))

    def test_flagd_latency_is_never_a_breach(self):
        self.assertEqual(guard._breaches({"flagd": metric(p95=9000)}, {}, THR, {}, None), {})

    def test_latency_over_threshold_breaches(self):
        self.assertIn("cart p95_ms", guard._breaches({"cart": metric(p95=250)}, {}, THR, {}, None))

    def test_container_down_and_restart_breach_but_expected_down_does_not(self):
        down = {"cart": {"running": 0, "restarts": 0}}
        self.assertIn("cart container", guard._breaches({}, down, THR, {}, None))
        self.assertEqual(guard._breaches({}, down, THR, {}, "cart"), {})
        restarted = {"cart": {"running": 1, "restarts": 2}}
        self.assertIn("cart restarts", guard._breaches({}, restarted, THR, {"cart": 1}, None))
        self.assertEqual(guard._breaches({}, restarted, THR, {"cart": 2}, None), {})

    def test_cpu_and_memory_are_never_abort_conditions(self):
        busy = {"cart": {"running": 1, "restarts": 0, "cpu_percent": 999.0, "memory_mb": 99999.0}}
        self.assertEqual(guard._breaches({}, busy, THR, {"cart": 0}, None), {})


class LiveMetricsTests(unittest.TestCase):
    def test_grafana_and_jaeger_traffic_through_envoy_is_not_shop_traffic(self):
        now = time.time()
        spans = {i: span("frontend-proxy", now - 1, 5000.0, url="http://localhost:8080/grafana/api/ds/query") for i in range(10)}
        spans.update({100 + i: span("frontend-proxy", now - 1, 8.0, url="http://localhost:8080/api/cart") for i in range(10)})
        with patch.object(guard, "fetch_spans", return_value=spans):
            m = guard.live_metrics(["frontend-proxy"])["frontend-proxy"]
        self.assertEqual(m["n"], 10)
        self.assertLess(m["p95_ms"], 100)

    def test_since_ignores_spans_from_before_the_rollback(self):
        now = time.time()
        spans = {1: span("cart", now - 20, 900.0), **{i: span("cart", now - 1, 5.0) for i in range(2, 12)}}
        with patch.object(guard, "fetch_spans", return_value=spans):
            m = guard.live_metrics(["cart"], window_s=30, since=now - 5)["cart"]
        self.assertEqual(m["n"], 10)
        self.assertLess(m["p95_ms"], 100)

    def test_too_few_spans_give_no_latency(self):
        spans = {i: span("cart", time.time() - 1) for i in range(3)}
        with patch.object(guard, "fetch_spans", return_value=spans):
            m = guard.live_metrics(["cart"])["cart"]
        self.assertIsNone(m["p95_ms"])
        self.assertEqual(m["errors"], 0)

    def test_error_status_codes_are_counted(self):
        spans = {i: span("cart", time.time() - 1, status=2 if i < 4 else 0) for i in range(10)}
        with patch.object(guard, "fetch_spans", return_value=spans):
            m = guard.live_metrics(["cart"])["cart"]
        self.assertEqual((m["errors"], m["error_rate"]), (4, 40.0))


class RecoveryTests(unittest.TestCase):
    BASELINE = {"thresholds": {"services": {"frontend-proxy": {"error_rate": 5.0, "p95_ms": 100.0}},
                               "containers": {"cart": {"restarts": 0}}}}
    PLAN = {"target": "cart", "fault": "network_delay"}

    def run_recovery(self, probes, metrics, container=None):
        container = container or {"cart": {"running": 1, "restarts": 0, "cpu_percent": 1.0, "memory_mb": 1.0}}
        with patch.object(guard, "probe_store", side_effect=probes), \
                patch.object(guard, "container_state", return_value=container), \
                patch.object(guard, "live_metrics", return_value=metrics), \
                patch.object(guard, "POLL_S", 0), patch.object(guard, "RECOVERY_SAMPLES", 2), \
                patch.object(guard, "RECOVERY_TIMEOUT_S", 2), patch.object(guard, "METRICS_GRACE_S", 0):
            return guard.wait_for_recovery(self.PLAN, self.BASELINE, ["frontend-proxy"], emit=lambda line: None)

    @staticmethod
    def healthy():
        return [{"name": "products", "ok": True, "status": 200, "ms": 12}, {"name": "cart", "ok": True, "status": 200, "ms": 9}]

    def test_probes_are_the_primary_signal_even_while_span_metrics_are_still_bad(self):
        slow = {"frontend-proxy": metric(p95=900)}
        r = self.run_recovery(iter(lambda: self.healthy(), None), slow)
        self.assertTrue(r["recovered"])
        self.assertFalse(r["metrics_confirmed"])           # the secondary check is reported, not required

    def test_failing_or_slow_probes_mean_not_recovered(self):
        bad = [{"name": "cart", "ok": False, "status": "ConnectionError", "ms": 40}]
        self.assertFalse(self.run_recovery(iter(lambda: bad, None), {"frontend-proxy": metric(p95=5)})["recovered"])
        slow = [{"name": "cart", "ok": True, "status": 200, "ms": guard.RECOVERY_PROBE_MAX_MS + 1}]
        self.assertFalse(self.run_recovery(iter(lambda: slow, None), {"frontend-proxy": metric(p95=5)})["recovered"])

    def test_target_container_must_be_running_even_without_a_span_threshold(self):
        stopped = {"cart": {"running": 0, "restarts": 0, "cpu_percent": None, "memory_mb": None}}
        r = self.run_recovery(iter(lambda: self.healthy(), None), {"frontend-proxy": metric(p95=5)}, stopped)
        self.assertFalse(r["recovered"])

    def test_metrics_need_real_data_to_confirm(self):
        empty = {"frontend-proxy": {"n": 0, "errors": 0, "error_rate": None, "p95_ms": None}}
        r = self.run_recovery(iter(lambda: self.healthy(), None), empty)
        self.assertTrue(r["recovered"])
        self.assertFalse(r["metrics_confirmed"])
        good = {"frontend-proxy": metric(p95=5)}
        self.assertTrue(self.run_recovery(iter(lambda: self.healthy(), None), good)["metrics_confirmed"])


class RestartBaselineTests(unittest.TestCase):
    """A stack restart after the baseline leaves cumulative restart counts above the baseline's 0.
    Only restarts during the experiment are damage."""
    BASELINE = {"thresholds": {"services": {"cart": {"error_rate": 5.0, "p95_ms": 100.0}}, "containers": {"checkout": {"restarts": 0}}},
                "services": {"cart": {"error_rate": {"mean": 0.0}, "p95_ms": {"mean": 5.0}}}}
    PLAN = {"target": "cart", "fault": "network_delay", "duration_s": 1}

    def watch(self, restarts, restart_limits):
        containers = {"checkout": {"running": 1, "restarts": restarts, "cpu_percent": 1.0, "memory_mb": 1.0}}
        with patch.object(guard, "_sample", return_value=({"cart": metric(p95=5)}, containers)), \
                patch.object(guard, "probe_store", return_value=[]), patch.object(guard, "POLL_S", 0):
            return guard.watch(self.PLAN, self.BASELINE, ["cart"], emit=lambda line: None, restart_limits=restart_limits)

    def test_restarts_that_predate_the_experiment_do_not_abort_it(self):
        self.assertFalse(self.watch(restarts=5, restart_limits={"checkout": 5})["aborted"])

    def test_a_restart_during_the_experiment_aborts_it(self):
        r = self.watch(restarts=6, restart_limits={"checkout": 5})
        self.assertTrue(r["aborted"])
        self.assertIn("checkout restarts", r["reason"])

    def test_without_a_start_snapshot_the_baseline_limit_still_applies(self):
        self.assertTrue(self.watch(restarts=5, restart_limits=None)["aborted"])


class ProbeTests(unittest.TestCase):
    def test_availability_is_the_share_of_successful_probes(self):
        timeline = [{"probes": {"a": {"ok": True}, "b": {"ok": False}}}, {"probes": {"a": {"ok": True}, "b": {"ok": True}}}]
        self.assertEqual(guard.probe_availability(timeline), 75.0)
        self.assertIsNone(guard.probe_availability([]))


class ControlPlaneContractTests(unittest.TestCase):
    """control_plane.py reads request_rate and on_progress from the same guard functions run_experiment.py uses."""

    def test_live_metrics_exposes_request_rate_for_frontend_charts(self):
        now = time.time()
        spans = {"one": span("frontend", now, 40.0), "two": span("frontend", now, 60.0)}
        with patch.object(guard, "fetch_spans", return_value=spans):
            metrics = guard.live_metrics(["frontend"], window_s=10)
        self.assertEqual(metrics["frontend"]["request_rate"], 0.2)

    def test_recovery_reports_progress_and_requires_the_target_container(self):
        baseline = {"thresholds": {"services": {"frontend": {"error_rate": 5.0, "p95_ms": 150.0}},
                                   "containers": {"cart": {"restarts": 0}}}}
        containers = {"cart": {"running": 1, "restarts": 0, "cpu_percent": 1.0, "memory_mb": 20.0}}
        asked, progress = [], []

        def state(names):
            asked.append(set(names))
            return containers

        healthy = [{"name": "products", "ok": True, "status": 200, "ms": 10}]
        with patch.object(guard, "probe_store", return_value=healthy), patch.object(guard, "container_state", side_effect=state), \
                patch.object(guard, "live_metrics", return_value={"frontend": metric(p95=50)}), patch.object(guard, "POLL_S", 0), \
                patch.object(guard, "RECOVERY_SAMPLES", 2), patch.object(guard, "RECOVERY_TIMEOUT_S", 2), patch.object(guard, "METRICS_GRACE_S", 0):
            result = guard.wait_for_recovery({"target": "cart", "fault": "network_delay"}, baseline, ["frontend"],
                                             on_progress=progress.append)
        self.assertTrue(result["recovered"])
        self.assertTrue(all("cart" in names for names in asked))          # the target is checked although it has no span threshold
        self.assertEqual((progress[0]["healthy_samples"], progress[0]["required_samples"]), (1, 2))
        self.assertEqual(set(progress[0]) >= {"elapsed_s", "timeout_s", "breaches"}, True)
        self.assertEqual((result["healthy_samples"], result["required_samples"]), (2, 2))
        self.assertIsNotNone(result["first_healthy_seconds"])


class HistoryTests(unittest.TestCase):
    """guard.history: the 'normal' side of the charts, sampled like the live guard."""

    def history(self, spans, **kw):
        with patch.object(guard, "fetch_spans", return_value=spans):
            return guard.history(["cart"], **kw)

    def test_samples_every_step_with_negative_times_oldest_first(self):
        rows = self.history({}, seconds=20, step_s=5)
        self.assertEqual([r["t"] for r in rows], [-20, -15, -10, -5, 0])
        self.assertEqual(rows[0]["services"]["cart"]["n"], 0)
        self.assertIsNone(rows[0]["services"]["cart"]["p95_ms"])

    def test_each_point_only_counts_spans_inside_its_own_window(self):
        now = time.time()
        old = {i: span("cart", now - 75, 500.0) for i in range(10)}               # a slow burst 75 s ago
        fresh = {100 + i: span("cart", now - 2, 5.0) for i in range(10)}
        rows = {r["t"]: r["services"]["cart"] for r in self.history({**old, **fresh}, seconds=90, step_s=30, window_s=30)}
        self.assertEqual(rows[0]["p95_ms"], 5.0)                                  # the last 30 s: only the fast spans
        self.assertEqual(rows[-60]["p95_ms"], 500.0)                               # 60 s ago: the slow burst
        self.assertEqual(rows[-30]["n"], 0)                                        # a quiet window: no data, not zero latency

    def test_grafana_and_jaeger_traffic_is_left_out_like_in_live_metrics(self):
        now = time.time()
        spans = {i: span("frontend-proxy", now - 1, 5000.0, url="http://localhost:8080/grafana/api/ds/query") for i in range(10)}
        with patch.object(guard, "fetch_spans", return_value=spans):
            rows = guard.history(["frontend-proxy"], seconds=0)
        self.assertEqual(rows[0]["services"]["frontend-proxy"]["n"], 0)

    def test_live_metrics_and_history_agree_on_one_window(self):
        spans = {i: span("cart", time.time() - 1, float(i + 1), status=2 if i < 3 else 0) for i in range(10)}
        with patch.object(guard, "fetch_spans", return_value=spans):
            live = guard.live_metrics(["cart"])["cart"]
            past = guard.history(["cart"], seconds=0)[0]["services"]["cart"]
        self.assertEqual(live, past)


if __name__ == "__main__":
    unittest.main()
