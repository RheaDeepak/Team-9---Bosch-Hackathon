import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import activity
from agents import activity_store


class AgentStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def test_sqlite_restart_recovery_and_event_ordering(self):
        recorder = activity_store.Recorder("abcd0001", self.directory, "planning", "planning-v1")
        recorder.emit("node", "prepared", node="prepare_candidates")
        recorder.emit("decision", "chosen", node="validate_decision")
        recorder.finish(source="rank_agent", result={"chosen": "candidate-1"})
        with mock.patch.object(activity_store, "_events", []), mock.patch.object(activity_store, "_seq", 0):
            self.assertEqual(activity_store.latest("planning", self.directory), "abcd0001")
            events = activity_store.load("abcd0001", self.directory)
        self.assertEqual([event["step"] for event in events], [1, 2])
        self.assertEqual([event["node"] for event in events], ["prepare_candidates", "validate_decision"])

    def test_mode_filter_and_concurrent_readers(self):
        activity_store.Recorder("abcd0002", self.directory, "planning").emit("thought", "planning")
        activity_store.Recorder("abcd0003", self.directory, "analysis").emit("thought", "analysis")
        self.assertEqual([row["run_id"] for row in activity_store.runs(self.directory, "analysis")], ["abcd0003"])
        with ThreadPoolExecutor(max_workers=8) as pool:
            reads = list(pool.map(lambda _: activity_store.load("abcd0002", self.directory), range(24)))
        self.assertTrue(all(rows and rows[0]["summary"] == "planning" for rows in reads))

    def test_arguments_are_sanitized(self):
        recorder = activity_store.Recorder("abcd0004", self.directory, "planning")
        recorder.emit("tool_call", "call", args={"api_key": "secret", "pattern": "x" * 900})
        args = activity_store.load("abcd0004", self.directory)[0]["args"]
        self.assertEqual(args["api_key"], "<redacted>")
        self.assertEqual(len(args["pattern"]), 500)

    def test_legacy_jsonl_session_is_still_readable(self):
        event = {"seq": 7, "run_id": "abcd0005", "mode": "analysis", "step": 1,
                 "ts": "2026-10-11T00:00:00+00:00", "type": "decision", "summary": "legacy"}
        (self.directory / "abcd0005.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
        with mock.patch.object(activity, "SESSIONS_DIR", self.directory):
            self.assertEqual(activity.load_session("abcd0005")[0]["summary"], "legacy")


if __name__ == "__main__":
    unittest.main()
