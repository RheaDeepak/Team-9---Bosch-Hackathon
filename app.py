"""Web UI backend: FastAPI around the existing experiment modules. Bound to 127.0.0.1 only.

Run from anywhere: python app.py  ->  http://127.0.0.1:8000
"""
import json
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import docker
import requests
import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import discovery
import planner
import grafana
import guard
import inject
import metrics
import run_experiment as exp
from policy import PROTECTED, validate

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)  # the modules read baseline.json, results/ ... relative to the repo
HOST, PORT = "127.0.0.1", 8000
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}   # blocks DNS-rebinding
CSRF_HEADER = "X-Chaos-UI"                                    # required on POST; browsers can't send it cross-site
PROM_READY_URL = f"{metrics.PROM_URL}/-/ready"
PROBE_INTERVAL_S = 2
MAX_EVENTS, MAX_SAMPLES, MAX_PROBES = 600, 400, 300
SHUTDOWN_WAIT_S = 90
RESULT_ID = re.compile(r"^\d{8}T\d{6}Z$")
CLIENT_NODES = {"load-generator", "frontend-web"}   # traffic sources, not services under test
CONTROL_NODES = {"flagd"}                            # the feature-flag service

LOCK = threading.RLock()
STOP = threading.Event()
WORKER = None


def _blank():
    return {"phase": "idle", "running": False, "plan": None, "source": None, "note": None, "validation": None,
            "approved_at": None, "fault_started_ts": None, "ended_ts": None, "rollback_at_s": None,
            "watch": {}, "samples": [], "probes": [], "events": [], "result": None, "refused": None}


STATE = {**_blank(), "startup_note": None}


def _log(msg):
    with LOCK:
        STATE["events"].append({"ts": datetime.now().strftime("%H:%M:%S"), "msg": str(msg).strip()})
        del STATE["events"][:-MAX_EVENTS]


def _elapsed():
    start = STATE["fault_started_ts"]
    return round((STATE["ended_ts"] or time.time()) - start, 1) if start else 0


# ---- hooks handed to run_experiment.execute --------------------------------------------

def _on_phase(name):
    if name == "done":  # the worker publishes "done" together with the result
        return
    with LOCK:
        STATE["phase"] = name
        if name == "monitoring":
            STATE["fault_started_ts"] = time.time()
        elif name == "rolling_back":
            STATE["rollback_at_s"] = _elapsed()
    _log(f"-- phase: {name.replace('_', ' ')}")


def _on_watch(info):
    with LOCK:
        STATE["watch"] = info


def _on_sample(phase, t, metrics_, containers, breaches):
    with LOCK:
        STATE["samples"].append({
            "t": _elapsed(), "phase": phase, "breaches": breaches,
            "services": {s: {"error_rate": v["error_rate"], "p95_ms": v["p95_ms"], "n": v["n"], "errors": v["errors"]}
                         for s, v in metrics_.items()},
            "containers": {n: {"running": bool(v["running"]), "cpu_percent": v["cpu_percent"],
                               "memory_mb": v["memory_mb"]} for n, v in containers.items()}})
        del STATE["samples"][:-MAX_SAMPLES]


def _probe_loop(done):
    """Live display of the same probes the guard uses for recovery (guard.probe_store)."""
    while not done.wait(PROBE_INTERVAL_S):
        results = guard.probe_store()
        with LOCK:
            if STATE["fault_started_ts"] and not STATE["ended_ts"]:
                STATE["probes"].append({"t": _elapsed(), "results": results})
                del STATE["probes"][:-MAX_PROBES]


