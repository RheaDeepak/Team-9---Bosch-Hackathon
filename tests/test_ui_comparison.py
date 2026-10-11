"""Runs the node tests for the helpers inside static/index.html (chart comparison, agent status text) when node is installed."""
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).parent


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class UiHelpersTest(unittest.TestCase):
    def run_js(self, name):
        out = subprocess.run(["node", str(HERE / name)], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("all passed", out.stdout)

    def test_comparison_helpers(self):
        self.run_js("test_ui_comparison.js")

    def test_agent_status_text(self):
        self.run_js("test_ui_agent_status.js")

    def test_workflow_models(self):
        self.run_js("test_ui_workflow.js")

    def test_api_ui_package_tests(self):                 # frontend/js/*.test.js: api adapter, workflow models, graph layout
        out = subprocess.run(["node", "--test"], cwd=HERE.parent / "frontend", capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout[-2000:] + out.stderr)


if __name__ == "__main__":
    unittest.main()
