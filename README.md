# Autonomous Devin engineering remediation for Apache Superset

Apache Superset has a backlog of actionable bug reports, and engineers spend
time on triage, reproduction, and root-cause analysis before anyone can write a
fix. This service autonomously discovers GitHub issues and turns the actionable
ones into verified pull requests, while a dashboard shows automation, blockers,
and delivery speed. Intake needs no human trigger label.

It runs **externally** against a configurable `TARGET_REPO`. The target
repository remains the home for issues, Devin work branches, and pull requests;
this service does not install an application, agent, or source tree into it.
The service stores orchestration state in SQLite and talks to GitHub and Devin
through their APIs.

## 1. Lifecycle

Every open issue in `TARGET_REPO` is discovered by polling (or an `issues`
webhook), persisted as `DISCOVERED`, and put through a cheap Triage Devin that
returns one of three verdicts. Actionable issues then move through a
deterministic Python-owned state machine:

```text
GitHub issue (no label required)
    |
    v
DISCOVERED --> TRIAGING --+--> SKIPPED (not engineering / duplicate / invalid)
                          +--> NEEDS_INFO --human reply--> QUEUED
                          +--> QUEUED --> INVESTIGATING
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
                              dedup gate (open-PR scan,
                              optional Dedup Devin verdict)
                                      |
                                      +--> SKIPPED (DUPLICATE -- an open PR
                                      |            already fixes this)
                                      +--> BLOCKED (ambiguous -- needs a human)
                                      |
                                      +--> REMEDIATING --> VERIFYING
                                                        |       |
                                                        |       +--> FAILED
                                                        |
                                                        v
                                                    PR_OPENED
                                                        |
                                                        v
                                                  CI_CHECKING
                                              (the PR's real GitHub
                                               checks must pass)
                                                        |
                                                        v
                                                 READY_FOR_REVIEW
                                                        |
                                  merged PR / closed issue
                                                        v
                                                    COMPLETED
```

### Autonomous intake

| Triage verdict | State | Action on the issue |
| --- | --- | --- |
| `ACTIONABLE` | `QUEUED` | Investigator Devin starts on the next tick |
| `NEEDS_INFO` | `NEEDS_INFO` | One concise clarification question is posted; a human reply queues investigation |
| `SKIP` | `SKIPPED` | Comment explains the reason (`NOT_ENGINEERING`, `DUPLICATE`, `FEATURE_REQUEST`, `INVALID`, `UNSUITABLE`) |

Deterministic filters run *before* any Devin session is created, so obviously
unsuitable issues cost nothing: pull requests, `IGNORE_LABELS`,
`IGNORE_ISSUE_TYPES`, issues older than `ISSUE_LOOKBACK_DAYS`, and anything
beyond `MAX_NEW_ISSUES_PER_POLL` in a single poll. Filtered issues are persisted
as `SKIPPED` with reason `FILTERED`, so they are never re-evaluated. Backlog
issues that already exist are discovered the same way (`POLL_BACKLOG=true`).

Opt-in gating is still available: set `ELIGIBILITY_LABEL=devin-remediate` and
only labelled issues enter intake. Set `TRIAGE_ENABLED=false` to queue every
admitted issue straight to the Investigator without a triage verdict.

`NEEDS_INFO` is a waiting loop, not a failure; `BLOCKED` is the equivalent for
remediation. A PR closed without merging becomes `FAILED`; a closed target
issue in `PR_OPENED`, `CI_CHECKING`, or `READY_FOR_REVIEW` becomes `COMPLETED`.

Two different things are called "discovery" and are named explicitly:

- **Issue intake discovery** — the poller/webhook path that discovers open
  GitHub issues and admits them as `DISCOVERED` workflows.
- **Related-defect discovery** — the Analyst's job after a fix: finding similar
  code locations and related defects worth follow-up issues.

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

Five independent session roles keep intake, investigation, implementation,
duplicate screening, and follow-up analysis separate. The schemas in
`app/prompts.py` are the contracts stored with each workflow.

### Triage

