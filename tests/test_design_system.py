"""The canonical app.py/static UI uses the shared design system and honest data."""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED = ("chaos-ui.css", "chaos-ui.js", "component-meta.json")
IDS = ["topology", "planner", "policy", "executor", "guard", "analysis"]
EMOJI = re.compile("[✅❌⚠✔✖\U0001F300-\U0001FAFF]")


def text(path):
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


class SharedFilesTest(unittest.TestCase):
    def test_canonical_page_loads_the_shared_design_system(self):
        html = text(ROOT / "static" / "index.html")
        self.assertRegex(html, r'href="(\./)?chaos-ui\.css"')
        self.assertRegex(html, r'src="(\./)?chaos-ui\.js"')

    def test_the_design_tokens_from_the_brief(self):
        css = text(ROOT / "static" / "chaos-ui.css")
        for token in ("#0B0F14", "#111821", "#161F2B", "#38BDF8"):
            self.assertIn(token, css)
        self.assertIn("prefers-reduced-motion", css)


class ComponentMetaTest(unittest.TestCase):
    def setUp(self):
        self.meta = json.loads(text(ROOT / "static" / "component-meta.json"))

    def test_every_component_is_described_once_with_a_valid_maturity(self):
        components = self.meta["components"]
        self.assertEqual([c["id"] for c in components], IDS)
        for c in components:
            self.assertIn(c["implementation"], ("implemented", "in_development", "planned"), c["id"])
            self.assertTrue(c["name"] and c["summary"] and c["evidence"], c["id"])
            for note in c["notes"]:
                self.assertIn(note["status"], ("implemented", "in_development", "planned"))

    def test_each_component_points_at_a_module_that_exists(self):
        for c in self.meta["components"]:
            self.assertTrue((ROOT / c["module"]).is_file(), f"{c['id']}: {c['module']} not found")

    def test_maturity_labels_match_the_code(self):
        policy = text(ROOT / "policy.py")
        executor = next(c for c in self.meta["components"] if c["id"] == "executor")
        for fault in ("container_stop", "network_delay", "traffic_spike"):
            self.assertIn("TRAFFIC_SPIKE" if fault == "traffic_spike" else fault, policy)
        # packet loss is listed as planned only while the allowlist does not contain it
        planned = [n["text"] for n in executor["notes"] if n["status"] == "planned"]
        self.assertEqual(planned == ["Packet-loss fault"], "packet_loss" not in re.search(r"ALLOWED_FAULTS = \((.*?)\)", policy).group(1))


class HonestPagesTest(unittest.TestCase):
    def test_no_placeholder_numbers_or_fake_session_in_the_canonical_ui(self):
        html = text(ROOT / "static" / "index.html")
        for fake in ("CC-042", "99.8%", "0.4%", "184 ms", "42.1 rps"):
            self.assertNotIn(fake, html)

    def test_canonical_ui_has_no_silent_mock_data_fallback(self):
        html = text(ROOT / "static" / "index.html")
        self.assertNotIn("mockReadiness", html)
        self.assertNotIn("demo=1", html)

    def test_no_emoji_used_as_interface_icons(self):
        for page in (ROOT / "static" / "index.html", ROOT / "static" / "chaos-ui.js"):
            self.assertIsNone(EMOJI.search(text(page)), f"emoji icon in {page.name}")

    def test_the_ui_does_not_call_endpoints_that_do_not_exist(self):
        import app
        routes = {route.path for route in app.app.routes}
        for path in ("/api/status", "/api/readiness", "/api/graph", "/api/propose", "/api/approve", "/api/stop",
                     "/api/analysis/latest", "/api/analysis/run", "/api/agent-events", "/api/agent-sessions", "/api/report"):
            self.assertIn(path, routes)


if __name__ == "__main__":
    unittest.main()
