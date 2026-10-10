"""Monitoring baseline: per-service span metrics (Prometheus) + per-container stats (Docker)."""
import json
import math
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import docker
import requests

# Prometheus is published on the host by compose.observability.yaml (${PROMETHEUS_PORT}=9090).
PROM_URL = "http://localhost:9090"
DEMO_DIR = Path(r"C:\Users\rhea\opentelemetry-demo")  # read-only
FLAGD_FILE = DEMO_DIR / "src" / "flagd" / "demo.flagd.json"
COMPOSE_PROJECT = "opentelemetry-demo"
BASELINE_JSON = Path("baseline.json")
SAMPLE_INTERVAL_S = 15
BASELINE_DURATION_S = 300
# Prometheus scrapes every 60s, so a rate window needs >= 2 scrapes to return anything.
RATE_WINDOW = "3m"
CALLS = "traces_span_metrics_calls_total"
BUCKET = "traces_span_metrics_duration_milliseconds_bucket"
# A "request" is a span the service handled: HTTP/gRPC server spans and Kafka consumer spans.
REQUEST_KINDS = 'span_kind=~"SPAN_KIND_SERVER|SPAN_KIND_CONSUMER"'
ERROR_STATUS = "STATUS_CODE_ERROR"
# Suggested abort thresholds
ERROR_PP = 5            # error rate: baseline mean + 5 percentage points
LATENCY_MULT = 3        # latency: max(3x mean, mean + 3 std)
STD_MULT = 3
CPU_MULT, CPU_MIN = 3, 20.0   # container CPU %: max(3x mean, mean + 3 std, 20%)
MEM_MULT = 1.5                # container memory: max(1.5x mean, mean + 3 std)

SERVICE_METRICS = ("request_rate", "error_rate", "p50_ms", "p95_ms", "p99_ms")
CONTAINER_METRICS = ("running", "restarts", "cpu_percent", "memory_mb")


# ---- Prometheus: per-service span metrics ---------------------------------------------

def _num(x):
    v = float(x)
    return None if math.isnan(v) or math.isinf(v) else v


def _prom(expr):
    resp = requests.get(f"{PROM_URL}/api/v1/query", params={"query": expr}, timeout=10)
    resp.raise_for_status()
    return {m["metric"].get("service_name"): _num(m["value"][1]) for m in resp.json()["data"]["result"]}


def service_metrics():
    """{service: {request_rate (req/s), error_rate (%), p50_ms, p95_ms, p99_ms}}; None = no data."""
    w = RATE_WINDOW
    try:
        rate = _prom(f"sum by (service_name) (rate({CALLS}{{{REQUEST_KINDS}}}[{w}]))")
        errs = _prom(f'sum by (service_name) (rate({CALLS}{{{REQUEST_KINDS},status_code="{ERROR_STATUS}"}}[{w}]))')
        quant = {q: _prom(f"histogram_quantile({q}, sum by (service_name, le) "
                          f"(rate({BUCKET}{{{REQUEST_KINDS}}}[{w}])))") for q in (0.5, 0.95, 0.99)}
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"Prometheus query failed at {PROM_URL}: {e}")
        return {}
    out = {}
    for svc, r in rate.items():
        busy = r is not None and r > 0
        out[svc] = {"request_rate": r,
                    "error_rate": 100 * (errs.get(svc) or 0) / r if busy else None,
                    **{f"p{int(q * 100)}_ms": quant[q].get(svc) if busy else None for q in quant}}
    return out


# ---- Docker: per-container status and resource use ------------------------------------

def _container_stats(c):
    info = {"running": int(c.status == "running"), "status": c.status,
            "restarts": c.attrs["RestartCount"], "cpu_percent": None, "memory_mb": None}
    if c.status != "running":
        return c.labels["com.docker.compose.service"], info
    try:
        s = c.stats(stream=False)
        cpu, pre = s["cpu_stats"], s["precpu_stats"]
        d_cpu = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        d_sys = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        if d_sys > 0 and d_cpu >= 0:
            info["cpu_percent"] = d_cpu / d_sys * (cpu.get("online_cpus") or 1) * 100
        mem = s["memory_stats"]
        used = mem["usage"] - mem.get("stats", {}).get("inactive_file", 0)  # like `docker stats`
        info["memory_mb"] = used / 2**20
    except (KeyError, docker.errors.DockerException, requests.RequestException):
        pass  # keep status/restarts, leave CPU/memory as None
    return c.labels["com.docker.compose.service"], info


def container_metrics():
    """{compose service: {running, status, restarts, cpu_percent, memory_mb}} for the demo project."""
    try:
        client = docker.from_env()
        containers = client.containers.list(
            all=True, filters={"label": f"com.docker.compose.project={COMPOSE_PROJECT}"})
        with ThreadPoolExecutor(8) as pool:  # each stats call takes ~1s
            return dict(pool.map(_container_stats, containers))
    except (docker.errors.DockerException, requests.RequestException) as e:
        print(f"Docker query failed: {e}")
        return {}


def current_snapshot():
    """Live values, shaped like baseline["services"] / baseline["containers"] but one number per metric."""
    return {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "services": service_metrics(), "containers": container_metrics()}


# ---- Feature flags --------------------------------------------------------------------

