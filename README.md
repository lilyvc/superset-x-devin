# Autonomous Devin engineering remediation for Apache Superset

Apache Superset has a backlog of actionable bug reports, and engineers spend
time on triage, reproduction, and root-cause analysis before anyone can write a
fix. This service turns eligible GitHub issues into verified pull requests,
while a dashboard shows automation, blockers, and delivery speed.

It runs **externally** against a configurable `TARGET_REPO`. The target
repository remains the home for issues, Devin work branches, and pull requests;
this service does not install an application, agent, or source tree into it.
The service stores orchestration state in SQLite and talks to GitHub and Devin
through their APIs.

## 1. Lifecycle

An eligible issue is discovered by polling or an `issues` webhook, then moves
through a deterministic Python-owned state machine:

```text
GitHub issue
    |
    v
DISCOVERED --> QUEUED --> TRIAGING --> INVESTIGATING
                                      |       |
                                      |       +--> NEEDS_INFO --human reply-->
                                      |       |                       |
                                      |       +<----------------------+
                                      |
                                      +--> NOT_REPRODUCIBLE
                                      +--> BLOCKED
                                      +--> FAILED / ESCALATED
                                      |
                                      v
                                 REPRODUCED
                                      |
                                      v
                              ROOT_CAUSE_FOUND
                                      |
                                      +--> REMEDIATING --> VERIFYING
                                                        |       |
                                                        |       +--> FAILED
                                                        |
                                                        v
                                                    PR_OPENED
                                                        |
                                                        v
                                                 READY_FOR_REVIEW
                                                        |
                                  merged PR / closed issue
                                                        v
                                                    COMPLETED
```

`NEEDS_INFO` is a waiting loop, not a failure; `BLOCKED` is the equivalent for
remediation. A PR closed without merging becomes `FAILED`; a closed target
issue in `PR_OPENED` or `READY_FOR_REVIEW` becomes `COMPLETED`.

## 2. Architecture

```text
                         +----------------------+
                         |  Dashboard / JSON API |
                         +----------+-----------+
                                    |
                                    v
+----------------+       +---------+------------------+       +----------------------+
| GitHub issues  | <----> | Poller + Workflow Engine  | <----> | Devin API sessions   |
| comments, PRs  |       | Python + SQLite            |       | Investigator         |
+----------------+       +----------------------------+       | Remediator / Analyst |
                                                              +----------------------+
```

The control plane owns decisions. Devin supplies engineering work and
structured evidence; it does not directly choose workflow transitions.

| Deterministic control plane (Python) | Agentic engineering plane (Devin) |
|---|---|
| Polling, webhooks, eligibility, and comment cursors | Interpret issues and decide when clarification is needed |
| Issue/session/PR linkage and deduplication | Navigate Superset and reproduce backend, API, or UI behavior |
| State machine, events, SQLite, and restart reconciliation | Debug and identify the root cause |
| Concurrency limits, dispatch ordering, and API calls | Implement the smallest fix and add regression tests |
| Timestamps, metrics, dashboard APIs, and structured gates | Run verification, iterate, and provide behavioral proof |
| Human-comment forwarding and role linkage | Work in separate Investigator, Remediator, and Analyst sessions |

## 3. Devin roles and success gates

Three independent session roles keep investigation, implementation, and
follow-up analysis separate. The schemas in `app/prompts.py` are the contracts
stored with each workflow.

### Investigator

The Investigator does not modify product source or open a PR. Its structured
output includes `status` (`REPRODUCED`, `NOT_REPRODUCIBLE`, `NEEDS_INFO`, or
`BLOCKED`), `enough_information`, `reproduced`, expected/observed behavior,
reproduction steps/evidence, root cause/confidence, affected components,
verification plan, suggested tests, missing information, clarification
question, needs-info kind, product/design-input flags, and `summary`.

Python's `investigation_gate` accepts only when all of these exact conditions
hold:

