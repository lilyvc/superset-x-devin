# Architecture

## Control plane and engineering plane

The Python service controls each workflow. It finds issues, stores state, checks evidence, and starts Devin sessions.
Devin works in separate sessions. It investigates issues, prepares code changes, and opens PRs in the target repository.
The service does not install a runtime or agent in the target repository.

## Components

| Module | Responsibility |
|---|---|
| `app/workflow.py` | Runs ticks, finds issues, reconciles sessions and PRs, forwards replies, and dispatches sessions |
| `app/handlers.py` | Applies structured output from settled role sessions |
| `app/gates.py` | Checks intake, investigation, verification, CI, and duplicate decisions |
| `app/parsing.py` | Parses GitHub payloads, Devin sessions, dates, and output fingerprints |
| `app/store.py` | Stores workflows, sessions, events, deliveries, and issue provenance in SQLite |
| `app/prompts.py` | Defines role prompts, output schemas, and issue comments |
| `app/github_client.py` | Reads issues, comments, PRs, files, and CI results; posts issue comments |
| `app/devin_client.py` | Creates Devin sessions and reads or resumes session output |
| `app/devin_status.py` | Normalizes Devin v1 and v3 session status and messages |
| `app/main.py` | Creates the FastAPI service, routes, clients, store, and workflow engine |
| `app/metrics.py` | Builds dashboard and API metrics from stored workflow data |
| `app/poller.py` | Runs workflow ticks at the configured interval |
| `app/static/` | Serves the dashboard and issue detail pages |

## Workflow lifecycle

```text
Open GitHub issue
    |
    v
DISCOVERED --> TRIAGING
                  +--> SKIPPED
                  +--> NEEDS_INFO --human reply--> QUEUED
                  +--> QUEUED --> INVESTIGATING
                                     +--> NEEDS_INFO --human reply--> INVESTIGATING
                                     +--> NOT_REPRODUCIBLE
                                     +--> BLOCKED --human reply--> role recovery
                                     +--> FAILED or ESCALATED
                                     +--> REPRODUCED --> ROOT_CAUSE_FOUND
                                                              |
                                                         dedup gate
                                      +-----------------------+-----------------+
                                      |                                         |
                                   SKIPPED                              REMEDIATING
                                                                        |
                                                                    VERIFYING
                                                                        +--> FAILED
                                                                        +--> PR_OPENED
                                                                               |
                                                                  CI_REQUIRED?  |
                                                               yes /           \ no
                                                                  v             v
                                                             CI_CHECKING   READY_FOR_REVIEW
                                                                  |             |
                                                                  +--> READY_FOR_REVIEW
                                                                               |
                                                        merged PR or closed issue
                                                                               v
                                                                          COMPLETED
```

An open PR that already links the issue as fixed makes the workflow `SKIPPED`.
An uncertain duplicate puts the workflow in `BLOCKED` until a human reply resumes the Dedup session.
A closed issue before the PR stage causes `ESCALATED`. A PR closed without a merge causes `FAILED`.

## Tick sequence

`WorkflowEngine.tick` runs these steps in order:

1. Discover open issues. Apply intake filters, provenance, and the per-poll issue limit.
2. Reconcile active Devin sessions. Apply settled output and check for stalled sessions.
3. Reconcile open PRs and, when required, check the PR head commit's CI results.
4. Read issue comments after the stored comment cursor. Forward new human replies.
5. Dispatch new Devin sessions while capacity and the ACU budget allow.

The dispatcher uses this order:

| Order | Role | Condition |
|---:|---|---|
| 1 | Remediator | Investigation reached `ROOT_CAUSE_FOUND`; the duplicate gate is clear |
| 2 | Analyst | `ANALYSIS_ENABLED=true`; state is `ROOT_CAUSE_FOUND`, `REMEDIATING`, `VERIFYING`, `PR_OPENED`, `CI_CHECKING`, or `READY_FOR_REVIEW` |
| 3 | Investigator | Workflow state is `QUEUED` |
| 4 | Triage | Workflow state is `DISCOVERED` and `TRIAGE_ENABLED=true` |

