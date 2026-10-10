"""Guard: watch the target and its blast radius while a fault is active, and decide when to abort.

Live numbers come from Jaeger's recent server spans, not Prometheus: span metrics reach Prometheus
once per 60s (OTLP push + 60s scrape), so a 30s rate window is empty and detection would lag minutes.
Error rate and p95 are defined as in metrics.py (server/consumer spans; status ERROR; duration in ms).
"""
import math
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import docker
import requests
from dotenv import load_dotenv

from discovery import CONSUMER, SERVER, fetch_spans
from inject import COMPOSE_PROJECT, PROJECT_LABEL, SERVICE_LABEL
from metrics import LATENCY_EXCLUDED, _container_stats
from policy import MAX_DURATION_S

load_dotenv()

POLL_S = 5
WINDOW_S = 30             # spans newer than this count towards a sample
RECOVERY_WINDOW_S = 15    # shorter, so old fault-time errors age out sooner
TRACES_PER_SERVICE = 60
MIN_SPANS_ERROR = 1       # smallest sample that still yields an error rate
MIN_FAILED_REQUESTS = 3   # abort on error rate only with at least this many failed requests in the window
MIN_SPANS_LATENCY = 5     # a p95 from fewer spans is noise
CONSECUTIVE_BREACHES = 2
RECOVERY_SAMPLES = max(1, int(os.getenv("RECOVERY_REQUIRED_SAMPLES", "2")))
RECOVERY_TIMEOUT_S = max(POLL_S, int(os.getenv("RECOVERY_TIMEOUT_SECONDS", "180")))
MAX_SPAN_MS = 60_000      # longer spans are streams (flagd EventStream), not request latency
ERROR_CODES = (2, "STATUS_CODE_ERROR")  # OTLP status codes: 0 unset, 1 ok, 2 error
METRICS = ("error_rate", "p95_ms")


def watched_services(plan, baseline, blast_radius):
    """Target + everything that depends on it, limited to services with baseline thresholds."""
    thr = baseline["thresholds"]["services"]
    return [s for s in [plan["target"], *blast_radius]
            if any(thr.get(s, {}).get(m) is not None for m in METRICS)]


def live_metrics(services, window_s=WINDOW_S):
    """{service: {n, error_rate (%), p95_ms}} from the last window_s seconds of server spans."""
    spans = fetch_spans(lookback=timedelta(seconds=window_s + 5),
                        limits={s: TRACES_PER_SERVICE for s in services})
    cutoff = (time.time() - window_s) * 1e9
    out = {}
    for s in services:
        mine = [x for x in spans.values()
                if x["svc"] == s and x["kind"] in (SERVER, CONSUMER) and x["start"] >= cutoff]
        durs = sorted(x["dur"] for x in mine if x["dur"] < MAX_SPAN_MS)
        errors = sum(x["status"] in ERROR_CODES for x in mine)
        out[s] = {"n": len(mine), "errors": errors,
                  "request_rate": len(mine) / max(window_s, 1),
                  "error_rate": 100 * errors / len(mine) if len(mine) >= MIN_SPANS_ERROR else None,
                  "p95_ms": durs[math.ceil(0.95 * len(durs)) - 1] if len(durs) >= MIN_SPANS_LATENCY else None}
    return out


def container_state(names):
    """{service: {running, restarts, cpu_percent, memory_mb}} for the demo containers among `names`.
    CPU/memory are for display and the summary only; they are never abort conditions."""
    client = docker.from_env()
    wanted = []
    for c in client.containers.list(all=True, filters={"label": f"{PROJECT_LABEL}={COMPOSE_PROJECT}"}):
        if c.labels.get(SERVICE_LABEL) in names:
            c.reload()
            wanted.append(c)
    with ThreadPoolExecutor(8) as pool:  # each stats call takes ~1s
        return dict(pool.map(_container_stats, wanted))


def _breaches(metrics, containers, thr, restart_limits, expected_down):
    out = {}
    for s, v in metrics.items():
        for m, unit in (("error_rate", "%"), ("p95_ms", "ms")):
            limit = thr.get(s, {}).get(m)
            if m == "p95_ms" and s in LATENCY_EXCLUDED:
                continue  # e.g. flagd: streaming spans skew latency
            if m == "error_rate" and v["errors"] < MIN_FAILED_REQUESTS:
                continue  # a percentage over a handful of requests is noise
            if v[m] is not None and limit is not None and v[m] > limit:
                out[f"{s} {m}"] = f"{v[m]:.1f}{unit} > {limit:.1f}{unit}" + (
                    f" ({v['errors']} failed)" if m == "error_rate" else "")
    for s, v in containers.items():
        if not v["running"] and s != expected_down:
            out[f"{s} container"] = "not running"
        elif v["restarts"] > (restart_limits.get(s) if restart_limits.get(s) is not None else math.inf):
            out[f"{s} restarts"] = f"restarted ({v['restarts']})"
    return out


