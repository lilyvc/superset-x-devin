This document uses ASD-STE100 Simplified Technical English.

# GitHub issue-to-fix service

This external service processes GitHub issues in `TARGET_REPO`. Devin investigates issues and prepares pull requests (PRs) with code changes.
The service stores its state outside the target repository. It does not install an application or agent there.

## The problem

Engineers must decide which reports describe real problems.
They must reproduce each problem, find its root cause, and plan a safe fix.

## The result

The service does this work and asks an engineer for help when it cannot continue safely.
The goal is not to create the most autonomous PRs.
The goal is to get a trustworthy fix with the least engineer attention.

## How it works

1. The service finds open issues through polling or GitHub webhooks.
2. Intake filters reject issues that match configured rules. Triage sends each other issue to investigation, clarification, or `SKIPPED`.
3. The Investigator reproduces the issue and returns structured evidence.
4. Python checks the evidence and checks open PRs for an existing fix.
5. The Remediator prepares a fix and reports test results.
6. Python checks the Remediator's evidence and the PR's GitHub CI results.
7. The workflow reaches `READY_FOR_REVIEW` when required checks pass.
8. The service marks a merged PR as `COMPLETED`. It asks a human for help when a workflow is `NEEDS_INFO` or `BLOCKED`.

The service can also mark an issue `NOT_REPRODUCIBLE`, `SKIPPED`, `FAILED`, or `ESCALATED`.

## Quick start

1. Copy `.env.example` to `.env`.
2. Set `TARGET_REPO`, `GITHUB_TOKEN`, `ADMIN_TOKEN`, and `DRY_RUN=true` in `.env`.
3. Run `docker compose up --build`.
4. Export `ADMIN_TOKEN` in your shell.
5. Request one workflow tick from another terminal:

   ```bash
   curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/admin/poll-now
   ```

6. Open the dashboard at `http://localhost:8000`.

Dry-run uses GitHub reads, fake Devin sessions, and no GitHub comment writes.
See [Operations](docs/OPERATIONS.md) for the full setup.

## Documentation

| Document | Description |
|---|---|
| [Architecture](docs/ARCHITECTURE.md) | Components, workflow rules, gates, and data |
| [Operations](docs/OPERATIONS.md) | Setup, configuration, credentials, and troubleshooting |
| [Plan](docs/PLAN.md) | Implemented work, follow-ups, non-goals, and risks |

## Project layout

```text
app/
  main.py          FastAPI routes and service setup
  workflow.py      Workflow tick, discovery, reconciliation, and dispatch
  handlers.py      Role output handling
  gates.py         Python decision gates
  parsing.py       GitHub and Devin parsers
  states.py        Workflow states and role names
  prompts.py       Devin prompts and output schemas
  devin_client.py  Devin v1/v3 API client
  devin_status.py  Devin status normalization
  github_client.py GitHub API client
  store.py         SQLite persistence
  metrics.py       Database-derived metrics
  poller.py        Periodic workflow tick
  static/          Dashboard and issue pages
docs/
  ARCHITECTURE.md
  OPERATIONS.md
  PLAN.md
scripts/simulate.py
skills/
tests/
Dockerfile
docker-compose.yml
.env.example
```
