import json
import threading
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import inject as inject_module
from traffic_spike import LocustController, TrafficSpikeExecutor


class LocustHandler(BaseHTTPRequestHandler):
    runtime = {"state": "running", "users": 5, "spawn_rate": 1.0, "rps": 2.0}
    mutations = []

    def log_message(self, format, *args):
        return

    def do_GET(self):
        if self.path == "/stats/requests":
            self._json({
                "state": self.runtime["state"],
                "user_count": self.runtime["users"],
                "current_rps": self.runtime["rps"],
            })
            return
        if self.path == "/stop":
            self.mutations.append(("stop",))
            self.runtime.update(state="stopped", users=0, rps=0.0)
            self._json({"success": True})
            return
        if self.path == "/":
            template = {
                "state": self.runtime["state"],
                "user_count": self.runtime["users"],
                "spawn_rate": self.runtime["spawn_rate"],
                "profile": None,
                "version": "2.46.6",
                "users": {
                    "WebsiteBrowserUser": {"weight": 1},
                    "WebsiteUser": {"weight": 9},
                },
            }
            body = f"<html>\n<script>window.templateArgs = {json.dumps(template)};</script>\n</html>"
            encoded = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        self.send_error(404)

    def do_POST(self):
        if self.path != "/swarm":
            self.send_error(404)
            return
        size = int(self.headers.get("Content-Length", "0"))
        values = parse_qs(self.rfile.read(size).decode())
        users = int(values["user_count"][0])
        spawn_rate = float(values["spawn_rate"][0])
        self.mutations.append(("swarm", users, spawn_rate))
        self.runtime.update(state="running", users=users, spawn_rate=spawn_rate, rps=float(users))
        self._json({"success": True})

    def _json(self, item):
        body = json.dumps(item).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TrafficSpikeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        LocustHandler.runtime = {"state": "running", "users": 5, "spawn_rate": 1.0, "rps": 2.0}
        LocustHandler.mutations = []
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), LocustHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        LocustHandler.runtime = {"state": "running", "users": 5, "spawn_rate": 1.0, "rps": 2.0}
        LocustHandler.mutations = []

    def test_real_http_injection_verification_and_retry_safe_restore(self):
        controller = LocustController(
            f"http://127.0.0.1:{self.server.server_port}",
            request_timeout_seconds=1,
            state_timeout_seconds=1,
            poll_interval_seconds=0.01,
        )
        executor = TrafficSpikeExecutor(controller)
        plan = {
            "target": "frontend",
            "fault": "traffic_spike",
            "magnitude": 8,
            "duration_s": 20,
            "parameters": {
                "baseline_users": 5,
                "peak_users": 8,
                "spawn_rate_per_second": 2,
                "duration_seconds": 20,
                "journey_profile": "astronomy-shop-default",
            },
        }
        original_active_file = inject_module.ACTIVE_FILE
        with tempfile.TemporaryDirectory() as directory:
            inject_module.ACTIVE_FILE = Path(directory) / "active_experiment.json"
            try:
                handle = inject_module.inject(plan, "exp-integration", traffic_executor=executor)
                self.assertTrue(inject_module.ACTIVE_FILE.exists())
                self.assertTrue(inject_module.verify_active(handle, traffic_executor=executor)["verified"])
                self.assertEqual(LocustHandler.runtime["users"], 8)
                self.assertTrue(inject_module.rollback(handle, traffic_executor=executor))
                self.assertFalse(inject_module.ACTIVE_FILE.exists())
                self.assertEqual(LocustHandler.runtime["users"], 5)
                self.assertEqual(LocustHandler.runtime["spawn_rate"], 1.0)
                mutations = len(LocustHandler.mutations)
                self.assertTrue(inject_module.rollback(handle, traffic_executor=executor))
                self.assertEqual(len(LocustHandler.mutations), mutations)
                self.assertTrue(inject_module.verify_recovered(handle, traffic_executor=executor)["verified"])
            finally:
                inject_module.ACTIVE_FILE = original_active_file


if __name__ == "__main__":
    unittest.main()
