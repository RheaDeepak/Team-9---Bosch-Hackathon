"""Discovery: build the service dependency graph from Jaeger + compose config."""
import json
import os
import re
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx
import requests
import yaml
from dotenv import load_dotenv

load_dotenv()

JAEGER_BASE_URL = os.getenv("JAEGER_URL", "http://localhost:16686").rstrip("/")
JAEGER_URL = os.getenv("JAEGER_DEPENDENCY_URL", f"{JAEGER_BASE_URL}/api/dependencies")
DEMO_DIR = Path(os.getenv("TARGET_REPOSITORY_PATH", "../opentelemetry-demo")).resolve()
COMPOSE_FILES = ["compose.yaml", "compose.full.yaml"]
ENV_FILE = ".env"
OUT_JSON = Path("dependency_map.json")
OUT_PNG = Path("dependency_map.png")
ROOT_SERVICE = "checkout"
TOP_N = 5
# Observability infra: every service talks to it, so it is noise.
EXCLUDED = {"otel-collector", "jaeger", "prometheus", "grafana", "opensearch"}
# Config targets count as "hidden" only if untraced and they look like data infrastructure.
HIDDEN_HINTS = ("kafka", "valkey", "redis", "postgres", "db")
# Jaeger v2 has no /api/traces; it serves OTLP-JSON traces under /api/v3/traces.
TRACES_URL = os.getenv("JAEGER_TRACES_URL", f"{JAEGER_BASE_URL}/api/v3/traces")
# load-generator roots every user journey; the others are the Kafka consumers.
TRACE_SERVICES = ("load-generator", "accounting", "fraud-detection")
TRACE_LIMIT = 100          # traces per service above
EXTRA_LIMIT = 20           # traces per remaining service, to cover rarely-hit edges
MAX_LATENCY_MS = 60_000    # longer spans are streams (e.g. flagd EventStream), not request latency
TRACE_LOOKBACK = timedelta(hours=1)
JOURNEY_PREFIX = "user_"   # root span names of the load generator's user tasks
MIN_STEP_SHARE = 0.5       # a step must appear in this share of a journey's traces
CACHE_SYSTEMS = {"redis", "valkey"}
SERVER, CLIENT, PRODUCER, CONSUMER = 2, 3, 4, 5  # OTLP span kinds
EDGE_COLORS = {"sync": "#1f77b4", "async": "#c2185b", "data": "#2a9d6f"}


def fetch_dependencies(lookback_ms=3_600_000, url=JAEGER_URL):
    params = {"endTs": int(time.time() * 1000), "lookback": lookback_ms}
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()["data"]


def build_graph(deps):
    """Directed graph: edge parent -> child, weighted by call count.
    Self-calls (intra-service spans) are dropped."""
    g = nx.DiGraph()
    for d in deps:
        if d["parent"] == d["child"]:
            g.add_node(d["parent"])
            continue
        g.add_edge(d["parent"], d["child"], call_count=d["callCount"], source="trace")
    return g


def compute_stats(g):
    fan_in = {n: g.in_degree(n) for n in g}
    fan_out = {n: g.out_degree(n) for n in g}
    path = {ROOT_SERVICE} | nx.descendants(g, ROOT_SERVICE) if ROOT_SERVICE in g else set()
    top = sorted(fan_in, key=lambda n: (-fan_in[n], n))[:TOP_N]
    return {"fan_in": fan_in, "fan_out": fan_out,
            "checkout_path": sorted(path), "top_fan_in": top}


def _load_env(path):
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*([A-Za-z_]\w*)=(.*)", line)
        if m:
            env[m[1]] = m[2].split(" #")[0].strip().strip("\"'")
    return env


def _expand(text, env):
    for _ in range(5):  # .env values nest, e.g. KAFKA_ADDR=${KAFKA_HOST}:${KAFKA_PORT}
        text = re.sub(r"\$\{(\w+)[^}]*\}", lambda m: env.get(m[1], ""), text)
    return text


