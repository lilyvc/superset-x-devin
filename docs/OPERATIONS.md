# Operations

## Requirements

Use a persistent host that can run Docker Compose or Python 3.
Give the service a GitHub token, a Devin credential, and a stable SQLite path.

## Docker Compose

1. Copy `.env.example` to `.env`.
2. Set `TARGET_REPO`, `GITHUB_TOKEN`, and `ADMIN_TOKEN` in `.env`.
3. Choose a Devin credential for live operation.
4. Run `docker compose up --build`.
5. Keep the host running while polling is enabled.

Compose starts the `orchestrator` service on port `8000`.
It stores the database at `/data/orchestrator.db` in the persistent `orchestrator-data` volume.
Compose sets `DB_PATH` to this file inside the container.

## Local virtual environment

1. Create a virtual environment with `python3 -m venv .venv`.
2. Install requirements with `.venv/bin/pip install -r requirements.txt`.
3. Copy `.env.example` to `.env`.
4. Set the required values in `.env`.
5. Load the environment file with `set -a; . ./.env; set +a`.
6. Start the service with `.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000`.

The `.env` file does not load by itself in a local Python run.
The shell command in step 5 exports its values to the service.

## Webhook intake and polling

Use webhook intake when GitHub can reach the service over HTTPS.
Set `GITHUB_WEBHOOK_SECRET` and configure the GitHub webhook to send issue, issue comment, pull request review, and pull request review comment events to `/webhooks/github`.
The service checks `X-Hub-Signature-256` and deduplicates deliveries by `X-GitHub-Delivery`.
It accepts opened or reopened issue events, created issue comments, created pull request review comments, submitted pull request reviews, and ping events.

Use polling when the service does not have a public URL.
Polling is on by default (`ENABLE_POLLING=true`) and runs every `GITHUB_POLL_INTERVAL_SECONDS` (5s).
The service runs one tick at startup and repeats a tick at the configured interval.
Set `POLL_BACKLOG=false` to baseline existing issues at startup instead of processing them.
Keep the host and service running for polling to continue.

## Credentials

### GitHub token

Use a fine-grained personal access token with these repository permissions:

- Issues: read and write.
- Pull requests: read.
- Checks: read.
- Commit statuses: read.

The service reads issues, comments, PRs, PR files, PR review comments and reviews, check runs, and commit statuses.
It writes issue comments. Devin creates the PR work in the target repository.
A classic token for a private repository needs the broad `repo` scope.
Protect the token as a production credential.

### Devin credential

Production runs use the v3 API: set `DEVIN_API_KEY` to a **service-user key** (recommended for a running service — Devin guidance reserves PATs for scripts acting as a human user) and `DEVIN_ORG_ID` to the Devin organization that owns the sessions. A PAT also works on v3 but sessions then run as that user.
Leaving `DEVIN_ORG_ID` unset falls back to the legacy v1 service-user API (a startup warning is logged).
Set `DEVIN_API_BASE_URL` only when the API base URL differs from its default.

## Security configuration

`POST /admin/poll-now` returns `503` when `ADMIN_TOKEN` is unset and `401` when its value is invalid.
The webhook route rejects requests when `GITHUB_WEBHOOK_SECRET` is unset or invalid.
Dashboard pages and GET APIs do not require authentication.

1. Set a strong `ADMIN_TOKEN` before you expose the service.
2. Set `GITHUB_WEBHOOK_SECRET` before you use webhooks.
3. Place dashboard pages and GET APIs behind a trusted network or an authenticated proxy.
4. Set `MAX_TOTAL_ACUS` to limit ACU use.
5. Keep `.env`, tokens, databases, and session output out of version control.

## Environment variables

Defaults below come from `app/config.py`. Compose overrides `DB_PATH` inside the container.

