import copy
import unittest

from fixtures import baseline, flows, graph, run, traffic_capability

import ranker
from policy import validate


def best(ranking, target):
    return min((c for c in ranking if c["target"] == target), key=lambda c: c["rank"])


def find(ranking, cid):
    return next(c for c in ranking if c["id"] == cid)


class RankerTest(unittest.TestCase):
    def setUp(self):
        self.g, self.base, self.flows = graph(), baseline(), flows()

    def rank(self, history=(), weights=ranker.WEIGHTS):
        return ranker.rank(self.g, self.base, self.flows, list(history), weights=weights)

    def test_protected_and_stopped_services_are_excluded(self):
        ranking = self.rank()
        targets = {c["target"] for c in ranking}
        self.assertNotIn("flagd", targets)   # protected
        self.assertNotIn("ghost", targets)   # no running container in the baseline
        self.assertEqual(targets, {"web", "checkout", "pay", "queue", "worker"})
        for c in ranking:  # every candidate passes policy and has a why-line
            plan = {**{k: c[k] for k in ("target", "fault", "magnitude", "duration_s")},
                    "hypothesis": "h", "reasoning": "r"}
            self.assertTrue(validate(plan, self.g)["valid"], c["id"])
            self.assertTrue(c["why"])

    def test_untested_sync_checkout_service_outranks_tested_async_only(self):
        ranking = self.rank([run("queue", "container_stop", 0)])
        pay, queue = best(ranking, "pay"), best(ranking, "queue")
        self.assertTrue(pay["features"]["checkout_path"]["raw"])
        self.assertEqual(queue["features"]["sync_callers"]["raw"], 0)
        self.assertLess(pay["rank"], queue["rank"])
        self.assertGreater(pay["risk"], queue["risk"])

    def test_exact_repeat_gets_lower_informativeness(self):
        fresh = find(self.rank(), "pay:network_delay:500")
        ranking = self.rank([run("pay", "network_delay", 500)])
        repeat = find(ranking, "pay:network_delay:500")
        self.assertLess(repeat["info"], fresh["info"])
        self.assertLess(repeat["info"], find(ranking, "pay:network_delay:200")["info"])
        self.assertEqual(repeat["features"]["novelty"]["raw"], "exact_repeat")
        self.assertIn("exact repeat (x1)", repeat["why"])
        # a smaller step that held makes the next step up an escalation
        self.assertEqual(find(ranking, "pay:network_delay:1000")["features"]["escalation"]["score"], 1.0)

    def test_weights_change_the_order(self):
        # pay is high-risk but fully explored; worker is low-risk and never tested
        history = [run("pay", "container_stop", 0)] + [run("pay", "network_delay", m) for m in (200, 500, 1000)]
        risk_only, info_only = copy.deepcopy(ranker.WEIGHTS), copy.deepcopy(ranker.WEIGHTS)
        risk_only["priority"] = {"risk": 1.0, "info": 0.0, "safety": 0.0, "prior_abort": 0.0}
        info_only["priority"] = {"risk": 0.0, "info": 1.0, "safety": 0.0, "prior_abort": 0.0}
        by_risk, by_info = self.rank(history, risk_only), self.rank(history, info_only)
        self.assertLess(best(by_risk, "pay")["rank"], best(by_risk, "worker")["rank"])
        self.assertLess(best(by_info, "worker")["rank"], best(by_info, "pay")["rank"])

    def test_guard_abort_penalises_same_or_bigger_magnitude(self):
        ranking = self.rank([run("pay", "network_delay", 500, guard_aborted=True)])
        self.assertEqual(find(ranking, "pay:network_delay:1000")["features"]["prior_abort"]["score"], 1.0)
        self.assertEqual(find(ranking, "pay:network_delay:200")["features"]["prior_abort"]["score"], 0.0)

    def test_entry_point_stop_is_penalised_and_gets_factors(self):
        ranking = self.rank()
        stop, delay = find(ranking, "web:container_stop:0"), find(ranking, "web:network_delay:200")
        self.assertEqual(stop["outage"], 1.0)                      # web has no callers: users hit it directly
        self.assertGreater(delay["outage"], 0.0)                   # a delay at the front door is half an outage
        self.assertLess(delay["outage"], stop["outage"])
        self.assertLess(stop["priority"], delay["priority"])
        self.assertLess(find(ranking, "queue:container_stop:0")["outage"], stop["outage"])   # async-only: breaks fewer calls
        names = {f["name"]: f for f in stop["factors"]}
        self.assertEqual(names["outage"]["kind"], "penalty")
        self.assertLess(names["outage"]["contribution"], 0)
        self.assertIn("entry point", names["outage"]["label"])
        self.assertIn("entry point", stop["why"])
        self.assertEqual([abs(f["contribution"]) for f in stop["factors"]],
                         sorted((abs(f["contribution"]) for f in stop["factors"]), reverse=True))

    def test_partial_degradation_is_rewarded(self):
        ranking = self.rank()
        mild, severe = find(ranking, "pay:network_delay:200"), find(ranking, "pay:network_delay:1000")
        self.assertGreater(mild["features"]["partial"]["score"], severe["features"]["partial"]["score"])
        self.assertGreater(mild["features"]["partial"]["score"], find(ranking, "web:container_stop:0")["features"]["partial"]["score"])
        self.assertEqual({f["name"] for f in mild["factors"]} >= {"partial", "outage", "novelty"}, True)

    def test_outage_weight_can_be_turned_off(self):
        w = copy.deepcopy(ranker.WEIGHTS)
        w["priority"]["outage"] = 0.0
        self.assertGreater(find(self.rank(weights=w), "web:container_stop:0")["priority"],
                           find(self.rank(), "web:container_stop:0")["priority"])

    def test_traffic_spike_candidate_requires_complete_capability_readiness(self):
        g, base = self.g.copy(), copy.deepcopy(self.base)
        g.add_edge("frontend", "checkout", mode="sync", type="sync-http")
        base["services"]["frontend"] = copy.deepcopy(base["services"]["web"])
        base["containers"]["frontend"] = {"running": {"mean": 1}}

        unavailable = traffic_capability(rollback_ready=False)
        hidden = ranker.rank(g, base, self.flows, [], traffic_capability=unavailable)
        self.assertNotIn("traffic_spike", {candidate["fault"] for candidate in hidden})

        ranking = ranker.rank(g, base, self.flows, [], traffic_capability=traffic_capability())
        candidate = find(ranking, "frontend:traffic_spike:8")
        self.assertEqual(candidate["parameters"]["baseline_users"], 5)
        self.assertEqual(candidate["parameters"]["peak_users"], 8)
        self.assertEqual(candidate["candidate_id"], "traffic-spike-core-v1")
        plan = {**{key: candidate[key] for key in ("target", "fault", "magnitude", "duration_s", "parameters")},
                "hypothesis": "h", "reasoning": "r"}
        self.assertTrue(validate(plan, g)["valid"])


if __name__ == "__main__":
    unittest.main()
