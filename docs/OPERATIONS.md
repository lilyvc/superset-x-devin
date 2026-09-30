# Operations

## Requirements

Use a persistent host that can run Docker Compose or Python 3.
Give the service a GitHub token, a Devin credential for live sessions, and a stable SQLite path.
Dry-run still reads GitHub data. It fakes Devin sessions and suppresses GitHub comment writes.

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

## Dry-run walkthrough

1. Copy `.env.example` to `.env`.
2. Set `TARGET_REPO`, `GITHUB_TOKEN`, and `ADMIN_TOKEN` in `.env`.
3. Set `DRY_RUN=true` and `ENABLE_POLLING=false` in `.env`.
4. Load `.env` with `set -a; . ./.env; set +a`.
5. Start the service with `.venv/bin/uvicorn app.main:app --port 8000`.
6. Export `ADMIN_TOKEN` in the shell.
7. Request one tick with `curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/admin/poll-now`.
8. Open the dashboard at `http://localhost:8000`.

The service reads real GitHub issues during the tick. Devin sessions are fake, and GitHub comments are not posted.
When `ENABLE_POLLING=true`, the service uses `POLL_BACKLOG` at startup.

## Webhook intake and polling

Use webhook intake when GitHub can reach the service over HTTPS.
Set `GITHUB_WEBHOOK_SECRET` and configure the GitHub webhook to send issue and issue comment events to `/webhooks/github`.
The service checks `X-Hub-Signature-256` and deduplicates deliveries by `X-GitHub-Delivery`.
It accepts opened or reopened issue events, created issue comments, and ping events.

Use polling when the service does not have a public URL.
Set `ENABLE_POLLING=true` and `GITHUB_POLL_INTERVAL_SECONDS`.
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

The service reads issues, comments, PRs, PR files, check runs, and commit statuses.
It writes issue comments. Devin creates the PR work in the target repository.
A classic token for a private repository needs the broad `repo` scope.
Protect the token as a production credential.

### Devin credential

Set `DEVIN_API_KEY` to a Devin service-user key or personal access token.
A service-user key selects the v1 API.
A personal access token selects the organization-scoped v3 API when `DEVIN_ORG_ID` is set.
Set `DEVIN_ORG_ID` for the Devin organization that owns the sessions.
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
| `DEVIN_API_KEY` | empty | Devin service-user key or personal access token |
| `DEVIN_API_BASE_URL` | `https://api.devin.ai` | Devin API base URL |
| `DEVIN_ORG_ID` | empty | Selects the organization-scoped v3 API |
| `DRY_RUN` | `false` | Uses fake Devin sessions and suppresses GitHub comment writes |
| `MAX_ACU_LIMIT` | unset | Optional ACU cap for each Devin session |
| `ELIGIBILITY_LABEL` | empty | Optional label required for issue intake |
| `MAX_NEW_ISSUES_PER_POLL` | `5` | Maximum newly discovered issues admitted per poll |
| `ISSUE_LOOKBACK_DAYS` | unset | Optional age limit for issue intake |
| `IGNORE_LABELS` | `question,duplicate,invalid,wontfix,discussion,rfc,sip,devin-analysis` | Labels that block intake |
| `IGNORE_ISSUE_TYPES` | `feature,task,epic` | Issue types that block intake |
| `TRIAGE_ENABLED` | `true` | Starts a Triage session for admitted issues |
| `TRIAGE_ACU_LIMIT` | `2` | ACU cap for a Triage session |
| `MAX_CONCURRENT_DEVINS` | `3` | Maximum active sessions across roles |
| `MAX_REMEDIATION_ATTEMPTS` | `2` | Maximum verification retries |
| `MAX_TOTAL_ACUS` | unset | Optional total ACU budget across sessions |
| `DEDUP_ENABLED` | `true` | Checks open PRs before remediation |
| `DEDUP_ACU_LIMIT` | `3` | ACU cap for a Dedup session |
| `CI_REQUIRED` | `true` | Requires GitHub checks before `READY_FOR_REVIEW` |
| `CI_TIMEOUT_SECONDS` | `14400` | Records a timeout after this CI wait |
| `SESSION_STALL_SECONDS` | `5400` | Nudges a stalled session; escalation occurs at twice this value |
| `ANALYSIS_ENABLED` | `true` | Starts an Analyst session when eligible |
| `INVESTIGATOR_ACU_LIMIT` | `10` | ACU cap for an Investigator session |
| `REMEDIATOR_ACU_LIMIT` | `25` | ACU cap for a Remediator session |
| `ANALYST_ACU_LIMIT` | `8` | ACU cap for an Analyst session |
| `DB_PATH` | `orchestrator.db` | SQLite database path; Compose uses `/data/orchestrator.db` |
| `ENABLE_POLLING` | `false` | Starts periodic GitHub polling |
| `GITHUB_POLL_INTERVAL_SECONDS` | `30` | Delay between polling ticks |
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