def _worker(plan, source, validation):
    probes_done = threading.Event()
    threading.Thread(target=_probe_loop, args=(probes_done,), daemon=True).start()
    try:
        result = exp.execute(plan, source, validation, emit=_log, on_phase=_on_phase, on_sample=_on_sample,
                             on_watch=_on_watch, stop=STOP)
        with LOCK:
            STATE["ended_ts"] = time.time()
            if "refused" in result:
                STATE.update(phase="refused", refused=result["refused"])
            else:
                STATE.update(phase="done", result=result)
    except Exception as e:  # execute() rolls back in a finally; this is the last safety net
        _log(f"experiment crashed: {type(e).__name__}: {e}")
        exp._recover_crashed_run(_log)
        with LOCK:
            STATE.update(phase="error", ended_ts=time.time(), refused=f"{type(e).__name__}: {e}")
    finally:
        probes_done.set()
        with LOCK:
            STATE["running"] = False


# ---- readiness -------------------------------------------------------------------------

def _readiness():
    checks = []

    def add(name, ok, detail, optional=False):  # optional checks are shown but never gate an experiment
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "optional": optional})

    def http(name, url):
        try:
            add(name, requests.get(url, timeout=5).ok, url)
        except requests.RequestException as e:
            add(name, False, f"{type(e).__name__} at {url}")

    baseline = metrics.load_baseline()
    try:
        client = docker.from_env()
        client.ping()
        add("Docker reachable", True, "daemon responds")
        status = {c.labels.get(inject.SERVICE_LABEL): c.status for c in client.containers.list(
            all=True, filters={"label": f"{inject.PROJECT_LABEL}={inject.COMPOSE_PROJECT}"})}
    except Exception as e:
        add("Docker reachable", False, f"{type(e).__name__}: {str(e)[:100]}")
        status = None
    add("Demo compose project found", bool(status), f"{len(status or {})} containers in '{inject.COMPOSE_PROJECT}'")
    required = sorted((baseline or {}).get("containers", {}))
    down = [s for s in required if (status or {}).get(s) != "running"]
    add("Required services up", bool(required) and not down,
        f"{len(required) - len(down)}/{len(required)} running" + (f"; not running: {', '.join(down)}" if down else ""))
    http("Prometheus reachable", PROM_READY_URL)
    http("Jaeger reachable", discovery.JAEGER_URL)
    if metrics.FLAGD_FILE.exists():
        flags = metrics.enabled_flags()
        add("All feature flags off", not flags, ", ".join(flags) or "none enabled")
    else:
        add("All feature flags off", False, f"cannot read {metrics.FLAGD_FILE}")
    busy = inject.ACTIVE_FILE.exists() or STATE["running"]
    add("No active experiment", not busy, "an experiment or leftover fault is active" if busy else "idle")
    add("Baseline available", baseline is not None, "baseline.json" if baseline else "run metrics.py first")
    add("Grafana panels found", *grafana.check_panels(), optional=True)
    add("Grafana embedding enabled", *grafana.embedding_allowed(), optional=True)
    return {"ready": all(c["ok"] for c in checks if not c["optional"]), "checks": checks}


# ---- app -------------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app):
    if inject.ACTIVE_FILE.exists():  # a previous run died mid-experiment: undo it first
        ok = exp._recover_crashed_run(_log)
        STATE["startup_note"] = ("Rolled back a leftover experiment from a previous run." if ok
                                 else "A leftover fault could NOT be rolled back; see active_experiment.json.")
    yield
    deadline = time.time() + SHUTDOWN_WAIT_S  # shutting down: abort through the same path, wait for rollback
    while WORKER is not None and WORKER.is_alive() and time.time() < deadline:
        STOP.set()
        WORKER.join(1)


app = FastAPI(title="Chaos Copilot", lifespan=lifespan)


@app.middleware("http")
async def only_local_ui(request: Request, call_next):
    if request.headers.get("host") not in ALLOWED_HOSTS:
        return JSONResponse({"detail": "bad Host header"}, status_code=403)
    if request.method == "POST" and request.headers.get(CSRF_HEADER) != "1":
        return JSONResponse({"detail": f"missing {CSRF_HEADER} header"}, status_code=403)
    return await call_next(request)


@app.get("/api/readiness")
def api_readiness():
    return _readiness()


