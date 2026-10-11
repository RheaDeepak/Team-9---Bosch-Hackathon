import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from test_analysis import CART_DOWN, EMAIL_DOWN, analyse

import activity
import analysis
import analysis_agent
import app

CSRF = {app.CSRF_HEADER: "1"}


class ModeFilterTest(unittest.TestCase):
    """/api/agent-events and /api/agent-sessions keep the planning and analysis agents apart."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.client = TestClient(app.app, base_url="http://127.0.0.1:8000")
        patcher = mock.patch.object(activity, "SESSIONS_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_newest_run_is_chosen_per_mode(self):
        plan = activity.Recorder("aaaa0001", self.dir, mode="planning")
        plan.emit("thought", "ranking")
        ana = activity.Recorder("bbbb0002", self.dir, mode="analysis")
        ana.emit("tool_call", "get_weakness", tool="get_weakness", call="1/10")
        plan.emit("decision", "chose x", fallback=False)        # planning wrote last, so it is the overall newest
        get = lambda q: self.client.get("/api/agent-events" + q).json()
        self.assertEqual(get("?mode=analysis")["run_id"], "bbbb0002")
        self.assertEqual(get("?mode=planning")["run_id"], "aaaa0001")
        self.assertEqual(get("")["run_id"], "aaaa0001")
        self.assertEqual({e["mode"] for e in get("?mode=analysis")["events"]}, {"analysis"})
        stream = get("?mode=all")                                 # every agent, interleaved, newest server session
        self.assertIsNone(stream["run_id"])
        self.assertGreaterEqual({e["run_id"] for e in stream["events"]}, {"aaaa0001", "bbbb0002"})
        self.assertEqual(self.client.get("/api/agent-events?mode=bogus").status_code, 400)

    def test_sessions_are_filtered_by_mode_and_carry_it(self):
        activity.Recorder("cccc0003", self.dir, mode="planning").emit("thought", "p")
        activity.Recorder("dddd0004", self.dir, mode="analysis").emit("thought", "a")
        rows = lambda q: self.client.get("/api/agent-sessions" + q).json()
        self.assertEqual([r["run_id"] for r in rows("?mode=analysis")], ["dddd0004"])
        self.assertEqual([r["run_id"] for r in rows("?mode=planning")], ["cccc0003"])
        self.assertEqual({r["mode"] for r in rows("")}, {"planning", "analysis"})

    def test_restart_leaves_live_feed_empty_but_saved_session_replays_explicitly(self):
        rec = activity.Recorder("eeee0005", self.dir, mode="analysis")
        rec.emit("decision", "done", fallback=False)
        with mock.patch.object(activity._sqlite, "_events", []):
            body = self.client.get("/api/agent-events?mode=analysis").json()
            replay = self.client.get("/api/agent-events?run=eeee0005&mode=analysis").json()
        self.assertEqual((body["run_id"], body["events"]), (None, []))
        self.assertEqual((replay["run_id"], [e["type"] for e in replay["events"]]), ("eeee0005", ["decision"]))


class LatestAnalysisTest(unittest.TestCase):
    """/api/analysis/latest says which runs the AI analysis covers and whether newer runs exist."""

    def setUp(self):
        self.client = TestClient(app.app, base_url="http://127.0.0.1:8000")
        self.old, self.new = CART_DOWN("20261010T110000Z"), EMAIL_DOWN

    def latest(self, data, latest):
        with mock.patch.object(analysis, "current", return_value=data), \
                mock.patch.object(analysis_agent, "load_latest", return_value=latest), \
                mock.patch.dict(app.ANALYSIS, {"running": False, "run_id": None}):
            return self.client.get("/api/analysis/latest").json()

    def agent_output(self, results, run_id="abcd1234"):
        out = analysis_agent.build_output(analyse(results), "analysis_agent", "model fake", run_id=run_id)
        out["generated_at"] = "2026-10-10T12:00:00+00:00"
        return out

    def test_fresh_analysis(self):
        body = self.latest(analyse([self.old]), self.agent_output([self.old]))
        self.assertEqual((body["stale"], body["covered_runs"], body["run_id"]), (False, ["20261010T110000Z"], "abcd1234"))
        self.assertEqual(body["generated_at"], "2026-10-10T12:00:00+00:00")
        self.assertEqual(body["analysis"]["source"], "analysis_agent")

    def test_new_runs_make_it_stale(self):
        body = self.latest(analyse([self.old, self.new]), self.agent_output([self.old]))
        self.assertTrue(body["stale"])
        self.assertEqual((body["covered_runs"], body["new_runs"]), (["20261010T110000Z"], [self.new["id"]]))
        self.assertEqual(body["analysis"]["source"], "analysis_fallback")   # the rules cover the new run; the AI did not

    def test_never_analysed(self):
        body = self.latest(analyse([self.old]), None)
        self.assertEqual((body["analysed"], body["stale"], body["covered_runs"], body["run_id"]), (False, True, [], None))
        self.assertTrue(body["generated_at"])


class AutoAnalysisGuardTest(unittest.TestCase):
    """The analysis never starts during an experiment and never twice; auto-analysis is off by default."""

    def setUp(self):
        self.client = TestClient(app.app, base_url="http://127.0.0.1:8000")
        self.release, self.calls = threading.Event(), []
        state = mock.patch.dict(app.STATE, {"running": False, "phase": "idle"})
        ana = mock.patch.dict(app.ANALYSIS, {"running": False, "auto": False, "run_id": None, "error": None})
        agent = mock.patch.object(analysis_agent, "run_analysis", side_effect=self.fake_run)
        for p in (state, ana, agent):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.release.set)

    def fake_run(self):
        self.calls.append(1)
        self.release.wait(5)
        return {"source": "analysis_fallback", "note": "test", "run_id": "ffff0006"}

    def wait_idle(self):
        self.release.set()
        for t in threading.enumerate():
            if t.name == "analysis":
                t.join(5)

    def test_conflict_while_running_and_during_an_experiment(self):
        self.assertEqual(self.client.post("/api/analysis/run", headers=CSRF).status_code, 202)
        second = self.client.post("/api/analysis/run", headers=CSRF)
        self.assertEqual((second.status_code, second.json()["detail"]), (409, "an analysis is already running"))
        self.wait_idle()
        self.assertEqual(app.ANALYSIS["run_id"], "ffff0006")
        with mock.patch.dict(app.STATE, {"running": True}):
            r = self.client.post("/api/analysis/run", headers=CSRF)
        self.assertEqual(r.status_code, 409)
        self.assertIn("experiment is running", r.json()["detail"])
        self.assertEqual(len(self.calls), 1)

    def test_new_posts_need_the_csrf_header(self):
        self.assertEqual(self.client.post("/api/analysis/run").status_code, 403)
        self.assertEqual(self.client.post("/api/analysis/auto", json={"enabled": True}).status_code, 403)
        self.assertFalse(app.ANALYSIS["auto"])

    def test_auto_is_off_by_default_and_only_runs_after_an_experiment(self):
        self.assertFalse(self.client.get("/api/status").json()["analysis"]["auto"])
        with mock.patch.dict(app.STATE, {"phase": "done"}):
            self.assertFalse(app._maybe_auto_analyse())                      # off: nothing starts
        self.assertEqual(self.client.post("/api/analysis/auto", headers=CSRF, json={"enabled": "yes"}).status_code, 422)
        self.assertEqual(self.client.post("/api/analysis/auto", headers=CSRF, json={"enabled": True}).json(), {"auto": True})
        with mock.patch.dict(app.STATE, {"phase": "done", "running": True}):
            self.assertFalse(app._maybe_auto_analyse())                      # an experiment is still running
        with mock.patch.dict(app.STATE, {"phase": "refused"}):
            self.assertFalse(app._maybe_auto_analyse())                      # nothing new to analyse
        with mock.patch.dict(app.STATE, {"phase": "done"}):
            self.assertTrue(app._maybe_auto_analyse())
            self.assertFalse(app._maybe_auto_analyse())                      # already running: no second start
        self.wait_idle()
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
