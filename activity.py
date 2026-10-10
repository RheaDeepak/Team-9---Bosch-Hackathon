"""Structured events from the agents (mode "planning" or "analysis").

Kept in memory so the UI can poll them once a second, and written to sessions/<run_id>.jsonl (full detail)
so a session can be replayed later.
"""
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

SESSIONS_DIR = Path("sessions")
MAX_EVENTS = 3000
RUN_ID = re.compile(r"[0-9a-f]{8}")   # also what keeps a run id from being a path

_lock = threading.Lock()
_events = []
_seq = 0


def valid_run_id(run_id):
    return isinstance(run_id, str) and bool(RUN_ID.fullmatch(run_id))


MODES = ("planning", "analysis")


def _mode(ev):
    return ev.get("mode") or "planning"   # events recorded before modes existed came from the planning agent


class Recorder:
    """One agent run: numbers its steps and stores every event, tagged with the agent's mode."""

    def __init__(self, run_id, sessions_dir=None, mode="planning"):
        self.run_id, self.dir, self.step, self.mode = run_id, Path(sessions_dir or SESSIONS_DIR), 0, mode

    def emit(self, type_, summary, tool=None, args=None, detail=None, **extra):
        global _seq
        self.step += 1
        with _lock:
            _seq += 1
            ev = {"seq": _seq, "run_id": self.run_id, "mode": self.mode, "step": self.step,
                  "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "type": type_, "tool": tool,
                  "args": args, "summary": " ".join(str(summary).split())[:240],
                  "detail": None if detail is None else str(detail), **extra}
            _events.append(ev)
            del _events[:-MAX_EVENTS]
        try:
            self.dir.mkdir(exist_ok=True)
            with (self.dir / f"{self.run_id}.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, default=str) + "\n")
        except OSError:
            pass  # recording must never break planning
        return ev


def latest_run_id(mode=None):
    """The newest run in memory, optionally of one mode."""
    with _lock:
        return next((e["run_id"] for e in reversed(_events) if mode is None or _mode(e) == mode), None)


def events(run_id=None, after=0, sessions_dir=None, mode=None):
    """(run_id, events after seq `after`, last seq) for a run; the newest run (of `mode`, if given) when run_id
    is empty. A run no longer in memory (earlier server session) is read from its file."""
    run_id = run_id or latest_run_id(mode)
    if not run_id or not valid_run_id(run_id):
        return run_id, [], after
    with _lock:
        mine = [e for e in _events if e["run_id"] == run_id]
    if not mine:
        mine = load_session(run_id, sessions_dir)
    new = [e for e in mine if e["seq"] > after]
    return run_id, new, (new[-1]["seq"] if new else after)


def load_session(run_id, sessions_dir=None):
    if not valid_run_id(run_id):
        return []
    out = []
    try:
        for line in (Path(sessions_dir or SESSIONS_DIR) / f"{run_id}.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return []
    return out


def list_sessions(sessions_dir=None, limit=30, mode=None):
    """Saved sessions, newest first: [{run_id, mode, started, steps, outcome}], optionally of one mode."""
    rows = []
    for p in Path(sessions_dir or SESSIONS_DIR).glob("*.jsonl"):
        if not valid_run_id(p.stem):
            continue
        evs = load_session(p.stem, sessions_dir)
        if not evs:
            continue
        if mode is not None and _mode(evs[0]) != mode:
            continue
        decision = next((e for e in reversed(evs) if e["type"] == "decision"), None)
        rows.append({"run_id": p.stem, "mode": _mode(evs[0]), "started": evs[0]["ts"], "steps": len(evs),
                     "outcome": (decision or {}).get("summary", "")[:80]})
    return sorted(rows, key=lambda r: r["started"], reverse=True)[:limit]