def enabled_flags():
    """Flags in the demo's flagd config whose default variant is not off."""
    try:
        flags = json.loads(FLAGD_FILE.read_text(encoding="utf-8"))["flags"]
    except (OSError, ValueError, KeyError) as e:
        print(f"Could not read flagd config {FLAGD_FILE}: {e}")
        return []
    on = []
    for name, f in sorted(flags.items()):
        variant = f.get("defaultVariant")
        if f.get("state") == "ENABLED" and variant != "off" and f["variants"].get(variant) not in (False, 0, None):
            on.append(f"{name}={variant}")
    return on


# ---- Baseline -------------------------------------------------------------------------

def _stats(values):
    v = [x for x in values if x is not None]
    if not v:
        return None
    return {"mean": statistics.mean(v), "std": statistics.pstdev(v), "max": max(v), "n": len(v)}


def _latency_limit(s):
    return max(LATENCY_MULT * s["mean"], s["mean"] + STD_MULT * s["std"])


def _aggregate(samples, group, metrics):
    names = sorted({n for s in samples for n in s[group]})
    return {n: {m: _stats([s[group].get(n, {}).get(m) for s in samples]) for m in metrics} for n in names}


def _thresholds(services, containers):
    svc = {}
    for n, m in services.items():
        e, p95, p99 = m["error_rate"], m["p95_ms"], m["p99_ms"]
        svc[n] = {"error_rate": e["mean"] + ERROR_PP if e else None,
                  "p95_ms": _latency_limit(p95) if p95 else None,
                  "p99_ms": _latency_limit(p99) if p99 else None}
    con = {}
    for n, m in containers.items():
        cpu, mem, rst = m["cpu_percent"], m["memory_mb"], m["restarts"]
        con[n] = {"cpu_percent": max(CPU_MULT * cpu["mean"], cpu["mean"] + STD_MULT * cpu["std"], CPU_MIN) if cpu else None,
                  "memory_mb": max(MEM_MULT * mem["mean"], mem["mean"] + STD_MULT * mem["std"]) if mem else None,
                  "restarts": rst["max"] if rst else None,  # any restart beyond this is an abort
                  "running": 1}
    return {"services": svc, "containers": con}


def capture_baseline(duration_s=BASELINE_DURATION_S, interval_s=SAMPLE_INTERVAL_S, path=BASELINE_JSON):
    flags = enabled_flags()
    if flags:
        print(f"WARNING: feature flags are enabled, the baseline will be polluted: {', '.join(flags)}")
    n = max(1, duration_s // interval_s)
    print(f"Sampling {n} times every {interval_s}s ({n * interval_s}s)...")
    samples, start = [], time.monotonic()
    for i in range(n):
        samples.append(current_snapshot())
        s = samples[-1]
        print(f"  sample {i + 1}/{n}: {len(s['services'])} services, {len(s['containers'])} containers")
        if i < n - 1:
            time.sleep(max(0, start + (i + 1) * interval_s - time.monotonic()))
    services = _aggregate(samples, "services", SERVICE_METRICS)
    containers = _aggregate(samples, "containers", CONTAINER_METRICS)
    baseline = {"captured_at": samples[0]["timestamp"], "duration_s": n * interval_s,
                "interval_s": interval_s, "samples": n, "flags_enabled": flags,
                "services": services, "containers": containers,
                "thresholds": _thresholds(services, containers)}
    path.write_text(json.dumps(baseline, indent=2), encoding="utf-8")
    print_baseline(baseline)
    print(f"\nSaved {path}")
    return baseline


def load_baseline(path=BASELINE_JSON):
    """Baseline dict, or None if it has not been captured yet."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"No usable baseline at {path}: {e}")
        return None


def _f(x, spec=".1f"):
    return "-" if x is None else format(x, spec)


def print_baseline(b):
    print(f"\nBaseline: {b['samples']} samples over {b['duration_s']}s"
          + (f"  (flags on: {', '.join(b['flags_enabled'])})" if b["flags_enabled"] else ""))
    print(f"\n{'service':<16}{'req/s':>8}{'err% avg':>9}{'err% max':>9}{'p50':>7}{'p95 avg':>9}"
          f"{'p95 std':>8}{'p95 max':>8}{'p99 avg':>9} | {'abort err%':>10}{'abort p95':>10}")
    for n, m in b["services"].items():
        g = lambda k, f: _f((m[k] or {}).get(f))
        t = b["thresholds"]["services"][n]
        print(f"{n:<16}{g('request_rate', 'mean'):>8}{g('error_rate', 'mean'):>9}{g('error_rate', 'max'):>9}"
              f"{g('p50_ms', 'mean'):>7}{g('p95_ms', 'mean'):>9}{g('p95_ms', 'std'):>8}{g('p95_ms', 'max'):>8}"
              f"{g('p99_ms', 'mean'):>9} | {_f(t['error_rate']):>10}{_f(t['p95_ms']):>10}")
    print(f"\n{'container':<16}{'up':>5}{'restarts':>9}{'cpu% avg':>9}{'cpu% max':>9}{'mem avg MB':>11}"
          f"{'mem max MB':>11} | {'abort cpu%':>10}{'abort MB':>9}")
    for n, m in b["containers"].items():
        g = lambda k, f: _f((m[k] or {}).get(f))
        t = b["thresholds"]["containers"][n]
        print(f"{n:<16}{g('running', 'mean'):>5}{g('restarts', 'max', ):>9}{g('cpu_percent', 'mean'):>9}"
              f"{g('cpu_percent', 'max'):>9}{g('memory_mb', 'mean'):>11}{g('memory_mb', 'max'):>11}"
              f" | {_f(t['cpu_percent']):>10}{_f(t['memory_mb'], '.0f'):>9}")


if __name__ == "__main__":
    capture_baseline()