def parse_compose(demo_dir=DEMO_DIR):
    """Return {service: {(target, reason)}} from depends_on and env-var hostnames."""
    env = _load_env(demo_dir / ENV_FILE)
    services = {}  # name -> {"deps": set, "env": [(key, resolved value)]}
    for fname in COMPOSE_FILES:
        doc = yaml.safe_load((demo_dir / fname).read_text(encoding="utf-8"))
        for name, svc in doc.get("services", {}).items():
            entry = services.setdefault(name, {"deps": set(), "env": []})
            entry["deps"] |= set(svc.get("depends_on") or [])
            raw = svc.get("environment") or []
            pairs = raw.items() if isinstance(raw, dict) else (
                x.split("=", 1) if "=" in x else (x, None) for x in raw)
            for k, v in pairs:
                # a bare "KAFKA_ADDR" entry takes its value from .env
                entry["env"].append((k, _expand(str(env.get(k, "") if v is None else v), env)))
    links = {}
    for name, entry in services.items():
        found = {(t, "depends_on") for t in entry["deps"] if t in services}
        for k, v in entry["env"]:
            for t in services:
                if re.search(rf"(?<![\w.-]){re.escape(t)}(?![\w-])", v):
                    found.add((t, f"env {k}"))
        links[name] = {(t, why) for t, why in found if t != name}
    return links


def add_hidden(g, links):
    """Add source="config" edges to untraced data infrastructure; return (src, dst) added."""
    traced = set(g.nodes)
    for src, targets in sorted(links.items()):
        if src in EXCLUDED or src not in traced:
            continue
        for tgt, why in sorted(targets):
            if tgt in EXCLUDED or tgt in traced or not any(h in tgt for h in HIDDEN_HINTS):
                continue
            if not g.has_edge(src, tgt):
                g.add_edge(src, tgt, call_count=0, source="config", via=[])
            g[src][tgt]["via"].append(why)
    return [(u, v) for u, v, d in g.edges(data=True) if d["source"] == "config"]


def _attrs(obj):
    """OTLP attribute list -> {key: value}."""
    return {a["key"]: next(iter(a["value"].values()), None) for a in obj.get("attributes", [])}


def fetch_spans(extra_services=(), url=TRACES_URL, lookback=TRACE_LOOKBACK, limits=None):
    """Sample traces from Jaeger (OTLP JSON) -> {(trace_id, span_id): span dict}.
    `limits` ({service: max traces}) overrides the default service list."""
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    spans = {}
    limits = limits or {**{s: EXTRA_LIMIT for s in extra_services}, **{s: TRACE_LIMIT for s in TRACE_SERVICES}}
    for svc, limit in limits.items():
        params = {"query.service_name": svc, "query.num_traces": limit,
                  "query.start_time_min": (now - lookback).strftime(fmt),
                  "query.start_time_max": now.strftime(fmt)}
        resp = requests.get(url, params=params, timeout=60)
        resp.raise_for_status()
        for rs in resp.json().get("result", {}).get("resourceSpans", []):
            svc_name = _attrs(rs.get("resource", {})).get("service.name", "unknown")
            for ss in rs.get("scopeSpans", []):
                for s in ss.get("spans", []):
                    start, end = int(s["startTimeUnixNano"]), int(s["endTimeUnixNano"])
                    spans[(s["traceId"], s["spanId"])] = {
                        "trace": s["traceId"], "parent": s.get("parentSpanId", ""),
                        "svc": svc_name, "name": s["name"], "kind": s.get("kind", 0),
                        "start": start, "dur": (end - start) / 1e6, "attrs": _attrs(s),
                        "status": s.get("status", {}).get("code", 0)}  # 0 unset, 1 ok, 2 error
    return spans


def _classify(a):
    """(protocol, type) from span attributes; instrumentations use old and new semconv names."""
    if "messaging.system" in a:
        return a["messaging.system"], "async-messaging"
    db = a.get("db.system") or a.get("db.system.name")
    if db:
        return db, "cache" if db in CACHE_SYSTEMS else "database"
    rpc = a.get("rpc.system") or a.get("rpc.system.name")
    if rpc:
        return rpc, f"sync-{rpc}"
    if "http.request.method" in a or "http.method" in a:
        return "http", "sync-http"
    return None