1. `status == "REPRODUCED"`, `enough_information is True`, and `reproduced is True`.
2. `expected_behavior`, `observed_behavior`, and `root_cause` are non-empty strings.
3. `reproduction_steps`, `reproduction_evidence`, and `verification_plan` each
   contain at least one item.

Passing moves the workflow through `REPRODUCED` and `ROOT_CAUSE_FOUND`.
`NOT_REPRODUCIBLE`, `BLOCKED`, missing information, and repeated incomplete
evidence take their respective controlled paths.

### Remediator

The Remediator receives the investigation and verification plan. Its output
includes `status` (`PR_OPENED`, `VERIFICATION_FAILED`, `BLOCKED`, or `FAILED`),
PR/branch/fix details, changed files, regression tests, `tests_executed`
(`command`, `result`, optional `notes`), reproduction rerun and verification
booleans, verification evidence, blockers, and `summary`.

Python's `verification_gate` accepts only when:

1. `status == "PR_OPENED"`, `verification_passed` is true, and
   `reproduction_rerun_passed` is true.
2. `tests_executed` is non-empty and every result is `"passed"`.
3. A PR URL is present, either in `pr_url` or the session's `pull_requests`.

Passing moves through `PR_OPENED` to `READY_FOR_REVIEW`. Failed verification
can be sent back to the same session for another attempt, up to
`MAX_REMEDIATION_ATTEMPTS`; exhausting attempts enters `FAILED`.

### Analyst

The Analyst does not change workflow state or modify the fix. Its contract
captures `similar_code_locations`, `related_issues`, `systemic_risk`,
`missing_engineering_practice`, `recommended_followup`, `followup_type`,
optional `followup_issue_url`, and `summary`.

Analysis is dispatched when `ANALYSIS_ENABLED=true` and is shown with the
investigation and remediation outputs.

**A session completing is not success. A PR existing is not verification.**
The Python gates decide whether evidence is sufficient to move the workflow.

## 4. Human clarification flow

When an Investigator returns `NEEDS_INFO`, or asks a question in Devin chat
without filling the contract, the engine stores the state and kind, posts one
precise issue question with the session link, records `waiting_since`, and
shows the duration on the dashboard. It polls comments after the persisted
`last_comment_id`, ignores bot and automation comments, forwards the human
reply into the **same** Investigator session, clears the wait, and resumes.

The same pattern applies to a waiting Remediator, except its workflow state is
`BLOCKED` until the active session receives the answer. If a blocked workflow
has no active session, the reply is not silently lost: the engine records a
`human_comment_unforwarded` event and leaves the state unchanged.

## 5. Idempotency, concurrency, and restart safety

- `MAX_CONCURRENT_DEVINS` caps active Investigator, Remediator, and Analyst
  sessions together; the default is `3`.
- Persisted role sessions gate dispatch, so an active role is not duplicated;
  Devin creation also sends `idempotent: true`.
- SQLite stores session IDs, roles, URLs, status, ACUs, outputs, fingerprints,
  active state, workflow linkage, and transition events.
- On restart, active sessions are reconciled from SQLite before dispatch;
  terminal workflows and settled-output fingerprints are not restarted.
- GitHub deliveries are deduplicated by `X-GitHub-Delivery` in SQLite.

## 6. Dashboard and APIs

Open `/` for KPI cards (backlog, active, waiting, PRs, completion, failures,
ACUs, utilization), a live workflow table with stage age/session/PR links, a
funnel, delivery metrics, pipeline lanes, and a lock-guarded `Poll now` button.

Open `/issues/{n}` for lifecycle steps, issue metadata, Devin sessions,
outcome, investigation/remediation/analysis outputs, and the event timeline.

The JSON and operational endpoints are:

