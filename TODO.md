# Roadmap / TODO

The deterministic multi-stage workflow is implemented. The Python engine owns
state transitions, persists workflows/sessions/events in SQLite, and uses
Triage, Investigator, Remediator, and Analyst Devin sessions with
structured-output gates. Intake is autonomous: no trigger label is required.

## Completed

- [x] **Dispatch deduplication** — `WorkflowEngine._dispatch` checks persisted
      role sessions and active-session capacity before creating work;
      `app/workflow.py`.
- [x] **End-to-end idempotency** — Devin creation sends `idempotent: true`,
      workflow/session uniqueness and fingerprints are persisted by
      `app/store.py`, and GitHub deliveries are deduplicated by delivery ID;
      `app/devin_client.py`, `app/workflow.py`, `app/store.py`.
- [x] **Mark remediation started** — remediation dispatch records
      `remediation_started_at` in the workflow; the legacy compatibility path
      also supports `REMEDIATION_LABEL` in `app/orchestrator.py`;
      `app/workflow.py`, `app/orchestrator.py`.
- [x] **Devin remediation workflow** — investigation, clarification, root
      cause, remediation, verification, PR reconciliation, and analyst
      follow-up are implemented with schemas, prompts, gates, comments, and
      tests; `app/workflow.py`, `app/prompts.py`, `tests/test_workflow.py`.
- [x] **Autonomous intake** — every open issue is discovered, persisted as
      `DISCOVERED`, deterministically filtered, and triaged by a cheap Devin
      session that routes to `QUEUED`, `NEEDS_INFO`, or `SKIPPED` with a
      reason; safety rails are `MAX_NEW_ISSUES_PER_POLL`,
      `MAX_CONCURRENT_DEVINS`, `ISSUE_LOOKBACK_DAYS`, `IGNORE_LABELS`, and
      `IGNORE_ISSUE_TYPES`; `app/workflow.py`, `app/config.py`.

## Remaining follow-ups

- [ ] **Duplicate detection across issues** — give triage a persisted index of
      prior workflows so `DUPLICATE` verdicts can cite the earlier issue
      reliably instead of relying on a single session's search.
- [ ] **Re-triage on issue edits** — re-evaluate `SKIPPED`/`NEEDS_INFO`
      workflows when the reporter substantially edits the issue body.
- [ ] **Dashboard authentication** — add an optional auth layer or integrate
      with the deployment's identity-aware proxy before exposing the dashboard
      publicly.
- [ ] **Per-repository configuration** — support an allowlist or separate
      settings when one deployment needs to serve multiple repositories.
- [ ] **Retry backoff tuning** — add exponential backoff, jitter, and a
      dead-letter path for repeated Devin/GitHub failures and poll timeouts.
- [ ] **Analyst follow-up PR automation** — define bounded policy for converting
      Analyst recommendations into follow-up issues or PRs with human approval.
