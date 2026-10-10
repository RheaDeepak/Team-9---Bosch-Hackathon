"""Thread-safe experiment orchestration built on the existing backend modules."""

from __future__ import annotations

import copy
import math
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import guard
from inject import ACTIVE_FILE, ROLLBACK_ACTIONS, inject, rollback
from metrics import current_snapshot, load_baseline
from planner import judge_hypothesis, propose_experiment
from policy import ALLOWED_FAULTS, MAX_DURATION_S, MAX_NETWORK_DELAY_MS, PROTECTED, validate


ACTIVE_STATES = {"approved", "baseline", "injecting", "observing", "rolling_back", "recovering", "analyzing"}
TERMINAL_STATES = {"completed", "aborted", "failed", "rejected"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def baseline_summary(baseline: dict[str, Any] | None) -> dict[str, float | None]:
    services = (baseline or {}).get("services", {})
    frontend = services.get("frontend", {})

    def mean(name: str) -> float | None:
        value = frontend.get(name)
        return value.get("mean") if isinstance(value, dict) else None

    error_rate = mean("error_rate")
    return {
        "availability": None if error_rate is None else max(0.0, 100.0 - error_rate),
        "error_rate": error_rate,
        "p95_latency_ms": mean("p95_ms"),
        "throughput_rps": mean("request_rate"),
    }


def snapshot_summary(snapshot: dict[str, Any] | None) -> dict[str, float | None]:
    frontend = (snapshot or {}).get("services", {}).get("frontend", {})
    error_rate = frontend.get("error_rate")
    return {
        "availability": None if error_rate is None else max(0.0, 100.0 - error_rate),
        "error_rate": error_rate,
        "p95_latency_ms": frontend.get("p95_ms"),
        "throughput_rps": frontend.get("request_rate"),
    }


def aggregate_snapshots(snapshots: list[dict[str, Any]]) -> dict[str, float | None]:
    summaries = [snapshot_summary(snapshot) for snapshot in snapshots]

    def average(name: str) -> float | None:
        values = [summary[name] for summary in summaries if summary[name] is not None]
        return sum(values) / len(values) if values else None

    return {name: average(name) for name in ("availability", "error_rate", "p95_latency_ms", "throughput_rps")}


def score_experiment(
    baseline: dict[str, float | None],
    fault: dict[str, float | None],
    recovery_seconds: float,
    recovery_timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    clamp = lambda value: max(0.0, min(1.0, value))
    base_error, fault_error = baseline.get("error_rate"), fault.get("error_rate")
    base_p95, fault_p95 = baseline.get("p95_latency_ms"), fault.get("p95_latency_ms")
    base_avail, fault_avail = baseline.get("availability"), fault.get("availability")

    error_impact = clamp(((fault_error or 0.0) - (base_error or 0.0)) / 100.0)
    latency_impact = 0.0
    if base_p95 not in (None, 0) and fault_p95 is not None:
        latency_impact = clamp((fault_p95 / max(base_p95, 0.001) - 1.0) / 4.0)
    availability_loss = 0.0
    if base_avail is not None and fault_avail is not None:
        availability_loss = clamp((base_avail - fault_avail) / 100.0)
    recovery_impact = clamp(recovery_seconds / max(recovery_timeout_seconds, 0.001))
    impact = round(100 * (0.35 * error_impact + 0.25 * latency_impact + 0.20 * availability_loss + 0.20 * recovery_impact), 1)
    severity = "Low" if impact < 20 else "Moderate" if impact < 40 else "High" if impact < 70 else "Critical"
    return {
        "impact_score": impact,
        "resilience_score": round(100 - impact),
        "severity": severity,
        "components": {
            "error_impact": round(error_impact, 4),
            "latency_impact": round(latency_impact, 4),
            "availability_loss": round(availability_loss, 4),
            "recovery_impact": round(recovery_impact, 4),
        },
    }


def proposal_view(experiment: dict[str, Any]) -> dict[str, Any]:
    plan = experiment["plan"]
    target = plan["target"]
    fault = plan["fault"]
    magnitude = plan.get("magnitude", 0)
    title = f"{target.replace('-', ' ').title()} {fault.replace('_', ' ')}"
    parameters = {"duration_seconds": plan["duration_s"]}
    if fault == "network_delay":
        parameters["delay_ms"] = magnitude
    return {
        "id": experiment["id"],
        "title": title,
        "goal": experiment["goal"],
        "hypothesis": plan["hypothesis"],
        "target_service": target,
        "failure_type": fault.upper(),
        "parameters": parameters,
        "blast_radius": f"{len(experiment.get('blast_radius', [])) or 1} service(s)",
        "rollback": ROLLBACK_ACTIONS.get(fault, "Executor rollback"),
        "reasoning_summary": plan["reasoning"],
        "source": experiment["source"],
        "planner_note": experiment["planner_note"],
    }


def policy_checks(plan: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    reasons = result.get("reasons", [])
    duration = plan.get("duration_s")
    target = plan.get("target")
    fault = plan.get("fault")
    magnitude = plan.get("magnitude")

    def check(name: str, passed: bool, detail: str) -> dict[str, Any]:
        return {"name": name, "passed": bool(passed), "detail": detail}

    magnitude_ok = isinstance(magnitude, (int, float)) and not isinstance(magnitude, bool) and magnitude >= 0
    if fault == "network_delay":
        magnitude_ok = magnitude_ok and 0 < magnitude <= MAX_NETWORK_DELAY_MS
    checks = [
        check("Proposal schema is complete", not any("missing field" in reason for reason in reasons), "All six planner fields are required"),
        check("Fault type is allowlisted", fault in ALLOWED_FAULTS, str(fault)),
        check("Target is not protected", bool(target) and target not in PROTECTED, str(target)),
        check("Target exists in the dependency map", not any("not a service" in reason for reason in reasons), str(target)),
        check("Duration is within the safety bound", isinstance(duration, (int, float)) and not isinstance(duration, bool) and 0 < duration <= MAX_DURATION_S, f"{duration}s ≤ {MAX_DURATION_S}s"),
        check("Fault magnitude is bounded", magnitude_ok, f"{magnitude}{'ms' if fault == 'network_delay' else ''}"),
        check("Rollback implementation is available", fault in ROLLBACK_ACTIONS, ROLLBACK_ACTIONS.get(fault, "Unavailable")),
    ]
    return checks


class ExperimentManager:
    def __init__(
        self,
        *,
        planner: Callable[[str | None], tuple[dict[str, Any], str, str]] = propose_experiment,
        validator: Callable[[dict[str, Any]], dict[str, Any]] = validate,
        injector: Callable[[dict[str, Any]], dict[str, Any]] = inject,
        rollback_fn: Callable[[dict[str, Any] | None], bool] = rollback,
        snapshotter: Callable[[], dict[str, Any]] = current_snapshot,
        baseline_loader: Callable[[], dict[str, Any] | None] = load_baseline,
        judge: Callable[[dict[str, Any], dict[str, Any]], dict[str, str]] = judge_hypothesis,
        telemetry_probe: Callable[[list[str]], dict[str, Any]] = guard.live_metrics,
        guard_watcher: Callable[..., dict[str, Any]] = guard.watch,
        recovery_waiter: Callable[..., dict[str, Any]] = guard.wait_for_recovery,
        observation_interval_s: float = 5.0,
    ) -> None:
        self._planner = planner
        self._validator = validator
        self._injector = injector
        self._rollback = rollback_fn
        self._snapshotter = snapshotter
        self._baseline_loader = baseline_loader
        self._judge = judge
        self._telemetry_probe = telemetry_probe
        self._guard_watcher = guard_watcher
        self._recovery_waiter = recovery_waiter
        self._observation_interval_s = observation_interval_s
        self._lock = threading.RLock()
        self._experiments: dict[str, dict[str, Any]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._abort_events: dict[str, threading.Event] = {}
        self._active_id: str | None = None

    @property
    def active_id(self) -> str | None:
        with self._lock:
            return self._active_id

    def propose(self, goal: str | None = None) -> dict[str, Any]:
        plan, source, note = self._planner(goal)
        experiment_id = f"exp-{uuid.uuid4().hex[:8]}"
        experiment = {
            "id": experiment_id,
            "goal": goal,
            "plan": plan,
            "source": source,
            "planner_note": note,
            "state": "draft",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "blast_radius": [],
            "policy": None,
            "approval": None,
            "live_metrics": None,
            "report": None,
            "error": None,
        }
        with self._lock:
            self._experiments[experiment_id] = experiment
            self._events[experiment_id] = []
            self._record_event_locked(experiment_id, "proposal_created", "Planner created one structured experiment proposal.")
        return proposal_view(experiment)

    def validate(self, experiment_id: str) -> dict[str, Any]:
        with self._lock:
            experiment = self._require_locked(experiment_id)
            if experiment["state"] not in {"draft", "rejected", "validated"}:
                raise ValueError(f"experiment cannot be validated from state {experiment['state']}")
            plan = copy.deepcopy(experiment["plan"])
        result = self._validator(plan)
        checks = policy_checks(plan, result)
        with self._lock:
            experiment = self._require_locked(experiment_id)
            experiment["policy"] = result
            experiment["blast_radius"] = result.get("blast_radius", [])
            experiment["state"] = "validated" if result["valid"] else "rejected"
            experiment["updated_at"] = utc_now()
            event = "validation_passed" if result["valid"] else "validation_failed"
            self._record_event_locked(experiment_id, event, "Deterministic safety policy passed." if result["valid"] else "Deterministic safety policy rejected the proposal.")
        return {"valid": result["valid"], "allowed": result["valid"], "checks": checks, "reasons": result["reasons"], "blast_radius": result["blast_radius"]}

    def approve(self, experiment_id: str, approval_name: str) -> dict[str, Any]:
        with self._lock:
            experiment = self._require_locked(experiment_id)
            if experiment["state"] != "validated" or not experiment.get("policy", {}).get("valid"):
                raise ValueError("only a policy-validated experiment can be approved")
            if self._active_id and self._active_id != experiment_id:
                raise RuntimeError(f"experiment {self._active_id} is already active")
            if ACTIVE_FILE.exists():
                raise RuntimeError("an unresolved active_experiment.json exists; reconcile rollback before starting")
            experiment["approval"] = {"name": approval_name, "approved_at": utc_now()}
            experiment["state"] = "approved"
            experiment["updated_at"] = utc_now()
            self._active_id = experiment_id
            self._abort_events[experiment_id] = threading.Event()
            self._record_event_locked(experiment_id, "approval_granted", f"Human approval recorded for {approval_name}.")
        worker = threading.Thread(target=self._run, args=(experiment_id,), daemon=True, name=f"chaos-{experiment_id}")
        worker.start()
        return self.get(experiment_id)

    def abort(self, experiment_id: str, reason: str) -> dict[str, Any]:
        with self._lock:
            experiment = self._require_locked(experiment_id)
            abort_event = self._abort_events.get(experiment_id)
            if experiment["state"] in {"rolling_back", "recovering", "analyzing"}:
                raise ValueError("the fault is already removed; recovery verification cannot be emergency-stopped")
            if experiment["state"] not in ACTIVE_STATES or abort_event is None:
                raise ValueError(f"experiment is not active (state {experiment['state']})")
            experiment["abort_reason"] = reason
            abort_event.set()
            self._record_event_locked(experiment_id, "manual_abort", reason)
        return {"accepted": True, "experiment_id": experiment_id, "state": experiment["state"]}

    def get(self, experiment_id: str) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._require_locked(experiment_id))

    def events_since(self, experiment_id: str, index: int) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            self._require_locked(experiment_id)
            events = self._events[experiment_id][index:]
            return copy.deepcopy(events), index + len(events)

    def report(self, experiment_id: str) -> dict[str, Any] | None:
        with self._lock:
            return copy.deepcopy(self._require_locked(experiment_id).get("report"))

    def findings(self) -> list[dict[str, Any]]:
        with self._lock:
            reports = [copy.deepcopy(item["report"]) for item in self._experiments.values() if item.get("report")]
        return sorted(reports, key=lambda report: report.get("impact_score", 0), reverse=True)

    def _require_locked(self, experiment_id: str) -> dict[str, Any]:
        try:
            return self._experiments[experiment_id]
        except KeyError as exc:
            raise KeyError(f"unknown experiment {experiment_id}") from exc

    def _record_event_locked(self, experiment_id: str, event_type: str, message: str) -> None:
        experiment = self._experiments[experiment_id]
        experiment["latest_event"] = message
        self._events[experiment_id].append({
            "sequence": len(self._events[experiment_id]) + 1,
            "timestamp": utc_now(),
            "type": event_type,
            "message": message,
            "state": experiment["state"],
        })

    def _set_state(self, experiment_id: str, state: str, message: str, event_type: str | None = None) -> None:
        with self._lock:
            experiment = self._require_locked(experiment_id)
            experiment["state"] = state
            experiment["updated_at"] = utc_now()
            self._record_event_locked(experiment_id, event_type or state, message)

    def _set_live_metrics(self, experiment_id: str, snapshot: dict[str, Any]) -> None:
        summary = snapshot_summary(snapshot)
        with self._lock:
            self._require_locked(experiment_id)["live_metrics"] = summary

    def _set_guard_metrics(self, experiment_id: str, metrics: dict[str, Any], containers: dict[str, Any]) -> None:
        self._set_live_metrics(experiment_id, {"services": metrics, "containers": containers})

    def _run(self, experiment_id: str) -> None:
        active_handle = None
        injected = False
        services: list[str] = []
        guard_result: dict[str, Any] = {
            "aborted": True,
            "reason": "experiment did not start",
            "elapsed_s": 0,
            "peaks": {},
            "container_peaks": {},
            "timeline": [],
        }
        recovery = {"recovered": False, "seconds": None, "note": "recovery was not checked"}
        run_error: str | None = None
        abort_event = self._abort_events[experiment_id]
        experiment = self.get(experiment_id)
        plan = experiment["plan"]
        baseline_data = self._baseline_loader()
        if not baseline_data:
            self._finish_failed(experiment_id, "No measured baseline is available; fault injection was not attempted.")
            return
        base_summary = baseline_summary(baseline_data)

        try:
            self._set_state(experiment_id, "baseline", "Loading the measured baseline and verifying live guard telemetry.", "baseline_started")
            services = guard.watched_services(plan, baseline_data, experiment.get("blast_radius", []))
            if not services:
                raise RuntimeError("None of the affected services has a measured safety threshold")
            initial_metrics = self._telemetry_probe(services)
            self._set_guard_metrics(experiment_id, initial_metrics, {})
            self._record_phase_event(experiment_id, "baseline_completed", f"Baseline loaded; telemetry guard is watching {len(services)} service(s).")
            if abort_event.is_set():
                raise InterruptedError("aborted before fault injection")

            self._set_state(experiment_id, "injecting", f"Injecting {plan['fault']} into {plan['target']}.", "fault_injecting")
            active_handle = self._injector(plan)
            injected = True
            self._record_phase_event(experiment_id, "fault_verified", "The bounded fault is active on the approved target.")

            self._set_state(experiment_id, "observing", "The completed backend guard is comparing Jaeger spans and container health with the baseline.", "observation_started")
            guard_result = self._guard_watcher(
                plan,
                baseline_data,
                services,
                stop_event=abort_event,
                on_sample=lambda metrics, containers: self._set_guard_metrics(experiment_id, metrics, containers),
            )
        except InterruptedError as exc:
            run_error = str(exc)
            guard_result["reason"] = run_error
        except Exception as exc:
            run_error = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._require_locked(experiment_id)["error"] = run_error
        finally:
            if injected or active_handle is not None or ACTIVE_FILE.exists():
                self._set_state(experiment_id, "rolling_back", "Rollback invoked for every active fault handle.", "rollback_invoked")
                rollback_ok = self._rollback(active_handle)
                if not rollback_ok:
                    self._finish_failed(experiment_id, "Rollback could not be verified. Manual operator action is required.")
                    return
                self._record_phase_event(experiment_id, "rollback_completed", "Rollback completed and the fault handle was cleared.")

        if run_error:
            if isinstance(run_error, str) and run_error.startswith("aborted"):
                self._finish_aborted(experiment_id, base_summary, [], 0.0)
                return
            self._finish_failed(experiment_id, run_error)
            return

        self._set_state(experiment_id, "recovering", "Fault removed; waiting for three consecutive healthy telemetry samples.", "recovery_check_started")
        recovery = self._recovery_waiter(
            plan,
            baseline_data,
            services,
            on_sample=lambda metrics, containers: self._set_guard_metrics(experiment_id, metrics, containers),
            on_progress=lambda progress: self._record_recovery_progress(experiment_id, progress),
        )
        recovery_seconds = float(recovery.get("seconds") or guard.RECOVERY_TIMEOUT_S)
        recovery_event = "recovery_detected" if recovery.get("recovered") else "recovery_unverified"
        recovery_message = "Recovery verified across three consecutive samples." if recovery.get("recovered") else "Rollback succeeded, but telemetry recovery was not verified before the timeout."
        self._record_phase_event(experiment_id, recovery_event, recovery_message)
        self._set_state(experiment_id, "analyzing", "Calculating deterministic impact and resilience scores.", "analysis_started")
        self._finish_guarded(experiment_id, baseline_data, base_summary, guard_result, recovery, recovery_seconds)

    def _record_recovery_progress(self, experiment_id: str, progress: dict[str, Any]) -> None:
        healthy = progress.get("healthy_samples", 0)
        required = progress.get("required_samples", guard.RECOVERY_SAMPLES)
        breaches = progress.get("breaches", {})
        if breaches:
            detail = "; ".join(f"{key}: {value}" for key, value in breaches.items())
            message = f"Recovery check {healthy}/{required} healthy samples; waiting on {detail}."
        else:
            message = f"Recovery check {healthy}/{required} healthy samples passed."
        self._record_phase_event(experiment_id, "recovery_sample", message)

    def _finish_guarded(
        self,
        experiment_id: str,
        baseline_data: dict[str, Any],
        base_summary: dict[str, float | None],
        guard_result: dict[str, Any],
        recovery: dict[str, Any],
        recovery_seconds: float,
    ) -> None:
        experiment = self.get(experiment_id)
        impact = guard.impact_table(baseline_data, guard_result.get("peaks", {}))
        containers = guard.container_table(baseline_data, guard_result.get("container_peaks", {}))
        frontend_impact = next((row for row in impact if row["service"] == "frontend"), None)
        if frontend_impact is None:
            frontend_impact = next((row for row in impact if row["service"] == experiment["plan"]["target"]), None)
        error_rate = (frontend_impact or {}).get("error_rate", {}).get("peak")
        p95 = (frontend_impact or {}).get("p95_ms", {}).get("peak")
        fault_summary = {
            "availability": None if error_rate is None else max(0.0, 100.0 - error_rate),
            "error_rate": error_rate,
            "p95_latency_ms": p95,
            "throughput_rps": None,
        }
        scoring = score_experiment(base_summary, fault_summary, recovery_seconds, guard.RECOVERY_TIMEOUT_S)
        measured = {
            "aborted": bool(guard_result.get("aborted")),
            "reason": guard_result.get("reason"),
            "elapsed_s": guard_result.get("elapsed_s"),
            "impact_baseline_to_peak": impact,
            "recovery": recovery,
        }
        if guard_result.get("aborted"):
            judge = {"verdict": "inconclusive", "explanation": "The guard stopped the experiment before the complete observation window elapsed."}
        elif not recovery.get("recovered"):
            judge = {"verdict": "inconclusive", "explanation": "Rollback completed, but recovery was not verified within the bounded recovery window."}
        else:
            judge = self._judge(experiment["plan"], measured)
        recovery_summary = base_summary if recovery.get("recovered") else snapshot_summary({})
        report = self._build_report(experiment, base_summary, fault_summary, recovery_summary, recovery_seconds, scoring, judge)
        report.update({
            "status": "aborted" if guard_result.get("aborted") else "completed",
            "summary": f"{experiment['plan']['target']} was tested with {experiment['plan']['fault']}. {guard_result.get('reason', '')}",
            "guard": guard_result,
            "impact": impact,
            "container_resources": containers,
            "rollback_verified": True,
            "recovery_verified": bool(recovery.get("recovered")),
            "recovery": recovery,
        })
        with self._lock:
            current = self._require_locked(experiment_id)
            current["report"] = report
            current["state"] = report["status"]
            current["updated_at"] = utc_now()
            event_type = "experiment_aborted" if report["status"] == "aborted" else "report_generated"
            self._record_event_locked(experiment_id, event_type, "Experiment evidence is ready; rollback was verified.")
            self._active_id = None

    def _record_phase_event(self, experiment_id: str, event_type: str, message: str) -> None:
        with self._lock:
            self._record_event_locked(experiment_id, event_type, message)

    def _finish_completed(
        self,
        experiment_id: str,
        base_summary: dict[str, float | None],
        fault_snapshots: list[dict[str, Any]],
        recovery_snapshot: dict[str, Any],
        recovery_seconds: float,
    ) -> None:
        experiment = self.get(experiment_id)
        fault_summary = aggregate_snapshots(fault_snapshots)
        recovery_summary = snapshot_summary(recovery_snapshot)
        scoring = score_experiment(base_summary, fault_summary, recovery_seconds)
        judge = self._judge(experiment["plan"], {
            "baseline": base_summary,
            "fault": fault_summary,
            "recovery": recovery_summary,
            "recovery_seconds": recovery_seconds,
        })
        report = self._build_report(experiment, base_summary, fault_summary, recovery_summary, recovery_seconds, scoring, judge)
        with self._lock:
            current = self._require_locked(experiment_id)
            current["report"] = report
            current["state"] = "completed"
            current["updated_at"] = utc_now()
            self._record_event_locked(experiment_id, "report_generated", "Experiment complete; the evidence-backed report is ready.")
            self._active_id = None

    def _finish_aborted(
        self,
        experiment_id: str,
        base_summary: dict[str, float | None],
        fault_snapshots: list[dict[str, Any]],
        recovery_seconds: float,
    ) -> None:
        experiment = self.get(experiment_id)
        fault_summary = aggregate_snapshots(fault_snapshots)
        scoring = score_experiment(base_summary, fault_summary, recovery_seconds)
        judge = {"verdict": "inconclusive", "explanation": "The operator stopped the experiment before the full observation window completed."}
        report = self._build_report(experiment, base_summary, fault_summary, snapshot_summary({}), recovery_seconds, scoring, judge)
        report["status"] = "aborted"
        with self._lock:
            current = self._require_locked(experiment_id)
            current["report"] = report
            current["state"] = "aborted"
            current["updated_at"] = utc_now()
            self._record_event_locked(experiment_id, "recovery_detected", "Emergency stop completed and rollback was verified.")
            self._active_id = None

    def _finish_failed(self, experiment_id: str, message: str) -> None:
        with self._lock:
            experiment = self._require_locked(experiment_id)
            experiment["state"] = "failed"
            experiment["error"] = message
            experiment["updated_at"] = utc_now()
            self._record_event_locked(experiment_id, "experiment_failed", message)
            self._active_id = None

    @staticmethod
    def _build_report(
        experiment: dict[str, Any],
        baseline: dict[str, float | None],
        fault: dict[str, float | None],
        recovery: dict[str, float | None],
        recovery_seconds: float,
        scoring: dict[str, Any],
        judge: dict[str, str],
    ) -> dict[str, Any]:
        missing = [name for name, value in fault.items() if value is None]
        confidence = "Low confidence" if len(missing) >= 2 else "Medium confidence" if missing else "High confidence"
        evidence = [
            {"label": "Frontend availability", "value": _comparison(baseline.get("availability"), fault.get("availability"), "%"), "tone": _tone(fault.get("availability"), baseline.get("availability"), higher_is_better=True)},
            {"label": "Frontend error rate", "value": _comparison(baseline.get("error_rate"), fault.get("error_rate"), "%"), "tone": _tone(fault.get("error_rate"), baseline.get("error_rate"), higher_is_better=False)},
            {"label": "Frontend p95 latency", "value": _comparison(baseline.get("p95_latency_ms"), fault.get("p95_latency_ms"), " ms"), "tone": _tone(fault.get("p95_latency_ms"), baseline.get("p95_latency_ms"), higher_is_better=False)},
            {"label": "Verified rollback", "value": f"{recovery_seconds:.1f} seconds", "tone": "good"},
        ]
        remediation = _remediation_for(experiment["plan"])
        return {
            "experiment_id": experiment["id"],
            "generated_at": utc_now(),
            "status": "completed",
            "hypothesis": experiment["plan"]["hypothesis"],
            "approved_plan": experiment["plan"],
            "policy_decision": experiment["policy"],
            "baseline": baseline,
            "fault": fault,
            "recovery": recovery,
            "recovery_seconds": round(recovery_seconds, 2),
            **scoring,
            "confidence": confidence,
            "summary": f"{experiment['plan']['target']} was tested with {experiment['plan']['fault']}; rollback was verified.",
            "evidence": evidence,
            "ai_interpretation": judge,
            "remediation": remediation,
            "limitations": [f"No data for: {', '.join(missing)}"] if missing else [],
        }


def _comparison(before: float | None, during: float | None, suffix: str) -> str:
    if before is None or during is None:
        return "No data"
    return f"{before:.1f}{suffix} → {during:.1f}{suffix}"


def _tone(value: float | None, baseline: float | None, *, higher_is_better: bool) -> str:
    if value is None or baseline is None or math.isclose(value, baseline, rel_tol=0.02, abs_tol=0.1):
        return ""
    improved = value > baseline if higher_is_better else value < baseline
    return "good" if improved else "warn"


def _remediation_for(plan: dict[str, Any]) -> str:
    target = plan.get("target", "the dependency")
    if plan.get("fault") == "network_delay":
        return f"Add a bounded timeout, retry budget, and circuit breaker around calls to {target}; preserve a degraded response path when the latency budget is exhausted."
    return f"Add graceful fallback behavior for {target}, cap caller timeouts, and verify that core journeys remain available while the dependency restarts."
