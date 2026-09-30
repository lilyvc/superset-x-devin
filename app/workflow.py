"""Deterministic, restart-safe multi-stage workflow engine."""

import logging
from datetime import datetime, timezone
from typing import ClassVar

from .config import Settings
from .devin_status import session_state
from .gates import ci_verdict, dedup_candidates, intake_skip_reason
from .github_client import GitHubClient
from .handlers import ROLE_HANDLERS, SettledSession
from .parsing import (
    fingerprint,
    issue_labels,
    linked_issue_numbers,
    parse_dt,
)
from .prompts import (
    ANALYSIS_SCHEMA,
    DEDUP_SCHEMA,
    INVESTIGATION_SCHEMA,
    REMEDIATION_SCHEMA,
    TRIAGE_SCHEMA,
    analyst_prompt,
    dedup_prompt,
    investigator_prompt,
    remediator_prompt,
    triage_prompt,
)
from .states import ACTIVE_STATES, TERMINAL_STATES, Origin, Role, SkipReason, State
from .store import Store, utcnow

logger = logging.getLogger("workflow")


class WorkflowEngine:
    def __init__(self, settings: Settings, store: Store, github: GitHubClient, devin):
        self.settings, self.store, self.github, self.devin = settings, store, github, devin
        self._startup_baseline: int | None = None
        self._github_login: str | None = None
        self._budget_event_sent = False

    async def tick(self) -> None:
        issues = []
        try:
            issues = await self.github.list_open_issues(self.settings.target_repo)
            await self._discover(issues)
        except Exception as exc:
            logger.exception("discovery failed")
            self.store.add_event(None, "error", detail=f"discovery: {exc}")
        try:
            await self._reconcile_sessions()
        except Exception as exc:
            logger.exception("session reconciliation failed")
            self.store.add_event(None, "error", detail=f"reconcile: {exc}")
        try:
            await self._reconcile_prs()
        except Exception as exc:
            logger.exception("pull request reconciliation failed")
            self.store.add_event(None, "error", detail=f"pull requests: {exc}")
        try:
            await self._replies()
        except Exception as exc:
            logger.exception("reply forwarding failed")
            self.store.add_event(None, "error", detail=f"replies: {exc}")
        try:
            await self._dispatch()
        except Exception as exc:
            logger.exception("dispatch failed")
            self.store.add_event(None, "error", detail=f"dispatch: {exc}")

    def _admit(self, issue: dict) -> tuple[dict, str | None]:
        provenance = self.store.get_issue_origin(
            self.settings.target_repo, issue["number"])
        workflow = self.store.upsert_workflow(
            self.settings.target_repo, issue["number"], title=issue.get("title", ""),
            issue_url=issue.get("html_url", ""),
            author=(issue.get("user") or {}).get("login", ""),
            labels=list(issue_labels(issue)), state=State.DISCOVERED.value,
            discovered_at=utcnow(),
            origin=(provenance or {}).get("origin", Origin.HUMAN_REPORTED.value),
            parent_issue_number=(provenance or {}).get("parent_issue_number"),
            discovered_by_session_id=(provenance or {}).get("session_id"),
        )
        reason = intake_skip_reason(issue, self.settings, datetime.now(timezone.utc))
        if reason:
            self.store.set_state(
                workflow["id"], State.SKIPPED,
                needs_info_kind=SkipReason.FILTERED.value,
                failure_reason=reason, completed_at=utcnow(),
            )
        elif not self.settings.triage_enabled:
            self.store.set_state(workflow["id"], State.QUEUED)
        return workflow, reason

    async def _discover(self, issues: list[dict]) -> None:
        numbers = {i.get("number") for i in issues}
        if self._startup_baseline is None:
            self._startup_baseline = max(numbers or {0})
        admitted = 0
        for issue in issues:
            if "pull_request" in issue:
                continue
            if not self.settings.poll_backlog and issue["number"] <= self._startup_baseline:
                continue
            if self.store.get_workflow(self.settings.target_repo, issue["number"]):
                continue
            if admitted >= self.settings.max_new_issues_per_poll:
                break
            admitted += 1
            self._admit(issue)

        open_ids = {i["number"] for i in issues}
        for workflow in self.store.list_workflows():
            if workflow["repo"] != self.settings.target_repo or workflow["issue_number"] in open_ids:
                continue
            if workflow["state"] in {s.value for s in TERMINAL_STATES}:
                continue
            try:
                issue = await self.github.get_issue(workflow["repo"], workflow["issue_number"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not reconcile closed issue #%s: %s",
                               workflow["issue_number"], exc)
                continue
            if issue.get("state") == "closed":
                if workflow["state"] in {State.PR_OPENED.value, State.CI_CHECKING.value,
                                        State.READY_FOR_REVIEW.value}:
                    self.store.set_state(workflow["id"], State.COMPLETED, completed_at=utcnow())
                else:
                    self.store.set_state(
                        workflow["id"], State.ESCALATED,
                        failure_reason="issue closed on GitHub before workflow finished",
                    )

    async def _reconcile_sessions(self) -> None:
        for session_row in self.store.list_active_sessions():
            try:
                await self._reconcile_session(session_row)
            except Exception as exc:
                logger.exception("session reconciliation failed for %s", session_row["session_id"])
                self.store.add_event(
                    session_row["workflow_id"], "error", detail=f"session {session_row['session_id']}: {exc}"
                )

    async def _reconcile_prs(self) -> None:
        states = {State.PR_OPENED.value, State.CI_CHECKING.value,
                  State.READY_FOR_REVIEW.value}
        for workflow in self.store.list_workflows(states):
            if not workflow.get("pr_number"):
                continue
            try:
                pull = await self.github.get_pull(workflow["repo"], workflow["pr_number"])
                head_sha = (pull.get("head") or {}).get("sha")
                if head_sha and not workflow.get("pr_head_sha"):
                    self.store.set_state(workflow["id"], workflow["state"],
                                         pr_head_sha=head_sha)
                    workflow["pr_head_sha"] = head_sha
                if pull.get("merged"):
                    clean = (
                        head_sha is not None
                        and workflow.get("pr_head_sha") == head_sha
                    )
                    self.store.set_state(workflow["id"], State.COMPLETED,
                                         completed_at=utcnow(),
                                         merged_without_changes=1 if clean else 0)
                    continue
                if pull.get("state") == "closed":
                    self.store.set_state(
                        workflow["id"], State.FAILED,
                        failure_reason="PR closed without merge",
                    )
                    continue
                if workflow["state"] == State.READY_FOR_REVIEW.value:
                    continue
                if self.settings.ci_required:
                    await self._reconcile_ci(workflow, pull)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not reconcile PR #%s: %s", workflow["pr_number"], exc)
                self.store.add_event(
                    workflow["id"], "error",
                    detail=f"pull request reconciliation: {exc}",
                )

    async def _reconcile_ci(self, workflow: dict, pull: dict) -> None:
        """Independent CI gate: READY_FOR_REVIEW only when the PR's actual
        GitHub checks are green — the Remediator's own verification claim is
        necessary but not sufficient."""
        head = (pull.get("head") or {}).get("sha")
        if not head:
            return
        data = await self.github.get_commit_checks(workflow["repo"], head)
        verdict, names = ci_verdict(data)
        previous = workflow.get("ci_status")
        now = utcnow()
        if verdict == "passed":
            self.store.set_state(workflow["id"], State.READY_FOR_REVIEW,
                                 ci_status="passed", ci_checked_at=now)
            await self.comment(
                workflow,
                f"**CI checks passed** — fix ready for review: {workflow.get('pr_url')}",
            )
            return
        opened = parse_dt(workflow.get("pr_opened_at"))
        timed_out = (
            opened is not None
            and self.settings.ci_timeout_seconds > 0
            and (datetime.now(timezone.utc) - opened).total_seconds()
            > self.settings.ci_timeout_seconds
        )
        if timed_out and not workflow.get("ci_timeout_notified"):
            self.store.set_state(workflow["id"], workflow["state"], ci_status=verdict,
                                 ci_checked_at=now, ci_timeout_notified=1)
            self.store.add_event(
                workflow["id"], "ci_timeout",
                detail=f"CI has not concluded within {self.settings.ci_timeout_seconds:.0f}s "
                       f"(status: {verdict}); check the PR manually or set CI_REQUIRED=false.",
            )
        else:
            self.store.set_state(workflow["id"], State.CI_CHECKING,
                                 ci_status=verdict, ci_checked_at=now)
        if verdict == "failed" and previous != "failed":
            await self.comment(
                workflow,
                "**CI checks are failing on the fix PR** "
                f"({workflow.get('pr_url')}): {', '.join(names[:10])}.\n\n"
                "_The workflow stays in CI checking — it only moves to ready for "
                "review once the checks pass._",
            )

    async def _reconcile_session(self, row: dict) -> None:
        response = await self.devin.get_session(row["session_id"])
        output = response.get("structured_output") or {}
        pulls = response.get("pull_requests") or []
        _, detail = session_state(response)
        fp = fingerprint(output)
        self.store.update_session(
            row["session_id"], devin_status=response.get("status_enum") or response.get("status"),
            devin_status_detail=response.get("status_detail") or detail,
            acus=response.get("acus_consumed")
            if response.get("acus_consumed") is not None
            else response.get("acu"),
            structured_output=output, output_fingerprint=fp, pull_requests=pulls,
            last_polled_at=utcnow(),
        )
        settled = detail in {"final", "waiting"}
        if not settled:
            await self._check_stalled(row)
            return
        if fp == row.get("acted_fingerprint"):
            return
        workflow = self.store.get_workflow_by_id(row["workflow_id"])
        if not workflow:
            return
        if row["role"] not in ROLE_HANDLERS:
            return
        settled = SettledSession(
            row=row, workflow=workflow, output=output, kind=detail,
            fingerprint=fp, response=response, pulls=pulls,
        )
        await ROLE_HANDLERS[row["role"]](self, settled)

    async def _check_stalled(self, row: dict) -> None:
        """A session that keeps running without ever settling would hold its
        concurrency slot forever, so nudge it once and then escalate."""
        limit = self.settings.session_stall_seconds
        started = parse_dt(row.get("created_at"))
        if limit <= 0 or started is None:
            return
        age = (datetime.now(timezone.utc) - started).total_seconds()
        if age < limit:
            return
        if age < limit * 2:
            if row.get("stall_nudged"):
                return
            self.store.update_session(row["session_id"], stall_nudged=1)
            self.store.add_event(
                row["workflow_id"], "session_stall_nudged",
                detail=f"{row['role']} session running for {age / 3600:.1f}h without a verdict",
            )
            await self.devin.send_message(
                row["session_id"],
                "Orchestrator: this session has been running without producing a verdict. "
                "Please stop any further work, and report what you have now by setting the "
                "structured output required by your instructions — including partial or "
                "negative results and any blockers.",
            )
            return
        self.store.update_session(row["session_id"], active=0, finished_at=utcnow())
        workflow = self.store.get_workflow_by_id(row["workflow_id"])
        if workflow and workflow["state"] not in TERMINAL_STATES:
            self.store.set_state(
                workflow["id"], State.ESCALATED,
                failure_reason=f"{row['role']} session stalled for {age / 3600:.1f}h "
                               "without structured output",
            )
            await self.comment(
                workflow,
                f"**Escalating to a human** — the {row['role']} Devin session "
                f"({row.get('url')}) ran for {age / 3600:.1f} hours without reporting a "
                "verdict, so the orchestrator stopped waiting on it.",
            )

    async def _replies(self):
        if self._github_login is None and not self.settings.dry_run:
            try:
                self._github_login = (await self.github.get_authenticated_user()).get("login")
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not fetch authenticated GitHub user: %s", exc)
                self._github_login = ""
        for workflow in self.store.list_workflows():
            if workflow["state"] not in (
                {s.value for s in {State.NEEDS_INFO, State.BLOCKED} | ACTIVE_STATES}
            ):
                continue
            active = self.store.get_sessions(workflow["id"], active_only=True)
            session = next((s for s in active if s["role"] in
                            {Role.INVESTIGATOR.value, Role.REMEDIATOR.value}), None)
            waiting = workflow["state"] in {State.NEEDS_INFO.value, State.BLOCKED.value}
            if not session and not waiting:
                continue
            comments = await self.github.list_issue_comments(workflow["repo"], workflow["issue_number"])
            last_id = workflow.get("last_comment_id") or 0
            for comment in sorted(comments, key=lambda c: c.get("id", 0)):
                if comment.get("id", 0) <= last_id:
                    continue
                last_id = comment["id"]
                user = comment.get("user") or {}
                login = user.get("login", "")
                if user.get("type") == "Bot" or login.endswith("[bot]") or login == self._github_login:
                    continue
                if session:
                    await self.devin.send_message(
                        session["session_id"],
                        f"A human reply was posted on GitHub issue #{workflow['issue_number']} by {login}:\n\n"
                        f"{comment.get('body', '')}",
                    )
                    self.store.add_event(workflow["id"], "human_reply_forwarded",
                                         detail={"comment_id": comment["id"], "session_id": session["session_id"]})
                    self.store.set_state(workflow["id"], State.INVESTIGATING if
                                         workflow["state"] in {State.NEEDS_INFO.value, State.BLOCKED.value}
                                         else workflow["state"], waiting_since=None)
                elif workflow["state"] == State.NEEDS_INFO.value:
                    # Clarification was asked by the (now finished) triage session: the
                    # reply is on the issue, so queue a fresh investigation that reads it.
                    self.store.set_state(workflow["id"], State.QUEUED, waiting_since=None)
                    self.store.add_event(
                        workflow["id"], "human_reply_queued_investigation",
                        detail={"comment_id": comment["id"]},
                    )
                elif workflow["state"] == State.BLOCKED.value:
                    # Blocked with no live session: resume the last role session
                    # if possible, otherwise start a fresh one with the context.
                    await self._resume_or_recover(workflow, comment, login)
                else:
                    self.store.add_event(
                        workflow["id"], "human_comment_unforwarded",
                        detail={"comment_id": comment["id"], "reason": "no active session"},
                    )
            current = self.store.get_workflow_by_id(workflow["id"])
            if current:
                self.store.set_state(workflow["id"], current["state"], last_comment_id=last_id)
        await self._pr_replies()

    _PR_REPLY_STATES: ClassVar[set[str]] = {
        State.REMEDIATING.value, State.VERIFYING.value, State.PR_OPENED.value,
        State.CI_CHECKING.value, State.READY_FOR_REVIEW.value,
    }

    async def _pr_replies(self) -> None:
        """Forward human feedback on the tracked remediation PR to the
        Remediator — a live session gets it directly, a settled one is
        resumed or recovered so it can address the review.

        Three comment surfaces exist on a PR, each in its own id namespace:
        conversation comments (issue comments on the PR number), inline
        review comments, and submitted review bodies."""
        for workflow in self.store.list_workflows():
            if workflow["state"] not in self._PR_REPLY_STATES or not workflow.get("pr_number"):
                continue
            repo, pr = workflow["repo"], workflow["pr_number"]
            reviews = await self.github.list_pull_reviews(repo, pr)
            sources = [
                ("last_pr_comment_id", await self.github.list_issue_comments(repo, pr), None),
                ("last_pr_review_comment_id", await self.github.list_pull_review_comments(repo, pr), None),
                ("last_pr_review_id",
                 [r for r in reviews
                  if r.get("body") and r.get("state") in {"CHANGES_REQUESTED", "COMMENTED"}],
                 "review"),
            ]
            for field, comments, kind in sources:
                last_id = workflow.get(field) or 0
                for comment in sorted(comments, key=lambda c: c.get("id", 0)):
                    if comment.get("id", 0) <= last_id:
                        continue
                    last_id = comment["id"]
                    user = comment.get("user") or {}
                    login = user.get("login", "")
                    if (user.get("type") == "Bot" or login.endswith("[bot]")
                            or login == self._github_login):
                        continue
                    await self._forward_pr_comment(workflow, comment, login, kind)
                current = self.store.get_workflow_by_id(workflow["id"])
                if current:
                    self.store.set_state(workflow["id"], current["state"], **{field: last_id})

    async def _forward_pr_comment(self, workflow: dict, comment: dict,
                                  login: str, kind: str | None) -> None:
        path = comment.get("path")
        location = (f" on {path}:{comment.get('line') or comment.get('original_line')}"
                    if path else "")
        where = "reviewed" if kind == "review" else "commented"
        note = (
            f"A reviewer {where} on PR #{workflow['pr_number']} "
            f"for issue #{workflow['issue_number']} (by {login}){location}:\n\n"
            f"{comment.get('body', '')}\n\n"
            "Address the feedback on the same PR branch and update the structured output."
        )
        session = next((s for s in self.store.get_sessions(workflow["id"], active_only=True)
                        if s["role"] == Role.REMEDIATOR.value), None)
        if session is not None:
            await self.devin.send_message(session["session_id"], note)
            self.store.add_event(workflow["id"], "pr_comment_forwarded",
                                 detail={"comment_id": comment.get("id"),
                                         "session_id": session["session_id"]})
            return
        last = next((s for s in reversed(self.store.get_sessions(workflow["id"]))
                     if s["role"] == Role.REMEDIATOR.value), None)
        if last is not None:
            try:
                await self.devin.send_message(last["session_id"], note)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not resume remediator %s: %s", last["session_id"], exc)
            else:
                self.store.update_session(last["session_id"], active=1, finished_at=None)
                self.store.set_state(workflow["id"], State.REMEDIATING, waiting_since=None)
                self.store.add_event(workflow["id"], "pr_comment_resumed_session",
                                     detail={"comment_id": comment.get("id"),
                                             "session_id": last["session_id"]})
                return
        context = (
            f"A reviewer {where} on the remediation PR {workflow.get('pr_url')} "
            f"(by {login}){location}: {comment.get('body', '')}\n"
            "The previous remediator session could not be resumed; address "
            "this feedback on the same PR."
        )
        await self._create_role_session(workflow, Role.REMEDIATOR, extra_context=context)
        self.store.add_event(workflow["id"], "pr_comment_recovery_session",
                             detail={"comment_id": comment.get("id")})

    _RESUMABLE_ROLES: ClassVar[set[str]] = {
        Role.INVESTIGATOR.value,
        Role.REMEDIATOR.value,
        Role.DEDUP.value,
    }

    async def _resume_or_recover(self, workflow: dict, comment: dict, login: str) -> None:
        """A blocked workflow got a human reply but has no live session.

        First try resuming the most recent role session — messaging a settled
        Devin session wakes it and preserves all prior context. If resumption
        is impossible (session expired/gone), start a fresh same-role session
        seeded with the blocker, the question asked, and the human's answer.
        """
        sessions = [s for s in self.store.get_sessions(workflow["id"])
                    if s["role"] in self._RESUMABLE_ROLES]
        last = sessions[-1] if sessions else None
        reply = (
            f"A human reply was posted on GitHub issue #{workflow['issue_number']} "
            f"by {login}:\n\n{comment.get('body', '')}\n\n"
            "Continue the work that was previously blocked and update the "
            "structured output."
        )
        if last is not None:
            try:
                await self.devin.send_message(last["session_id"], reply)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not resume session %s: %s",
                               last["session_id"], exc)
            else:
                self.store.update_session(last["session_id"], active=1, finished_at=None)
                resumed = {
                    Role.REMEDIATOR.value: State.REMEDIATING,
                    Role.DEDUP.value: State.ROOT_CAUSE_FOUND,
                }.get(last["role"], State.INVESTIGATING)
                self.store.set_state(workflow["id"], resumed, waiting_since=None)
                self.store.add_event(
                    workflow["id"], "human_reply_resumed_session",
                    detail={"comment_id": comment.get("id"),
                            "session_id": last["session_id"], "role": last["role"]},
                )
                return
        role = (Role.REMEDIATOR
                if last is not None and last["role"] == Role.REMEDIATOR.value
                else Role.INVESTIGATOR)
        context = (
            "The previous session for this workflow ended while it was blocked "
            "and could not be resumed, so you are continuing its work.\n"
            "Blocker/question the workflow was waiting on: "
            f"{workflow.get('failure_reason') or '(unspecified)'}\n"
            f"Human reply on the issue (by {login}): {comment.get('body', '')}\n"
            "The full question-and-answer thread is in the issue comments."
        )
        if role == Role.INVESTIGATOR:
            self.store.set_state(workflow["id"], State.INVESTIGATING, waiting_since=None)
        await self._create_role_session(workflow, role, extra_context=context)
        self.store.add_event(
            workflow["id"], "human_reply_recovery_session",
            detail={"comment_id": comment.get("id"), "role": role.value},
        )

    async def _dedup_gate(self, workflow: dict) -> str:
        """Pre-remediation reconciliation: is an open PR already fixing this
        root cause? Returns 'clear' (dispatch the Remediator), 'pending' (a
        dedup check is running or already ran), or 'duplicate' (resolved here).
        """
        sessions = self.store.get_sessions(workflow["id"], Role.DEDUP)
        if any(s.get("active") for s in sessions):
            return "pending"
        if sessions:
            # Settled dedup verdicts are applied by the role handler. Still being
            # in ROOT_CAUSE_FOUND means a PROCEED cleared the way; anything else
            # would have moved the workflow out of this state already.
            return "clear" if any(
                (s.get("structured_output") or {}).get("verdict") == "PROCEED"
                for s in sessions
            ) else "pending"
        try:
            pulls = await self.github.list_open_pulls(workflow["repo"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("dedup: could not list open PRs for %s: %s",
                           workflow["repo"], exc)
            self.store.add_event(workflow["id"], "dedup_skipped",
                                 detail=f"could not list open PRs: {exc}")
            return "clear"
        # Definitive: an open PR already links this issue as fixed.
        for pull in pulls:
            text = f"{pull.get('title') or ''}\n{pull.get('body') or ''}"
            linked = linked_issue_numbers(text)
            if workflow["issue_number"] in linked:
                pr_url = pull.get("html_url", "")
                self.store.set_state(
                    workflow["id"], State.SKIPPED,
                    needs_info_kind=SkipReason.DUPLICATE.value,
                    failure_reason=f"open PR already linked to this issue: {pr_url}",
                    completed_at=utcnow(),
                )
                self.store.add_event(workflow["id"], "dedup_hit", detail=pr_url)
                await self.comment(
                    workflow,
                    "**Skipped — duplicate remediation avoided**\n\n"
                    f"An open PR already references this issue: {pr_url}. "
                    "No remediation session was started.",
                )
                return "duplicate"
        # Ambiguous: cheap token/file overlap produces candidates; a small
        # Dedup Devin decides whether any of them fixes the same root cause.
        candidates = dedup_candidates(workflow, pulls)
        if not candidates:
            return "clear"
        blocks = []
        for pull, shared in candidates[:5]:
            try:
                files = await self.github.list_pull_files(workflow["repo"], pull["number"])
            except Exception:  # noqa: BLE001
                files = []
            blocks.append(
                f"- PR #{pull['number']}: {pull.get('title', '')} — {pull.get('html_url', '')}\n"
                f"  shared signals: {', '.join(sorted(shared))}\n"
                f"  changed files: {', '.join(files[:20]) or 'unavailable'}\n"
                f"  body excerpt: {(pull.get('body') or '')[:400]}"
            )
        self.store.add_event(workflow["id"], "dedup_candidates",
                             detail=[p.get("html_url") for p, _ in candidates[:5]])
        await self._create_role_session(workflow, Role.DEDUP,
                                        prompt_extra={"candidates": blocks})
        return "dispatched"

    def _budget_exceeded(self) -> bool:
        return (
            self.settings.max_total_acus is not None
            and self.store.total_acus() >= self.settings.max_total_acus
        )

    async def _dispatch(self):
        if self._budget_exceeded():
            if not self._budget_event_sent:
                self._budget_event_sent = True
                self.store.add_event(
                    None, "budget_exceeded",
                    detail=f"MAX_TOTAL_ACUS {self.settings.max_total_acus} reached; "
                           "no new Devin sessions will be dispatched.",
                )
            return
        capacity = max(0, self.settings.max_concurrent_devins - self.store.count_active_sessions())
        if not capacity:
            return
        workflows = self.store.list_workflows()
        for workflow in workflows:
            if capacity <= 0:
                break
            if workflow["state"] == State.ROOT_CAUSE_FOUND.value and not self.store.get_sessions(
                workflow["id"], Role.REMEDIATOR, active_only=True
            ):
                if self.settings.dedup_enabled:
                    verdict = await self._dedup_gate(workflow)
                    if verdict != "clear":
                        # 'dispatched' consumed a concurrency slot this tick;
                        # 'pending'/'duplicate' did not create new work.
                        capacity -= verdict == "dispatched"
                        continue
                await self._create_role_session(workflow, Role.REMEDIATOR)
                capacity -= 1
        if self.settings.analysis_enabled:
            for workflow in workflows:
                if capacity <= 0:
                    break
                # Defect-family analysis only runs once a fix PR exists —
                # investigations/remediations that never produce one aren't
                # worth the ACUs.
                if workflow["state"] in {s.value for s in
                                         {State.PR_OPENED, State.CI_CHECKING,
                                          State.READY_FOR_REVIEW, State.COMPLETED}} and not self.store.get_sessions(
                                              workflow["id"], Role.ANALYST
                                          ):
                    await self._create_role_session(workflow, Role.ANALYST)
                    capacity -= 1
        for workflow in workflows:
            if capacity <= 0:
                break
            if workflow["state"] == State.QUEUED.value and not self.store.get_sessions(
                workflow["id"], Role.INVESTIGATOR, active_only=True
            ):
                self.store.set_state(workflow["id"], State.INVESTIGATING, started_at=utcnow())
                await self._create_role_session(workflow, Role.INVESTIGATOR)
                capacity -= 1
        if not self.settings.triage_enabled:
            return
        for workflow in workflows:
            if capacity <= 0:
                break
            if workflow["state"] == State.DISCOVERED.value and not self.store.get_sessions(
                workflow["id"], Role.TRIAGE, active_only=True
            ):
                self.store.set_state(workflow["id"], State.TRIAGING, triaged_started_at=utcnow())
                await self._create_role_session(workflow, Role.TRIAGE)
                capacity -= 1

    async def _create_role_session(
        self,
        workflow: dict,
        role: Role,
        *,
        extra_context: str | None = None,
        prompt_extra: dict | None = None,
    ) -> dict | None:
        if self._budget_exceeded():
            self.store.add_event(
                workflow["id"], "dispatch_skipped_budget",
                detail=f"{role.value} session skipped: MAX_TOTAL_ACUS "
                       f"{self.settings.max_total_acus} reached",
            )
            return None
        issue = await self.github.get_issue(workflow["repo"], workflow["issue_number"])
        comments = await self.github.list_issue_comments(workflow["repo"], workflow["issue_number"])
        prompt, schema, limit = self._role_prompt(
            workflow, role, issue, comments, prompt_extra,
        )
        if role == Role.REMEDIATOR:
            self.store.set_state(workflow["id"], State.REMEDIATING,
                                 remediation_started_at=utcnow(), waiting_since=None)
        if extra_context:
            prompt += f"\n\n## Recovery context\n{extra_context}\n"
        tags = ["superset-x-devin", f"repo:{workflow['repo']}",
                f"issue:{workflow['issue_number']}", f"role:{role.value}"]
        session = await self.devin.create_session(
            prompt, title=f"[{role.value}] {workflow['repo']}#{workflow['issue_number']}: {workflow.get('title', '')[:60]}",
            tags=tags, structured_output_schema=schema,
            max_acu_limit=limit if self.settings.max_acu_limit is None else min(limit, self.settings.max_acu_limit),
        )
        session_url = session.get("url") or (
            f"https://app.devin.ai/sessions/"
            f"{session['session_id'].removeprefix('devin-')}"
        )
        self.store.record_session(session_id=session["session_id"], workflow_id=workflow["id"],
                                  role=role.value, url=session_url, active=1)
        self.store.add_event(workflow["id"], "session_created", detail={"session_id": session["session_id"],
                                                                        "role": role.value})
        return session

    def _role_prompt(
        self,
        workflow: dict,
        role: Role,
        issue: dict,
        comments: list[dict],
        prompt_extra: dict | None,
    ) -> tuple[str, dict, int]:
        inv = workflow.get("investigation") or {}
        if role == Role.TRIAGE:
            return (
                triage_prompt(workflow["repo"], issue, comments),
                TRIAGE_SCHEMA,
                self.settings.triage_acu_limit,
            )
        if role == Role.INVESTIGATOR:
            return (
                investigator_prompt(workflow["repo"], issue, comments),
                INVESTIGATION_SCHEMA,
                self.settings.investigator_acu_limit,
            )
        if role == Role.REMEDIATOR:
            investigator = next(iter(self.store.get_sessions(workflow["id"], Role.INVESTIGATOR)), {})
            return (
                remediator_prompt(
                    workflow["repo"], issue, inv, investigator.get("url", ""),
                ),
                REMEDIATION_SCHEMA,
                self.settings.remediator_acu_limit,
            )
        if role == Role.DEDUP:
            return (
                dedup_prompt(workflow["repo"], issue, inv,
                             (prompt_extra or {}).get("candidates") or []),
                DEDUP_SCHEMA,
                self.settings.dedup_acu_limit,
            )
        return (
            analyst_prompt(workflow["repo"], issue, inv),
            ANALYSIS_SCHEMA,
            self.settings.analyst_acu_limit,
        )

    async def comment(self, workflow: dict, body: str):
        if self.settings.dry_run:
            logger.info("[dry-run] would comment on #%s: %s", workflow["issue_number"], body)
            self.store.add_event(workflow["id"], "comment_posted", detail=body)
            return
        comment = await self.github.post_issue_comment(workflow["repo"], workflow["issue_number"], body)
        self.store.add_event(workflow["id"], "comment_posted", detail={"comment_id": comment.get("id")})

    async def handle_issue_event(self, payload: dict) -> dict:
        if payload.get("action") not in {"opened", "reopened"}:
            return {"handled": False, "reason": "unsupported action"}
        issue = payload.get("issue") or {}
        repo = (payload.get("repository") or {}).get("full_name", self.settings.target_repo)
        if repo != self.settings.target_repo or "pull_request" in issue:
            return {"handled": False, "reason": "ineligible repository or pull request"}
        workflow = self.store.get_workflow(repo, issue["number"])
        if workflow:
            await self.tick()
            return {"handled": True, "workflow_id": workflow["id"]}
        workflow, reason = self._admit(issue)
        if reason:
            return {"handled": False, "reason": reason}
        await self.tick()
        return {"handled": True, "workflow_id": workflow["id"]}

    async def handle_issue_comment_event(self, payload: dict) -> dict:
        if payload.get("action") != "created":
            return {"handled": False, "reason": "not a created comment"}
        await self.tick()
        return {"handled": True}