| Endpoint | Purpose |
|---|---|
| `GET /api/metrics` | Persisted state counts, groups, funnel, rates, medians, throughput, and ACUs |
| `GET /api/workflows` | Workflow list with timestamps, waiting duration, PR, and session summaries |
| `GET /api/workflows/{owner}/{repo}/{n}` | Full workflow, events, sessions, and structured outputs |
| `GET /healthz` | Health response with target repository and dry-run flag |
| `POST /admin/poll-now` | Run one serialized workflow tick and return counts |
| `POST /webhooks/github` | Signed GitHub `issues` and `issue_comment` intake |

All dashboard values come from persisted SQLite workflow, session, and event
data. There is no sample or hard-coded dashboard data.

## 7. Run

### Docker Compose

```bash
cp .env.example .env
# Edit .env with TARGET_REPO, GITHUB_TOKEN, and Devin credentials.
docker compose up --build
```

The container listens on `8000` and stores SQLite in the
`orchestrator-data` volume at `/data/orchestrator.db`.

### Local virtual environment

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env  # edit credentials
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Dry-run walkthrough

Dry-run fakes Devin sessions and logs comments instead of posting them. GitHub
polling still needs read access, so set `GITHUB_TOKEN` for real issues:

```bash
DRY_RUN=true \
ENABLE_POLLING=true \
POLL_BACKLOG=true \
ELIGIBILITY_LABEL= \
TARGET_REPO=lilyvc/superset \
GITHUB_TOKEN="$GITHUB_TOKEN" \
.venv/bin/uvicorn app.main:app --port 8000
```

In another terminal, seed an issue with a signed simulated webhook:

```bash
.venv/bin/python scripts/simulate.py issue-opened \
  --number 1 --title "Dashboard crashes on load"
```

Run serialized ticks and open the dashboard:

```bash
curl -X POST http://localhost:8000/admin/poll-now
curl -X POST http://localhost:8000/admin/poll-now
curl -X POST http://localhost:8000/admin/poll-now
open http://localhost:8000
```

On Linux, replace `open` with `xdg-open`. `DRY_RUN=true` prevents Devin and
GitHub comment writes while keeping the same state-machine path.

### Environment variables

| Name | Default | Purpose |
|---|---:|---|
| `TARGET_REPO` | `lilyvc/superset` | GitHub repository in `owner/name` form |
| `GITHUB_TOKEN` | empty | GitHub API credential for the target repository |
| `GITHUB_WEBHOOK_SECRET` | empty | Secret used to verify `X-Hub-Signature-256`; recommended |
| `GITHUB_API_URL` | `https://api.github.com` | GitHub API base URL; set for GHES |
| `DEVIN_API_KEY` | empty | Devin service-user key or personal access token |
| `DEVIN_API_BASE_URL` | `https://api.devin.ai` | Devin API base URL |
| `DEVIN_ORG_ID` | empty | Required with a PAT; selects the org-scoped v3 API. A service-user key uses v1 |
| `DRY_RUN` | `false` | Fake Devin sessions and log comments instead of external writes |
| `POLL_INTERVAL_SECONDS` / `POLL_TIMEOUT_SECONDS` | `10` / `1800` | Legacy summary-orchestrator polling interval / timeout |
| `MAX_ACU_LIMIT` | unset | Optional global cap applied to each created session |
| `ELIGIBILITY_LABEL` | `devin-remediate` | Only issues with this label are eligible; empty means every open issue |
| `MAX_CONCURRENT_DEVINS` | `3` | Maximum active Devin sessions across all roles |
| `MAX_REMEDIATION_ATTEMPTS` | `2` | Maximum verification retries for a Remediator |
| `ANALYSIS_ENABLED` | `true` | Dispatch the Analyst role after investigation |
| `INVESTIGATOR_ACU_LIMIT` / `REMEDIATOR_ACU_LIMIT` / `ANALYST_ACU_LIMIT` | `10` / `25` / `8` | Per-role session ACU limits |
| `REMEDIATION_LABEL` | empty | Legacy orchestrator label setting; the active engine records `remediation_started_at` |
| `DB_PATH` | `orchestrator.db` | SQLite database path; Compose overrides it to `/data/orchestrator.db` |
| `ENABLE_POLLING` | `false` | Start the GitHub poller and run a startup tick |
| `GITHUB_POLL_INTERVAL_SECONDS` | `30` | GitHub discovery/comment polling interval |
| `POLL_BACKLOG` | label-dependent | Include existing eligible open issues; otherwise baseline existing issues at startup |