`MAX_CONCURRENT_DEVINS` limits active sessions across roles.
`MAX_TOTAL_ACUS` stops new session dispatch when recorded session ACUs reach the configured total.
The engine records one `budget_exceeded` event when this limit stops dispatch.
After `SESSION_STALL_SECONDS`, the engine nudges a stalled session once. At twice that interval, it escalates the workflow.

## Devin roles and gates

| Role | Work |
|---|---|
| Triage | Routes an issue to investigation, clarification, or a skip reason |
| Investigator | Reproduces an issue and reports root-cause evidence |
| Remediator | Changes code, runs tests, and reports PR verification |
| Dedup | Checks whether a candidate open PR already fixes the issue |
| Analyst | Reports related issues and engineering risks |

### Intake and triage

Python filters pull requests, missing eligibility labels, ignored labels, ignored issue types, and issues beyond `ISSUE_LOOKBACK_DAYS`.
The Triage session returns `ACTIONABLE`, `NEEDS_INFO`, or `SKIP`.
The engine maps these results to `QUEUED`, `NEEDS_INFO`, or `SKIPPED`.
Set `TRIAGE_ENABLED=false` to queue admitted issues without a Triage session.

### Investigation gate

`investigation_gate` accepts output only when every condition below is true:

1. `status` equals `REPRODUCED`.
2. `enough_information` and `reproduced` are `true`.
3. `expected_behavior`, `observed_behavior`, and `root_cause` are non-empty strings.
4. `reproduction_steps`, `reproduction_evidence`, and `verification_plan` each contain an item.

The gate moves accepted output through `REPRODUCED` and `ROOT_CAUSE_FOUND`.
`NEEDS_INFO`, `NOT_REPRODUCIBLE`, and `BLOCKED` follow separate workflow paths.
The engine asks once for missing structured fields. A second incomplete result moves the workflow to `BLOCKED`.

### Duplicate gate

- The engine scans open PR titles and bodies for a closing reference to the issue.
- A matching reference makes the workflow `SKIPPED` with reason `DUPLICATE`.
- The engine compares issue title, root cause, and affected components with PR titles and bodies.
- It sends up to five PRs with at least two shared tokens to the Dedup session.
- The session receives shared terms, changed files, and a body excerpt.
- `DUPLICATE` moves the workflow to `SKIPPED`; `PROCEED` clears the gate; other verdicts move it to `BLOCKED`.
- If GitHub cannot list open PRs, the engine records `dedup_skipped` and continues to remediation.
- Set `DEDUP_ENABLED=false` to bypass this gate.

### Verification and CI gates

`verification_gate` accepts output only when all conditions below are true:

1. `status` equals `PR_OPENED`.
2. `verification_passed` and `reproduction_rerun_passed` are `true`.
3. `tests_executed` contains at least one test, and every result equals `passed`.
4. A PR URL exists in structured output or the session's pull requests.

- The engine checks GitHub check runs and commit statuses for the PR head commit when `CI_REQUIRED=true`.
- A check run that has not completed or a status with state `pending` keeps the workflow in `CI_CHECKING`.
- A failing check conclusion or a status with state `failure` or `error` also keeps the workflow in `CI_CHECKING`.
- The engine moves the workflow to `READY_FOR_REVIEW` only when checks exist and none are pending or failing.
- No configured checks produce `unverified`, not a pass.
- Failing check conclusions are `failure`, `timed_out`, `action_required`, and `cancelled`.
- `CI_TIMEOUT_SECONDS` records a timeout event. It does not mark the PR as passed or failed.
- Set `CI_REQUIRED=false` to skip this gate; the engine records `ci_status=skipped`.

## Human replies and recovery