A deliberately cheap session (`TRIAGE_ACU_LIMIT`, default 2 ACUs) that reads the
issue and its comments, may grep the repo, and must not reproduce the bug,
modify code, run test suites, or open a PR. Its structured output is
`verdict` (`ACTIONABLE` / `NEEDS_INFO` / `SKIP`), `issue_kind`, `skip_reason`,
`duplicate_of`, `clarification_question`, `needs_info_kind`, `suspected_area`,
and `rationale`. This is an intake routing decision, not a proof gate: Python
maps the verdict onto `QUEUED`, `NEEDS_INFO`, or `SKIPPED` and comments on the
issue for the latter two.

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

Passing moves the workflow to `PR_OPENED` and then `CI_CHECKING`. Failed
verification can be sent back to the same session for another attempt, up to
`MAX_REMEDIATION_ATTEMPTS`; exhausting attempts enters `FAILED`.

`READY_FOR_REVIEW` is independently verified, not self-reported: once the
Remediator's contract passes, the engine polls the PR's head SHA against
GitHub's check-runs and commit-status APIs every tick. The workflow only
reaches `READY_FOR_REVIEW` when the checks are green; failing checks post one
comment naming them and stay in `CI_CHECKING`, and a PR with no checks at all
shows `ci_status: unverified` on the dashboard and issue API rather than an
implied green result. `CI_TIMEOUT_SECONDS` (default 4h) bounds the wait and
records a `ci_timeout` event. Set `CI_REQUIRED=false` only for targets without
CI — the gate is then skipped and `ci_status` shows `skipped` honestly.

A session that keeps running without ever settling would otherwise hold its
concurrency slot indefinitely, so after `SESSION_STALL_SECONDS` the engine sends
the session one message asking it to report what it has (including partial or
negative results), and at twice that it closes the session out and moves the
workflow to `ESCALATED` with a comment on the issue.

### Dedup gate

Before a Remediator session is dispatched, a cheap reconciliation step scans
the repository's open PRs (no Devin session). A PR whose title or body contains
a closing reference to the issue (`fixes #N`, `closes #N`, ...) is a definitive
duplicate: the workflow goes `SKIPPED` with reason `DUPLICATE` and a comment
citing the existing PR, so overlapping fix PRs are never opened twice. When the
match is only plausible — shared terms between the issue/root cause and a PR's
title/body — a small Dedup Devin session (`DEDUP_ACU_LIMIT`, default 3 ACUs)
adjudicates with the candidate PRs' changed-file lists: `DUPLICATE` skips the
workflow, `PROCEED` clears the gate, and `UNSURE` goes `BLOCKED` for a human.
Set `DEDUP_ENABLED=false` to bypass the gate entirely.

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
has no active session, the reply is still delivered: the engine first tries to
resume the last Investigator/Remediator/Dedup session (messaging a settled
Devin session wakes it, preserving its context). When resumption fails — the
session expired or was deleted — a fresh same-role session is started with a
`## Recovery context` block carrying the original blocker, the human's
question, and the answer, and a `human_reply_recovery_session` event is
recorded.

## 5. Idempotency, concurrency, and restart safety

- `MAX_CONCURRENT_DEVINS` caps active Investigator, Remediator, Dedup, and
  Analyst sessions together; the default is `3`.
- `MAX_TOTAL_ACUS` is an organization-level budget ceiling: once the ACUs
  recorded across all sessions reach it, no new Devin sessions are dispatched
  (existing ones keep running; a `budget_exceeded` event is recorded once).
- Persisted role sessions gate dispatch, so an active role is not duplicated;
  Devin creation also sends `idempotent: true`.
- SQLite stores session IDs, roles, URLs, status, ACUs, outputs, fingerprints,
  active state, workflow linkage, and transition events.
- On restart, active sessions are reconciled from SQLite before dispatch;
  terminal workflows and settled-output fingerprints are not restarted.
- GitHub deliveries are deduplicated by `X-GitHub-Delivery` in SQLite.

## 6. Dashboard and APIs