def _operation(a):
    if a.get("rpc.method"):
        return a["rpc.method"].split("/")[-1]
    if "messaging.system" in a:
        return f"{a.get('messaging.operation', '')} {a.get('messaging.destination.name', '')}".strip()
    stmt = a.get("db.query.text") or a.get("db.statement") or ""
    if a.get("db.operation.name") or stmt.strip():
        return a.get("db.operation.name") or stmt.split()[0].upper()
    method = a.get("http.request.method") or a.get("http.method")
    if method:
        url = a.get("url.full") or a.get("http.url") or ""
        return f"{method} {a.get('http.route') or a.get('url.path') or urlparse(url).path}".strip()
    return None


def extract_edges(spans):
    """One record per cross-service call, store access or Kafka hop seen in the traces."""
    recs = []
    for s in spans.values():
        a, p = s["attrs"], spans.get((s["trace"], s["parent"]))
        topic, lat = None, s["dur"]
        if s["kind"] == SERVER and p and p["svc"] != s["svc"]:
            src, dst = p["svc"], s["svc"]
            cls = _classify(p["attrs"]) or _classify(a)
            # prefer the gRPC method name from the client span over the server's HTTP/2 route
            op = (_operation(p["attrs"]) if "rpc.method" in p["attrs"] else None)                 or _operation(a) or _operation(p["attrs"])
            lat = p["dur"] if p["kind"] == CLIENT else s["dur"]
        elif s["kind"] == CLIENT and ("db.system" in a or "db.system.name" in a):
            src, dst, cls, op = s["svc"], a.get("server.address"), _classify(a), _operation(a)
        elif s["kind"] in (PRODUCER, CONSUMER) and "messaging.system" in a:
            src, dst, cls, op = s["svc"], a.get("peer.service") or a["messaging.system"], _classify(a), _operation(a)
            topic = a.get("messaging.destination.name")
            if s["kind"] == CONSUMER:
                lat = None  # a receive span times the poll, not the hop
        else:
            continue
        if cls and dst:
            recs.append({"trace": s["trace"], "start": s["start"], "src": src, "dst": dst,
                         "protocol": cls[0], "type": cls[1], "op": op, "topic": topic, "lat": lat})
    return recs


def annotate_edges(g, recs):
    """Add protocol/type/mode/operations/topic/avg latency to the matching graph edges."""
    by_edge = defaultdict(list)
    for r in recs:
        by_edge[(r["src"], r["dst"])].append(r)
    for (u, v), rs in by_edge.items():
        if not g.has_edge(u, v):
            continue
        protocol, typ = Counter((r["protocol"], r["type"]) for r in rs).most_common(1)[0][0]
        lats = [r["lat"] for r in rs if r["lat"] is not None and r["lat"] < MAX_LATENCY_MS]
        g[u][v].update(
            protocol=protocol, type=typ, mode="async" if typ == "async-messaging" else "sync",
            operations=sorted({r["op"] for r in rs if r["op"]}),
            topic=",".join(sorted({r["topic"] for r in rs if r["topic"]})) or None,
            avg_latency_ms=round(statistics.mean(lats), 2) if lats else None, samples=len(rs))