The engine stores the latest issue comment ID and ignores bot comments and its own GitHub account.
For an active Investigator or Remediator, it sends the human reply to the same session.
For triage clarification without an active session, it queues a new Investigator session.
For a blocked workflow without an active session, it first tries to resume the latest eligible session.
If resume fails, it creates a session with the blocker, question, and human reply in recovery context.
The engine records reply, resume, and recovery events in SQLite.

## Issue provenance

The `issue_origins` table records each issue's origin, parent issue number, Devin session ID, and creation time.
The workflow row stores `origin`, `parent_issue_number`, and `discovered_by_session_id`.
When an Analyst records a follow-up issue, the engine links it to the parent workflow and session.
The poller later discovers the issue and applies the stored provenance.
The dashboard marks human-reported issues and Devin-discovered issues.
The issue page shows parent and child links.

## Persistence and restart safety

| Table | Stored data |
|---|---|
| `deliveries` | GitHub webhook delivery IDs and receive times |
| `workflows` | Issue state, structured output, timestamps, PR details, and provenance |
| `sessions` | Devin session IDs, roles, status, output fingerprints, ACUs, and active state |
| `events` | Workflow state changes and operational events |
| `issue_origins` | Parent and Devin provenance for issues discovered later |

The store enforces one workflow per repository and issue number.
Webhook delivery IDs prevent repeated webhook work.
Devin session creation sends `idempotent=true`.
Session IDs, active-session checks, and output fingerprints prevent duplicate dispatch and repeated output handling.
After restart, the engine reads active sessions from SQLite and reconciles them before it dispatches new work.

## Dashboard and API

- The dashboard shows four KPIs: `Bugs handled`, `Verified fixes`, `Median time to fix`, and `Needs human`.
- The `Needs human` KPI counts workflows in `NEEDS_INFO` and `BLOCKED`.
- An expandable Issues list shows a vertical nine-step tracker and origin markers.
- The markers identify reported issues and Devin-discovered issues from parent issues.
- A panel titled Latest verified fix shows fix evidence and CI status.
- The panel selects the newest workflow with a PR URL; it does not require passing CI.
- The Operations section starts collapsed. It shows the funnel, workflow lanes, latencies, sessions, and ACUs.
- The issue page shows the tracker, structured outputs, events, and provenance links.

The `executive` metrics fields are `bugs_handled`, `verified_fixes`, `median_time_to_fix`, `needs_human`, `defects_discovered`, and `acus_per_verified_fix`.
`bugs_handled` counts stored workflows. `verified_fixes` counts workflows in `READY_FOR_REVIEW` or `COMPLETED`.
`median_time_to_fix` measures discovery to PR open time. `needs_human` counts `NEEDS_INFO` and `BLOCKED` workflows.

| Route | Purpose |
|---|---|
| `GET /` | Dashboard page |
| `GET /issues/{number}` | Issue detail page |
| `GET /api/metrics` | Counts, groups, funnel, rates, medians, throughput, and ACUs |
| `GET /api/workflows` | Workflow list with state, timing, provenance, and session summaries |
| `GET /api/workflows/{repo_owner}/{repo}/{number}` | One workflow with sessions, events, parent, and children |
| `GET /healthz` | Service status, target repository, and dry-run flag |
| `POST /admin/poll-now` | Runs one serialized workflow tick |
| `POST /webhooks/github` | Accepts signed issue, comment, and ping events |

## Security model

- `POST /webhooks/github` requires `GITHUB_WEBHOOK_SECRET` and a valid HMAC signature.
- The route returns `503` when the secret is unset and `401` when the signature is invalid.
- `POST /admin/poll-now` requires `Authorization: Bearer $ADMIN_TOKEN`.
- It returns `503` when `ADMIN_TOKEN` is unset and `401` for an invalid token.
- Dashboard pages and GET APIs have no built-in authentication.
- Place them behind a trusted network or an authenticated proxy.
- The service reads issues and PRs, posts issue comments, and uses Devin to create PRs.
- It does not install a service or agent in the target repository.