| Variable | Default | Purpose |
|---|---:|---|
| `TARGET_REPO` | `lilyvc/superset` | Repository in `owner/name` form |
| `GITHUB_TOKEN` | empty | GitHub API token |
| `GITHUB_WEBHOOK_SECRET` | empty | HMAC secret for webhook verification |
| `GITHUB_API_URL` | `https://api.github.com` | GitHub API base URL; set for GHES |
| `ADMIN_TOKEN` | empty | Bearer token for `POST /admin/poll-now` |
| `DEVIN_API_KEY` | empty | Devin service-user key (production) or PAT (human scripts) |
| `DEVIN_API_BASE_URL` | `https://api.devin.ai` | Devin API base URL |
| `DEVIN_ORG_ID` | empty | Required for production — selects the org-scoped v3 API; empty = legacy v1 |
| `MAX_ACU_LIMIT` | unset | Optional ACU cap for each Devin session |
| `ELIGIBILITY_LABEL` | empty | Optional label required for issue intake |
| `MAX_NEW_ISSUES_PER_POLL` | `5` | Maximum newly discovered issues admitted per poll |
| `ISSUE_LOOKBACK_DAYS` | unset | Optional age limit for issue intake |
| `IGNORE_LABELS` | `question,duplicate,invalid,wontfix,discussion,rfc,sip` | Labels that block intake |
| `ANALYSIS_LABEL` | `devin-analysis` | Label on Retro-filed issues; they are fixed but never get a retro of their own |
| `IGNORE_ISSUE_TYPES` | `feature,task,epic` | Issue types that block intake |
| `TRIAGE_ENABLED` | `true` | Starts a Triage session for admitted issues |
| `TRIAGE_ACU_LIMIT` | `2` | ACU cap for a Triage session |
| `MAX_CONCURRENT_DEVINS` | `3` | Maximum running sessions across roles (sessions parked in `NEEDS_INFO`/`BLOCKED` don't hold a slot) |
| `MAX_REMEDIATION_ATTEMPTS` | `2` | Maximum verification retries |
| `MAX_TOTAL_ACUS` | unset | Optional total ACU budget across sessions |
| `DEDUP_ENABLED` | `true` | Checks open PRs before remediation |
| `DEDUP_ACU_LIMIT` | `3` | ACU cap for a Dedup session |
| `CI_REQUIRED` | `true` | Requires GitHub checks before `READY_FOR_REVIEW` |
| `CI_TIMEOUT_SECONDS` | `14400` | Records a timeout after this CI wait |
| `MAX_CI_FIX_ATTEMPTS` | `2` | Failing head commits handed back to the Remediator before a human is asked; `0` disables the handoff |
| `SESSION_STALL_SECONDS` | `5400` | Nudges a stalled session; escalation occurs at twice this value |
| `ANALYSIS_ENABLED` | `true` | Starts a Retro Devin session once a fix PR exists |
| `LEARN_FROM_REVIEWS` | `true` | Saves standing rules from maintainer PR reviews as Devin Knowledge notes (needs the v3 API and Knowledge write permission) |
| `INVESTIGATOR_ACU_LIMIT` | `10` | ACU cap for an Investigator session |
| `REMEDIATOR_ACU_LIMIT` | `25` | ACU cap for a Remediator session |
| `ANALYST_ACU_LIMIT` | `8` | ACU cap for a Retro Devin session |
| `TRIAGE_DEVIN_MODE` | `lite` | Agent mode for a Triage session |
| `DEDUP_DEVIN_MODE` | `lite` | Agent mode for a Dedup session |
| `INVESTIGATOR_DEVIN_MODE` | empty | Agent mode for an Investigator session |
| `REMEDIATOR_DEVIN_MODE` | `fusion` | Agent mode for a Remediator session |
| `ANALYST_DEVIN_MODE` | empty | Agent mode for a Retro Devin session |
| `DB_PATH` | `orchestrator.db` | SQLite database path; Compose uses `/data/orchestrator.db` |
| `ENABLE_POLLING` | `true` | Starts periodic GitHub polling |
| `GITHUB_POLL_INTERVAL_SECONDS` | `5` | Delay between polling ticks |
| `POLL_BACKLOG` | `true` | Processes open issues that existed at startup |

Boolean values accept `1`, `true`, `yes`, or `on`, without regard to letter case.
An empty value leaves an optional limit unset.

## Tests

Run the test suite from the repository root:

```bash
.venv/bin/python -m pytest -q
```

## Troubleshooting

| Symptom | Cause and action |
|---|---|
| `POST /admin/poll-now` returns `503` | Set `ADMIN_TOKEN`, then restart the service |
| `POST /admin/poll-now` returns `401` | Send the exact bearer token from `ADMIN_TOKEN` |
| `POST /webhooks/github` returns `503` | Set `GITHUB_WEBHOOK_SECRET`, or use polling |
| `POST /webhooks/github` returns `401` | Check the GitHub webhook secret and signature |
| No issue enters the workflow | Check intake labels, issue types, age, and `POLL_BACKLOG` |
| Polling stops | Keep the container and its host running |
| The service loses workflow state | Keep `DB_PATH` on persistent storage |
| CI remains pending | Check the PR head commit's GitHub check runs and commit statuses |
| CI fails for a reason unrelated to the fix | The Remediator opens a separate CI-fix PR (linked on the tracker); merge it and the fix PR is updated automatically |
