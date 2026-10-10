# Chaos Copilot frontend

A dependency-free, single-screen operator console for the Chaos Copilot MVP.
It implements the six UI zones in `CHAOS_COPILOT_PRD.md`: readiness, topology,
proposal, policy, live run, and findings.

## Run locally

```powershell
cd frontend
python -m http.server 4173
```

Then open <http://localhost:4173>. The UI first attempts the PRD's relative
`/api/*` endpoints. If the backend is unavailable, it switches to clearly
labelled demo data so the hackathon flow remains presentable.

## Backend integration

Serve this directory from the FastAPI origin (recommended), or reverse-proxy
`/api` to the backend. The adapter accepts the endpoint contract from PRD
section 14 and tolerates common response aliases while the backend models are
being finalized.

Mutating calls include an `Idempotency-Key` header. The UI never accepts or
sends shell commands, requires an explicit checkbox before approval, and keeps
the emergency stop available throughout active experiment phases.

## Verify

```powershell
npm test
npm run check
```

There are no runtime or development dependencies to install.
