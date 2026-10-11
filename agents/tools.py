"""Read-only, bounded tools shared by the planning and analysis graphs."""
import json
import os
import re
import statistics
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

import yaml
from dotenv import load_dotenv
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from discovery import CLIENT, COMPOSE_FILES, ENV_FILE, SERVER, _expand, _load_env, fetch_spans
from policy import blast_radius

SRC_ENV_VAR = "OTEL_DEMO_SRC"
SRC_FALLBACK_VAR = "TARGET_REPOSITORY_PATH"
MAX_RESULT_CHARS = 3000
MAX_MATCHES = 20
MAX_LINE_CHARS = 160
MAX_PATTERN_CHARS = 200
MAX_FILE_BYTES = 1_000_000
SKIP_DIRS = {".git", "node_modules", "genproto", "protos", "vendor", "target", "bin", "obj", "dist", ".next",
             "__pycache__", "test", "tests", "cypress"}
SOURCE_EXT = {".go", ".py", ".js", ".mjs", ".ts", ".tsx", ".cs", ".rs", ".java", ".kt", ".rb", ".php", ".ex",
              ".exs", ".erl", ".cpp", ".cc", ".h", ".hpp", ".lua", ".yaml", ".yml", ".json", ".toml",
              ".properties", ".ini", ".conf", ".env"}
TEST_FILE = re.compile(r"(_test\.go|\.(test|spec)\.[jt]sx?|Tests?\.cs)$|^test_.*\.py$")
RESILIENCE_KEYS = re.compile(r"timeout|retr|deadline|backoff|circuit|limit|pool|keepalive", re.I)
SECRET_KEYS = re.compile(r"secret|password|passwd|token|api.?key|credential|private|certificate", re.I)
RETRY_TRACES = 40
RETRY_LOOKBACK = timedelta(minutes=30)
TIMEOUT_CLUSTER = 0.10


class ToolError(Exception):
    pass


def _edge_mode(edge):
    return edge.get("mode") or ("async" if edge.get("type") == "async-messaging" else "sync")


def _src_root():
    load_dotenv()
    raw = os.getenv(SRC_ENV_VAR, "").strip() or os.getenv(SRC_FALLBACK_VAR, "").strip()
    if not raw:
        raise ToolError(f"{SRC_ENV_VAR} is not set")
    root = Path(raw).resolve()
    if not root.is_dir():
        raise ToolError(f"{SRC_ENV_VAR}={raw} is not a directory")
    return root


def _service_dir(root, service):
    if not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", service) or ".." in service:
        raise ToolError(f"refused: {service!r} is not a service name")
    service_path = (root / "src" / service).resolve()
    if not service_path.is_relative_to(root):
        raise ToolError(f"refused: {service!r} resolves outside {SRC_ENV_VAR}")
    if not service_path.is_dir():
        raise ToolError(f"no source directory for {service!r}")
    return service_path


def search_source(service, pattern):
    root = _src_root()
    base = _service_dir(root, service)
    if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_PATTERN_CHARS:
        raise ToolError(f"pattern must be 1-{MAX_PATTERN_CHARS} characters")
    if re.search(r"\([^)]*[+*][^)]*\)[+*{]", pattern):
        raise ToolError("refused: nested quantifiers (catastrophic backtracking)")
    try:
        regex = re.compile(pattern, re.I)
    except re.error as exc:
        raise ToolError(f"bad regex: {exc}") from exc
    matches, files, more = [], 0, Counter()
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d)))
        for name in sorted(filenames):
            file_path = Path(dirpath, name)
            if (file_path.suffix.lower() not in SOURCE_EXT or TEST_FILE.search(name) or file_path.is_symlink()
                    or not file_path.resolve().is_relative_to(root)):
                continue
            try:
                if file_path.stat().st_size > MAX_FILE_BYTES:
                    continue
                lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            files += 1
            relative = file_path.relative_to(root).as_posix()
            for line_number, line in enumerate(lines, 1):
                if regex.search(line[:500]):
                    if len(matches) < MAX_MATCHES:
                        matches.append({"at": f"{relative}:{line_number}", "line": line.strip()[:MAX_LINE_CHARS]})
                    else:
                        more[relative] += 1
    result = {"service": service, "pattern": pattern, "files_searched": files,
              "matches": matches, "truncated": bool(more)}
    if more:
        result["more_matches_in"] = dict(more.most_common(10))
    return result