def build_flows(spans, recs):
    """Group traces by root operation (user_*) -> ordered service/operation steps."""
    roots = {s["trace"]: s for s in spans.values()
             if not s["parent"] and s["name"].startswith(JOURNEY_PREFIX)}
    runs = defaultdict(list)  # journey -> [{step: offset_ms of first occurrence}, ...]
    by_trace = defaultdict(dict)
    for r in sorted(recs, key=lambda r: r["start"]):
        if r["trace"] in roots:
            step = (r["src"], r["op"], r["dst"], r["protocol"])
            by_trace[r["trace"]].setdefault(step, (r["start"] - roots[r["trace"]]["start"]) / 1e6)
    for t, steps in by_trace.items():
        runs[roots[t]["name"][len(JOURNEY_PREFIX):]].append(steps)
    flows = {}
    for name, rs in sorted(runs.items()):
        offsets = defaultdict(list)
        for steps in rs:
            for step, off in steps.items():
                offsets[step].append(off)
        keep = sorted(((statistics.mean(o), st, len(o) / len(rs)) for st, o in offsets.items()
                       if len(o) / len(rs) >= MIN_STEP_SHARE), key=lambda k: k[0])
        steps = [{"from": st[0], "operation": st[1], "to": st[2], "protocol": st[3],
                  "share": round(sh, 2)} for _, st, sh in keep]
        flows[name] = {"traces": len(rs), "steps": steps,
                       "path": [f"{x['from']} -{x['operation']}-> {x['to']}" for x in steps]}
    return flows


def print_dataflow(g, flows):
    print("\nUser journeys:")
    for name, f in flows.items():
        print(f"  {name} ({f['traces']} traces)")
        for i, step in enumerate(f["path"], 1):
            print(f"    {i:>2}. {step}")
    edges = sorted(g.edges(data=True), key=lambda e: (e[0], e[1]))
    print("\nAsync (messaging) edges:")
    for u, v, d in edges:
        if d.get("mode") == "async":
            print(f"  {u} -> {v}  topic={d['topic']}  ops={', '.join(d['operations'])}")
    print("\nSync edges (type, avg latency, operations):")
    for u, v, d in edges:
        if d.get("mode") == "sync":
            lat = f"{d['avg_latency_ms']:.1f}ms" if d["avg_latency_ms"] is not None else "-"
            print(f"  {u:>16} -> {v:<16} {d['type']:<10} {lat:>9}  {', '.join(d['operations'][:4])}")
    untyped = [f"{u}->{v}" for u, v, d in edges if "mode" not in d]
    if untyped:
        print(f"No span data for: {', '.join(untyped)}")


