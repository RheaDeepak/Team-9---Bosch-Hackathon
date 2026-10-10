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
from urllib.parse import urlparse

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
# Synthetic user requests through the store's front door: no metric window, so no lag.
STORE_URL = os.getenv("STORE_URL", "http://localhost:8080").rstrip("/")
PROBES = (("products", "/api/products"), ("cart", "/api/cart?sessionId=chaos-probe&currencyCode=USD"))
PROBE_TIMEOUT_S = 3
RECOVERY_PROBE_MAX_MS = 200   # healthy probes answer in ~10-20 ms; a 250 ms delay fault must read as unhealthy
METRICS_GRACE_S = 30          # after the probes recover, wait this long for the span metrics to confirm
# Envoy (frontend-proxy) also fronts Grafana and Jaeger, which this tool itself polls. That traffic is slow
# (Grafana panel queries took 2-7 s) and is not shop traffic, so it is left out of frontend-proxy's numbers.
TOOLING_PATHS = ("/grafana", "/jaeger")


def watched_services(plan, baseline, blast_radius):
    """Target + everything that depends on it, limited to services with baseline thresholds."""
    thr = baseline["thresholds"]["services"]
    return [s for s in [plan["target"], *blast_radius]
            if any(thr.get(s, {}).get(m) is not None for m in METRICS)]


def _is_tooling(span):
    return span["svc"] == "frontend-proxy" and \
        urlparse(span["attrs"].get("http.url", "")).path.startswith(TOOLING_PATHS)


def probe_store():
    """One round of synthetic requests -> [{name, ok, status, ms}]."""
    out = []
    for name, path in PROBES:
        t = time.time()
        try:
            r = requests.get(STORE_URL + path, timeout=PROBE_TIMEOUT_S)
            ok, status = r.status_code < 400, r.status_code
        except requests.RequestException as e:
            ok, status = False, type(e).__name__
        out.append({"name": name, "ok": ok, "status": status, "ms": round((time.time() - t) * 1000)})
    return out


