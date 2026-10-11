"""Queryable SQLite run/event store used by the UI and graph callbacks."""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

STATE_DB = Path("state/agent_runs.sqlite3")
MAX_EVENTS = 3000
MAX_STREAM = 300
_lock = threading.Lock()
_events: list[dict] = []
_seq = 0


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _safe(value, key=""):
    if any(part in key.lower() for part in ("secret", "password", "token", "api_key", "credential")):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k)[:80]: _safe(v, str(k)) for k, v in list(value.items())[:30]}
    if isinstance(value, list):
        return [_safe(item) for item in value[:30]]
    if isinstance(value, str):
        return value[:500]
    return value


def _path(sessions_dir=None):
    return Path(sessions_dir) / "agent_runs.sqlite3" if sessions_dir is not None else STATE_DB


def connect(sessions_dir=None):
    path = _path(sessions_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, factory=_ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS agent_runs (
            run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, prompt_version TEXT, status TEXT NOT NULL,
            model TEXT, source TEXT, note TEXT, started_at TEXT NOT NULL, completed_at TEXT,
            result_json TEXT, error TEXT
        );
        CREATE TABLE IF NOT EXISTS agent_events (
            run_id TEXT NOT NULL, step INTEGER NOT NULL, sequence INTEGER NOT NULL, mode TEXT NOT NULL,
            ts TEXT NOT NULL, type TEXT NOT NULL, node TEXT, status TEXT, tool TEXT, args_json TEXT,
            summary TEXT NOT NULL, detail TEXT, model TEXT, usage_json TEXT, extra_json TEXT,
            PRIMARY KEY (run_id, step)
        );
        CREATE INDEX IF NOT EXISTS idx_agent_events_sequence ON agent_events(sequence);
        CREATE INDEX IF NOT EXISTS idx_agent_runs_mode_started ON agent_runs(mode, started_at DESC);
    """)
    return conn


class Recorder:
    def __init__(self, run_id, sessions_dir=None, mode="planning", prompt_version=None):
        self.run_id, self.sessions_dir, self.mode, self.step = run_id, sessions_dir, mode, 0
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with connect(sessions_dir) as conn:
            conn.execute("INSERT OR IGNORE INTO agent_runs(run_id,mode,prompt_version,status,started_at) VALUES(?,?,?,?,?)",
                         (run_id, mode, prompt_version, "running", now))
            latest_sequence = conn.execute("SELECT COALESCE(MAX(sequence), 0) FROM agent_events").fetchone()[0]
        global _seq
        with _lock:
            _seq = max(_seq, latest_sequence)

    def emit(self, type_, summary, tool=None, args=None, detail=None, node=None, status=None, **extra):
        global _seq
        self.step += 1
        args = _safe(args)
        with _lock:
            _seq += 1
            ev = {"seq": _seq, "run_id": self.run_id, "mode": self.mode, "step": self.step,
                  "sequence": _seq,
                  "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "type": type_,
                  "node": node, "status": status, "tool": tool, "args": args,
                  "summary": " ".join(str(summary).split())[:240],
                  "detail": None if detail is None else str(detail), **extra}
            _events.append(ev)
            del _events[:-MAX_EVENTS]
        remainder = {k: v for k, v in extra.items() if k not in {"model", "usage"}}
        try:
            with connect(self.sessions_dir) as conn:
                conn.execute("INSERT OR REPLACE INTO agent_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (self.run_id, self.step, ev["seq"], self.mode, ev["ts"], type_, node, status, tool,
                              json.dumps(args, default=str) if args is not None else None, ev["summary"], ev["detail"],
                              extra.get("model"), json.dumps(extra.get("usage"), default=str) if extra.get("usage") else None,
                              json.dumps(remainder, default=str) if remainder else None))
        except sqlite3.Error:
            pass
        return ev

    def finish(self, status="completed", *, model=None, source=None, note=None, result=None, error=None):
        with connect(self.sessions_dir) as conn:
            conn.execute("UPDATE agent_runs SET status=?,model=?,source=?,note=?,completed_at=?,result_json=?,error=? WHERE run_id=?",
                         (status, model, source, note, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          json.dumps(result, default=str) if result is not None else None, error, self.run_id))


def _event(row):
    ev = {"seq": row["sequence"], "sequence": row["sequence"], "run_id": row["run_id"],
          "mode": row["mode"], "step": row["step"],
          "ts": row["ts"], "type": row["type"], "node": row["node"], "status": row["status"],
          "tool": row["tool"], "args": json.loads(row["args_json"]) if row["args_json"] else None,
          "summary": row["summary"], "detail": row["detail"]}
    if row["model"]:
        ev["model"] = row["model"]
    if row["usage_json"]:
        ev["usage"] = json.loads(row["usage_json"])
    if row["extra_json"]:
        ev.update(json.loads(row["extra_json"]))
    return ev


def load(run_id, sessions_dir=None):
    try:
        with connect(sessions_dir) as conn:
            return [_event(row) for row in conn.execute(
                "SELECT * FROM agent_events WHERE run_id=? ORDER BY step", (run_id,)).fetchall()]
    except sqlite3.Error:
        return []


def latest(mode=None, sessions_dir=None):
    if sessions_dir is None:
        with _lock:
            live = next((e["run_id"] for e in reversed(_events) if mode is None or e["mode"] == mode), None)
        if live:
            return live
    with connect(sessions_dir) as conn:
        sql = ("SELECT r.run_id FROM agent_runs r LEFT JOIN "
               "(SELECT run_id, MAX(sequence) AS last_sequence FROM agent_events GROUP BY run_id) e "
               "ON e.run_id=r.run_id")
        args = ()
        if mode:
            sql, args = sql + " WHERE r.mode=?", (mode,)
        row = conn.execute(sql + " ORDER BY COALESCE(e.last_sequence, 0) DESC, r.started_at DESC LIMIT 1", args).fetchone()
    return row["run_id"] if row else None


def all_live(after=0):
    with _lock:
        return [e for e in _events if e["seq"] > after][-MAX_STREAM:]


def runs(sessions_dir=None, mode=None, limit=30):
    with connect(sessions_dir) as conn:
        sql, args = "SELECT * FROM agent_runs", []
        if mode:
            sql, args = sql + " WHERE mode=?", [mode]
        rows = conn.execute(sql + " ORDER BY started_at DESC LIMIT ?", (*args, limit)).fetchall()
        out = []
        for row in rows:
            evs = load(row["run_id"], sessions_dir)
            decision = next((e for e in reversed(evs) if e["type"] == "decision"), None)
            out.append({"run_id": row["run_id"], "mode": row["mode"], "started": row["started_at"],
                        "steps": len(evs), "outcome": ((decision or {}).get("summary") or row["note"] or "")[:80]})
        return out
