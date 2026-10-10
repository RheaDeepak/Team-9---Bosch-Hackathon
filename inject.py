"""Fault injection and rollback via the docker SDK. Only touches the demo's compose project."""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import docker
from dotenv import load_dotenv

from policy import ALLOWED_FAULTS
from traffic_spike import FAULT_TYPE as TRAFFIC_SPIKE, TrafficSpikeExecutor

load_dotenv()

COMPOSE_PROJECT = os.getenv("TARGET_COMPOSE_PROJECT", "opentelemetry-demo")
PROJECT_LABEL, SERVICE_LABEL = "com.docker.compose.project", "com.docker.compose.service"
ACTIVE_FILE = Path("active_experiment.json")
HELPER_IMAGE = "nicolaka/netshoot"
HELPER_LABEL = "chaos-copilot.helper"   # value = id of the container whose network it shares
NET_IFACE = "eth0"
STOP_TIMEOUT_S = 5
START_WAIT_S = 30
ROLLBACK_ACTIONS = {
    "container_stop": "start container",
    "network_delay": "remove netem qdisc + helper container",
    TRAFFIC_SPIKE: "restore exact Locust running state, user count, and spawn rate",
}


class InjectionRefused(Exception):
    pass


def _save(active):
    temporary = ACTIVE_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(active, indent=2), encoding="utf-8")
    temporary.replace(ACTIVE_FILE)


def _target_container(client, service):
    """The single running container of this compose service; refuse anything outside the project."""
    found = client.containers.list(all=True, filters={"label": [f"{PROJECT_LABEL}={COMPOSE_PROJECT}",
                                                                  f"{SERVICE_LABEL}={service}"]})
    if len(found) != 1 or found[0].labels.get(PROJECT_LABEL) != COMPOSE_PROJECT:
        raise InjectionRefused(f"{service!r}: expected exactly one container in compose project "
                               f"{COMPOSE_PROJECT!r}, found {len(found)}")
    return found[0]


def _tc(helper, *args):
    code, out = helper.exec_run(["tc", *args])
    return code, out.decode(errors="replace").strip()


def inject(plan, experiment_id=None, traffic_executor=None):
    """Start the fault. active_experiment.json is written first, so a crash can always be rolled back."""
    fault, service = plan["fault"], plan["target"]
    if fault not in ALLOWED_FAULTS:
        raise InjectionRefused(f"fault {fault!r} not allowed")
    if fault == TRAFFIC_SPIKE:
        executor = traffic_executor or TrafficSpikeExecutor()
        return executor.inject(plan, experiment_id or f"manual-{int(time.time())}", _save)
    client = docker.from_env()
    c = _target_container(client, service)
    if c.status != "running":
        raise InjectionRefused(f"{service!r} is not running (status {c.status})")
    active = {"target": service, "fault": fault, "magnitude": plan["magnitude"], "duration_s": plan["duration_s"],
              "container_id": c.id, "container_name": c.name, "helper_id": None,
              "rollback": ROLLBACK_ACTIONS[fault], "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _save(active)
    if fault == "container_stop":
        c.stop(timeout=STOP_TIMEOUT_S)
    else:  # network_delay: a helper in the target's network namespace shapes its egress traffic
        helper = client.containers.run(HELPER_IMAGE, ["sleep", "3600"], detach=True, cap_add=["NET_ADMIN"],
                                       network_mode=f"container:{c.id}", labels={HELPER_LABEL: c.id})
        active["helper_id"] = helper.id
        _save(active)
        code, out = _tc(helper, "qdisc", "add", "dev", NET_IFACE, "root", "netem", "delay", f"{int(plan['magnitude'])}ms")
        if code != 0:
            raise RuntimeError(f"tc netem failed: {out}")
    return active


def _remove_netem(client, c):
    """Delete the netem qdisc from c's namespace (using a fresh helper if needed), then remove helpers."""
    helpers = client.containers.list(all=True, filters={"label": f"{HELPER_LABEL}={c.id}"})
    try:
        if not helpers:
            helpers = [client.containers.run(HELPER_IMAGE, ["sleep", "60"], detach=True, cap_add=["NET_ADMIN"],
                                             network_mode=f"container:{c.id}", labels={HELPER_LABEL: c.id})]
        h = helpers[0]
        h.reload()
        if h.status != "running":
            h.start()
        _, shown = _tc(h, "qdisc", "show", "dev", NET_IFACE)
        if "netem" in shown:
            _tc(h, "qdisc", "del", "dev", NET_IFACE, "root")
            _, shown = _tc(h, "qdisc", "show", "dev", NET_IFACE)
        if "netem" in shown:
            raise RuntimeError(f"netem still present: {shown}")
    finally:
        for h in helpers:
            h.remove(force=True)


def verify_active(active, traffic_executor=None):
    """Verify the executor's concrete active state before observation starts."""
    if active["fault"] == TRAFFIC_SPIKE:
        executor = traffic_executor or TrafficSpikeExecutor()
        return executor.verify_active(active, _save)
    return {"verified": True, "fault": active["fault"], "target": active["target"]}


def verify_recovered(active, traffic_executor=None):
    """Verify executor-specific restoration independently of telemetry recovery."""
    if active["fault"] == TRAFFIC_SPIKE:
        executor = traffic_executor or TrafficSpikeExecutor()
        return executor.verify_recovered(active)
    return {"verified": True, "fault": active["fault"], "target": active["target"]}


def rollback(active=None, traffic_executor=None):
    """Undo the active fault. True if verified undone (or nothing active); the state file goes only on success."""
    if active is None:
        if not ACTIVE_FILE.exists():
            return True
        active = json.loads(ACTIVE_FILE.read_text(encoding="utf-8"))
    try:
        if active["fault"] == TRAFFIC_SPIKE:
            executor = traffic_executor or TrafficSpikeExecutor()
            if not executor.rollback(active, _save):
                return False
            ACTIVE_FILE.unlink(missing_ok=True)
            return True
        client = docker.from_env()
        try:
            c = client.containers.get(active["container_id"])
        except docker.errors.NotFound:
            c = None
        if active["fault"] == "container_stop":
            if c is None:
                raise RuntimeError(f"container {active['container_name']} no longer exists")
            if c.status != "running":
                c.start()
            deadline = time.time() + START_WAIT_S
            while c.status != "running" and time.time() < deadline:
                time.sleep(1)
                c.reload()
            if c.status != "running":
                raise RuntimeError(f"{active['target']} did not start (status {c.status})")
        elif c is not None:
            _remove_netem(client, c)
        else:  # container gone: its netns and qdisc went with it; just clean up any helper
            for h in client.containers.list(all=True, filters={"label": f"{HELPER_LABEL}={active['container_id']}"}):
                h.remove(force=True)
    except Exception as e:
        print(f"ROLLBACK FAILED for {active['target']} ({active['fault']}): {type(e).__name__}: {e}")
        return False
    ACTIVE_FILE.unlink(missing_ok=True)
    return True
