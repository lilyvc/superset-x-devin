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
Each role runs as its own Devin session with a per-role agent mode and ACU cap (cheap modes for triage/dedup, `fusion` for remediation — see [Operations](docs/OPERATIONS.md)). Completed analyses feed back into new prompts as known defect families, so the system accumulates knowledge across issues.

## Quick start

You need two credentials:

- `GITHUB_TOKEN` — a GitHub token for the target repo (issues and pull requests: read and write; checks/commit statuses: read).
- `DEVIN_API_KEY` — a Devin API key (org secret or personal access token; for a PAT also set `DEVIN_ORG_ID`).

Then:

1. Copy `.env.example` to `.env` and set `TARGET_REPO`, `GITHUB_TOKEN`, `DEVIN_API_KEY`, and `ADMIN_TOKEN`.
2. Run `docker compose up --build`.
3. Open the dashboard at `http://localhost:8000`. The service polls `TARGET_REPO`, so every open issue gets triaged automatically — file a new issue to watch the pipeline live.

To force an immediate poll instead of waiting for the interval:

```bash
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/admin/poll-now
```

Optional: point a GitHub webhook (`issues`, `issue_comment`, `pull_request_review`, `pull_request_review_comment` events) at `POST /webhooks/github` and set `GITHUB_WEBHOOK_SECRET` for instant intake instead of polling.

See [Operations](docs/OPERATIONS.md) for the full configuration reference.

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
skills/
tests/
Dockerfile
docker-compose.yml
.env.example
```
