import copy
import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

import networkx as nx

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
        self.assertEqual(stop["outage"], 1.0)                      # the load generator (users) calls web directly
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
        listed = ranker.rank(g, base, self.flows, [], traffic_capability=unavailable)
        spike = next(c for c in listed if c["fault"] == "traffic_spike")      # shown, but marked and never runnable
        self.assertTrue(spike["unavailable"])
        self.assertNotIn("parameters", spike)

        ranking = ranker.rank(g, base, self.flows, [], traffic_capability=traffic_capability())
        self.assertFalse(find(ranking, "frontend:traffic_spike:8").get("unavailable"))
        candidate = find(ranking, "frontend:traffic_spike:8")
        self.assertEqual(candidate["parameters"]["baseline_users"], 5)
        self.assertEqual(candidate["parameters"]["peak_users"], 8)
        self.assertEqual(candidate["candidate_id"], "traffic-spike-core-v1")
        plan = {**{key: candidate[key] for key in ("target", "fault", "magnitude", "duration_s", "parameters")},
                "hypothesis": "h", "reasoning": "r"}
        self.assertTrue(validate(plan, g)["valid"])


def with_frontend(g, base):
    """The fixture plus a frontend (the traffic_spike target) behind the load generator."""
    g, base = g.copy(), copy.deepcopy(base)
    g.add_edge("load-generator", "frontend", mode="sync", type="sync-http")
    g.add_edge("frontend", "checkout", mode="sync", type="sync-http")
    base["services"]["frontend"] = copy.deepcopy(base["services"]["web"])
    base["containers"]["frontend"] = {"running": {"mean": 1}}
    return g, base


