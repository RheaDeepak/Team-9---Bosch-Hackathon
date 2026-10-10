"""FastAPI bridge between the operator UI and the Chaos Copilot backend."""

from __future__ import annotations

import copy
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from dotenv import load_dotenv

load_dotenv()

import docker
import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from control_plane import TERMINAL_STATES, ExperimentManager, baseline_summary
from inject import ACTIVE_FILE, COMPOSE_PROJECT, PROJECT_LABEL
from metrics import BASELINE_JSON, PROM_URL, capture_baseline, load_baseline
from policy import PROTECTED
from traffic_spike import capability as traffic_spike_capability


ROOT = Path(__file__).resolve().parent
FRONTEND_DIR = ROOT / "frontend"
DEPENDENCY_MAP = ROOT / "dependency_map.json"
JAEGER_URL = os.getenv("JAEGER_URL", "http://localhost:16686")

app = FastAPI(title="Chaos Copilot", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Idempotency-Key"],
)

manager = ExperimentManager()
_idempotency_cache: dict[tuple[str, str], Any] = {}
_idempotency_lock = RLock()


class GoalRequest(BaseModel):
    goal: str | None = Field(default=None, max_length=120)


class ApprovalRequest(BaseModel):
    approval_name: str = Field(default="hackathon-operator", min_length=1, max_length=120)


class AbortRequest(BaseModel):
    reason: str = Field(default="Manual emergency stop", min_length=1, max_length=300)


class BaselineRequest(BaseModel):
    duration_seconds: int = Field(default=60, ge=15, le=300)
    interval_seconds: int = Field(default=15, ge=5, le=60)


def _idempotent(scope: str, key: str | None, action: Callable[[], Any]) -> Any:
    if not key:
        return action()
    cache_key = (scope, key)
    with _idempotency_lock:
        if cache_key in _idempotency_cache:
            return copy.deepcopy(_idempotency_cache[cache_key])
    result = action()
    with _idempotency_lock:
        _idempotency_cache[cache_key] = copy.deepcopy(result)
    return result


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (ValueError, RuntimeError)):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")


def _check_docker() -> tuple[bool, str, int]:
    try:
        client = docker.from_env(timeout=2)
        client.ping()
        containers = client.containers.list(all=True, filters={"label": f"{PROJECT_LABEL}={COMPOSE_PROJECT}"})
        return bool(containers), f"{len(containers)} containers", len(containers)
    except (docker.errors.DockerException, requests.RequestException) as exc:
        return False, f"Unavailable: {type(exc).__name__}", 0


def _check_url(url: str, label: str) -> tuple[bool, str]:
    try:
        response = requests.get(url, timeout=2)
        return response.status_code < 500, f"HTTP {response.status_code}"
    except requests.RequestException:
        return False, f"{label} unreachable"


def _runtime_checks() -> tuple[tuple[bool, str, int], tuple[bool, str], tuple[bool, str], dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=4) as pool:
        docker_future = pool.submit(_check_docker)
        prometheus_future = pool.submit(_check_url, f"{PROM_URL}/-/ready", "Prometheus")
        jaeger_future = pool.submit(_check_url, JAEGER_URL, "Jaeger")
        traffic_future = pool.submit(traffic_spike_capability)
        return docker_future.result(), prometheus_future.result(), jaeger_future.result(), traffic_future.result()


def _ensure_execution_ready(experiment_id: str) -> None:
    (docker_ready, docker_detail, container_count), (prometheus_ready, prometheus_detail), (jaeger_ready, jaeger_detail), traffic = _runtime_checks()
    if not docker_ready or container_count == 0:
        raise RuntimeError(f"Astronomy Shop is not ready: {docker_detail}")
    if not prometheus_ready:
        raise RuntimeError(f"Telemetry is not ready: {prometheus_detail}")
    if not jaeger_ready:
        raise RuntimeError(f"Trace evidence is not ready: {jaeger_detail}")
    if not BASELINE_JSON.exists():
        raise RuntimeError("A measured baseline is required before fault injection")
    experiment = manager.get(experiment_id)
    if experiment.get("plan", {}).get("fault") == "traffic_spike" and not traffic["ready"]:
        raise RuntimeError(f"Traffic-spike capability is not ready: {traffic['detail']}")


def _approve_ready(experiment_id: str, approval_name: str) -> dict[str, Any]:
    _ensure_execution_ready(experiment_id)
    return manager.approve(experiment_id, approval_name)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "chaos-copilot", "active_experiment": manager.active_id}


@app.get("/api/readiness")
def readiness() -> dict[str, Any]:
    (docker_ready, docker_detail, container_count), (prometheus_ready, prometheus_detail), (jaeger_ready, jaeger_detail), traffic = _runtime_checks()
    topology_ready = DEPENDENCY_MAP.exists()
    baseline_ready = BASELINE_JSON.exists()
    safety_ready = topology_ready and not ACTIVE_FILE.exists() and manager.active_id is None
    checks = [
        {"id": "docker", "label": "Docker", "detail": docker_detail, "ready": docker_ready},
        {"id": "target", "label": "Astronomy Shop", "detail": f"{container_count} containers" if container_count else "Not detected", "ready": docker_ready and container_count > 0},
        {"id": "prometheus", "label": "Prometheus", "detail": prometheus_detail, "ready": prometheus_ready},
        {"id": "jaeger", "label": "Jaeger", "detail": jaeger_detail, "ready": jaeger_ready},
        {"id": "safety", "label": "Safety policy", "detail": "Loaded" if safety_ready else "Blocked by active or missing state", "ready": safety_ready},
        {"id": "baseline", "label": "Baseline", "detail": "Available" if baseline_ready else "Capture required", "ready": baseline_ready},
        {"id": "traffic_spike", "label": "Traffic spike", "detail": traffic["detail"], "ready": traffic["ready"]},
    ]
    return {"status": "ready" if all(check["ready"] for check in checks) else "blocked", "checks": checks}