@app.get("/api/grafana")
def api_grafana():
    """Where the embedded Spanmetrics panels live (the constants are looked up in grafana.py)."""
    return {"url": grafana.GRAFANA_URL, "uid": grafana.DASHBOARD_UID, "slug": grafana.DASHBOARD_SLUG,
            "panels": {k: {"id": pid, "title": title} for k, (pid, title) in grafana.PANELS.items()},
            "before_s": grafana.WINDOW_BEFORE_S}


def _node_info(data, baseline):
    """Per node: role, live container health, criticality and baseline numbers, for the topology view."""
    stats, edges = data["stats"], data["edges"]
    try:  # live: one cheap docker listing
        client = docker.from_env()
        live = {c.labels.get(inject.SERVICE_LABEL): c.status for c in client.containers.list(
            all=True, filters={"label": f"{inject.PROJECT_LABEL}={inject.COMPOSE_PROJECT}"})}
    except Exception:
        live = {}
    max_degree = max([1, *stats["fan_in"].values(), *stats["fan_out"].values()])
    in_types = {n: {e.get("type") for e in edges if e["child"] == n} for n in data["nodes"]}
    base = (baseline or {}).get("services", {})
    mean = lambda n, k: (base.get(n, {}).get(k) or {}).get("mean")
    info = {}
    for n in data["nodes"]:
        stores = in_types[n] and in_types[n] <= {"database", "cache", "async-messaging"}
        info[n] = {
            "role": "datastore" if stores else "client" if n in CLIENT_NODES else "control plane" if n in CONTROL_NODES else "service",
            "health": {"running": "running", None: "unknown"}.get(live.get(n), live.get(n)),
            "protected": n in PROTECTED, "fan_in": stats["fan_in"].get(n, 0), "fan_out": stats["fan_out"].get(n, 0),
            "on_checkout_path": n in stats["checkout_path"],
            "criticality": round(min(1.0, (stats["fan_in"].get(n, 0) + stats["fan_out"].get(n, 0)) / (2 * max_degree)
                                     + (0.25 if n in stats["checkout_path"] else 0)), 2),
            "baseline": {"req_per_s": mean(n, "request_rate"), "p95_ms": mean(n, "p95_ms"), "error_pct": mean(n, "error_rate"),
                         "requests": ((baseline or {}).get("requests") or {}).get(n)}}
    return info


