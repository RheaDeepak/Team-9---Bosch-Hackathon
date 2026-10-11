"""Grafana helpers: where the Spanmetrics (RED) panels live, and experiment annotations.

Grafana is reached through the demo's Envoy front door. Anonymous access is enabled in the demo's
grafana.ini, so no credentials are needed; embedding needs grafana-embed.yaml (see README).
"""
import os
import time

import requests

GRAFANA_URL = os.getenv("GRAFANA_URL", "http://localhost:8080/grafana").rstrip("/")
# Looked up through /api/search and /api/dashboards/uid/<uid>: these three panels live in the Demo folder's
# "Demo Dashboard" (not in "Spanmetrics Demo Dashboard"). Panel ids are per dashboard.
DASHBOARD_UID = "W2gX2zHVk"
DASHBOARD_SLUG = "demo-dashboard"
PANELS = {"requests": (12, "Requests Rate by Span Name"),
          "errors": (10, "Error Rate by Span Name"),
          "duration": (2, "Average Duration by Span Name")}
WINDOW_BEFORE_S = 120   # live and result views start this long before the fault was injected
WINDOW_AFTER_S = 45     # result view ends this long after recovery finished
TAGS = ["chaos-copilot"]
TIMEOUT_S = 2           # Grafana must never hold up an experiment


def annotate(text, tags=(), when=None):
    """Create a dashboard annotation. Returns (ok, detail) and never raises."""
    try:
        r = requests.post(f"{GRAFANA_URL}/api/annotations", timeout=TIMEOUT_S, json={
            "dashboardUID": DASHBOARD_UID, "time": int((when or time.time()) * 1000),
            "text": text, "tags": [*TAGS, *tags]})
        return r.ok, f"annotation {r.json().get('id')}" if r.ok else f"HTTP {r.status_code}: {r.text[:120]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:150]


def window_ms(injected_at, ended_at):
    """[from, to] in epoch ms covering the whole experiment, with some normal behaviour before it."""
    return [int((injected_at - WINDOW_BEFORE_S) * 1000), int((ended_at + WINDOW_AFTER_S) * 1000)]


def _panels(ps):
    for p in ps:
        yield p
        yield from _panels(p.get("panels", []))


def check_panels():
    """Look the panels up again; confirm the constants above still match. Returns (ok, detail)."""
    try:
        d = requests.get(f"{GRAFANA_URL}/api/dashboards/uid/{DASHBOARD_UID}", timeout=5).json()["dashboard"]
        titles = {p["id"]: p.get("title") for p in _panels(d.get("panels", []))}
        wrong = [f"{pid} should be '{t}'" for pid, t in PANELS.values() if titles.get(pid) != t]
        return not wrong, "; ".join(wrong) or f"'{d['title']}' panels {', '.join(str(i) for i, _ in PANELS.values())} found"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:100]}"


def embedding_allowed():
    """True unless Grafana still sends X-Frame-Options (i.e. the grafana-embed.yaml override is missing)."""
    try:
        r = requests.get(f"{GRAFANA_URL}/login", timeout=5)
        return "x-frame-options" not in r.headers or r.headers["x-frame-options"].lower() == "allow", \
            "iframes allowed" if "x-frame-options" not in r.headers else "Grafana sends X-Frame-Options; start it with grafana-embed.yaml"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:100]}"