Open `/` for KPI cards (discovered/triaging, queued/skipped, active, waiting,
PRs ready with an awaiting-CI count, completion, failures, ACUs, utilization),
a live workflow table with stage age/session/PR links and per-PR CI status, an
intake funnel (discovered → triaged → actionable → investigated → …), delivery
metrics including triage skip rate and discovered→triage latency, and a
lock-guarded `Poll now` button (which sends `ADMIN_TOKEN` when configured).
Pipeline lanes distinguish `DISCOVERED`, `TRIAGING`, `QUEUED`, `INVESTIGATING`,
waiting for human, remediation, `PR_OPENED`/`CI_CHECKING`, `READY_FOR_REVIEW`,
completed, and `SKIPPED`.

Open `/issues/{n}` for lifecycle steps, issue metadata, Devin sessions,
outcome, triage verdict, investigation/remediation/analysis outputs, and the
event timeline.

The JSON and operational endpoints are:

| Endpoint | Purpose |
|---|---|
| `GET /api/metrics` | Persisted state counts, groups, funnel, rates, medians, throughput, and ACUs |
| `GET /api/workflows` | Workflow list with timestamps, waiting duration, PR, and session summaries |
| `GET /api/workflows/{owner}/{repo}/{n}` | Full workflow, events, sessions, and structured outputs |
| `GET /healthz` | Health response with target repository and dry-run flag |
| `POST /admin/poll-now` | Run one serialized workflow tick; requires `Authorization: Bearer $ADMIN_TOKEN` when set, and returns 503 when it is not |
| `POST /webhooks/github` | Signed GitHub `issues` and `issue_comment` intake; returns 503 when `GITHUB_WEBHOOK_SECRET` is unset |

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
MAX_NEW_ISSUES_PER_POLL=5 \
TARGET_REPO=lilyvc/superset \
GITHUB_TOKEN="$GITHUB_TOKEN" \
.venv/bin/uvicorn app.main:app --port 8000
```

In another terminal, seed an issue with a signed simulated webhook:

```bash
.venv/bin/python scripts/simulate.py issue-opened \
  --number 1 --title "Dashboard crashes on load"
```

Run serialized ticks and open the dashboard (the first tick discovers and
triages, the second starts the Investigator):

```bash
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/admin/poll-now
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/admin/poll-now
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/admin/poll-now
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/admin/poll-now
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
| `MAX_ACU_LIMIT` | unset | Optional global cap applied to each created session |
| `ELIGIBILITY_LABEL` | empty | Empty means autonomous intake of every open issue; set a label to restrict intake to issues carrying it |
| `TRIAGE_ENABLED` | `true` | Run the cheap Triage Devin before the Investigator |
| `TRIAGE_ACU_LIMIT` | `2` | ACU cap for a triage session |
| `MAX_NEW_ISSUES_PER_POLL` | `5` | Maximum newly discovered issues admitted per poll |
| `ISSUE_LOOKBACK_DAYS` | unset | Skip issues created longer ago than this |
| `IGNORE_LABELS` | `question,duplicate,invalid,wontfix,discussion,rfc,sip,devin-analysis` | Labels that skip intake without a Devin session |
| `IGNORE_ISSUE_TYPES` | `feature,task,epic` | GitHub issue types that skip intake |
| `MAX_CONCURRENT_DEVINS` | `3` | Maximum active Devin sessions across all roles |
| `MAX_REMEDIATION_ATTEMPTS` | `2` | Maximum verification retries for a Remediator |
| `ANALYSIS_ENABLED` | `true` | Dispatch the Analyst role after investigation |
| `INVESTIGATOR_ACU_LIMIT` / `REMEDIATOR_ACU_LIMIT` / `ANALYST_ACU_LIMIT` | `10` / `25` / `8` | Per-role session ACU limits |
| `DEDUP_ENABLED` | `true` | Scan open PRs for duplicates before dispatching a Remediator |
| `DEDUP_ACU_LIMIT` | `3` | ACU cap for the Dedup adjudication session |
| `CI_REQUIRED` | `true` | Require the fix PR's GitHub checks to pass before `READY_FOR_REVIEW` |
| `CI_TIMEOUT_SECONDS` | `14400` | Bound on the CI wait; records `ci_timeout` and keeps `CI_CHECKING` |
| `SESSION_STALL_SECONDS` | `5400` | Nudge a Devin session that never settles; escalate the workflow at twice this |
| `ADMIN_TOKEN` | empty | Bearer token for `POST /admin/poll-now`; required for that endpoint |
| `MAX_TOTAL_ACUS` | unset | Organization-level ACU budget ceiling across all sessions |
| `DB_PATH` | `orchestrator.db` | SQLite database path; Compose overrides it to `/data/orchestrator.db` |
| `ENABLE_POLLING` | `false` | Start the GitHub poller and run a startup tick |
| `GITHUB_POLL_INTERVAL_SECONDS` | `30` | GitHub discovery/comment polling interval |
| `POLL_BACKLOG` | `true` | Discover the existing open-issue backlog; `false` baselines existing issues at startup |

