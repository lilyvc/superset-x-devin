# superset-x-devin: GitHub issue-to-fix service

A service that autonomously investigates incoming issues and fixes bugs in `TARGET_REPO` using Devin sessions. Devin triages, reproduces, and remediates issues, then opens PRs for human review. When appropriate, a follow up agent looks for similar defects elsewhere in the repo or deeper root causes. A dashboard tracks every issue's progress, Devin sessions, and evidence.

## How it works

The service is written in Python and orchestrates the workflow: it starts Devin sessions, limits concurrency, stores progress, and controls state transitions. Devin investigates bugs, writes fixes, and reports results.

Each Devin role returns a structured report defined by an output contract. The service applies deterministic gates to check required fields and reported outcomes before allowing the workflow to advance. For example, investigation requires reproduction evidence and a root cause; remediation requires a PR URL, a successful reproduction rerun, and passed test results. The service checks GitHub CI separately.

1. **Intake:** The service collects issues through polling or webhooks and applies configured filters.
2. **Triage:** Devin decides whether to investigate, request clarification, or skip the issue.
3. **Investigation:** Devin reproduces the bug and reports expected and observed behavior, reproduction steps, evidence, a root cause, and a verification plan. The service checks open PRs for an existing fix before remediation.
4. **Remediation:** Devin writes a fix and regression tests, reruns the original reproduction, and opens a PR. Changes to existing test expectations require maintainer approval.
5. **CI:** By default, the service requires GitHub checks to pass before setting `READY_FOR_REVIEW`. Failed checks go back to the Remediator, which fixes failures caused by its change or opens a separate CI-fix PR for unrelated failures. After that PR merges, the Remediator updates the original fix branch.
6. **Related defect analysis:** Once a fix PR exists, a separate Devin session looks for related bug patterns and files follow-up issues.
7. **Completion:** The service marks the workflow `COMPLETED` when the fix PR merges. Workflows in `NEEDS_INFO` or `BLOCKED` wait for human input.

Comments on tracked issues and PRs are forwarded to an active or resumed Devin session. Standing rules from trusted maintainer reviews are saved as [Devin Knowledge](https://docs.devin.ai/product-guides/knowledge) notes pinned to the repo for future sessions.

The service can also mark an issue `NOT_REPRODUCIBLE`, `SKIPPED`, `FAILED`, or `ESCALATED`.
Each role runs as its own Devin session with a per-role agent mode and ACU cap. By default, triage and dedup use `lite`, remediation uses `fusion`, and investigation and related defect analysis use the organization's default mode. Modes and caps are configurable (see [Operations](docs/OPERATIONS.md)).

Completed analyses feed known defect families into future prompts. Maintainer review rules appear under "Learned from reviews" on the dashboard (see [Architecture](docs/ARCHITECTURE.md#learning-from-reviews)).

## Prerequisites

1. Choose a GitHub repo to watch, such as your fork of `apache/superset`. Enable Issues in the repo's Settings → General → Features; forks have Issues disabled by default.
2. In Devin, connect GitHub under Settings → Connections → GitHub with access to that repo. Add the repo to your [Devin environment](https://docs.devin.ai/onboard-devin/environment) so sessions can build and test it. Superset's [AGENTS.md](https://github.com/apache/superset/blob/master/AGENTS.md) covers its build and test setup.
3. Create a Devin service user under Settings → Devin API → Service users with permission to use Devin sessions. For review learning, also grant Knowledge write access. Note its API key and your Devin org id (`org-…`).
4. Create a fine-grained GitHub token for the target repo with Issues read/write, Pull requests read/write, Checks read, and Commit statuses read.

## Start the service

You need Docker with Compose v2 (`docker compose`).

1. Clone this repo and `cd` into it.
2. Copy `.env.example` to `.env`.
3. Set `TARGET_REPO`, `GITHUB_TOKEN`, `DEVIN_API_KEY`, `DEVIN_ORG_ID`, and `ADMIN_TOKEN` in `.env`.
4. Run `docker compose up --build`.
5. Open `http://localhost:8000`. A red **Setup problem** banner lists any misconfigured settings. File an issue in the target repo to watch the workflow.

Open issues are processed at startup too. Set `POLL_BACKLOG=false` to process only new issues.
For a local run without Docker, see [Operations](docs/OPERATIONS.md).

To force an immediate poll instead of waiting for the interval:

```bash
set -a; . ./.env; set +a
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/admin/poll-now
```

The service polls `TARGET_REPO` every 5 seconds by default, so it works anywhere Docker runs. It also supports GitHub webhooks (`POST /webhooks/github` for `issues`, `issue_comment`, `pull_request_review`, and `pull_request_review_comment` events, verified with `GITHUB_WEBHOOK_SECRET`) for instant delivery in production where the service is publicly reachable. For a local demo, polling is the simpler choice.

See [Operations](docs/OPERATIONS.md) for the full configuration reference.

## Try it: run the workflow on a sample bug

There is no simulated mode. Each step runs a real Devin session, so set `MAX_TOTAL_ACUS` in `.env` to stop new sessions once that many ACUs are used. Sessions already running finish under their own per-role caps.

1. Start the service as above, with `TARGET_REPO` set to your Superset fork.
2. File this issue in the fork. The bug is real on `apache/superset` master.

   > **Title:** Time-comparison "percentage" returns inf when the baseline value is 0
   >
   > `superset/utils/pandas_postprocessing/compare.py` divides `(s_df - c_df) / c_df` with no zero guard. With compare type `percentage` or `ratio`, a baseline of 0 produces `inf`: chart cells show blank and CSV exports contain `inf`.
   >
   > ```python
   > import pandas as pd
   > from superset.utils.pandas_postprocessing.compare import compare
   > df = pd.DataFrame({"y": [100.0, 0.0, 2.0], "z": [0.0, 0.0, 4.0]})
   > compare(df, source_columns=["y"], compare_columns=["z"], compare_type="percentage")
   > # percentage column: [inf, nan, -0.5]; expected NaN where the baseline is 0
   > ```

3. Watch the issue move through the dashboard tracker. Each step links to its Devin session, and the service posts the same updates as comments on the issue.
   - **Triaging**: a cheap session decides whether the issue is an actionable bug.
   - **Investigating**: the Investigator reproduces the bug on master and finds the root cause.
   - **Remediating**: the Remediator writes a fix and regression tests that fail before the fix and pass after it.
   - **PR ready for review**: the fix PR is open and its CI is green.
   - **Related defect analysis**: the Analyst looks for the same pattern elsewhere and files follow-up issues. On this bug our run found the same unguarded division in `contribution.py`, and that follow-up issue went through the same steps.
4. Comment on the issue or the PR to steer the active session. A maintainer review comment that states a rule (for example "PRs must list affected chart types") appears under **Learned from reviews**.
5. Merge the PR. The workflow is marked completed.

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