def get_service_env(service):
    """Return only resilience settings; never expose broad environment values to the model."""
    root = _src_root()
    if not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", service):
        raise ToolError(f"refused: {service!r} is not a service name")
    env = _load_env(root / ENV_FILE) if (root / ENV_FILE).is_file() else {}
    for filename in COMPOSE_FILES:
        compose_path = root / filename
        if not compose_path.is_file():
            continue
        svc = (yaml.safe_load(compose_path.read_text(encoding="utf-8")).get("services") or {}).get(service)
        if svc is None:
            continue
        raw = svc.get("environment") or []
        pairs = raw.items() if isinstance(raw, dict) else (x.split("=", 1) if "=" in x else (x, None) for x in raw)
        values = {key: _expand(str(env.get(key, "") if value is None else value), env) for key, value in pairs}
        resilience = {key: ("<redacted>" if SECRET_KEYS.search(key) else value[:120])
                      for key, value in values.items() if RESILIENCE_KEYS.search(key)}
        return {"service": service, "file": filename, "resilience_settings": resilience,
                "environment_keys": sorted(key for key in values if not key.startswith("OTEL_")),
                "resource_limits": ((svc.get("deploy") or {}).get("resources") or {}).get("limits"),
                "restart": svc.get("restart"), "healthcheck": "healthcheck" in svc,
                "depends_on": sorted(svc.get("depends_on") or [])}
    raise ToolError(f"{service!r} not found in {', '.join(COMPOSE_FILES)}")