`DEVIN_ORG_ID` selects the org-scoped v3 API for PATs; service-user keys use v1.
The client normalizes both response shapes.

## 8. Demo flow

1. Open an issue on `TARGET_REPO` — no label needed.
2. Within `GITHUB_POLL_INTERVAL_SECONDS` (or after **Poll now**), the dashboard
   shows `DISCOVERED` → `TRIAGING` → `QUEUED` → `INVESTIGATING` with the triage
   verdict and session links (non-actionable issues land in `SKIPPED` or
   `NEEDS_INFO` instead).
3. Investigator Devin posts reproduction/root-cause evidence; the dedup gate
   scans open PRs, then Remediator Devin opens a PR with tests.
4. Python validates the contract, waits for the PR's GitHub checks in
   `CI_CHECKING`, and shows the verified PR in `READY_FOR_REVIEW`; PR
   reconciliation reaches `COMPLETED` after merge.

For a vague issue, Investigator enters `NEEDS_INFO` and posts one question.
Reply on the issue; polling forwards the answer to the same session.

## 9. Advanced Devin capabilities used

- Structured output feeds deterministic gates; creation uses `idempotent: true`.
- Roles have separate prompts, schemas, tags, titles, and ACU limits; human
  answers return to the same session, or to a fresh recovery session when the
  original cannot be resumed.
- Both `skills/*.md` playbooks are injected into prompts; they are copied into
  the image (`COPY skills ./skills`) and `validate_skills()` fails startup if
  either is missing, rather than silently degrading prompts.
- Investigator and Remediator prompts direct browser/computer use for UI bugs
  and require screenshot/recording evidence where appropriate.

## 10. Intentionally not implemented

- **Webhooks as the primary reliability mechanism:** signed, deduplicated
  webhooks are supported, but polling avoids requiring a public URL.
- **Kafka, Redis, Celery, or Kubernetes:** SQLite plus one asyncio process is
  sufficient at the current scale.
- **Auto-merge:** PRs remain visible for engineering review.
- **Multi-repository fan-out:** each process has one `TARGET_REPO`.
- **Read-side dashboard authentication:** `POST /admin/poll-now` requires
  `ADMIN_TOKEN`, but dashboard GETs are still unauthenticated — deploy behind
  your own network, proxy, or access-control layer.

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
  workflow.py      Engine: tick, discovery, reconciliation, replies, dispatch
  handlers.py      Per-role handling of settled Devin output
  gates.py         Python-owned decision gates
  parsing.py       Pure GitHub and Devin parsers
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

- `GITHUB_WEBHOOK_SECRET` is required for webhook intake: verification fails
  closed, and `POST /webhooks/github` returns 503 while it is unset instead of
  accepting unsigned requests.
- Only `issues`, `issue_comment`, and `ping` webhook paths are handled.
- Scope GitHub and Devin credentials to the target repository/session needs;
  protect them like production credentials. Never commit `.env`, tokens,
  databases, or sensitive session output.
- The dashboard and `/admin/poll-now` have no built-in authentication; use a
  trusted network or authenticated reverse proxy.
- This is external orchestration: it does not copy source into this repository
  or install code into `TARGET_REPO`.
