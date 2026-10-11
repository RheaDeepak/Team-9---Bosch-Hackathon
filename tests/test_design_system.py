"""The two UIs (static/ served by app.py, frontend/ served by api.py) share one design system and one component-maturity file.
These tests keep the copies identical and keep the pages honest: no placeholder numbers, no emoji icons, real code behind every label."""
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
    def test_both_uis_use_identical_copies_of_the_shared_files(self):
        for name in SHARED:
            self.assertEqual(text(ROOT / "static" / name), text(ROOT / "frontend" / name),
                             f"{name} differs between static/ and frontend/: edit one and copy it to the other")

    def test_both_pages_load_the_shared_design_system(self):
        for page in (ROOT / "static" / "index.html", ROOT / "frontend" / "index.html"):
            html = text(page)
            self.assertRegex(html, r'href="(\./)?chaos-ui\.css"', page)
            self.assertRegex(html, r'src="(\./)?chaos-ui\.js"', page)

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
    def test_no_placeholder_numbers_or_fake_session_in_the_api_ui(self):
        html = text(ROOT / "frontend" / "index.html")
        for fake in ("CC-042", "99.8%", "0.4%", "184 ms", "42.1 rps"):
            self.assertNotIn(fake, html)
        for metric in ("baselineAvailability", "baselineErrors", "baselineLatency", "baselineThroughput", "liveLatency"):
            self.assertRegex(html, rf'id="{metric}">No data<')

    def test_demo_data_is_only_used_behind_the_explicit_demo_flag(self):
        js = text(ROOT / "frontend" / "js" / "app.js")
        self.assertIn('get("demo") === "1"', js)
        self.assertNotRegex(js, r"catch\s*\{\s*state\.readiness = mockReadiness")           # the old silent fallback to demo data
        load = js[js.index("async function loadData"):js.index("elements.proposalForm")]
        self.assertEqual(load.count("mockReadiness"), 1)
        self.assertIn("if (demo) {", load)

    def test_no_emoji_used_as_interface_icons(self):
        for page in (ROOT / "static" / "index.html", ROOT / "frontend" / "index.html", ROOT / "static" / "chaos-ui.js", ROOT / "frontend" / "js" / "app.js"):
            self.assertIsNone(EMOJI.search(text(page)), f"emoji icon in {page.name}")

    def test_the_ui_does_not_call_endpoints_that_do_not_exist(self):
        import api  # noqa: F401  (the PRD endpoints the brief lists must exist in api.py)
        routes = {r.path for r in api.app.routes}
        for path in ("/api/health", "/api/readiness", "/api/topology", "/api/baselines/latest", "/api/experiments/propose",
                     "/api/experiments/{experiment_id}/validate", "/api/experiments/{experiment_id}/approve",
                     "/api/experiments/{experiment_id}/abort", "/api/experiments/{experiment_id}", "/api/experiments/{experiment_id}/events",
                     "/api/reports/{experiment_id}", "/api/findings"):
            self.assertIn(path, routes)


if __name__ == "__main__":
    unittest.main()