@app.get("/api/graph")
def api_graph():
    try:
        data = json.loads(discovery.OUT_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise HTTPException(404, f"{discovery.OUT_JSON} unavailable: {e}")
    return {**data, "fan_in": data["stats"]["fan_in"], "protected": sorted(PROTECTED),
            "nodes_info": _node_info(data, metrics.load_baseline())}


@app.get("/api/goals")
def api_goals():
    return {key: label for key, (label, _) in planner.GOALS.items()}


@app.post("/api/propose")
def api_propose(body: dict = Body(default=None)):
    goal = (body or {}).get("goal") or None
    with LOCK:
        if STATE["running"]:
            raise HTTPException(409, "an experiment is running")
        STATE.update(_blank(), phase="proposing")
    try:
        p = exp.propose(goal)
    except Exception as e:
        with LOCK:
            STATE.update(_blank())
        raise HTTPException(500, f"planning failed: {type(e).__name__}: {e}")
    with LOCK:
        STATE.update(phase="proposed", plan=p["plan"], source=p["source"], note=p["note"], validation=p["validation"])
    _log(f"proposed: {p['plan'].get('fault')} on {p['plan'].get('target')} ({p['source']})")
    return p


@app.post("/api/approve", status_code=202)
def api_approve():
    global WORKER
    with LOCK:
        if STATE["running"]:
            raise HTTPException(409, "an experiment is already running")
        if STATE["phase"] != "proposed" or not STATE["plan"]:
            raise HTTPException(409, "there is no proposed plan to approve")
        plan, source = dict(STATE["plan"]), STATE["source"]
    validation = validate(plan)  # re-checked server side; the UI's button state is not trusted
    if not validation["valid"]:
        raise HTTPException(422, "plan is invalid: " + "; ".join(validation["reasons"]))
    failing = [c for c in _readiness()["checks"] if not c["ok"] and not c["optional"]]
    if failing:
        raise HTTPException(409, "not ready: " + "; ".join(f"{c['name']} ({c['detail']})" for c in failing))
    with LOCK:
        if STATE["running"]:
            raise HTTPException(409, "an experiment is already running")
        STOP.clear()
        keep = {k: STATE[k] for k in ("plan", "source", "note", "validation")}
        STATE.update(_blank(), **keep, running=True, phase="starting",
                     approved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        STATE["validation"] = validation
    exp.APPROVED_JSON.write_text(json.dumps({"plan": plan, "source": source, "blast_radius": validation["blast_radius"],
                                             "approved_at": STATE["approved_at"]}, indent=2), encoding="utf-8")
    _log("approved: starting experiment")
    WORKER = threading.Thread(target=_worker, args=(plan, source, validation), name="experiment")
    WORKER.start()
    return {"started": True}


@app.post("/api/reject")
def api_reject():
    with LOCK:
        if STATE["running"]:
            raise HTTPException(409, "an experiment is running; use STOP")
        STATE.update(_blank())
    _log("plan rejected")
    return {"rejected": True}


@app.post("/api/stop")
def api_stop():
    with LOCK:
        if not STATE["running"]:
            raise HTTPException(409, "no experiment is running")
    STOP.set()  # the guard sees it like a breach: abort, then the one rollback path
    _log("STOP pressed: aborting; rollback follows")
    return {"stopping": True}


@app.get("/api/status")
def api_status():
    with LOCK:
        s = {k: (list(v) if isinstance(v, list) else v) for k, v in STATE.items()}
        s["elapsed_s"] = _elapsed()
        s["server_time"] = time.time()
        return s


def _load_result(path):
    d = json.loads(path.read_text(encoding="utf-8"))
    d["id"] = path.stem
    d.setdefault("container_resources", [])
    d["amplification"] = exp.amplification(d["plan"], d["impact"])  # recomputed: older results have an older shape
    d["recovery"].setdefault("metrics_confirmed", None)
    d["recovery"].setdefault("recovered", False)
    if not d.get("score"):  # older results: score what was recorded (no probe data then)
        d["score"] = exp.score_experiment(d["impact"], d["recovery"], None, d["guard"]["elapsed_s"])
    d["verdict"].setdefault("recommendation", planner.remediation_for(d["plan"]))
    if not d.get("grafana_window_ms"):  # older results: derive the window from what was recorded
        start = datetime.fromisoformat(d["started_at"]).timestamp()
        d["grafana_window_ms"] = grafana.window_ms(start, start + d["guard"]["elapsed_s"] + (d["recovery"]["seconds"] or 60))
    return d


@app.get("/api/results")
def api_results():
    rows = []
    for f in sorted(exp.RESULTS_DIR.glob("*.json"), reverse=True):
        try:
            d = _load_result(f)
            rows.append({"id": d["id"], "started_at": d["started_at"], "target": d["plan"]["target"],
                         "fault": d["plan"]["fault"], "magnitude": d["plan"]["magnitude"],
                         "aborted": d["guard"]["aborted"], "reason": d["guard"]["reason"],
                         "verdict": d["verdict"]["verdict"], "recovered": d["recovery"]["recovered"],
                         "rollback_verified": d["rollback_verified"]})
        except (OSError, ValueError, KeyError):
            continue
    return rows


@app.get("/api/results/{rid}")
def api_result(rid: str):
    path = exp.RESULTS_DIR / f"{rid}.json"
    if not RESULT_ID.match(rid) or not path.is_file():
        raise HTTPException(404, "no such result")
    return _load_result(path)


app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")

if __name__ == "__main__":
    print(f"Chaos Copilot UI: http://{HOST}:{PORT}  (Ctrl+C stops the server; a running experiment is rolled back first)")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