def _line(prefix, metrics, breaches, containers=None):
    parts = []
    for s, v in metrics.items():
        e = "-" if v["error_rate"] is None else f"{v['error_rate']:.1f}%"
        p = "-" if v["p95_ms"] is None else f"{v['p95_ms']:.0f}ms"
        flag = "!" if any(k.startswith(f"{s} ") for k in breaches) else ""
        parts.append(f"{s} e={e} p95={p}{flag}")
    if containers:
        parts.append("cpu/mem " + " ".join(
            f"{n}={'-' if v['cpu_percent'] is None else format(v['cpu_percent'], '.0f') + '%'}/"
            f"{'-' if v['memory_mb'] is None else format(v['memory_mb'], '.0f') + 'MB'}" for n, v in containers.items()))
    return f"{prefix} | " + " | ".join(parts) + (f"  <-- {'; '.join(f'{k}: {v}' for k, v in breaches.items())}" if breaches else "")


def _sample(services, window_s, required_containers=()):
    containers = set(services) | set(required_containers)
    return live_metrics(services, window_s), container_state(containers)


def watch(plan, baseline, services, thresholds=None, stop_event=None, on_sample=None):
    """Poll while the fault is active. Returns {aborted, reason, elapsed_s, peaks, timeline}."""
    thr = thresholds or baseline["thresholds"]["services"]
    restart_limits = {n: t["restarts"] for n, t in baseline["thresholds"]["containers"].items()}
    expected_down = plan["target"] if plan["fault"] == "container_stop" else None
    duration = min(plan["duration_s"], MAX_DURATION_S)
    for s in services:
        base = baseline["services"][s]
        b = lambda m: "-" if not base[m] else f"{base[m]['mean']:.1f}"
        print(f"  {s:<16} baseline e={b('error_rate')}% p95={b('p95_ms')}ms | abort at e>"
              f"{thr[s]['error_rate'] if thr[s].get('error_rate') is not None else '-'}% p95>"
              f"{thr[s]['p95_ms'] if thr[s].get('p95_ms') is not None else '-'}ms")
    peaks = {s: {m: None for m in METRICS} for s in services}
    container_peaks = {}
    streak, timeline = defaultdict(int), []
    failures, aborted, reason = 0, False, f"duration elapsed ({duration}s)"
    start = time.monotonic()
    try:
        while time.monotonic() - start < duration:
            if stop_event is not None and stop_event.is_set():
                aborted, reason = True, "operator requested emergency stop"
                break
            tick, t = time.monotonic(), time.monotonic() - start
            try:
                metrics, containers = _sample(services, WINDOW_S, [plan["target"]])
                failures = 0
            except (requests.RequestException, docker.errors.DockerException, ValueError, KeyError) as e:
                failures += 1
                print(f"[{t:4.0f}s] telemetry error {failures}/{CONSECUTIVE_BREACHES}: {type(e).__name__}: {e}"[:200])
                if failures >= CONSECUTIVE_BREACHES:
                    aborted, reason = True, "telemetry unavailable, cannot guard the experiment"
                    break
                time.sleep(POLL_S)
                continue
            for s, v in metrics.items():
                for m in METRICS:
                    if v[m] is not None and (peaks[s][m] is None or v[m] > peaks[s][m]):
                        peaks[s][m] = v[m]
            for n, v in containers.items():
                cp = container_peaks.setdefault(n, {"cpu_percent": None, "memory_mb": None})
                for m in cp:
                    if v[m] is not None and (cp[m] is None or v[m] > cp[m]):
                        cp[m] = v[m]
            breaches = _breaches(metrics, containers, thr, restart_limits, expected_down)
            if on_sample is not None:
                on_sample(metrics, containers)
            for k in set(streak) | set(breaches):
                streak[k] = streak[k] + 1 if k in breaches else 0
            timeline.append({"t": round(t, 1), "metrics": metrics, "breaches": breaches})
            print(_line(f"[{t:4.0f}s] {plan['target']}:{plan['fault']}", metrics, breaches, containers), flush=True)
            tripped = [k for k, n in streak.items() if n >= CONSECUTIVE_BREACHES]
            if tripped:
                aborted = True
                reason = f"threshold breached {CONSECUTIVE_BREACHES} samples in a row: " + "; ".join(
                    f"{k} ({breaches[k]})" for k in tripped)
                break
            wait_s = max(0, min(POLL_S - (time.monotonic() - tick), duration - (time.monotonic() - start)))
            if stop_event is not None:
                if stop_event.wait(wait_s):
                    aborted, reason = True, "operator requested emergency stop"
                    break
            else:
                time.sleep(wait_s)
    except KeyboardInterrupt:
        aborted, reason = True, "interrupted by user (Ctrl+C)"
    return {"aborted": aborted, "reason": reason, "elapsed_s": round(time.monotonic() - start, 1),
            "peaks": peaks, "container_peaks": container_peaks, "timeline": timeline}