class FaultDiversityTest(unittest.TestCase):
    def setUp(self):
        self.g, self.base, self.flows = graph(), baseline(), flows()

    def test_one_row_per_service_and_fault_with_the_other_magnitudes_as_variants(self):
        ranking = ranker.rank(self.g, self.base, self.flows, [])
        rows = [c for c in ranking if "variant_of" not in c]
        keys = [(c["target"], c["fault"]) for c in rows]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual([c["rank"] for c in rows], list(range(1, len(rows) + 1)))     # rows come first
        pay = next(c for c in rows if c["id"].startswith("pay:network_delay"))
        self.assertEqual(len(pay["variants"]), 2)
        self.assertTrue(all(find(ranking, v)["variant_of"] == pay["id"] for v in pay["variants"]))

    def test_after_only_delays_a_container_stop_reaches_the_top_5(self):
        history = [run(t, "network_delay", m) for t in ("pay", "checkout", "worker") for m in (200, 500)]
        rows = [c for c in ranker.rank(self.g, self.base, self.flows, history) if "variant_of" not in c]
        self.assertIn("container_stop", [c["fault"] for c in rows[:5]], [c["id"] for c in rows[:5]])
        stop = next(c for c in rows if c["fault"] == "container_stop")
        self.assertEqual(stop["features"]["coverage"]["score"], 1.0)              # never run: full coverage bonus
        self.assertEqual(next(c for c in rows if c["fault"] == "network_delay")["features"]["coverage"]["score"], 0.0)

    def test_coverage_weight_sums_with_the_other_info_weights(self):
        self.assertIn("coverage", ranker.WEIGHTS["info"])
        self.assertAlmostEqual(sum(ranker.WEIGHTS["info"].values()), 1.0)

    def test_stopping_an_async_only_leaf_is_not_an_outage(self):
        ranking = ranker.rank(self.g, self.base, self.flows, [])
        leaf, entry = find(ranking, "queue:container_stop:0"), find(ranking, "web:container_stop:0")
        self.assertEqual(leaf["features"]["sync_callers"]["raw"], 0)
        self.assertEqual((leaf["outage"], leaf["stop_reach"]), (0.0, 0.0))
        self.assertNotIn("outage", leaf["why"])
        self.assertEqual((entry["outage"], entry["stop_reach"]), (1.0, 1.0))             # the entry point keeps everything
        full = copy.deepcopy(ranker.WEIGHTS)
        full["stop_safety_floor"] = 1.0                                                 # the old, unscaled penalty
        unscaled = find(ranker.rank(self.g, self.base, self.flows, [], weights=full), "queue:container_stop:0")
        safety = lambda c: next(f["contribution"] for f in c["factors"] if f["name"] == "safety")
        self.assertGreater(safety(leaf), safety(unscaled))                              # smaller penalty for the leaf
        self.assertEqual(safety(entry), safety(find(ranker.rank(self.g, self.base, self.flows, [], weights=full),
                                                    "web:container_stop:0")))

    def test_kafka_consumers_are_not_entry_points(self):
        g = nx.DiGraph()
        for u, v, mode in [("load-generator", "frontend-proxy", "sync"), ("frontend-proxy", "frontend", "sync"),
                           ("frontend", "checkout", "sync"), ("checkout", "kafka", "async"),
                           ("accounting", "kafka", "async"), ("fraud-detection", "kafka", "async"),
                           ("frontend-web", "image-provider", "sync")]:
            g.add_edge(u, v, mode=mode, type="async-messaging" if mode == "async" else "sync-grpc")
        names = ("frontend", "checkout", "kafka", "accounting", "fraud-detection", "image-provider")
        base = {"services": {n: copy.deepcopy(self.base["services"]["web"]) for n in names if n != "kafka"},
                "containers": {n: {"running": {"mean": 1}} for n in names}}
        ranking = ranker.rank(g, base, {}, [])
        entry = {c["target"]: c["features"]["entry_point"]["raw"] for c in ranking}
        self.assertEqual({n for n, e in entry.items() if e}, {"frontend", "image-provider"})
        for consumer in ("accounting", "fraud-detection"):
            stop = find(ranking, f"{consumer}:container_stop:0")
            self.assertEqual(stop["outage"], 0.0)
            self.assertNotIn("entry point", stop["why"])

    def test_unavailable_traffic_spike_is_listed_with_its_reason_after_every_runnable_row(self):
        g, base = with_frontend(self.g, self.base)
        cap = traffic_capability(runtime_ready=False, ready=False, detail="Unavailable: ConnectionError: refused")
        ranking = ranker.rank(g, base, self.flows, [], traffic_capability=cap)
        spike = next(c for c in ranking if c["fault"] == "traffic_spike")
        self.assertTrue(spike["unavailable"])
        self.assertIn("ConnectionError", spike["unavailable_reason"])
        self.assertIn("runtime ready", spike["unavailable_reason"])
        self.assertIn("UNAVAILABLE", spike["why"])
        rows = [c for c in ranking if "variant_of" not in c]
        self.assertEqual(rows[-1]["id"], spike["id"])                                  # after every runnable row
        self.assertFalse(validate({**{k: spike[k] for k in ("target", "fault", "magnitude", "duration_s")},
                                   "hypothesis": "h", "reasoning": "r"}, g)["valid"])     # policy would refuse it anyway

    def test_ranker_cli_says_why_traffic_spike_is_unavailable_and_how_to_enable_it(self):
        g, base = with_frontend(self.g, self.base)
        cap = traffic_capability(runtime_ready=False, ready=False, detail="Unavailable: ConnectionError: refused")
        ranking = ranker.rank(g, base, self.flows, [], traffic_capability=cap)
        out = io.StringIO()
        with mock.patch.object(ranker, "rank_from_files", return_value=(ranking, g)), redirect_stdout(out):
            self.assertEqual(ranker.main(["--top", "3"]), 0)
        self.assertIn("UNAVAILABLE: Unavailable: ConnectionError", out.getvalue())
        self.assertIn("To enable it: start the load generator", out.getvalue())


if __name__ == "__main__":
    unittest.main()
