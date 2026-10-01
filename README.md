# superset-x-devin: GitHub issue-to-fix service

A service that autonomously investigates incoming issues and fixes bugs in `TARGET_REPO` using Devin sessions. Devin triages, reproduces, and remediates issues, then opens PRs for human review. When appropriate, a follow up agent looks for similar defects elsewhere in the repo or deeper root causes. A dashboard tracks every issue's progress, Devin sessions, and evidence.

## How it works

1. The service finds open issues through polling or GitHub webhooks.
2. Intake filters reject issues that match configured rules. Triage sends each other issue to investigation, clarification, or `SKIPPED`.
3. The Investigator reproduces the issue and returns structured evidence.
4. Python checks the evidence and checks open PRs for an existing fix.
5. The Remediator prepares a fix and reports test results.
6. Python checks the Remediator's evidence and the PR's GitHub CI results.
7. The workflow reaches `READY_FOR_REVIEW` when required checks pass. If CI fails, the Remediator reads the logs: it fixes its own breakage, or opens a separate CI-fix PR when the failure isn't caused by the fix, then updates the fix PR once that merges.
8. Once a fix PR exists, the Related Defect Analysis Devin searches for related defect patterns and files follow-up issues.
9. The service marks a merged PR as `COMPLETED`. It asks a human for help when a workflow is `NEEDS_INFO` or `BLOCKED`.
10. Comments on tracked issues and PRs reach the active Devin session — or resume it when it has finished.
11. When a maintainer's PR review states a standing rule ("PRs must include X"), it is saved as a [Devin Knowledge](https://docs.devin.ai/product-guides/knowledge) note pinned to the repo, so every future session follows it.

The service can also mark an issue `NOT_REPRODUCIBLE`, `SKIPPED`, `FAILED`, or `ESCALATED`.
Each role runs as its own Devin session with a per-role agent mode and ACU cap (cheap modes for triage/dedup, `fusion` for remediation — see [Operations](docs/OPERATIONS.md)). The system gets better the more it is used: completed analyses feed back into new prompts as known defect families, and maintainer review rules become Devin Knowledge (listed under "Learned from reviews" on the dashboard; see [Architecture](docs/ARCHITECTURE.md#learning-from-reviews)).

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