def wait_for_recovery(plan, baseline, services, on_sample=None, on_progress=None):
    """After rollback: poll until everything is within the baseline thresholds for RECOVERY_SAMPLES in a row.
    Returns {recovered, seconds} where seconds = time from rollback until the healthy streak is verified."""
    thr = baseline["thresholds"]["services"]
    restart_limits = {n: t["restarts"] for n, t in baseline["thresholds"]["containers"].items()}
    start, streak, first_ok, last_breaches = time.monotonic(), 0, None, {}
    try:
        while time.monotonic() - start < RECOVERY_TIMEOUT_S:
            tick, t = time.monotonic(), time.monotonic() - start
            try:
                metrics, containers = _sample(services, RECOVERY_WINDOW_S, [plan["target"]])
                breaches = _breaches(metrics, containers, thr, restart_limits, None)
                if on_sample is not None:
                    on_sample(metrics, containers)
                if plan["target"] not in containers or not containers[plan["target"]]["running"]:
                    breaches[f"{plan['target']} container"] = "not running"
            except (requests.RequestException, docker.errors.DockerException, ValueError, KeyError) as e:
                metrics, breaches = {}, {"telemetry": f"{type(e).__name__}: {str(e)[:90]}"}
            print(_line(f"[+{t:3.0f}s recovery {streak}/{RECOVERY_SAMPLES}]", metrics, breaches, containers if metrics else None), flush=True)
            if breaches:
                streak, first_ok = 0, None
            else:
                streak, first_ok = streak + 1, first_ok if first_ok is not None else t
                if streak >= RECOVERY_SAMPLES:
                    verified_at = round(time.monotonic() - start, 1)
                    return {
                        "recovered": True,
                        "seconds": verified_at,
                        "first_healthy_seconds": round(first_ok, 1),
                        "healthy_samples": streak,
                        "required_samples": RECOVERY_SAMPLES,
                    }
            last_breaches = breaches
            if on_progress is not None:
                on_progress({
                    "elapsed_s": round(t, 1),
                    "healthy_samples": streak,
                    "required_samples": RECOVERY_SAMPLES,
                    "timeout_s": RECOVERY_TIMEOUT_S,
                    "breaches": breaches,
                })
            time.sleep(max(0, POLL_S - (time.monotonic() - tick)))
    except KeyboardInterrupt:
        return {"recovered": False, "seconds": None, "note": "recovery wait interrupted by user"}
    detail = "; ".join(f"{key}: {value}" for key, value in last_breaches.items())
    note = f"not within thresholds after {RECOVERY_TIMEOUT_S}s"
    if detail:
        note += f" ({detail})"
    return {
        "recovered": False,
        "seconds": None,
        "elapsed_seconds": RECOVERY_TIMEOUT_S,
        "healthy_samples": streak,
        "required_samples": RECOVERY_SAMPLES,
        "note": note,
    }


def impact_table(baseline, peaks):
    """Per service: baseline -> peak for error rate and p95, plus the change."""
    rows = []
    for s, p in peaks.items():
        row = {"service": s}
        for m in METRICS:
            b = (baseline["services"][s][m] or {}).get("mean")
            row[m] = {"baseline": b, "peak": p[m], "change": None if b is None or p[m] is None else p[m] - b}
        rows.append(row)
    return rows


def container_table(baseline, container_peaks):
    """Per container: baseline mean -> peak CPU % and memory MB (reference only)."""
    rows = []
    for n, p in container_peaks.items():
        row = {"container": n}
        for m in ("cpu_percent", "memory_mb"):
            b = ((baseline["containers"].get(n) or {}).get(m) or {}).get("mean")
            row[m] = {"baseline": b, "peak": p[m]}
        rows.append(row)
    return rows
