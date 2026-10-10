import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from fixtures import baseline, flows, graph, run

import activity
import app
import ranker


class AgentApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.client = TestClient(app.app, base_url="http://127.0.0.1:8000")

    def ranking(self):
        return ranker.rank(graph(), baseline(), flows(), [run("pay", "network_delay", 200)]), graph()

    def test_ranking_endpoint_lists_candidates_with_factors(self):
        with mock.patch.object(ranker, "rank_from_files", self.ranking):
            body = self.client.get("/api/ranking").json()
        self.assertGreater(body["total"], 5)
        first = body["candidates"][0]
        self.assertEqual(set(first) >= {"id", "priority", "risk", "info", "outage", "why", "factors", "adjusted_priority"}, True)
        self.assertTrue(all(f["label"] and f["kind"] in ("risk", "info", "penalty") for f in first["factors"]))
        self.assertIsNone(body["chosen"])

    def test_ranking_endpoint_merges_the_agents_choice(self):
        ranking, _ = self.ranking()
        pick = ranking[3]["id"]
        plan = {"reasoning": "r", "ranking": {"chosen": pick, "source": "rank_agent", "note": "n", "run_id": "abcd1234", "top_k": [
            {"candidate_id": pick, "reason": "callers have no timeout", "adjusted_risk": 0.7, "adjusted_priority": 0.6,
             "evidence": [{"tool": "get_edges", "finding": "sync caller"}]}]}}
        with mock.patch.object(ranker, "rank_from_files", self.ranking), mock.patch.dict(app.STATE, {"plan": plan}):
            body = self.client.get("/api/ranking").json()
        self.assertEqual((body["chosen"], body["chosen_reason"], body["source"]), (pick, "callers have no timeout", "rank_agent"))
        row = next(c for c in body["candidates"] if c["id"] == pick)
        self.assertEqual((row["adjusted_priority"], row["in_top_k"], row["evidence"][0]["tool"]), (0.6, True, "get_edges"))

    def test_ranking_endpoint_reports_missing_baseline(self):
        with mock.patch.object(ranker, "rank_from_files", side_effect=ValueError("no baseline")):
            r = self.client.get("/api/ranking")
        self.assertEqual(r.status_code, 409)

    def test_events_poll_returns_only_new_steps_and_sessions_are_listed(self):
        rec = activity.Recorder("0badf00d", self.dir)
        rec.emit("thought", "investigating")
        rec.emit("tool_call", "get_edges", tool="get_edges", args={"service": "pay"})
        with mock.patch.object(activity, "SESSIONS_DIR", self.dir):
            first = self.client.get("/api/agent-events").json()
            self.assertEqual((first["run_id"], len(first["events"])), ("0badf00d", 2))
            rec.emit("decision", "chose x", fallback=False)
            later = self.client.get(f"/api/agent-events?run=0badf00d&after={first['last']}").json()
            self.assertEqual([e["type"] for e in later["events"]], ["decision"])
            self.assertEqual(self.client.get("/api/agent-events?run=../../etc&after=0").json()["events"], [])
            sessions = self.client.get("/api/agent-sessions").json()
        self.assertEqual(sessions[0]["run_id"], "0badf00d")
        self.assertEqual(sessions[0]["steps"], 3)

    def test_one_row_per_service_and_fault_with_the_other_magnitudes_inside(self):
        with mock.patch.object(ranker, "rank_from_files", self.ranking):
            body = self.client.get("/api/ranking").json()
        keys = [(c["target"], c["fault"]) for c in body["candidates"]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(body["services"], len({k[0] for k in keys}))
        self.assertEqual(body["total"], sum(1 + len(c["variants"]) for c in body["candidates"]))
        self.assertIn(("pay", "container_stop"), keys)                     # a stop is never hidden behind a delay
        pay = next(c for c in body["candidates"] if c["target"] == "pay" and c["fault"] == "network_delay")
        self.assertEqual(len(pay["variants"]), 2)                          # 200 is the row; 500 and 1000 are inside
        self.assertTrue(all(v["fault"] == "network_delay" and v["rank"] > pay["rank"] for v in pay["variants"]))
        self.assertEqual([c["rank"] for c in body["candidates"]], sorted(c["rank"] for c in body["candidates"]))

    def test_the_row_shows_the_variant_the_agent_examined(self):
        ranking, _ = self.ranking()
        stop = next(c for c in ranking if c["id"] == "pay:container_stop:0")
        plan = {"ranking": {"chosen": stop["id"], "top_k": [{"candidate_id": stop["id"], "reason": "r", "evidence": []}]}}
        with mock.patch.object(ranker, "rank_from_files", self.ranking), mock.patch.dict(app.STATE, {"plan": plan}):
            body = self.client.get("/api/ranking").json()
        pay = next(c for c in body["candidates"] if c["id"] == "pay:container_stop:0")
        self.assertTrue(pay["in_top_k"])
        self.assertEqual(pay["variants"], [])


if __name__ == "__main__":
    unittest.main()