`DEVIN_ORG_ID` selects the org-scoped v3 API for PATs; service-user keys use v1.
The client normalizes both response shapes.

## 8. Demo flow

1. Label an actionable `TARGET_REPO` issue `devin-remediate`.
2. Within `GITHUB_POLL_INTERVAL_SECONDS` (or after **Poll now**), the dashboard
   shows `QUEUED` → `INVESTIGATING` and an Investigator session link.
3. Investigator Devin posts reproduction/root-cause evidence; the workflow
   enters `REMEDIATING`, and Remediator Devin opens a PR with tests.
4. Python validates the contract, shows the PR/evidence in `READY_FOR_REVIEW`,
   and PR reconciliation reaches `COMPLETED` after merge.

For a vague issue, Investigator enters `NEEDS_INFO` and posts one question.
Reply on the issue; polling forwards the answer to the same session.

## 9. Advanced Devin capabilities used

- Structured output feeds deterministic gates; creation uses `idempotent: true`.
- Roles have separate prompts, schemas, tags, titles, and ACU limits; human
  answers return to the same session.
- Both `skills/*.md` playbooks are injected into prompts.
- Investigator and Remediator prompts direct browser/computer use for UI bugs
  and require screenshot/recording evidence where appropriate.

## 10. Intentionally not implemented

- **Webhooks as the primary reliability mechanism:** signed, deduplicated
  webhooks are supported, but polling avoids requiring a public URL.
- **Kafka, Redis, Celery, or Kubernetes:** SQLite plus one asyncio process is
  sufficient at the current scale.
- **Auto-merge:** PRs remain visible for engineering review.
- **Multi-repository fan-out:** each process has one `TARGET_REPO`.
- **Dashboard authentication:** deploy the service behind your own network,
  proxy, or access-control layer.

## 11. Known risks and limits

- Devin sessions can be long-running and consume ACUs; limits reduce but do
  not eliminate cost risk.
- Gates rely on honest structured output; evidence requirements and human PR
  review mitigate this but do not replace review.
- GitHub rate limits, eventual consistency, and the single-process SQLite
  design can delay work; persisted workflows/events remain operational truth.

## 12. Project layout

```text
app/
  main.py          FastAPI, dashboard/API routes, webhook verification
  workflow.py      Multi-stage engine, gates, dispatch, replies
  states.py        State, role, waiting, terminal, and funnel vocabulary
  prompts.py       Role prompts, schemas, and comment templates
  devin_client.py  Devin v1/v3 client and dry-run sessions
  devin_status.py  Devin status/message normalization
  github_client.py GitHub issues, comments, users, and PRs
  store.py         SQLite workflows, sessions, events, delivery dedup
  metrics.py       Database-derived metrics; poller.py periodic tick loop
  static/          Dashboard and issue-detail HTML/CSS
scripts/simulate.py  Signed fake or real webhook sender
skills/              Investigator and verification playbooks
tests/               Workflow and webhook tests
Dockerfile  docker-compose.yml  .env.example
```

## 13. Security notes

- Configure `GITHUB_WEBHOOK_SECRET`; signed requests use `X-Hub-Signature-256`.
  Without a secret, verification is intentionally open for local development.
- Only `issues`, `issue_comment`, and `ping` webhook paths are handled.
- Scope GitHub and Devin credentials to the target repository/session needs;
  protect them like production credentials. Never commit `.env`, tokens,
  databases, or sensitive session output.
- The dashboard and `/admin/poll-now` have no built-in authentication; use a
  trusted network or authenticated reverse proxy.
- This is external orchestration: it does not copy source into this repository
  or install code into `TARGET_REPO`.
