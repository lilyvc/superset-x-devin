# Plan

## Status

- The service discovers and filters GitHub issues, then routes them through Triage and investigation.
- The Investigator must provide reproduction and root-cause evidence before remediation.
- The Remediator must pass output checks before the workflow accepts a PR.
- The CI gate checks GitHub results before it marks a PR ready for review.
- The duplicate gate checks open PRs before it starts remediation.
- Human replies can resume an active session or start a recovery session.
- Admin requests and webhooks use bearer-token and signature checks.
- ACU limits, issue provenance, the executive dashboard, and the architecture split are implemented.
- Workflow uniqueness, webhook delivery IDs, and session fingerprints support idempotent processing.

## Next work

- Add an issue index so triage can cite earlier related issues.
- Re-triage a skipped or waiting workflow after a reporter edits the issue.
- Add dashboard authentication or require an identity-aware proxy.
- Add per-repository settings for deployments that serve more than one repository.
- Add retry backoff, jitter, and a dead-letter path for repeated API failures and poll timeouts.
- Define a human-approved policy for Analyst follow-up issues or PRs.
- Limit clarification rounds before the workflow escalates to a human.
- **Proposal:** Add optional Devin Org Knowledge notes about issue patterns for later investigations.
- **Proposal:** Add scheduled Devin code scans for proactive correctness checks.

## Non-goals

- Do not make webhooks the only intake method. Polling avoids a public service URL.
- Do not add Kafka, Redis, Celery, or Kubernetes for the current single-process service.
- Do not merge PRs automatically. Keep a human review before merge.
- Do not serve multiple repositories from one process.
- Do not add dashboard authentication to the service now. Protect dashboard access at the network or proxy.

## Known risks and limits

- Devin sessions can run for a long time and consume ACUs. Limits reduce cost risk but do not remove it.
- Structured output can be wrong. Evidence gates and human PR review reduce this risk but do not replace review.
- GitHub rate limits and delayed API updates can slow work.
- One SQLite database and one service process limit scale and availability.
- Polling stops when the host or service stops.
- The service can wait for a human when issue evidence is incomplete or a gate cannot decide.
