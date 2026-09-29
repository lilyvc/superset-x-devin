# Roadmap / TODO

Planned follow-ups, roughly in order. v1 (current) does: issue opened → Devin
summarizes → comment posted → replies forward back into the same session.

## Dispatch & idempotency

- [ ] **Ensure the issue has not already been dispatched** — a basic `session_id`
      check exists; promote this to a real per-issue state machine
      (`received → dispatched → working → waiting_on_reply → summarized/remediating → done`)
      so retries, `reopened`, and race-y deliveries can't double-dispatch.
- [ ] **Enforce idempotency end-to-end** —
      - Devin session creation already sends `idempotent: true`; add a deterministic
        prompt/session key so a retried dispatch re-attaches to the existing session
        instead of relying on the store alone.
      - Make comment posting idempotent (e.g. upsert a single orchestrator comment
        per issue instead of appending duplicates on redelivery).
      - Deliveries are deduped by `X-GitHub-Delivery`; extend to a processed-event log
        if at-least-once semantics ever matter beyond retries.
- [ ] **Mark the issue as remediation started** — apply `REMEDIATION_LABEL`
      (implemented) and/or set an assignee + status comment so humans see dispatch
      state at a glance. Decide whether labeling waits for the remediation phase
      rather than the summary phase.

## Remediation workflow

- [ ] **Start the Devin remediation workflow** — after triage, dispatch a second
      session (or upgrade the triage session) that actually fixes the issue:
      branch → implement → open a PR on the target repo. Needs:
      - a remediation prompt template + playbook/knowledge selection
      - `structured_output_schema` for the result (pr_url, summary, tests run)
      - pulling `pull_request.url` from the finished session and commenting it
        on the issue
      - ACU limits / timeout policy per dispatch
- [ ] Decide the trigger: automatic after summary, vs. gated on a maintainer
      command (e.g. `/devin fix` comment or a label applied by a human).

## Conversation resume (partially implemented)

- [x] `issue_comment.created` on a tracked issue forwards the reply into the
      recorded Devin session — decided design: **same session resumes**, keyed by
      the SQLite `issue → session_id` map. New sessions are never created for replies.
- [ ] Handle the suspended case: `send_message` requires a running session —
      detect `blocked`/`suspend_requested` and resume before sending, or surface
      "session expired, redispatch?" back on the issue.
- [ ] Ignore the orchestrator's own comments (and bot accounts generally) so a
      posted summary/question can't be forwarded back into the session as a "reply".
- [ ] Distinguish a **question reply** from an explicit **command** (`/devin fix`,
      `/devin stop`) so humans can steer the workflow from the issue thread.

## Operations

- [ ] Exponential backoff + jitter on session polling; a dead-letter path when
      `POLL_TIMEOUT_SECONDS` is exceeded.
- [ ] Persist redelivered-but-unprocessed events for replay.
- [ ] Structured logging + metrics endpoint; alerting on `poll_failed` /
      `comment_failed` states.
- [ ] Multi-repo: keep `TARGET_REPO` as config now; later a repo allowlist with
      per-repo settings if one deployment should serve several repos.
- [ ] Optional: verify the GitHub App manifest route (installation tokens)
      instead of a PAT for production use.