def live_metrics(services, window_s=WINDOW_S, since=None):
    """{service: {n, errors, error_rate (%), p95_ms}} from the last window_s seconds of server spans.
    `since` (epoch seconds) ignores older spans, so a window that straddles the rollback cannot
    still hold fault-time spans."""
    spans = fetch_spans(lookback=timedelta(seconds=window_s + 5),
                        limits={s: TRACES_PER_SERVICE for s in services})
    cutoff = max((time.time() - window_s) * 1e9, (since or 0) * 1e9)
    out = {}
    for s in services:
        mine = [x for x in spans.values() if x["svc"] == s and x["kind"] in (SERVER, CONSUMER)
                and x["start"] >= cutoff and not _is_tooling(x)]
        durs = sorted(x["dur"] for x in mine if x["dur"] < MAX_SPAN_MS)
        errors = sum(x["status"] in ERROR_CODES for x in mine)
        out[s] = {"n": len(mine), "errors": errors, "request_rate": len(mine) / max(window_s, 1),
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


def restart_counts(names):
    """{service: RestartCount} for the demo containers among `names` (no stats call, so it is quick).
    Taken just before injection: a restart only counts as damage if it happens during the experiment."""
    client = docker.from_env()
    out = {}
    for c in client.containers.list(all=True, filters={"label": f"{PROJECT_LABEL}={COMPOSE_PROJECT}"}):
        name = c.labels.get(SERVICE_LABEL)
        if name in names:
            c.reload()
            out[name] = c.attrs["RestartCount"]
    return out


def _restart_limits(baseline, restart_limits):
    """Restarts allowed before a container counts as damaged: the counts at experiment start when known,
    otherwise the (older) baseline's."""
    if restart_limits is not None:
        return restart_limits
    return {n: t["restarts"] for n, t in baseline["thresholds"]["containers"].items()}


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


def _line(prefix, metrics, breaches, containers=None, probes=None):
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
    if probes:
        parts.append("probes " + " ".join(f"{r['name']}={'ok' if r['ok'] else 'FAIL'}/{r['ms']}ms" for r in probes))
    return f"{prefix} | " + " | ".join(parts) + (f"  <-- {'; '.join(f'{k}: {v}' for k, v in breaches.items())}" if breaches else "")


def _sample(services, window_s, required_containers=()):
    return live_metrics(services, window_s), container_state(set(services) | set(required_containers))


def _sleep(stop, seconds):
    """Sleep, but wake immediately when the stop event is set."""
    if stop is not None:
        stop.wait(seconds)
    else:
        time.sleep(seconds)


def watch(plan, baseline, services, thresholds=None, emit=print, on_sample=None, stop=None, restart_limits=None):
    """Poll while the fault is active. Returns {aborted, reason, elapsed_s, peaks, timeline}.
    emit(line) receives output, on_sample(phase, t, metrics, containers, breaches) every sample,
    and a set `stop` event aborts like a threshold breach would."""
    thr = thresholds or baseline["thresholds"]["services"]
    restart_limits = _restart_limits(baseline, restart_limits)
    expected_down = plan["target"] if plan["fault"] == "container_stop" else None
    duration = min(plan["duration_s"], MAX_DURATION_S)
    for s in services:
        base = baseline["services"][s]
        b = lambda m: "-" if not base[m] else f"{base[m]['mean']:.1f}"
        emit(f"  {s:<16} baseline e={b('error_rate')}% p95={b('p95_ms')}ms | abort at e>"
              f"{thr[s]['error_rate'] if thr[s].get('error_rate') is not None else '-'}% p95>"
              f"{thr[s]['p95_ms'] if thr[s].get('p95_ms') is not None else '-'}ms")
    peaks = {s: {m: None for m in METRICS} for s in services}
    container_peaks = {}
    streak, timeline = defaultdict(int), []
    failures, aborted, reason = 0, False, f"duration elapsed ({duration}s)"
    start = time.monotonic()
    try:
        while time.monotonic() - start < duration:
            if stop is not None and stop.is_set():
                aborted, reason = True, "stopped by user (STOP button)"
                break
            tick, t = time.monotonic(), time.monotonic() - start
            try:
                with ThreadPoolExecutor(1) as pool:  # probes run alongside the metrics fetch, not after it
                    probing = pool.submit(probe_store)
                    metrics, containers = _sample(services, WINDOW_S, [plan["target"]])
                    probes = probing.result()
                failures = 0
            except (requests.RequestException, docker.errors.DockerException, ValueError, KeyError) as e:
                failures += 1
                emit(f"[{t:4.0f}s] telemetry error {failures}/{CONSECUTIVE_BREACHES}: {type(e).__name__}: {e}"[:200])
                if failures >= CONSECUTIVE_BREACHES:
                    aborted, reason = True, "telemetry unavailable, cannot guard the experiment"
                    break
                _sleep(stop, POLL_S)
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
            for k in set(streak) | set(breaches):
                streak[k] = streak[k] + 1 if k in breaches else 0
            timeline.append({"t": round(t, 1), "metrics": metrics, "breaches": breaches,
                             "probes": {r["name"]: {"ok": r["ok"], "ms": r["ms"]} for r in probes}})
            if on_sample:
                on_sample("monitoring", t, metrics, containers, breaches)
            emit(_line(f"[{t:4.0f}s] {plan['target']}:{plan['fault']}", metrics, breaches, containers, probes))
            tripped = [k for k, n in streak.items() if n >= CONSECUTIVE_BREACHES]
            if tripped:
                aborted = True
                reason = f"threshold breached {CONSECUTIVE_BREACHES} samples in a row: " + "; ".join(
                    f"{k} ({breaches[k]})" for k in tripped)
                break
            _sleep(stop, max(0, min(POLL_S - (time.monotonic() - tick), duration - (time.monotonic() - start))))
        else:
            if stop is not None and stop.is_set():  # STOP pressed during the last sleep
                aborted, reason = True, "stopped by user (STOP button)"
    except KeyboardInterrupt:
        aborted, reason = True, "interrupted by user (Ctrl+C)"
    return {"aborted": aborted, "reason": reason, "elapsed_s": round(time.monotonic() - start, 1),
            "peaks": peaks, "container_peaks": container_peaks, "timeline": timeline}


def wait_for_recovery(plan, baseline, services, emit=print, on_sample=None, stop=None, restart_limits=None,
                      on_progress=None):
    """After rollback, poll until the system is healthy again.
    Returns {recovered, seconds, metrics_confirmed, metrics_seconds, samples, healthy_samples, required_samples,
    first_healthy_seconds, elapsed_seconds} (plus `note` if cut short). on_progress({elapsed_s, healthy_samples,
    required_samples, timeout_s, breaches}) is called every sample.

    PRIMARY signal: the synthetic probes answer fast and the target container is running, RECOVERY_SAMPLES
    samples in a row. It has no metric window, so no lag. `seconds` is the time from rollback to the first
    sample of that streak.
    SECONDARY check: span metrics against the baseline thresholds. Their window would still hold fault-time
    spans, so only spans from after the rollback count. Once the primary signal is met we wait up to
    METRICS_GRACE_S for them to confirm; `metrics_confirmed` says whether they did."""
    thr = baseline["thresholds"]["services"]
    restart_limits = _restart_limits(baseline, restart_limits)
    names = set(services) | {plan["target"]}
    since, start = time.time(), time.monotonic()
    p_streak = m_streak = 0
    p_first = m_first = recovered_at = recovered_mono = None
    samples, note = [], None
    try:
        while time.monotonic() - start < RECOVERY_TIMEOUT_S:
            if stop is not None and stop.is_set():
                note = "recovery wait stopped by user"
                break
            tick, t = time.monotonic(), time.monotonic() - start
            probes = probe_store()
            try:
                containers = container_state(names)
                target_up = bool(containers.get(plan["target"], {}).get("running"))
            except (docker.errors.DockerException, requests.RequestException):
                containers, target_up = {}, False
            try:
                metrics = live_metrics(services, RECOVERY_WINDOW_S, since)
                breaches = _breaches(metrics, containers, thr, restart_limits, None)
            except (requests.RequestException, ValueError, KeyError) as e:
                metrics, breaches = {}, {"telemetry": f"{type(e).__name__}: {str(e)[:90]}"}
            primary = target_up and all(r["ok"] and r["ms"] <= RECOVERY_PROBE_MAX_MS for r in probes)
            p_streak, p_first = (p_streak + 1, t if p_first is None else p_first) if primary else (0, None)
            # "no data yet" (an empty post-rollback window) is not confirmation: it needs a service with real numbers
            has_data = any(v["p95_ms"] is not None or v["error_rate"] is not None for v in metrics.values())
            m_streak, m_first = (m_streak + 1, t if m_first is None else m_first) if has_data and not breaches else (0, None)
            samples.append({"t": round(t, 1), "probes": {r["name"]: {"ok": r["ok"], "ms": r["ms"]} for r in probes},
                            "primary_ok": primary, "metric_breaches": breaches})
            if on_sample and metrics:
                on_sample("recovering", t, metrics, containers, breaches)
            emit(_line(f"[+{t:3.0f}s recovery {p_streak}/{RECOVERY_SAMPLES}]", metrics, breaches,
                       containers if metrics else None, probes))
            if on_progress:
                on_progress({"elapsed_s": round(t, 1), "healthy_samples": p_streak, "required_samples": RECOVERY_SAMPLES,
                             "timeout_s": RECOVERY_TIMEOUT_S, "breaches": breaches})
            if p_streak >= RECOVERY_SAMPLES and recovered_at is None:
                recovered_at, recovered_mono = p_first, time.monotonic()
            if recovered_at is not None and (m_streak >= RECOVERY_SAMPLES
                                             or time.monotonic() - recovered_mono >= METRICS_GRACE_S):
                break
            _sleep(stop, max(0, POLL_S - (time.monotonic() - tick)))
    except KeyboardInterrupt:
        note = "recovery wait interrupted by user"
    confirmed = m_streak >= RECOVERY_SAMPLES
    out = {"recovered": recovered_at is not None, "seconds": None if recovered_at is None else round(recovered_at, 1),
           "first_healthy_seconds": None if recovered_at is None else round(recovered_at, 1),
           "healthy_samples": p_streak, "required_samples": RECOVERY_SAMPLES,
           "elapsed_seconds": round(time.monotonic() - start, 1),
           "metrics_confirmed": confirmed, "metrics_seconds": round(m_first, 1) if confirmed else None,
           "samples": samples}
    if note:
        out["note"] = note
    return out


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


def probe_availability(timeline):
    """Share (0-100) of synthetic probe requests that succeeded during the fault, or None if none ran."""
    results = [p["ok"] for t in timeline for p in t.get("probes", {}).values()]
    return 100 * sum(results) / len(results) if results else None
