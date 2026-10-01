# superset-x-devin: GitHub issue-to-fix service

A service that uses Devin to investigate bugs in `TARGET_REPO` and open fix PRs for human review. When appropriate, a follow up agent looks for similar defects elsewhere in the repo or deeper root causes. A dashboard tracks progress, sessions, and evidence.

## How it works

The service uses Python to orchestrate Devin sessions, limit concurrency, store progress, and control workflow states.

Each role returns a structured report under an output contract. Deterministic gates check required fields and reported results before advancing: investigation requires reproduction evidence and a root cause; remediation requires a PR URL, a successful reproduction rerun, and passed tests. GitHub CI is checked separately.

1. **Intake:** Collect issues through polling or webhooks and apply configured filters.
2. **Triage:** Investigate, request clarification, or skip.
3. **Investigation:** Reproduce the bug, identify its root cause, and plan verification. Check open PRs for an existing fix.
4. **Remediation:** Write a fix and regression tests, rerun the reproduction, and open a PR. Changes to existing test expectations need maintainer approval.
5. **CI:** GitHub checks must pass by default. Devin fixes failures caused by its change in the same PR. For unrelated failures, it opens a separate CI-fix PR and updates the original branch after that merges.
6. **Related defect analysis:** Once a fix PR exists, a separate session finds related bugs and files follow-up issues.
7. **Completion:** A merged fix PR completes the workflow. `NEEDS_INFO` and `BLOCKED` wait for human input.

Issue and PR comments guide or resume Devin sessions. Trusted maintainer rules become repo-pinned [Devin Knowledge](https://docs.devin.ai/product-guides/knowledge), shown under "Learned from reviews" on the dashboard. Completed analyses feed known defect patterns into future prompts.

Each role has its own session and ACU cap. Default modes: `lite` for triage/dedup, `fusion` for remediation, and the organization's default for investigation/related defect analysis. Modes and caps are [configurable](docs/OPERATIONS.md).

## Prerequisites

1. Choose a GitHub target repo with Issues enabled and a reproducible bug.
2. Connect GitHub in Devin with permission to push branches and open PRs in that repo. Add it to your [Devin environment](https://docs.devin.ai/onboard-devin/environment) with working dependencies and test commands. Sessions follow the target repo's `AGENTS.md` and setup docs.
3. Create a [Devin service user](https://docs.devin.ai/api-reference/authentication) with permission to create, read, message, and terminate sessions. Enable Knowledge writes for review learning. Record its API key and organization id (`org-…`). See [Operations](docs/OPERATIONS.md#devin-credential) for permissions and mode overrides.
4. Create a fine-grained GitHub token scoped to the target repo with Issues and Pull requests read/write, plus Checks and Commit statuses read. This token is separate from Devin's GitHub integration.

## Start the service

You need Docker with Compose v2 (`docker compose`).

1. Run `git clone https://github.com/lilyvc/superset-x-devin.git` and `cd superset-x-devin`.
2. Run `cp .env.example .env`.
3. Set `TARGET_REPO=owner/name`, `GITHUB_TOKEN`, `DEVIN_API_KEY`, and `DEVIN_ORG_ID` in `.env`.
4. Run `docker compose up --build`.
5. Open `http://localhost:8000`. The **Setup problem** banner lists configuration errors.

Open issues are processed at startup. Set `POLL_BACKLOG=false` before starting for new issues only. To test one issue, set `ELIGIBILITY_LABEL=devin-test` and give that issue the same label.

To poll immediately, set an `ADMIN_TOKEN` in `.env`, restart the service, and run:

```bash
set -a; . ./.env; set +a
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/admin/poll-now
```

Polling runs every 5 seconds by default and works locally. For instant issue intake, expose `POST /webhooks/github` and configure `GITHUB_WEBHOOK_SECRET`. See [Operations](docs/OPERATIONS.md) for webhook events, configuration, and running without Docker.

## Try one bug

Sessions are real and consume ACUs. Set `MAX_TOTAL_ACUS` to stop new sessions at the recorded budget; running sessions keep their own caps.

1. Confirm `curl http://localhost:8000/healthz` reports `"ok": true`. This checks startup credentials and repo access; branch pushes, comments, and Knowledge writes are checked when used.
2. File an issue in `TARGET_REPO` with a failing command or UI steps, expected behavior, observed behavior, and the tested version:

   > **Title:** [Component] produces [wrong result] for [input]
   >
   > Version/commit: ...
   > Steps or command: ...
   > Expected: ...
   > Observed output: ...

3. Follow progress and session links in the dashboard. Updates also appear on the issue.
4. Comment to guide Devin. On the fix PR, post a maintainer rule such as "PRs must include reproduction steps" to see **Learned from reviews** after the remediator reports it.
5. Review and merge the fix PR to complete the workflow.

GitHub CI must pass by default. If the target repo has no CI, set `CI_REQUIRED=false` before starting. Otherwise its PR stays in **CI checking** with an unverified result.

## Documentation

| Document | Description |
|---|---|
| [Architecture](docs/ARCHITECTURE.md) | Components, workflow rules, gates, and data |
| [Operations](docs/OPERATIONS.md) | Setup, configuration, credentials, and troubleshooting |

## Project layout

```text
app/
  main.py          FastAPI routes and service setup
  preflight.py     Credential and repository setup checks
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
skills/
tests/
Dockerfile
docker-compose.yml
.env.example
requirements-dev.txt
```
