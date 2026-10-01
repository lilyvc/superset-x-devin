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

1. Choose a target repo, such as a Superset fork. Enable Issues under Settings → General → Features.
2. Connect GitHub in Devin with access to the repo, then add it to your [Devin environment](https://docs.devin.ai/onboard-devin/environment). Superset's [AGENTS.md](https://github.com/apache/superset/blob/master/AGENTS.md) covers build and test setup.
3. Create a service user under Settings → Devin API → Service users with session and Knowledge write permissions. Record its API key and org id (`org-…`).
4. Create a fine-grained GitHub token with Issues and Pull requests read/write, plus Checks and Commit statuses read.

## Start the service

You need Docker with Compose v2 (`docker compose`).

1. Clone this repo and `cd` into it.
2. Copy `.env.example` to `.env`.
3. Set `TARGET_REPO`, `GITHUB_TOKEN`, `DEVIN_API_KEY`, `DEVIN_ORG_ID`, and `ADMIN_TOKEN` in `.env`.
4. Run `docker compose up --build`.
5. Open `http://localhost:8000`. The **Setup problem** banner lists configuration errors.

Open issues are processed at startup. Set `POLL_BACKLOG=false` for new issues only.

To poll immediately:

```bash
set -a; . ./.env; set +a
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/admin/poll-now
```

Polling runs every 5 seconds by default and works locally. For instant delivery, expose `POST /webhooks/github` and configure `GITHUB_WEBHOOK_SECRET`. See [Operations](docs/OPERATIONS.md) for webhook events, configuration, and running without Docker.

## Try a sample bug

Sessions are real. Set `MAX_TOTAL_ACUS` to stop new sessions at the budget; running sessions finish under their own caps.

1. Start the service with `TARGET_REPO` set to your Superset fork.
2. File this issue:

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

3. Follow progress and session links in the dashboard. Updates also appear on the issue.
4. Comment to guide Devin. Try a maintainer review rule such as "PRs must list affected chart types" to see **Learned from reviews**.
5. Review and merge the fix PR to complete the workflow.

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
