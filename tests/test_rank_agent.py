import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from langchain_core.messages import AIMessage

from fixtures import baseline, flows, graph, traffic_capability

import activity
import rank_agent
from agents.models import PlanningDecision
from agents.tools import READ_ONLY_TOOL_NAMES
from policy import validate


class SearchSourceSandboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "demo"
        (self.root / "src" / "checkout").mkdir(parents=True)
        (self.root / "src" / "checkout" / "main.go").write_text(
            "func charge(ctx context.Context) {\n\tctx, cancel := context.WithTimeout(ctx, time.Second)\n}\n"
        )
        self.outside = base / "outside"
        self.outside.mkdir()
        (self.outside / "secret.go").write_text("WithTimeout secret\n")
        env = mock.patch.dict(os.environ, {rank_agent.SRC_ENV_VAR: str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_finds_matches_inside_root(self):
        out = rank_agent.search_source("checkout", "WithTimeout")
        self.assertEqual([item["at"] for item in out["matches"]], ["src/checkout/main.go:2"])

    def test_refuses_paths_outside_root(self):
        for bad in ("../outside", "..", "../../outside", str(self.outside), "checkout/../../outside", "/etc", "C:\\Windows"):
            with self.assertRaises(rank_agent.ToolError, msg=bad):
                rank_agent.search_source(bad, "WithTimeout")
        toolbox = rank_agent.Toolbox(graph(), baseline(), [], [])
        self.assertIn("error", toolbox.call("search_source", {"service": "../outside", "pattern": "x"}))

    def test_does_not_follow_symlinks_out_of_root(self):
        try:
            os.symlink(self.outside, self.root / "src" / "checkout" / "link", target_is_directory=True)
            os.symlink(self.outside / "secret.go", self.root / "src" / "checkout" / "secret.go")
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks here")
        self.assertEqual(rank_agent.search_source("checkout", "secret")["matches"], [])

    def test_regex_and_size_limits(self):
        with self.assertRaises(rank_agent.ToolError):
            rank_agent.search_source("checkout", "(a+)+")
        with self.assertRaises(rank_agent.ToolError):
            rank_agent.search_source("checkout", "x" * (rank_agent.MAX_PATTERN_CHARS + 1))

    def test_compose_tool_redacts_secret_values(self):
        (self.root / "compose.yaml").write_text(
            "services:\n  checkout:\n    environment:\n"
            "      API_KEY: do-not-expose\n      RETRY_COUNT: '3'\n      OTHER: visible-but-not-returned\n",
            encoding="utf-8",
        )
        output = rank_agent.get_service_env("checkout")
        self.assertEqual(output["resilience_settings"], {"RETRY_COUNT": "3"})
        self.assertNotIn("do-not-expose", str(output))

    def test_falls_back_to_target_repository_path(self):
        env = {rank_agent.SRC_ENV_VAR: "", rank_agent.SRC_FALLBACK_VAR: str(self.root)}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(rank_agent._src_root(), self.root.resolve())


class FakeModels:
    def __init__(self, tool_responses=(), decisions=()):
        self.tool_responses = iter(tool_responses)
        self.decisions = iter(decisions)

    def invoke_tools(self, system, messages, tools):
        value = next(self.tool_responses)
        if isinstance(value, Exception):
            raise value
        return value, "fake-model"

    def invoke_structured(self, system, messages, schema):
        value = next(self.decisions)
        if isinstance(value, Exception):
            raise value
        return schema.model_validate(value), "fake-model"


def ready_message():
    return AIMessage(content="ready")


def tool_message(*calls):
    return AIMessage(content="", tool_calls=[
        {"name": name, "args": args, "id": f"call-{index}", "type": "tool_call"}
        for index, (name, args) in enumerate(calls, 1)
    ])


class PlanningGraphTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.g = graph()
        patcher = mock.patch.object(activity, "SESSIONS_DIR", self.directory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def candidates(self, graph_=None, baseline_=None, capability=None):
        return rank_agent.ranker.rank(
            graph_ or self.g, baseline_ or baseline(), flows(), [], traffic_capability=capability
        )

    def decision(self, chosen=None, graph_=None, capability=None):
        ranking = self.candidates(graph_=graph_, capability=capability)
        top = rank_agent.top_k(ranking, 3)
        selected = chosen or top[0]["id"]
        target = next(candidate["target"] for candidate in ranking if candidate["id"] == selected)
        return {
            "candidate_id": selected,
            "hypothesis": "The selected dependency degrades and then returns to baseline.",
            "predictions": [{"svc": target, "metric": "p95_ms", "direction": "up", "min_factor": 1.2}],
            "adjusted_ranking": [],
        }

    def run_agent(self, fake, **kwargs):
        return rank_agent.run_agent(
            graph=kwargs.pop("graph", self.g),
            baseline=kwargs.pop("baseline", baseline()),
            flows=flows(),
            history=[],
            model_client=fake,
            checkpointer_path=self.directory / "agent_runs.sqlite3",
            **kwargs,
        )

    def test_tool_route_then_valid_structured_decision(self):
        top = rank_agent.top_k(self.candidates(), 3)
        fake = FakeModels(
            [tool_message(("get_edges", {"service": top[0]["target"]})), ready_message()],
            [self.decision(top[1]["id"])],
        )
        out = self.run_agent(fake)
        self.assertEqual((out["source"], out["chosen"], out["tool_calls"]), ("rank_agent", top[1]["id"], 1))
        self.assertTrue(validate(out["plan"], self.g)["valid"])
        events = activity.load_session(out["run_id"], self.directory)
        self.assertIn("execute_tools", {event.get("node") for event in events})

    def test_malformed_output_and_provider_failure_fall_back(self):
        for fake in (
            FakeModels([ready_message()], [ValueError("malformed")]),
            FakeModels([RuntimeError("provider unavailable")], []),
        ):
            out = self.run_agent(fake)
            self.assertEqual(out["source"], "ranker_fallback")
            self.assertEqual(out["chosen"], out["top_k"][0]["candidate_id"])
            self.assertTrue(validate(out["plan"], self.g)["valid"])

    def test_unknown_candidate_and_prediction_fall_back(self):
        bad_candidate = self.decision()
        bad_candidate["candidate_id"] = "flagd:container_stop:0"
        bad_prediction = self.decision()
        bad_prediction["predictions"][0]["svc"] = "does-not-exist"
        for decision in (bad_candidate, bad_prediction):
            out = self.run_agent(FakeModels([ready_message()], [decision]))
            self.assertEqual(out["source"], "ranker_fallback")
            self.assertIn("invalid planning decision", out["note"])

    def test_multiple_tool_calls_share_one_hard_budget(self):
        calls = [("get_history", {"service": "pay"}) for _ in range(rank_agent.MAX_TOOL_CALLS + 4)]
        out = self.run_agent(FakeModels([tool_message(*calls)], [self.decision()]))
        self.assertEqual(out["tool_calls"], rank_agent.MAX_TOOL_CALLS)
        events = activity.load_session(out["run_id"], self.directory)
        self.assertEqual(sum(event["type"] == "tool_call" for event in events), rank_agent.MAX_TOOL_CALLS)

    def test_selected_plan_copies_candidate_parameters(self):
        ranking = self.candidates()
        selected = rank_agent.top_k(ranking, 3)[1]
        out = self.run_agent(FakeModels([ready_message()], [self.decision(selected["id"])]))
        for field in ("target", "fault", "magnitude", "duration_s"):
            self.assertEqual(out["plan"][field], selected[field])

    def test_capacity_is_restricted_and_unavailable_is_explicit(self):
        with self.assertRaises(rank_agent.CapabilityUnavailable):
            self.run_agent(FakeModels([], []), goal="capacity", traffic_capability={"selectable": False})
        g = self.g.copy()
        base = baseline()
        g.add_edge("frontend", "checkout", mode="sync", type="sync-http")
        base["services"]["frontend"] = dict(base["services"]["web"])
        base["containers"]["frontend"] = {"running": {"mean": 1}}
        capability = traffic_capability()
        ranking = self.candidates(g, base, capability)
        traffic = next(candidate for candidate in ranking if candidate["fault"] == "traffic_spike")
        decision = {
            "candidate_id": traffic["id"],
            "hypothesis": "Bounded traffic reveals saturation and then recovers.",
            "predictions": [{"svc": "frontend", "metric": "p95_ms", "direction": "up", "min_factor": 1.2}],
            "adjusted_ranking": [],
        }
        out = self.run_agent(FakeModels([ready_message()], [decision]), graph=g, baseline=base,
                             goal="capacity", traffic_capability=capability)
        self.assertEqual(out["plan"]["fault"], "traffic_spike")
        self.assertEqual(out["plan"]["parameters"], traffic["parameters"])

    def test_tools_are_read_only_and_agent_modules_do_not_import_mutators(self):
        forbidden_tools = {"inject", "execute", "approve", "rollback", "shell", "docker"}
        self.assertFalse(READ_ONLY_TOOL_NAMES & forbidden_tools)
        package = Path(rank_agent.__file__).parent / "agents"
        for filename in ("planning.py", "analysis.py", "tools.py"):
            source = (package / filename).read_text(encoding="utf-8").lower()
            self.assertNotIn("import inject", source)
            self.assertNotIn("import docker", source)
            self.assertNotIn("subprocess", source)


class CandidateAvailabilityTest(unittest.TestCase):
    def test_an_unavailable_candidate_is_never_shortlisted(self):
        spike = {"id": "frontend:traffic_spike:8", "target": "frontend", "fault": "traffic_spike", "priority": 0.99,
                 "unavailable": True}
        others = [{"id": f"{t}:network_delay:200", "target": t, "fault": "network_delay", "priority": 0.5} for t in ("a", "b", "c")]
        self.assertNotIn(spike, rank_agent.top_k([spike, *others], 3))

if __name__ == "__main__":
    unittest.main()
