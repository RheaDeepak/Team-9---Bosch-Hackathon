"""The whole experiment flow (inject -> guard -> rollback -> recovery -> summary) with Docker and the metrics mocked,
through the terminal path (run_experiment.main) and the web path (app.py: propose, approve, worker thread).

Only the outside world is faked (inject.inject/rollback, the guard's span/container/probe reads, Grafana, the LLM).
guard.watch and guard.wait_for_recovery run for real, so a signature mismatch between a caller and guard.py fails here.
"""
import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import analysis
import app
import grafana
import guard
import inject
import run_experiment as exp

PLAN = {"target": "cart", "fault": "network_delay", "magnitude": 200, "duration_s": 1,
        "hypothesis": "cart slows down and recovers", "reasoning": "r"}
HEALTHY = {"n": 40, "errors": 0, "error_rate": 0.0, "p95_ms": 20.0}
SLOW = {"n": 40, "errors": 0, "error_rate": 0.0, "p95_ms": 9000.0}
PHASES = ["injecting", "monitoring", "rolling_back", "recovering", "analyzing"]


def stat(mean):
    return {"mean": mean, "std": 0.0, "max": mean}


def baseline():
    svc = lambda e, p: {"error_rate": stat(e), "p95_ms": stat(p), "request_rate": stat(1.0)}
    names = ("cart", "frontend", "checkout")
    return {"captured_at": "2026-10-11T00:00:00+00:00", "flags_enabled": [],
            "services": {n: svc(0.0, 20.0) for n in names},
            "thresholds": {"services": {n: {"error_rate": 5.0, "p95_ms": 200.0} for n in names},
                           "containers": {n: {"restarts": 0} for n in names}},
            "containers": {n: {"cpu_percent": stat(1.0), "memory_mb": stat(20.0), "running": stat(1), "restarts": stat(0)}
                           for n in names},
            "requests": {}}


