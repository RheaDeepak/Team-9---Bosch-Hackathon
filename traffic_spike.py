"""Bounded Locust traffic-spike executor with reversible state capture."""
from __future__ import annotations

import json
import math
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Literal

import requests
from pydantic import BaseModel, ConfigDict, Field, model_validator


FAULT_TYPE = "traffic_spike"
TARGET_SERVICE = "frontend"
CONTROLLER_SERVICE = "load-generator"
JOURNEY_PROFILE = "astronomy-shop-default"
EXPECTED_USER_WEIGHTS = {"WebsiteBrowserUser": 1, "WebsiteUser": 9}
HANDLE_SCHEMA_VERSION = 1
CAPABILITY_VERSION = "traffic-spike-v1"
INTEGRATION_TEST_EVIDENCE = "tests/test_traffic_spike_integration.py"

MAX_BASELINE_USERS = int(os.getenv("TRAFFIC_SPIKE_MAX_BASELINE_USERS", "20"))
MAX_PEAK_USERS = int(os.getenv("TRAFFIC_SPIKE_MAX_USERS", "10"))
MAX_SPAWN_RATE = int(os.getenv("TRAFFIC_SPIKE_MAX_SPAWN_RATE", "2"))
MAX_DURATION_SECONDS = int(os.getenv("TRAFFIC_SPIKE_MAX_DURATION_SECONDS", "30"))
LOCUST_URL = os.getenv("LOCUST_URL", "http://localhost:8080/loadgen").rstrip("/")
LOCUST_REQUEST_TIMEOUT_SECONDS = float(os.getenv("LOCUST_REQUEST_TIMEOUT_SECONDS", "3"))
LOCUST_STATE_TIMEOUT_SECONDS = float(os.getenv("LOCUST_STATE_TIMEOUT_SECONDS", "20"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TrafficSpikeParameters(BaseModel):
    """Strict executor parameters; URLs and arbitrary Locust fields are intentionally absent."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    baseline_users: int = Field(ge=1, le=MAX_BASELINE_USERS)
    peak_users: int = Field(ge=2, le=MAX_PEAK_USERS)
    spawn_rate_per_second: int = Field(ge=1, le=MAX_SPAWN_RATE)
    duration_seconds: int = Field(ge=10, le=MAX_DURATION_SECONDS)
    journey_profile: Literal["astronomy-shop-default"] = JOURNEY_PROFILE

    @model_validator(mode="after")
    def peak_exceeds_baseline(self) -> "TrafficSpikeParameters":
        if self.peak_users <= self.baseline_users:
            raise ValueError("peak_users must be greater than baseline_users")
        return self


class LocustController:
    """Fixed-surface adapter for the existing Locust web API."""

    def __init__(
        self,
        base_url: str = LOCUST_URL,
        *,
        session: requests.Session | None = None,
        request_timeout_seconds: float = LOCUST_REQUEST_TIMEOUT_SECONDS,
        state_timeout_seconds: float = LOCUST_STATE_TIMEOUT_SECONDS,
        poll_interval_seconds: float = 0.25,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.request_timeout_seconds = request_timeout_seconds
        self.state_timeout_seconds = state_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds

    def _get(self, suffix: str) -> requests.Response:
        response = self.session.get(f"{self.base_url}{suffix}", timeout=self.request_timeout_seconds)
        response.raise_for_status()
        return response

    def capture_state(self) -> dict[str, Any]:
        stats = self._get("/stats/requests").json()
        html = self._get("").text
        line = next((item for item in html.splitlines() if "window.templateArgs" in item), "")
        start, end = line.find("{"), line.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError("Locust runtime configuration is unavailable")
        template = json.loads(line[start : end + 1])
        weights = {
            name: int(value.get("weight", 0))
            for name, value in template.get("users", {}).items()
        }
        state = str(stats.get("state") or template.get("state") or "unknown")
        users = stats.get("user_count", template.get("user_count"))
        spawn_rate = template.get("spawn_rate")
        if isinstance(users, bool) or not isinstance(users, int) or users < 0:
            raise RuntimeError(f"Locust returned invalid user_count {users!r}")
        if isinstance(spawn_rate, bool) or not isinstance(spawn_rate, (int, float)) or spawn_rate <= 0:
            raise RuntimeError(f"Locust returned invalid spawn_rate {spawn_rate!r}")
        return {
            "controller_service": CONTROLLER_SERVICE,
            "controller_url": self.base_url,
            "state": state,
            "running": state in {"running", "spawning"},
            "user_count": users,
            "spawn_rate_per_second": float(spawn_rate),
            "current_rps": float(stats.get("current_rps") or 0.0),
            "journey_profile": JOURNEY_PROFILE,
            "locust_profile": template.get("profile"),
            "locust_version": str(template.get("version") or "unknown"),
            "user_class_weights": weights,
        }

    @staticmethod
    def validate_supported_runtime(state: dict[str, Any]) -> None:
        if state.get("journey_profile") != JOURNEY_PROFILE:
            raise RuntimeError("the Locust journey profile is not supported")
        if state.get("user_class_weights") != EXPECTED_USER_WEIGHTS:
            raise RuntimeError(
                f"Locust user-class weights changed: expected {EXPECTED_USER_WEIGHTS}, "
                f"got {state.get('user_class_weights')}"
            )
        version = str(state.get("locust_version", ""))
        if not version.startswith("2."):
            raise RuntimeError(f"unsupported Locust version {version!r}")

    def set_load(self, users: int, spawn_rate: float) -> None:
        response = self.session.post(
            f"{self.base_url}/swarm",
            data={"user_count": str(users), "spawn_rate": str(spawn_rate)},
            timeout=self.request_timeout_seconds,
        )
        response.raise_for_status()

    def stop(self) -> None:
        self._get("/stop")

    def wait_for_state(self, *, running: bool, users: int) -> dict[str, Any]:
        deadline = time.monotonic() + self.state_timeout_seconds
        last: dict[str, Any] | None = None
        while time.monotonic() <= deadline:
            last = self.capture_state()
            running_match = last["running"] if running else not last["running"]
            if running_match and last["user_count"] == users:
                return last
            time.sleep(self.poll_interval_seconds)
        raise RuntimeError(
            f"Locust did not reach running={running}, users={users}; last state was {last}"
        )


class TrafficSpikeExecutor:
    def __init__(self, controller: LocustController | None = None) -> None:
        self.controller = controller or LocustController()

    def inject(
        self,
        plan: dict[str, Any],
        experiment_id: str,
        persist: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        if plan.get("fault") != FAULT_TYPE or plan.get("target") != TARGET_SERVICE:
            raise ValueError("traffic_spike is restricted to the frontend service")
        parameters = TrafficSpikeParameters.model_validate(plan.get("parameters"))
        original = self.controller.capture_state()
        self.controller.validate_supported_runtime(original)
        if not original["running"]:
            raise RuntimeError("Locust must already be running before a traffic spike")
        if original["user_count"] != parameters.baseline_users:
            raise RuntimeError(
                f"approved baseline_users={parameters.baseline_users} does not match "
                f"current Locust user_count={original['user_count']}"
            )
        handle = {
            "schema_version": HANDLE_SCHEMA_VERSION,
            "handle_id": f"traffic-{uuid.uuid4().hex[:12]}",
            "experiment_id": experiment_id,
            "fault": FAULT_TYPE,
            "target": TARGET_SERVICE,
            "controller_service": CONTROLLER_SERVICE,
            "parameters": parameters.model_dump(),
            "original_state": original,
            "applied_state": None,
            "active_evidence": None,
            "recovery_evidence": None,
            "rollback": "restore exact Locust running state, user count, and spawn rate",
            "status": "prepared",
            "started_at": utc_now(),
        }
        persist(handle)
        self.controller.set_load(parameters.peak_users, parameters.spawn_rate_per_second)
        handle["applied_state"] = {
            "running": True,
            "user_count": parameters.peak_users,
            "spawn_rate_per_second": parameters.spawn_rate_per_second,
            "journey_profile": parameters.journey_profile,
        }
        handle["status"] = "injected"
        persist(handle)
        return handle

    def verify_active(
        self,
        handle: dict[str, Any],
        persist: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        parameters = TrafficSpikeParameters.model_validate(handle.get("parameters"))
        observed = self.controller.wait_for_state(running=True, users=parameters.peak_users)
        self.controller.validate_supported_runtime(observed)
        if not math.isclose(
            observed["spawn_rate_per_second"],
            float(parameters.spawn_rate_per_second),
            rel_tol=0.0,
            abs_tol=0.001,
        ):
            raise RuntimeError("Locust reached the requested user count with an unexpected spawn rate")
        evidence = {
            "verified": True,
            "verified_at": utc_now(),
            "expected_users": parameters.peak_users,
            "observed_users": observed["user_count"],
            "observed_state": observed["state"],
            "observed_rps": observed["current_rps"],
            "observed_spawn_rate_per_second": observed["spawn_rate_per_second"],
            "locust_version": observed["locust_version"],
            "journey_profile": observed["journey_profile"],
        }
        handle["active_evidence"] = evidence
        handle["status"] = "active_verified"
        persist(handle)
        return evidence

    def rollback(
        self,
        handle: dict[str, Any],
        persist: Callable[[dict[str, Any]], None],
    ) -> bool:
        original = handle.get("original_state") or {}
        current = self.controller.capture_state()
        self.controller.validate_supported_runtime(current)
        desired_running = bool(original.get("running"))
        desired_users = int(original.get("user_count", 0))
        if desired_running:
            if not current["running"] or current["user_count"] != desired_users:
                self.controller.set_load(desired_users, float(original["spawn_rate_per_second"]))
            restored = self.controller.wait_for_state(running=True, users=desired_users)
        else:
            if current["running"] or current["user_count"] != 0:
                self.controller.stop()
            restored = self.controller.wait_for_state(running=False, users=0)
        self.controller.validate_supported_runtime(restored)
        expected_spawn_rate = float(original.get("spawn_rate_per_second", 0))
        if desired_running and not math.isclose(
            restored["spawn_rate_per_second"], expected_spawn_rate, rel_tol=0.0, abs_tol=0.001
        ):
            raise RuntimeError("Locust user count recovered, but its original spawn rate was not restored")
        handle["status"] = "rolled_back"
        handle["rollback_completed_at"] = utc_now()
        handle["recovery_evidence"] = {
            "verified": True,
            "verified_at": utc_now(),
            "expected_running": desired_running,
            "observed_running": restored["running"],
            "expected_users": desired_users,
            "observed_users": restored["user_count"],
            "restored_spawn_rate_per_second": expected_spawn_rate,
            "observed_spawn_rate_per_second": restored["spawn_rate_per_second"],
            "observed_rps": restored["current_rps"],
        }
        persist(handle)
        return True

    def verify_recovered(self, handle: dict[str, Any]) -> dict[str, Any]:
        original = handle.get("original_state") or {}
        observed = self.controller.capture_state()
        self.controller.validate_supported_runtime(observed)
        expected_spawn_rate = float(original.get("spawn_rate_per_second", 0))
        recovered = (
            observed["running"] == bool(original.get("running"))
            and observed["user_count"] == int(original.get("user_count", 0))
            and (
                not bool(original.get("running"))
                or math.isclose(
                    observed["spawn_rate_per_second"], expected_spawn_rate, rel_tol=0.0, abs_tol=0.001
                )
            )
        )
        return {
            "verified": recovered,
            "verified_at": utc_now(),
            "expected_running": bool(original.get("running")),
            "observed_running": observed["running"],
            "expected_users": int(original.get("user_count", 0)),
            "observed_users": observed["user_count"],
            "expected_spawn_rate_per_second": expected_spawn_rate,
            "observed_spawn_rate_per_second": observed["spawn_rate_per_second"],
            "observed_rps": observed["current_rps"],
        }


def capability(controller: LocustController | None = None) -> dict[str, Any]:
    item = {
        "failure_type": FAULT_TYPE,
        "capability_version": CAPABILITY_VERSION,
        "candidate_template": "traffic-spike-core-v1",
        "supported_targets": [TARGET_SERVICE],
        "controller_service": CONTROLLER_SERVICE,
        "integration_test_evidence": INTEGRATION_TEST_EVIDENCE,
        "executor_ready": True,
        "rollback_ready": True,
        "runtime_ready": False,
        "ready": False,
        "detail": "Locust has not been checked",
        "limits": {
            "peak_users": MAX_PEAK_USERS,
            "spawn_rate_per_second": MAX_SPAWN_RATE,
            "duration_seconds": MAX_DURATION_SECONDS,
        },
    }
    try:
        state = (controller or LocustController()).capture_state()
        LocustController.validate_supported_runtime(state)
        item["runtime_ready"] = state["running"] and 1 <= state["user_count"] < MAX_PEAK_USERS
        item["ready"] = bool(item["runtime_ready"])
        item["detail"] = (
            f"Locust {state['locust_version']} running with {state['user_count']} users"
            if item["ready"]
            else f"Locust must be running between 1 and {MAX_PEAK_USERS - 1} baseline users"
        )
        item["current_state"] = {
            "running": state["running"],
            "user_count": state["user_count"],
            "spawn_rate_per_second": state["spawn_rate_per_second"],
            "journey_profile": state["journey_profile"],
        }
    except (requests.RequestException, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        item["detail"] = f"Unavailable: {type(exc).__name__}: {exc}"[:300]
    return item


def candidate_ready(item: dict[str, Any]) -> bool:
    """A candidate is exposed only after its runtime, executor, rollback, and test evidence are ready."""
    return bool(
        item.get("ready")
        and item.get("executor_ready")
        and item.get("rollback_ready")
        and item.get("integration_test_evidence")
    )


def candidate_plan(item: dict[str, Any]) -> dict[str, Any] | None:
    """Build the single deterministic, bounded traffic-spike candidate from live capability state."""
    if not candidate_ready(item):
        return None
    baseline_users = int((item.get("current_state") or {}).get("user_count", 0))
    peak_users = min(MAX_PEAK_USERS, max(baseline_users + 1, 8))
    duration_seconds = min(20, MAX_DURATION_SECONDS)
    parameters = TrafficSpikeParameters.model_validate({
        "baseline_users": baseline_users,
        "peak_users": peak_users,
        "spawn_rate_per_second": min(2, MAX_SPAWN_RATE),
        "duration_seconds": duration_seconds,
        "journey_profile": JOURNEY_PROFILE,
    })
    return {
        "target": TARGET_SERVICE,
        "fault": FAULT_TYPE,
        "magnitude": parameters.peak_users,
        "duration_s": parameters.duration_seconds,
        "hypothesis": "Increasing the existing Astronomy Shop journey load reveals the frontend saturation response without changing the journey mix, then the original load is restored.",
        "reasoning": "The load-generator is already running the fixed 9:1 user-class mix against the frontend ingress, so a bounded user-count increase exercises the full request path without targeting infrastructure.",
        "parameters": parameters.model_dump(),
        "candidate_id": "traffic-spike-core-v1",
        "capability_version": item["capability_version"],
    }
