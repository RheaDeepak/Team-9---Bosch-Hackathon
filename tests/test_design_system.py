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


class ActivityDockTest(unittest.TestCase):
    def test_dock_layout_rules_are_in_the_shared_stylesheet(self):
        css = text(ROOT / "static" / "chaos-ui.css")
        self.assertRegex(css, r"\.cc-dock \{[^}]*position:fixed;[^}]*top:calc\(var\(--topbar-h,64px\) \+ 12px\);[^}]*width:320px;[^}]*max-height:70vh")
        self.assertIn("body.cc-dock-open main", css)                     # wide screens: the dock's column is reserved
        self.assertIn("body.cc-dock-pill main", css)                     # the collapsed pill gets its own strip
        narrow = css[css.index("@media (max-width:899px)"):]
        self.assertIn("bottom:0", narrow)                                # narrow screens: a slim bar at the bottom
        self.assertIn("body { padding-bottom:64px; }", narrow)           # ... with room left below the content
        self.assertIn(":focus-visible", css)

    def test_the_page_mounts_the_dock(self):
        html = text(ROOT / "static" / "index.html")
        self.assertIn('id="ccDock"', html)
        self.assertIn("ChaosUI.createDock($('ccDock')", html)


class SingleStopTest(unittest.TestCase):
    """STOP exists once, in the Activity dock, and has one handler."""

    def setUp(self):
        self.static = text(ROOT / "static" / "index.html")
        self.css = text(ROOT / "static" / "chaos-ui.css")

    def test_the_other_stop_buttons_are_gone(self):
        self.assertNotIn('id="stop"', self.static)
        self.assertNotIn("$('stop')", self.static)
        self.assertNotRegex(self.static, r"#stop")
        self.assertIn("Use STOP in the Activity panel.", self.static)              # the live section points at it instead

    def test_one_stop_handler_and_the_dock_calls_it_directly(self):
        self.assertEqual(self.static.count("async function requestStop()"), 1)
        self.assertEqual(self.static.count("onStop: requestStop"), 1)
        self.assertEqual(self.static.count("'/api/stop'"), 1)                       # the only place the stop request is made
        shared = text(ROOT / "static" / "chaos-ui.js")
        self.assertEqual(shared.count("if (onStop) onStop();"), 1)                  # both dock STOP buttons share this one call
        self.assertNotIn(".click()", shared)                                        # no hidden button is being pressed any more

    def test_the_pending_state_is_driven_by_state_not_by_a_button_label(self):
        self.assertIn("stopPending = true; renderWorkflow();", self.static)
        self.assertIn("pending: stopPending", self.static)
        shared = text(ROOT / "static" / "chaos-ui.js")
        self.assertIn("Stop requested: rolling back", shared)
        self.assertIn("b.disabled = !!stop.disabled || !!stop.pending", shared)    # pending STOP cannot be pressed twice

    def test_stop_stays_reachable_collapsed_and_on_the_narrow_bar(self):
        css = self.css
        self.assertIn(".cc-dock.running:not(.open) .cc-dock-stop-mini:not([hidden]) { display:inline-flex", css)    # collapsed pill keeps STOP
        narrow = css[css.index("@media (max-width:899px)"):]
        self.assertIn(".cc-dock.running .cc-dock-stop-mini:not([hidden]) { display:inline-flex", narrow)            # bottom bar: expanded or not
        self.assertIn(".cc-dock-stop { display:none !important; }", narrow)                                         # ... so the big one is not a second STOP there
        rows = css[css.index(".cc-drows {"):css.index("}", css.index(".cc-drows {"))]
        self.assertIn("overflow-y:auto", rows)                                                                      # only the component list scrolls ...
        self.assertIn("overflow:hidden", css[css.index(".cc-dock-body {"):css.index("}", css.index(".cc-dock-body {"))])   # ... so stage, STOP and events never scroll out of view


class WorkflowSectionRemovedTest(unittest.TestCase):
    """The lifecycle and the six components now live only in the floating Activity dock."""

    def test_the_page_section_is_gone(self):
        html = text(ROOT / "static" / "index.html")
        for gone in ("workflowSec", "workflowTitle", "wfSteps", "wfAgents", "wfHeadline", "wfElapsed"):
            self.assertNotIn(gone, html, f"{gone} still in index.html")
        self.assertNotIn("Agent &amp; Workflow Activity", html)
        self.assertNotRegex(html, r">Workflow</a>")                           # no navigation link to a section that no longer exists

    def test_nothing_still_calls_the_removed_renderers(self):
        for path in (ROOT / "static" / "index.html", ROOT / "static" / "chaos-ui.js"):
            js = text(path)
            for gone in ("stepperHtml", "agentsHtml", "agentRowHtml"):
                self.assertNotIn(gone, js, f"{gone} still in {path.name}")
        css = text(ROOT / "static" / "chaos-ui.css")
        for gone in (".cc-steps", ".cc-step ", ".cc-agents", ".wf-note"):
            self.assertNotIn(gone, css)

    def test_the_dock_carries_what_the_section_showed(self):
        js = text(ROOT / "static" / "chaos-ui.js")
        self.assertIn("Deterministic policy, not the LLM, decides whether an experiment may run, and a human approves it before anything is injected.", js)
        self.assertIn('class="cc-dock-caption"', js)
        self.assertIn('data-part="stepText"', js)
        for part in ("cc-drow-detail", "cc-drow-meta", "cc-drow-notes"):
            self.assertIn(part, js)

    def test_card_numbers_have_no_gap(self):
        numbers = [int(n) for n in re.findall(r'<span class="eyebrow"><b>(\d+)</b>', text(ROOT / "static" / "index.html"))]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))


if __name__ == "__main__":
    unittest.main()