class FlowFixture(unittest.TestCase):
    """Fakes the outside world and records the phase every fake was called in."""
    metric = HEALTHY

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.seen = []          # (what, phase) in call order
        self.injected = []
        self.phase = lambda: app.STATE["phase"]
        self.base = baseline()
        healthy_probes = [{"name": "products", "ok": True, "status": 200, "ms": 10}]
        containers = lambda names: {n: {"running": 1, "restarts": 0, "cpu_percent": 1.0, "memory_mb": 20.0} for n in names}

        def live_metrics(services, window_s=guard.WINDOW_S, since=None):
            self.seen.append(("metrics", self.phase()))
            return {s: dict(self.metric) for s in services}

        def fake_inject(plan, *a, **kw):
            self.seen.append(("inject", self.phase()))
            self.injected.append(plan["fault"])
            return {"container_name": "cart-1", "container_id": "abc", "rollback": "remove netem", "fault": plan["fault"],
                    "target": plan["target"], "started_at": "now"}

        def fake_rollback(*a, **kw):
            self.seen.append(("rollback", self.phase()))
            return True

        def judge(plan, measured):
            self.seen.append(("judge", self.phase()))
            return {"verdict": "held", "explanation": "stub", "claims": [], "recommendation": "stub"}

        self.normal = [{"t": t, "services": {"cart": dict(HEALTHY), "frontend": dict(HEALTHY), "checkout": dict(HEALTHY)}}
                       for t in range(-120, 1, 5)]
        patches = [
            mock.patch.object(guard, "history", lambda services, **kw: self.normal),
            mock.patch.object(exp, "load_baseline", lambda: self.base),
            mock.patch.object(exp, "RESULTS_DIR", self.dir / "results"),
            mock.patch.object(exp, "APPROVED_JSON", self.dir / "approved.json"),
            mock.patch.object(exp, "judge_hypothesis", judge),
            mock.patch.object(exp, "propose_experiment", lambda *a, **k: (dict(PLAN), "fallback", "test plan")),
            mock.patch.object(inject, "ACTIVE_FILE", self.dir / "active.json"),
            mock.patch.object(inject, "inject", fake_inject),
            mock.patch.object(inject, "rollback", fake_rollback),
            mock.patch.object(guard, "live_metrics", live_metrics),
            mock.patch.object(guard, "container_state", containers),
            mock.patch.object(guard, "restart_counts", lambda names: {n: 0 for n in names}),
            mock.patch.object(guard, "probe_store", lambda: [dict(p) for p in healthy_probes]),
            mock.patch.object(guard, "POLL_S", 0.05),
            mock.patch.object(guard, "RECOVERY_SAMPLES", 2),
            mock.patch.object(guard, "RECOVERY_TIMEOUT_S", 5),
            mock.patch.object(guard, "METRICS_GRACE_S", 0),
            mock.patch.object(grafana, "annotate", lambda *a, **k: (True, "stub")),
            mock.patch.object(analysis, "refresh", lambda *a, **k: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def saved(self):
        files = sorted((self.dir / "results").glob("*.json"))
        self.assertEqual(len(files), 1, "exactly one result file is written")
        return json.loads(files[0].read_text(encoding="utf-8"))

    def assert_full_flow(self, result, aborted):
        g = result["guard"]
        self.assertEqual(self.injected, ["network_delay"])
        self.assertGreaterEqual(len(g["timeline"]), 2, "the guard sampled more than once: monitoring really ran")
        if not aborted:
            self.assertGreater(g["elapsed_s"], 0.5, "the fault window ran its course")
        self.assertEqual(g["aborted"], aborted)
        self.assertNotIn("TypeError", g["reason"])
        self.assertTrue(result["rollback_verified"])
        self.assertTrue(result["recovery"]["recovered"], result["recovery"])
        self.assertEqual(result["recovery"]["required_samples"], 2)
        self.assertEqual(result["verdict"]["verdict"], "held")
        self.assertEqual(result["normal"], self.normal)          # the "Normal" side of the charts is saved with the result
        self.assertIn("resilience_score", result["score"])
        self.assertEqual([i["service"] for i in result["impact"]][0], "cart")
        order = [what for what, _ in self.seen]
        self.assertLess(order.index("inject"), order.index("rollback"))     # rollback after the fault, never before
        self.assertLess(order.index("rollback"), len(order) - 1 - order[::-1].index("metrics"))   # recovery sampled after it
        self.assertEqual(order.count("rollback"), 1)


class TerminalPathTest(FlowFixture):
    def run_main(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("builtins.input", return_value="y"):
            code = exp.main()
        return code, out.getvalue()

    def test_full_flow_from_main(self):
        code, text = self.run_main()
        self.assertEqual(code, 0, text[-800:])
        self.assert_full_flow(self.saved(), aborted=False)
        for line in ("Source: FALLBACK PLAN", "Validation: PASSED", "FAULT ACTIVE", "Rollback verified", "Recovery:"):
            self.assertIn(line, text)

    def test_guard_abort_still_rolls_back_and_recovers(self):
        self.metric = SLOW
        code, text = self.run_main()
        self.assertEqual(code, 0, text[-800:])
        result = self.saved()
        self.assert_full_flow(result, aborted=True)
        self.assertIn("threshold breached", result["guard"]["reason"])
        self.assertLess(result["guard"]["elapsed_s"], 1.0, "aborted before the fault window ended")
        self.assertIn("ABORTED", text)


class AppPathTest(FlowFixture):
    HEADERS = {"X-Chaos-UI": "1"}

    def setUp(self):
        super().setUp()
        ready = mock.patch.object(app, "_readiness", lambda: {"ready": True, "checks": []})
        ready.start()
        self.addCleanup(ready.stop)
        state = {k: app.STATE[k] for k in app.STATE}
        self.addCleanup(lambda: (app.STATE.clear(), app.STATE.update(state)))
        app.STATE.update(app._blank())
        self.client = TestClient(app.app, base_url="http://127.0.0.1:8000")

    def post(self, path, **kw):
        return self.client.post(path, headers=self.HEADERS, **kw)

    def wait_done(self, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            s = self.client.get("/api/status").json()
            if not s["running"] and s["phase"] in ("done", "error", "refused"):
                return s
            time.sleep(0.05)
        self.fail("experiment did not finish: " + json.dumps(self.client.get("/api/status").json())[:500])

    def test_full_flow_through_the_web_app(self):
        proposal = self.post("/api/propose", json={"goal": None})
        self.assertEqual(proposal.status_code, 200, proposal.text)
        self.assertTrue(proposal.json()["validation"]["valid"])
        self.assertEqual(self.post("/api/approve").status_code, 202)
        s = self.wait_done()
        self.assertEqual(s["phase"], "done", (s["refused"], s["events"][-5:]))
        self.assert_full_flow(s["result"], aborted=False)
        self.assert_full_flow(self.saved(), aborted=False)
        self.assertEqual(s["normal"], self.normal)               # and published for the live charts
        # every phase was entered, in order: what each fake saw is the phase the worker had published
        by_what = {}
        for what, phase in self.seen:
            by_what.setdefault(what, phase)
        self.assertEqual((by_what["inject"], by_what["rollback"], by_what["judge"]), ("injecting", "rolling_back", "analyzing"))
        phases = [phase for what, phase in self.seen if what == "metrics"]
        self.assertIn("monitoring", phases)
        self.assertIn("recovering", phases)
        self.assertEqual(phases.index("monitoring") < phases.index("recovering"), True)
        self.assertGreater(len(s["samples"]), 1)
        self.assertEqual(self.client.get("/api/status").json()["running"], False)

    def test_guard_abort_through_the_web_app(self):
        self.metric = SLOW
        self.post("/api/propose", json={"goal": None})
        self.post("/api/approve")
        s = self.wait_done()
        self.assertEqual(s["phase"], "done", (s["refused"], s["events"][-5:]))
        self.assert_full_flow(s["result"], aborted=True)
        self.assertIn("threshold breached", s["result"]["guard"]["reason"])


if __name__ == "__main__":
    unittest.main()
