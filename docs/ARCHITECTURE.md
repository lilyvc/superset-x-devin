# Architecture

## The one-paragraph version

A Python service is the control plane; Devin sessions are the workforce. The service owns all decisions — it finds issues, dispatches role-specific Devin sessions with structured-output contracts, validates what comes back with deterministic Python gates, and tracks every workflow in SQLite. Devin does the work that needs judgment — reading vague reports, reproducing bugs, writing fixes, spotting defect families. Nothing is installed in the target repository.

## Lifecycle

```text
open issue
  → DISCOVERED → TRIAGING → QUEUED → INVESTIGATING → ROOT_CAUSE_FOUND
  → dedup gate → REMEDIATING → VERIFYING → PR_OPENED → CI_CHECKING
  → READY_FOR_REVIEW → COMPLETED (merged)

sideways exits: SKIPPED / NEEDS_INFO / NOT_REPRODUCIBLE / BLOCKED / FAILED / ESCALATED
human replies forward into the active session — or resume/recover it
```

Each `WorkflowEngine.tick` runs five steps in order: discover issues → reconcile sessions → reconcile PRs/CI → forward human replies → dispatch new sessions (within capacity and ACU budget).

## Roles

| Role | Default mode | Job | Gate that accepts its output |
|---|---|---|---|
| Triage | lite | Routes an issue: investigate, clarify, or skip | verdict → `QUEUED` / `NEEDS_INFO` / `SKIPPED` |
| Investigator | org default | Reproduces the bug in a dev environment | `investigation_gate`: `REPRODUCED` + all evidence fields non-empty |
| Dedup | lite | Is an open PR already fixing this? | `DUPLICATE` → `SKIPPED`; `PROCEED` → continue |
| Remediator | fusion | Writes the fix + regression tests, opens PR | `verification_gate`: `PR_OPENED` + every test passed + reproduction re-ran green |
| Retro Devin (`analyst`) | org default | Maps the defect family, files follow-up issues | none — advisory; follow-ups enter as normal issues |

Dispatch order per tick: Remediator → Retro Devin → Investigator → Triage.
`MAX_CONCURRENT_DEVINS` caps running sessions (one parked on a human reply frees its slot); `MAX_TOTAL_ACUS` stops dispatch at the budget; `SESSION_STALL_SECONDS` nudges then escalates stuck sessions. Per-role ACU caps and agent modes are in `docs/OPERATIONS.md`.

Two deterministic gates sit between agents and progress:

- **Dedup gate** — scans open PRs for a closing reference (→ `SKIPPED`), then sends ambiguous candidates to a cheap Dedup session; `PROCEED` clears it.
- **CI gate** — `READY_FOR_REVIEW` only when the PR head commit's GitHub checks exist and none are pending/failing. No checks = `unverified`, not a pass.

## Knowledge loop and provenance

Every completed Retro analysis is stored on its workflow. New Investigator and Retro prompts include the repo's recent defect-family findings (`known_defects_block` in `app/prompts.py`), so recurring patterns are recognized instead of rediscovered. Retro-filed issues carry `DEVIN_DISCOVERED` provenance: they are triaged and fixed like any issue but never get a retro of their own — follow-ups cannot recurse.

Deliberately kept local rather than moved into Devin Knowledge/Playbooks: the prompt + structured-output contract + Python gate is one auditable unit in this repo, and the knowledge that matters here is defect-family findings scoped to this target repo. Devin Knowledge/Playbooks could host long-lived org knowledge later — the contract layer should not move.

## Replies and recovery

Issue comments and PR review comments/reviews are forwarded to the active session; with no active session, the engine resumes the last eligible one, or spawns a recovery session carrying prior findings + the new context. `NOT_REPRODUCIBLE` and `BLOCKED` workflows re-open the same way on a reporter reply.

## Persistence

SQLite (`app/store.py`): `workflows` (state + structured outputs), `sessions` (role, ACUs, fingerprints), `events`, `deliveries` (webhook dedup), `issue_origins` (provenance). Session creation is `idempotent`; on restart the engine reconciles stored sessions before dispatching anything new — the pipeline is resumable at every point.

## Resilience

- **v3-first auth** — `DEVIN_ORG_ID` selects the org-scoped v3 API (the production path); without it the client falls back to legacy v1 and logs a warning. v1 and v3 build their request bodies separately.
- **Retry/backoff** — every Devin call retries 429/5xx/network errors with exponential backoff + jitter, honoring `Retry-After`.
- **Remote reconciliation** — sessions are tagged `repo:*`, `issue:*`, `role:*`. Before creating a session (and once after an ambiguous create failure) the engine asks Devin for a live session with those tags and adopts it — idempotency survives restarts and lost responses, not just local DB dedup.
- **Serialized ticks** — `WorkflowEngine.tick()` holds an internal lock; concurrent ticks can't interleave dispatch.
- **Full lifecycle** — a stalled session is nudged, then escalated *and* terminated remotely, so abandoned sessions stop consuming ACUs.

## Dashboard

`/api/metrics` + `/api/workflows` feed two pages: the list (4 KPI cards, expandable per-issue tracker with session links) and the issue page (same tracker + evidence panel + "Related defect analysis" step + provenance box). `Waiting on a human` = `READY_FOR_REVIEW` + `NEEDS_INFO` + `BLOCKED` + `FAILED` + `ESCALATED`.

## Security

`POST /webhooks/github` requires an HMAC signature (`GITHUB_WEBHOOK_SECRET`), `/admin/*` requires `Authorization: Bearer $ADMIN_TOKEN` — both fail closed when unset. Dashboard pages have no built-in auth; run behind a trusted network or proxy.

## Modules

`app/workflow.py` (engine) · `app/handlers.py` (role output) · `app/gates.py` (decisions) · `app/prompts.py` (prompts + schemas) · `app/devin_client.py` + `app/devin_status.py` (v1/v3 API) · `app/github_client.py` · `app/store.py` · `app/metrics.py` · `app/poller.py` · `app/main.py` (FastAPI) · `app/static/` · `skills/` (role methodology injected into prompts)