@app.get("/api/fault-capabilities")
def fault_capabilities() -> dict[str, Any]:
    traffic = traffic_spike_capability()
    return {"capabilities": [traffic], "ready": traffic["ready"]}


@app.get("/api/topology")
def topology() -> dict[str, Any]:
    try:
        data = json.loads(DEPENDENCY_MAP.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=f"Dependency map unavailable: {exc}") from exc
    baseline = load_baseline() or {}
    containers = baseline.get("containers", {})
    stats = data.get("stats", {})
    fan_in, fan_out = stats.get("fan_in", {}), stats.get("fan_out", {})
    max_degree = max([1, *fan_in.values(), *fan_out.values()])
    checkout_path = set(stats.get("checkout_path", []))
    nodes = []
    for name in data.get("nodes", []):
        role = "application"
        if name in {"astronomy-db", "valkey-cart", "kafka"}:
            role = "datastore"
        elif name in {"otel-collector", "prometheus", "jaeger", "grafana", "opensearch"}:
            role = "observability"
        elif name in {"load-generator", "flagd"}:
            role = "control-plane"
        running = containers.get(name, {}).get("running", {}).get("mean")
        health = "healthy" if running == 1 else "unknown" if running is None else "unhealthy"
        degree_score = (fan_in.get(name, 0) + fan_out.get(name, 0)) / (2 * max_degree)
        criticality = min(1.0, degree_score + (0.25 if name in checkout_path else 0.0))
        nodes.append({"id": name, "label": name, "role": role, "health": health, "protected": name in PROTECTED, "criticality": round(criticality, 2)})
    edges = [{"source": edge["parent"], "target": edge["child"], "type": edge.get("type"), "source_kind": edge.get("source")} for edge in data.get("edges", [])]
    return {"nodes": nodes, "edges": edges, "stats": stats, "flows": data.get("flows", {})}


@app.get("/api/baselines/latest")
def latest_baseline() -> dict[str, Any]:
    baseline = load_baseline()
    if not baseline:
        raise HTTPException(status_code=404, detail="No baseline has been captured")
    return {"captured_at": baseline.get("captured_at"), "duration_seconds": baseline.get("duration_s"), "metrics": baseline_summary(baseline)}


@app.post("/api/baselines")
def create_baseline(body: BaselineRequest, idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
    if manager.active_id:
        raise HTTPException(status_code=409, detail="Cannot capture a baseline while an experiment is active")
    return _idempotent(
        "baseline",
        idempotency_key,
        lambda: {"metrics": baseline_summary(capture_baseline(body.duration_seconds, body.interval_seconds))},
    )


@app.post("/api/experiments/propose")
def propose(body: GoalRequest, idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
    try:
        goal = body.goal.strip() if body.goal and body.goal.strip() else None
        return _idempotent("propose", idempotency_key, lambda: manager.propose(goal))
    except Exception as exc:
        raise _http_error(exc) from exc


@app.post("/api/experiments/{experiment_id}/validate")
def validate_experiment(experiment_id: str, idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
    try:
        return _idempotent(f"validate:{experiment_id}", idempotency_key, lambda: manager.validate(experiment_id))
    except Exception as exc:
        raise _http_error(exc) from exc


@app.post("/api/experiments/{experiment_id}/approve")
def approve_experiment(experiment_id: str, body: ApprovalRequest, idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
    try:
        return _idempotent(f"approve:{experiment_id}", idempotency_key, lambda: _approve_ready(experiment_id, body.approval_name))
    except Exception as exc:
        raise _http_error(exc) from exc


@app.post("/api/experiments/{experiment_id}/abort")
def abort_experiment(experiment_id: str, body: AbortRequest, idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
    try:
        return _idempotent(f"abort:{experiment_id}", idempotency_key, lambda: manager.abort(experiment_id, body.reason))
    except Exception as exc:
        raise _http_error(exc) from exc


@app.get("/api/experiments/{experiment_id}")
def get_experiment(experiment_id: str) -> dict[str, Any]:
    try:
        return manager.get(experiment_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@app.get("/api/experiments/{experiment_id}/events")
def experiment_events(experiment_id: str) -> StreamingResponse:
    try:
        manager.get(experiment_id)
    except Exception as exc:
        raise _http_error(exc) from exc

    def stream():
        _, index = manager.events_since(experiment_id, 0)
        idle_ticks = 0
        experiment = manager.get(experiment_id)
        yield f"data: {json.dumps({'state': experiment['state'], 'message': 'Live experiment stream connected.', 'live_metrics': experiment.get('live_metrics'), 'live_services': experiment.get('live_services')})}\n\n"
        while True:
            events, index = manager.events_since(experiment_id, index)
            experiment = manager.get(experiment_id)
            if events:
                idle_ticks = 0
                for event in events:
                    payload = {
                        "state": event["state"],
                        "message": event["message"],
                        "live_metrics": experiment.get("live_metrics"),
                        "live_services": experiment.get("live_services"),
                        "event": event,
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
            else:
                idle_ticks += 1
                if idle_ticks % 20 == 0:
                    yield ": keep-alive\n\n"
            if experiment["state"] in TERMINAL_STATES and not events:
                break
            time.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/reports/{experiment_id}")
def report(experiment_id: str) -> dict[str, Any]:
    try:
        result = manager.report(experiment_id)
    except Exception as exc:
        raise _http_error(exc) from exc
    if result is None:
        raise HTTPException(status_code=409, detail="Report is not ready")
    return result


@app.get("/api/findings")
def findings() -> dict[str, Any]:
    return {"findings": manager.findings()}


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