def save_graph(g, stats, flows, path=OUT_JSON):
    data = {"nodes": sorted(g.nodes),
            "edges": [{"parent": u, "child": v, **d} for u, v, d in g.edges(data=True)],
            "stats": stats, "flows": flows}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_graph(path=OUT_JSON):
    """Read dependency_map.json back into (DiGraph, stats)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    g = nx.DiGraph()
    g.add_nodes_from(data["nodes"])
    for e in data["edges"]:
        g.add_edge(e["parent"], e["child"],
                   **{k: v for k, v in e.items() if k not in ("parent", "child")})
    return g, data["stats"]


def load_flows(path=OUT_JSON):
    """Read the user journeys back from dependency_map.json."""
    return json.loads(Path(path).read_text(encoding="utf-8")).get("flows", {})


def draw_graph(g, stats, path=OUT_PNG):
    fan_in = stats["fan_in"]
    # left-to-right layers by call depth, so callers sit left of what they call
    dag = nx.condensation(g)
    depth = {}
    for c in nx.topological_sort(dag):
        for n in dag.nodes[c]["members"]:
            depth[n] = max((depth[p] + 1 for p in g.predecessors(n) if p in depth), default=0)
    for n in g:
        g.nodes[n]["layer"] = depth.get(n, 0)
    pos = nx.multipartite_layout(g, subset_key="layer", scale=1.0)
    fig, ax = plt.subplots(figsize=(16, 11))
    sizes = [700 + 350 * fan_in[n] for n in g]
    nodes = nx.draw_networkx_nodes(g, pos, node_size=sizes, node_color=[fan_in[n] for n in g],
                                   cmap="YlOrRd", edgecolors="#333", ax=ax)
    # colour = kind of data flow, line style = where we learned about it
    groups = defaultdict(list)
    for u, v, d in g.edges(data=True):
        kind = ("async" if d.get("mode") == "async"
                else "data" if d.get("type") in ("database", "cache") else "sync")
        if d["source"] == "config" and kind == "sync":
            kind = "data"  # untraced config edges only point at data stores
        groups[(kind, "dashed" if d["source"] == "config" else "solid")].append((u, v))
    style = dict(connectionstyle="arc3,rad=0.12")
    for (kind, ls), es in groups.items():
        nx.draw_networkx_edges(g, pos, edgelist=es, style=ls, edge_color=EDGE_COLORS[kind], width=1.4,
                               arrowsize=16, node_size=sizes, alpha=0.85, ax=ax, **style)
    topics = {(u, v): d["topic"] for u, v, d in g.edges(data=True) if d.get("topic")}
    nx.draw_networkx_edge_labels(g, pos, topics, font_size=8, font_color=EDGE_COLORS["async"],
                                 bbox=dict(fc="white", ec="none", alpha=0.8), ax=ax, **style)
    nx.draw_networkx_labels(g, pos, font_size=9, font_weight="bold", ax=ax,
                            bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.75))
    fig.colorbar(nodes, ax=ax, shrink=0.5, label="fan-in (services calling it)")
    line = plt.Line2D
    ax.legend(handles=[
        line([], [], color=EDGE_COLORS["sync"], label="sync call (gRPC / HTTP)"),
        line([], [], color=EDGE_COLORS["async"], label="async messaging (Kafka, label = topic)"),
        line([], [], color=EDGE_COLORS["data"], label="database / cache"),
        line([], [], color="#444", label="seen in traces"),
        line([], [], color="#444", ls="--", label="config only (hidden)")], loc="lower left")
    ax.set_title("Service dependency map")
    ax.axis("off")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main():
    try:
        deps = fetch_dependencies()
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"Jaeger unreachable or bad response at {JAEGER_URL}: {e}")
        return
    if not deps:
        print(f"Jaeger returned no dependency data from {JAEGER_URL}; is the demo running with traffic?")
        return
    g = build_graph(deps)
    print(f"{g.number_of_nodes()} services, {g.number_of_edges()} edges")
    for u, v, w in sorted(g.edges(data="call_count"), key=lambda e: -e[2]):
        print(f"{u:>20} -> {v:<20} {w}")

    try:
        added = add_hidden(g, parse_compose())
    except (OSError, yaml.YAMLError) as e:
        print(f"Could not parse compose files in {DEMO_DIR}: {e}")
        added = []
    print(f"\nHidden dependencies added from config ({len(added)}):")
    for u, v in added:
        print(f"{u:>20} -> {v:<20} via {', '.join(g[u][v]['via'])}")

    stats = compute_stats(g)
    print("\nFan-in / fan-out:")
    for n in sorted(g, key=lambda n: (-stats["fan_in"][n], n)):
        print(f"{n:>20}  in={stats['fan_in'][n]:<3} out={stats['fan_out'][n]}")
    print(f"\n{ROOT_SERVICE} path ({len(stats['checkout_path'])}): {', '.join(stats['checkout_path'])}")
    print(f"Top {TOP_N} by fan-in: " + ", ".join(f"{n} ({stats['fan_in'][n]})" for n in stats["top_fan_in"]))

    flows = {}
    try:
        traced = {n for u, v, d in g.edges(data=True) if d["source"] == "trace" for n in (u, v)}
        spans = fetch_spans(traced - EXCLUDED)
        recs = extract_edges(spans)
        annotate_edges(g, recs)
        flows = build_flows(spans, recs)
        print(f"\nData flow: {len(spans)} spans sampled, {len(recs)} edge observations")
        if flows:
            print_dataflow(g, flows)
        else:
            print(f"No '{JOURNEY_PREFIX}*' journeys found in the sampled traces.")
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"\nCould not fetch/parse traces from {TRACES_URL}: {e}; skipping data-flow details.")

    save_graph(g, stats, flows)
    draw_graph(g, stats)
    print(f"\nSaved {OUT_JSON} and {OUT_PNG}")


if __name__ == "__main__":
    main()