def retry_evidence(caller, callee, spans=None):
    if spans is None:
        spans = fetch_spans(limits={caller: RETRY_TRACES}, lookback=RETRY_LOOKBACK)
    children = defaultdict(list)
    for (trace_id, _), span in spans.items():
        children[(trace_id, span["parent"])].append(span)
    calls = []
    for (trace_id, span_id), span in spans.items():
        if span["svc"] != caller or span["kind"] != CLIENT:
            continue
        attrs = span["attrs"]
        peer = str(attrs.get("server.address") or attrs.get("peer.service") or attrs.get("net.peer.name") or "").split(":")[0]
        if peer == callee or any(child["svc"] == callee and child["kind"] == SERVER for child in children[(trace_id, span_id)]):
            calls.append(span)
    groups = defaultdict(list)
    for span in calls:
        groups[(span["trace"], span["parent"], span["attrs"].get("rpc.method") or span["name"])].append(span)
    repeated = {key: value for key, value in groups.items() if len(value) > 1}
    after_error = sum(any(x["status"] == 2 for x in sorted(value, key=lambda x: x["start"])[:-1]) for value in repeated.values())
    durations = sorted(span["dur"] for span in calls)
    errors = [span["dur"] for span in calls if span["status"] == 2]
    timeout = None
    if len(errors) >= 3:
        median = statistics.median(errors)
        if median > 0 and sum(abs(duration - median) <= TIMEOUT_CLUSTER * median for duration in errors) >= 0.6 * len(errors):
            timeout = round(median)
    return {"caller": caller, "callee": callee, "lookback_min": int(RETRY_LOOKBACK.total_seconds() // 60),
            "traces_checked": len({span["trace"] for span in spans.values() if span["svc"] == caller}),
            "client_calls": len(calls), "errored_calls": len(errors),
            "traces_with_repeated_calls": len({key[0] for key in repeated}),
            "max_repeats": max((len(value) for value in groups.values()), default=0),
            "repeats_after_error": after_error,
            "p50_ms": round(statistics.median(durations), 1) if durations else None,
            "max_ms": round(durations[-1], 1) if durations else None, "client_timeout_ms": timeout,
            "note": ("no errored calls in the window, so retries could not show up here; check the source"
                     if not errors else "repeats can be normal fan-out; a retry usually follows an errored call")}


class PlanningToolbox:
    def __init__(self, graph, baseline, history, ranking):
        self.graph, self.baseline, self.history = graph, baseline or {}, history
        self.by_id = {candidate["id"]: candidate for candidate in ranking}

    def _service(self, service):
        if service not in self.graph:
            raise ToolError(f"unknown service {service!r}")
        return service

    def get_candidate(self, id):
        candidate = self.by_id.get(id)
        if candidate is None:
            raise ToolError(f"unknown candidate {id!r}")
        return {key: candidate[key] for key in ("id", "rank", "risk", "info", "priority", "why")} | {
            "features": {key: value["raw"] for key, value in candidate["features"].items()}}

    def get_edges(self, service):
        service = self._service(service)
        edge = lambda data: {"mode": _edge_mode(data), "type": data.get("type"),
                             "operations": (data.get("operations") or [])[:5],
                             "avg_latency_ms": data.get("avg_latency_ms"), "calls": data.get("call_count")}
        return {"service": service,
                "callers": {source: edge(data) for source, _, data in self.graph.in_edges(service, data=True)},
                "callees": {target: edge(data) for _, target, data in self.graph.out_edges(service, data=True)},
                "blast_radius": blast_radius(self.graph, service)}

    def get_baseline(self, service):
        service = self._service(service)
        metrics = self.baseline.get("services", {}).get(service)
        if not metrics:
            return {"service": service, "note": "no span metrics"}
        rounded = lambda value: None if value is None else round(value, 2)
        return {"service": service, "requests": self.baseline.get("requests", {}).get(service),
                **{key: value and {field: rounded(value[field]) for field in ("mean", "std", "max")}
                   for key, value in metrics.items()},
                "abort_thresholds": self.baseline.get("thresholds", {}).get("services", {}).get(service)}

    def get_history(self, service):
        service = self._service(service)
        runs = [run for run in self.history if run["target"] == service]
        return {"service": service, "runs": len(runs), "latest": runs[-10:]}

    def search_source(self, service, pattern):
        self._service(service)
        return search_source(service, pattern)

    def get_service_env(self, service):
        self._service(service)
        return get_service_env(service)

    def retry_evidence(self, caller, callee):
        return retry_evidence(self._service(caller), self._service(callee))

    def call(self, name, args):
        function = getattr(self, name, None)
        if name not in READ_ONLY_TOOL_NAMES or function is None:
            return {"error": f"unknown tool {name!r}"}
        try:
            return function(**(args or {}))
        except ToolError as exc:
            return {"error": str(exc)}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"[:300]}


class IdInput(BaseModel):
    id: str


class ServiceInput(BaseModel):
    service: str


class SearchInput(ServiceInput):
    pattern: str = Field(min_length=1, max_length=MAX_PATTERN_CHARS)


class EdgeInput(BaseModel):
    caller: str
    callee: str


READ_ONLY_TOOL_NAMES = frozenset({"get_candidate", "get_edges", "get_baseline", "get_history",
                                  "search_source", "get_service_env", "retry_evidence"})


def planning_tools(toolbox):
    specs = [
        ("get_candidate", "Get deterministic ranker features and scores for one candidate.", IdInput),
        ("get_edges", "Get callers, callees, operations, modes, and blast radius for a service.", ServiceInput),
        ("get_baseline", "Get baseline request, error-rate, latency, and abort-threshold data.", ServiceInput),
        ("get_history", "Get prior experiments for a service.", ServiceInput),
        ("search_source", "Regex-search one service's source for resilience mechanisms or call sites.", SearchInput),
        ("get_service_env", "Get non-secret resilience and resource settings from Compose.", ServiceInput),
        ("retry_evidence", "Inspect recent traces for repeated calls and fixed client timeouts.", EdgeInput),
    ]
    return [StructuredTool.from_function(name=name, description=description, args_schema=schema,
                                         func=lambda _name=name, **kwargs: toolbox.call(_name, kwargs))
            for name, description, schema in specs]


def compact(result):
    text = json.dumps(result, default=str)
    return result if len(text) <= MAX_RESULT_CHARS else {"truncated": True, "text": text[:MAX_RESULT_CHARS]}
