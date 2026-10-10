import unittest

import networkx as nx

import policy


def graph():
    g = nx.DiGraph()
    g.add_edges_from([("frontend", "cart"), ("checkout", "cart"), ("cart", "valkey-cart"), ("frontend", "checkout"),
                      ("frontend-proxy", "frontend"), ("cart", "flagd")])
    return g


def plan(**over):
    return {"target": "cart", "fault": "container_stop", "magnitude": 0, "duration_s": 60,
            "hypothesis": "h", "reasoning": "r", **over}


class PolicyTests(unittest.TestCase):
    def test_valid_plan_passes_every_check(self):
        r = policy.validate(plan(), graph())
        self.assertTrue(r["valid"])
        self.assertEqual(r["reasons"], [])
        self.assertTrue(all(c["passed"] for c in r["checks"]))

    def test_every_failing_reason_is_reported_not_just_the_first(self):
        r = policy.validate(plan(target="flagd", fault="network_delay", magnitude=5000, duration_s=500), graph())
        self.assertFalse(r["valid"])
        self.assertEqual(len(r["reasons"]), 3)
        self.assertEqual(sorted(c["name"] for c in r["checks"] if not c["passed"]),
                         ["Duration within cap", "Magnitude within cap", "Target is not protected"])

    def test_blast_radius_is_every_direct_and_indirect_caller(self):
        self.assertEqual(policy.validate(plan(target="cart"), graph())["blast_radius"],
                         ["checkout", "frontend", "frontend-proxy"])

    def test_unknown_target_unknown_fault_and_missing_fields(self):
        r = policy.validate({"target": "nope", "fault": "cpu_hog"}, graph())
        self.assertFalse(r["valid"])
        joined = " ".join(r["reasons"])
        for needle in ("not a service in the dependency map", "not allowed", "missing field: duration_s"):
            self.assertIn(needle, joined)
        self.assertEqual(r["blast_radius"], [])

    def test_caps_are_inclusive_and_booleans_are_not_numbers(self):
        self.assertTrue(policy.validate(plan(duration_s=policy.MAX_DURATION_S), graph())["valid"])
        self.assertFalse(policy.validate(plan(duration_s=policy.MAX_DURATION_S + 1), graph())["valid"])
        ok = plan(fault="network_delay", magnitude=policy.MAX_NETWORK_DELAY_MS)
        self.assertTrue(policy.validate(ok, graph())["valid"])
        self.assertFalse(policy.validate(plan(duration_s=True), graph())["valid"])

    def test_non_object_plan_is_rejected(self):
        self.assertFalse(policy.validate("stop everything", graph())["valid"])


if __name__ == "__main__":
    unittest.main()
