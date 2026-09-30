# superset-x-devin: GitHub issue-to-fix service

An external service that autonomously fixes issues in `TARGET_REPO` using Devin sessions. Devin triages, reproduces, and remediates issues, then opens PRs for human review. A dashboard tracks every issue's progress, cost, and evidence.
The service stores its state outside the target repository. It does not install an application or agent there.

## How it works

1. The service finds open issues through polling or GitHub webhooks.
2. Intake filters reject issues that match configured rules. Triage sends each other issue to investigation, clarification, or `SKIPPED`.
3. The Investigator reproduces the issue and returns structured evidence.
4. Python checks the evidence and checks open PRs for an existing fix.
5. The Remediator prepares a fix and reports test results.
6. Python checks the Remediator's evidence and the PR's GitHub CI results.
7. The workflow reaches `READY_FOR_REVIEW` when required checks pass.
8. Once a fix PR exists, the Retro Devin searches for related defect patterns and files follow-up issues.
9. The service marks a merged PR as `COMPLETED`. It asks a human for help when a workflow is `NEEDS_INFO` or `BLOCKED`.
10. Comments on tracked issues and PRs reach the active Devin session — or resume it when it has finished.

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
