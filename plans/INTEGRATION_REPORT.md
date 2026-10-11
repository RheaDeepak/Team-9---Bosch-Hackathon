# Chaos Copilot integration report

## Canonical application

The supported application is `app.py` with `static/index.html`. Start both the
API and UI with `python app.py`.

```text
UI -> Planning Graph (read-only) -> immutable candidate ID
   -> deterministic policy -> human approval -> fixed executor
   -> guard and rollback -> experiment evidence
   -> Analysis Graph (read-only) -> validated report
```

The model never supplies executable fault parameters. The planning graph starts
from deterministic candidates from `ranker.py`. After Gemini selects a
`candidate_id`, target, fault, magnitude, duration, and traffic parameters are
copied from that candidate. Policy validation and approval remain outside the
graph. A capacity goal can select only a ready `traffic_spike` candidate.

## Agent package

`agents/planning.py` contains `prepare_candidates`, `investigate`,
`execute_tools`, `finalize`, `validate_decision`, and `fallback` nodes. Read-only
calls are capped at eight. Failures retain the `ranker_fallback` discriminator.

`agents/analysis.py` contains `prepare_weaknesses`, `investigate`,
`execute_tools`, `finalize`, `validate_findings`, `repair_once`,
`persist_report`, and `fallback`. Read-only calls are capped at ten. Evidence
IDs, numerical claims, severity changes, and source locations are validated.

`agents/verdict.py` is a typed single-call Gemini chain, not an agent graph.
Shared Pydantic models, the Gemini factory, safe tools, prompts, checkpointing,
and activity persistence live under `agents/`. The legacy `rank_agent.py`,
`analysis_agent.py`, and `planner.py` modules are compatibility facades.

## Persistence and safety

`state/agent_runs.sqlite3` contains LangGraph checkpoints plus queryable
`agent_runs` and append-only `agent_events` tables. The UI keeps the existing
event/session endpoints. Existing JSONL sessions remain readable; new sessions
are written only to SQLite. Checkpoint decoding disables pickle fallback and
allows only LangGraph's known safe message/value types.

Agent tools are read-only and cannot invoke injection, Docker, shell, approval,
rollback, or experiment-control functions. Source search rejects traversal and
escaping symlinks and bounds regexes, files, matches, and returned text. Compose
inspection redacts secret-looking values.

Deterministic ranking, policy, approval, execution, concurrency gates, guard,
rollback, scoring, evidence normalization, and report validation remain
authoritative.

## Verification

Run the offline suite with:

```powershell
python -m unittest discover -s tests
```

Before release, manually verify one planning run, one explicitly approved local
sandbox experiment, rollback, one analysis run, and session replay after a
server restart.
