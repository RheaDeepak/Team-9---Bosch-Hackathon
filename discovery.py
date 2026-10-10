"""Discovery: build the service dependency graph from Jaeger + compose config."""
import json
import re
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx
import requests
import yaml

JAEGER_URL = "http://localhost:8080/jaeger/ui/api/dependencies"
DEMO_DIR = Path(r"C:\Users\rhea\opentelemetry-demo")  # read-only
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


def save_graph(g, stats, path=OUT_JSON):
    data = {"nodes": sorted(g.nodes),
            "edges": [{"parent": u, "child": v, **d} for u, v, d in g.edges(data=True)],
            "stats": stats}
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
    for src, style, color in (("trace", "solid", "#1f77b4"), ("config", "dashed", "#7b3fa0")):
        es = [(u, v) for u, v, d in g.edges(data=True) if d["source"] == src]
        nx.draw_networkx_edges(g, pos, edgelist=es, style=style, edge_color=color, width=1.4,
                               arrowsize=16, node_size=sizes, alpha=0.8, ax=ax,
                               connectionstyle="arc3,rad=0.12")
    nx.draw_networkx_labels(g, pos, font_size=9, font_weight="bold", ax=ax,
                            bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.75))
    fig.colorbar(nodes, ax=ax, shrink=0.5, label="fan-in (services calling it)")
    ax.legend(handles=[plt.Line2D([], [], color="#1f77b4", label="trace dependency"),
                       plt.Line2D([], [], color="#7b3fa0", ls="--", label="config (hidden) dependency")],
              loc="lower left")
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

    save_graph(g, stats)
    draw_graph(g, stats)
    print(f"\nSaved {OUT_JSON} and {OUT_PNG}")


if __name__ == "__main__":
    main()
