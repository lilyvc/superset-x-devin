# Roadmap / TODO

The deterministic multi-stage workflow is implemented. The Python engine owns
state transitions, persists workflows/sessions/events in SQLite, and uses
Investigator, Remediator, and Analyst Devin sessions with structured-output
gates.

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

## Remaining follow-ups

- [ ] **Webhook parity for label events** — handle label-added/removed events
      directly so eligibility changes can trigger discovery without waiting for
      the next poll.
- [ ] **Dashboard authentication** — add an optional auth layer or integrate
      with the deployment's identity-aware proxy before exposing the dashboard
      publicly.
- [ ] **Per-repository configuration** — support an allowlist or separate
      settings when one deployment needs to serve multiple repositories.
- [ ] **Retry backoff tuning** — add exponential backoff, jitter, and a
      dead-letter path for repeated Devin/GitHub failures and poll timeouts.
- [ ] **Analyst follow-up PR automation** — define bounded policy for converting
      Analyst recommendations into follow-up issues or PRs with human approval.
