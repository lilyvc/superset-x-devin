# superset-x-devin

Event-driven automation that orchestrates [Devin](https://devin.ai) sessions from GitHub
issue events. It runs **externally** — it watches a configurable target repository via
webhooks and never installs code into that repo.

```
GitHub webhook (issues / issue_comment)
        |
        v
  FastAPI endpoint  --verify signature-->  Orchestrator
        |                                     |
        |                                     |-- Devin API: create session / send message / poll
        |                                     |-- SQLite: delivery dedup + issue -> session map
        v                                     |
   202 response <-----------------------------+
                                              v
                                GitHub API: post comment / add label
```

**Current behaviour (v1)**

| GitHub event | What happens |
|---|---|
| `issues.opened` / `issues.reopened` | A Devin session is created to summarize the issue; the summary is posted back as an issue comment. |
| `issue_comment.created` | If the issue already has a Devin session on record, the comment is forwarded into that session — this is how a human reply to Devin's question resumes the workflow. |

Roadmap items (dispatch idempotency, remediation workflow, labels) live in [TODO.md](TODO.md).

## Prerequisites

- Python 3.12+ or Docker
- A **Devin API credential** — either a service-user API key or a personal access
  token (PAT), both created in the Devin web app under **Settings → Devin API**.
  PATs also need `DEVIN_ORG_ID` (they use the org-scoped v3 API).
- A **GitHub token** with `issues: write` on the target repo (a fine-grained PAT scoped to the repo works)
- A **webhook secret** — any random string, e.g. `openssl rand -hex 32`

## Configuration

Copy `.env.example` to `.env` and fill it in. Everything is env-driven so the same
deployment can be pointed at a different repo by changing `TARGET_REPO` plus the
token — no code changes needed.

| Variable | Required | Purpose |
|---|---|---|
| `TARGET_REPO` | yes | `owner/name` to watch, e.g. `lilyvc/superset` |
| `DEVIN_API_KEY` | yes* | Devin API credential — service-user key or PAT (`*`not needed with `DRY_RUN=true`) |
| `DEVIN_ORG_ID` | with PAT | `org-...` id; selects the org-scoped v3 API. Leave empty for service-user keys (v1) |
| `GITHUB_TOKEN` | yes* | Token with issues write on the target repo |
| `GITHUB_WEBHOOK_SECRET` | recommended | Must match the webhook's secret; requests without a valid `X-Hub-Signature-256` are rejected |
| `DRY_RUN` | no | `true` = run the whole pipeline with no external calls (comments are logged) |
| `DEVIN_API_BASE_URL` | no | Defaults to `https://api.devin.ai` |
| `POLL_INTERVAL_SECONDS` / `POLL_TIMEOUT_SECONDS` | no | Devin session polling cadence (10s / 30min defaults) |
| `MAX_ACU_LIMIT` | no | Cap ACUs per spawned Devin session |
| `REMEDIATION_LABEL` | no | Label applied to an issue when dispatched, e.g. `devin-remediation-started` |
| `DB_PATH` | no | SQLite path (default `orchestrator.db`; `/data` under compose) |

## Run

### Docker

```bash
cp .env.example .env   # fill in secrets
docker compose up --build
```

### Locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

The server exposes `POST /webhooks/github` and `GET /healthz`.

## Simulate the workflow (no webhook required)

With the server running in dry-run mode you can exercise the full pipeline —
signature check, dedup, dispatch, poll, comment — with zero credentials:

```bash
DRY_RUN=true uvicorn app.main:app --port 8000

# fake issue opened:
python scripts/simulate.py issue-opened --number 1 --title "Dashboard crashes on load"

# human replying on a tracked issue (forwards into the recorded session):
python scripts/simulate.py issue-comment --number 1 --body "It's the Explore page"

# build the payload from a real issue in TARGET_REPO (needs GITHUB_TOKEN):
python scripts/simulate.py issue-opened --real --number 4
```

The simulated comment is written to the server log. Turn off `DRY_RUN` and set real
credentials to run end-to-end for real — the same `simulate.py` calls drive it.

## Point GitHub at it (real events)

1. Expose the service on a public URL (deploy it, or `ngrok http 8000` / `cloudflared`
   for local testing).
2. In the target repo: **Settings → Webhooks → Add webhook**
   - Payload URL: `https://<your-host>/webhooks/github`
   - Content type: `application/json`
   - Secret: same value as `GITHUB_WEBHOOK_SECRET`
   - Events: **Issues** and **Issue comments** (or "Let me select individual events")
3. GitHub sends a `ping`; check `/healthz` and the server logs.

Open an issue on the target repo → a Devin session link appears in the logs → a summary
comment lands on the issue.

## How a reply resumes the workflow

When a Devin session finishes `blocked` (typically because it asked a question), the
orchestrator posts the question as an issue comment and keeps the `issue → session_id`
mapping in SQLite. A subsequent `issue_comment.created` webhook looks up that mapping and
calls `POST /v1/sessions/{id}/message`, so the **same** Devin session picks the work back
up with the reply as new context — a new session is never created for replies.

## Project layout

```
app/
  main.py          FastAPI app, signature verification, webhook routing
  orchestrator.py  Event handling, Devin session lifecycle, commenting
  devin_client.py  Devin v1 API client (create/poll/message sessions)
  github_client.py GitHub REST client (comments, labels)
  store.py         SQLite: webhook delivery dedup + issue→session mapping
  config.py        Env-based settings
scripts/simulate.py  Signed fake webhook sender
tests/               pytest
Dockerfile / docker-compose.yml
```

## Security notes

- Webhook payloads are verified with `X-Hub-Signature-256` when a secret is configured.
- Only `issues` and `issue_comment` events are handled; everything else is ignored.
- The GitHub token only ever writes comments/labels on `TARGET_REPO`.
- Devin sessions created here are given no repository secrets beyond what the prompt
  contains; scope `secret_ids` on session creation if you later need that.
