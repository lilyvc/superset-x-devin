"""Deterministic, restart-safe multi-stage workflow engine."""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, ClassVar

from .config import Settings
from .devin_status import _last_devin_message, _session_state
from .github_client import GitHubClient
from .prompts import (
    ANALYSIS_SCHEMA,
    DEDUP_SCHEMA,
    INVESTIGATION_SCHEMA,
    REMEDIATION_SCHEMA,
    TRIAGE_SCHEMA,
    analyst_prompt,
    clarification_comment,
    dedup_prompt,
    investigator_prompt,
    remediator_prompt,
    skip_comment,
    triage_prompt,
)
from .states import ACTIVE_STATES, TERMINAL_STATES, Role, SkipReason, State
from .store import Store, utcnow

logger = logging.getLogger("workflow")


def investigation_gate(out: dict) -> tuple[bool, list[str]]:
    reasons = []
    required = {
        "status": "REPRODUCED",
        "enough_information": True,
        "reproduced": True,
        "expected_behavior": "non-empty",
        "observed_behavior": "non-empty",
        "reproduction_steps": "at least one item",
        "reproduction_evidence": "at least one item",
        "root_cause": "non-empty",
        "verification_plan": "at least one item",
    }
    for key, expected in required.items():
        value = out.get(key)
        if expected == "non-empty" and (not isinstance(value, str) or not value.strip()):
            reasons.append(f"{key} must be non-empty")
        elif expected == "at least one item" and (not isinstance(value, list) or not value):
            reasons.append(f"{key} must contain at least one item")
        elif expected is True and value is not True:
            reasons.append(f"{key} must be true")
        elif expected == "REPRODUCED" and value != expected:
            reasons.append("status must be REPRODUCED")
    return not reasons, reasons


def verification_gate(out: dict, pr_url: str | None) -> tuple[bool, list[str]]:
    reasons = []
    if out.get("status") != "PR_OPENED":
        reasons.append("status must be PR_OPENED")
    if not out.get("verification_passed"):
        reasons.append("verification_passed must be true")
    if not out.get("reproduction_rerun_passed"):
        reasons.append("reproduction_rerun_passed must be true")
    tests = out.get("tests_executed")
    if not isinstance(tests, list) or not tests:
        reasons.append("tests_executed must contain at least one test")
    elif any(not isinstance(test, dict) or test.get("result") != "passed" for test in tests):
        reasons.append("all tests_executed results must be passed")
    if not pr_url:
        reasons.append("pr_url is required")
    return not reasons, reasons


_FAILING_CONCLUSIONS = {"failure", "timed_out", "action_required", "cancelled"}


def _ci_verdict(data: dict) -> tuple[str, list[str]]:
    """Aggregate GitHub check-runs + commit statuses into one verdict.

    Returns (verdict, names) where verdict is passed | failed | pending |
    unverified (no checks configured at all).
    """
    runs = data.get("check_runs") or []
    statuses = data.get("statuses") or []
    if not runs and not statuses:
        return "unverified", []
    failing, pending = [], []
    for run in runs:
        name = run.get("name") or "check"
        if run.get("status") != "completed":
            pending.append(name)
        elif run.get("conclusion") in _FAILING_CONCLUSIONS:
            failing.append(name)
    for status in statuses:
        context = status.get("context") or "status"
        if status.get("state") in {"failure", "error"}:
            failing.append(context)
        elif status.get("state") == "pending":
            pending.append(context)
    if failing:
        return "failed", failing
    if pending:
        return "pending", pending
    return "passed", []


# Matches explicit fix references in PR bodies/titles: "fixes #54", "close #54", ...
_FIX_LINK_RE = re.compile(r"(?:fix(?:e[sd])?|fixing|close[sd]?|closing|resolve[sd]?|resolving)"
                          r"[:\s]+#?(\d+)", re.IGNORECASE)

_STOPWORDS = {
    "with", "that", "this", "from", "have", "been", "when", "where", "which",
    "would", "could", "should", "into", "their", "there", "about", "after",
    "before", "issue", "does", "http", "https", "com", "github",
}


def _tokens(text: str) -> set[str]:
    return {
        word for word in re.findall(r"[a-z0-9_./-]{4,}", (text or "").lower())
        if word not in _STOPWORDS
    }


def _labels(issue: dict) -> set[str]:
    return {
        item if isinstance(item, str) else item.get("name", "")
        for item in issue.get("labels") or []
    }


def _issue_type(issue: dict) -> str:
    issue_type = issue.get("type")
    if isinstance(issue_type, dict):
        return (issue_type.get("name") or "").lower()
    return (issue_type or "").lower() if isinstance(issue_type, str) else ""


def _fingerprint(output: Any) -> str:
    return hashlib.sha256(json.dumps(output or {}, sort_keys=True).encode()).hexdigest()


def _now_ts() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class WorkflowEngine:
    def __init__(self, settings: Settings, store: Store, github: GitHubClient, devin):
        self.settings, self.store, self.github, self.devin = settings, store, github, devin
        self._startup_baseline: int | None = None
        self._github_login: str | None = None

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

    def _intake_filter(self, issue: dict) -> str | None:
        """Cheap deterministic pre-filter; returns a skip reason detail or None."""
        settings = self.settings
        if settings.eligibility_label and settings.eligibility_label not in _labels(issue):
            return f"missing eligibility label {settings.eligibility_label}"
        ignored = {label.lower() for label in _labels(issue)} & set(settings.ignore_labels)
        if ignored:
            return f"ignored label(s): {', '.join(sorted(ignored))}"
        issue_type = _issue_type(issue)
        if issue_type and issue_type in settings.ignore_issue_types:
            return f"ignored issue type: {issue_type}"
        if settings.issue_lookback_days is not None:
            created = _parse_dt(issue.get("created_at"))
            if created and (_now_ts() - created).days > settings.issue_lookback_days:
                return f"older than lookback of {settings.issue_lookback_days} days"
        return None

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
            workflow = self.store.upsert_workflow(
                self.settings.target_repo, issue["number"], title=issue.get("title", ""),
                issue_url=issue.get("html_url", ""),
                author=(issue.get("user") or {}).get("login", ""),
                labels=list(_labels(issue)), state=State.DISCOVERED.value,
                discovered_at=utcnow(),
            )
            filtered = self._intake_filter(issue)
            if filtered:
                self.store.set_state(
                    workflow["id"], State.SKIPPED,
                    needs_info_kind=SkipReason.FILTERED.value,
                    failure_reason=filtered, completed_at=utcnow(),
                )
            elif not self.settings.triage_enabled:
                self.store.set_state(workflow["id"], State.QUEUED)

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
                if pull.get("merged"):
                    self.store.set_state(workflow["id"], State.COMPLETED,
                                         completed_at=utcnow())
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
        verdict, names = _ci_verdict(data)
        previous = workflow.get("ci_status")
        now = utcnow()
        if verdict == "passed":
            self.store.set_state(workflow["id"], State.READY_FOR_REVIEW,
                                 ci_status="passed", ci_checked_at=now)
            await self._comment(
                workflow,
                f"**CI checks passed** — fix ready for review: {workflow.get('pr_url')}",
            )
            return
        opened = _parse_dt(workflow.get("pr_opened_at"))
        timed_out = (
            opened is not None
            and self.settings.ci_timeout_seconds > 0
            and (_now_ts() - opened).total_seconds() > self.settings.ci_timeout_seconds
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
            await self._comment(
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
        status, detail = _session_state(response)
        fp = _fingerprint(output)
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
        role = row["role"]
        if role == Role.TRIAGE.value:
            await self._handle_triage(row, workflow, output, detail, fp)
        elif role == Role.INVESTIGATOR.value:
            await self._handle_investigator(row, workflow, output, status, detail, fp)
        elif role == Role.REMEDIATOR.value:
            await self._handle_remediator(row, workflow, output, status, detail, fp, pulls)
        elif role == Role.ANALYST.value:
            await self._handle_analyst(row, workflow, output, fp)
        elif role == Role.DEDUP.value:
            await self._handle_dedup(row, workflow, output, detail, fp)

    async def _check_stalled(self, row: dict) -> None:
        """A session that keeps running without ever settling would hold its
        concurrency slot forever, so nudge it once and then escalate."""
        limit = self.settings.session_stall_seconds
        started = _parse_dt(row.get("created_at"))
        if limit <= 0 or started is None:
            return
        age = (_now_ts() - started).total_seconds()
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
            await self._comment(
                workflow,
                f"**Escalating to a human** — the {row['role']} Devin session "
                f"({row.get('url')}) ran for {age / 3600:.1f} hours without reporting a "
                "verdict, so the orchestrator stopped waiting on it.",
            )

    async def _handle_triage(self, row, workflow, out, session_kind, fp):
        """Cheap intake verdict: queue for investigation, ask the reporter, or skip."""
        self.store.set_state(workflow["id"], workflow["state"], triage=out)
        finish = {"active": 0, "finished_at": utcnow(), "acted_fingerprint": fp}
        if not out:
            # No verdict: fall through to investigation rather than dropping the issue.
            self.store.set_state(
                workflow["id"], State.QUEUED,
                failure_reason=f"triage produced no verdict ({session_kind}); queued anyway",
            )
            self.store.update_session(row["session_id"], **finish)
            return
        verdict = (out.get("verdict") or "").upper()
        rationale = out.get("rationale") or ""
        if verdict == "SKIP":
            reason = out.get("skip_reason") or SkipReason.UNSUITABLE.value
            detail = f"{reason}: {rationale}"
            if out.get("duplicate_of"):
                detail += f" (duplicate of {out['duplicate_of']})"
            self.store.set_state(workflow["id"], State.SKIPPED, needs_info_kind=reason,
                                 failure_reason=detail, completed_at=utcnow(), triage=out)
            await self._comment(workflow, skip_comment(reason, rationale, row.get("url", "")))
        elif verdict == "NEEDS_INFO":
            question = out.get("clarification_question") or (
                "Could you add the missing details (Superset version, exact steps, and what you "
                "expected to happen)?"
            )
            self.store.set_state(workflow["id"], State.NEEDS_INFO,
                                 needs_info_kind=out.get("needs_info_kind"),
                                 waiting_since=utcnow(), triage=out)
            await self._comment(
                workflow,
                clarification_comment(question, out.get("needs_info_kind"), row.get("url", "")),
            )
        else:
            self.store.set_state(workflow["id"], State.QUEUED, triage=out)
        self.store.update_session(row["session_id"], **finish)

    async def _handle_investigator(self, row, workflow, out, status, session_kind, fp):
        self.store.set_state(workflow["id"], workflow["state"], investigation=out)
        if not out:
            if session_kind == "waiting":
                question = _last_devin_message(
                    await self.devin.get_session(row["session_id"])
                ) or "Please provide more information so the investigation can continue."
                self.store.set_state(
                    workflow["id"], State.NEEDS_INFO, needs_info_kind=None,
                    waiting_since=utcnow(), investigation=out,
                )
                await self._comment(
                    workflow, clarification_comment(question, None, row.get("url", "")),
                )
                self.store.update_session(row["session_id"], acted_fingerprint=fp)
            else:
                self.store.set_state(
                    workflow["id"], State.FAILED,
                    failure_reason="investigator session ended without structured output",
                )
                self.store.update_session(row["session_id"], active=0, finished_at=utcnow(),
                                          acted_fingerprint=fp)
            return
        kind = out.get("status")
        question = out.get("clarification_question") or "Please provide more information."
        if kind == "NEEDS_INFO" or (not out.get("enough_information") and out.get("clarification_question")):
            self.store.set_state(
                workflow["id"], State.NEEDS_INFO, needs_info_kind=out.get("needs_info_kind"),
                waiting_since=utcnow(), investigation=out,
            )
            await self._comment(
                workflow, clarification_comment(question,
                                                out.get("needs_info_kind"), row.get("url", "")),
            )
            self.store.update_session(row["session_id"], acted_fingerprint=fp)
            return
        if kind == "NOT_REPRODUCIBLE":
            self.store.set_state(workflow["id"], State.NOT_REPRODUCIBLE, investigation=out)
            await self._comment(workflow, f"**Issue investigation: not reproducible**\n\n"
                                f"{out.get('summary', '')}\n\n{out.get('observed_behavior', '')}")
            self.store.update_session(row["session_id"], active=0, finished_at=utcnow(),
                                      acted_fingerprint=fp)
            return
        if kind == "BLOCKED":
            reason = "; ".join(out.get("missing_information") or [out.get("summary", "Investigator blocked")])
            self.store.set_state(workflow["id"], State.BLOCKED, failure_reason=reason,
                                 waiting_since=utcnow(), investigation=out)
            await self._comment(workflow, f"**Investigation blocked**\n\n{reason}")
            self.store.update_session(row["session_id"], acted_fingerprint=fp)
            return
        ok, reasons = investigation_gate(out)
        if not ok:
            attempts = int(row.get("attempts") or 0)
            if attempts == 0:
                await self.devin.send_message(row["session_id"],
                    "Please fill the missing structured-output fields: " + "; ".join(reasons))
                self.store.update_session(row["session_id"], attempts=1, acted_fingerprint=fp)
            else:
                self.store.set_state(workflow["id"], State.BLOCKED,
                                     failure_reason="; ".join(reasons), waiting_since=utcnow())
                self.store.update_session(row["session_id"], active=0, finished_at=utcnow(),
                                          acted_fingerprint=fp, attempts=attempts + 1)
            return
        now = utcnow()
        self.store.set_state(workflow["id"], State.REPRODUCED, investigation=out, reproduced_at=now)
        self.store.set_state(workflow["id"], State.ROOT_CAUSE_FOUND, root_cause_at=now,
                             investigated_at=now)
        evidence = "\n".join(f"- {x}" for x in out.get("reproduction_evidence", []))
        await self._comment(workflow, f"**Reproduced + root cause** ([investigation]({row.get('url', '')}))\n\n"
                            f"{out.get('summary', '')}\n\n**Root cause:** {out.get('root_cause', '')}\n\n"
                            f"**Evidence:**\n{evidence}")
        self.store.update_session(row["session_id"], active=0, finished_at=utcnow(),
                                  acted_fingerprint=fp)

    async def _handle_remediator(self, row, workflow, out, status, session_kind, fp, pulls):
        pr_url = out.get("pr_url") or self._pull_url(pulls)
        self.store.set_state(workflow["id"], workflow["state"], remediation=out)
        if not out:
            if session_kind == "waiting":
                reason = _last_devin_message(
                    await self.devin.get_session(row["session_id"])
                ) or "Remediator is waiting for human input."
                self.store.set_state(
                    workflow["id"], State.BLOCKED, failure_reason=reason,
                    waiting_since=utcnow(),
                )
                await self._comment(workflow, f"**Remediation blocked**\n\n{reason}")
                self.store.update_session(row["session_id"], acted_fingerprint=fp)
            else:
                self.store.set_state(
                    workflow["id"], State.FAILED,
                    failure_reason="remediator session ended without structured output",
                )
                self.store.update_session(row["session_id"], active=0, finished_at=utcnow(),
                                          acted_fingerprint=fp)
            return
        if out.get("status") == "BLOCKED":
            reason = "; ".join(out.get("blockers") or [out.get("summary", "Remediator blocked")])
            self.store.set_state(workflow["id"], State.BLOCKED, failure_reason=reason, waiting_since=utcnow())
            await self._comment(workflow, f"**Remediation blocked**\n\n{reason}")
            self.store.update_session(row["session_id"], active=0, finished_at=utcnow(),
                                      acted_fingerprint=fp)
            return
        ok, reasons = verification_gate(out, pr_url)
        if ok:
            pr_number = self._pr_number(pr_url)
            now = utcnow()
            self.store.set_state(workflow["id"], State.PR_OPENED, remediation=out,
                                 pr_url=pr_url, pr_number=pr_number, pr_opened_at=now)
            tests = "\n".join(f"- {t.get('command', '')}" for t in out.get("tests_executed", []))
            evidence = "\n".join(f"- {x}" for x in out.get("verification_evidence", []))
            if self.settings.ci_required:
                self.store.set_state(workflow["id"], State.CI_CHECKING, ci_status="pending")
                await self._comment(workflow, f"**Fix opened:** {pr_url}\n\n"
                                    f"**Tests:**\n{tests}\n\n**Evidence:**\n{evidence}\n\n"
                                    "_Agent-verified — the workflow moves to ready for review "
                                    "only once the PR's GitHub checks pass._")
            else:
                self.store.set_state(workflow["id"], State.READY_FOR_REVIEW,
                                     ci_status="skipped")
                await self._comment(workflow, f"**Fix ready for review:** {pr_url}\n\n"
                                    f"**Tests:**\n{tests}\n\n**Evidence:**\n{evidence}")
            self.store.update_session(row["session_id"], active=0, finished_at=utcnow(),
                                      acted_fingerprint=fp)
            return
        self.store.set_state(workflow["id"], State.VERIFYING, remediation=out)
        attempts = int(workflow.get("remediation_attempts") or 0)
        if attempts < self.settings.max_remediation_attempts:
            await self.devin.send_message(row["session_id"],
                "Fix verification failed: " + "; ".join(reasons) +
                "; iterate and update structured output.")
            self.store.set_state(workflow["id"], State.REMEDIATING,
                                 remediation_attempts=attempts + 1)
            self.store.update_session(row["session_id"], acted_fingerprint=fp)
        else:
            self.store.set_state(workflow["id"], State.FAILED,
                                 failure_reason="; ".join(reasons))
            await self._comment(workflow, "**Fix verification failed**\n\n" + "; ".join(reasons))
            self.store.update_session(row["session_id"], active=0, finished_at=utcnow(),
                                      acted_fingerprint=fp)

    async def _handle_analyst(self, row, workflow, out, fp):
        self.store.set_state(workflow["id"], workflow["state"], analysis=out)
        await self._comment(workflow, f"**Engineering analysis**\n\n"
                            f"Systemic risk: {out.get('systemic_risk', 'unknown')}\n\n"
                            f"{out.get('summary', '')}\n\n"
                            f"Recommended follow-up: {out.get('recommended_followup', '')}\n"
                            f"{out.get('followup_issue_url') or ''}")
        self.store.update_session(row["session_id"], active=0, finished_at=utcnow(),
                                  acted_fingerprint=fp)

    async def _handle_dedup(self, row, workflow, out, session_kind, fp):
        """Verdict of the pre-remediation dedup check."""
        finish = {"active": 0, "finished_at": utcnow(), "acted_fingerprint": fp}
        if not out:
            if session_kind == "waiting":
                reason = _last_devin_message(
                    await self.devin.get_session(row["session_id"])
                ) or "Dedup check is waiting for input."
                self.store.set_state(workflow["id"], State.BLOCKED,
                                     failure_reason=reason, waiting_since=utcnow())
                self.store.update_session(row["session_id"], acted_fingerprint=fp)
            else:
                self.store.set_state(workflow["id"], State.BLOCKED,
                                     failure_reason="dedup session ended without a verdict",
                                     waiting_since=utcnow())
                self.store.update_session(row["session_id"], **finish)
            return
        verdict = (out.get("verdict") or "").upper()
        rationale = out.get("rationale") or ""
        if verdict == "DUPLICATE":
            pr = out.get("duplicate_pr_url") or ""
            self.store.set_state(workflow["id"], State.SKIPPED,
                                 needs_info_kind=SkipReason.DUPLICATE.value,
                                 failure_reason=f"already fixed by open PR {pr}: {rationale}",
                                 completed_at=utcnow())
            await self._comment(
                workflow,
                f"**Skipped — duplicate remediation avoided** "
                f"([dedup check]({row.get('url', '')}))\n\n"
                f"An open PR already fixes this root cause: {pr}\n\n{rationale}",
            )
        elif verdict == "PROCEED":
            # Workflow stays ROOT_CAUSE_FOUND; dispatch remediates next tick.
            self.store.add_event(workflow["id"], "dedup_cleared", detail=rationale)
        else:
            reason = rationale or "dedup verdict was UNSURE"
            self.store.set_state(workflow["id"], State.BLOCKED,
                                 failure_reason=reason, waiting_since=utcnow())
            await self._comment(
                workflow,
                f"**Duplicate check needs a maintainer** ([dedup check]({row.get('url', '')}))\n\n"
                f"{reason}\n\n_Reply on this issue to confirm whether an open PR already "
                "fixes this root cause._",
            )
        self.store.update_session(row["session_id"], **finish)

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
            # Settled dedup verdicts are applied by _handle_dedup. Still being
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
            linked = {int(m.group(1)) for m in _FIX_LINK_RE.finditer(text)}
            if workflow["issue_number"] in linked:
                pr_url = pull.get("html_url", "")
                self.store.set_state(
                    workflow["id"], State.SKIPPED,
                    needs_info_kind=SkipReason.DUPLICATE.value,
                    failure_reason=f"open PR already linked to this issue: {pr_url}",
                    completed_at=utcnow(),
                )
                self.store.add_event(workflow["id"], "dedup_hit", detail=pr_url)
                await self._comment(
                    workflow,
                    "**Skipped — duplicate remediation avoided**\n\n"
                    f"An open PR already references this issue: {pr_url}. "
                    "No remediation session was started.",
                )
                return "duplicate"
        # Ambiguous: cheap token/file overlap produces candidates; a small
        # Dedup Devin decides whether any of them fixes the same root cause.
        inv = workflow.get("investigation") or {}
        query = _tokens(" ".join([
            workflow.get("title") or "",
            inv.get("root_cause") or "",
            " ".join(inv.get("affected_components") or []),
        ]))
        candidates: list[tuple[dict, set[str]]] = []
        for pull in pulls:
            shared = query & _tokens(f"{pull.get('title') or ''} {pull.get('body') or ''}")
            if len(shared) >= 2:
                candidates.append((pull, shared))
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
            if not getattr(self, "_budget_event_sent", False):
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
                if workflow["state"] in {s.value for s in
                                         {State.ROOT_CAUSE_FOUND, State.REMEDIATING, State.VERIFYING,
                                          State.PR_OPENED, State.CI_CHECKING,
                                          State.READY_FOR_REVIEW}} and not self.store.get_sessions(
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
        inv = workflow.get("investigation") or {}
        if role == Role.TRIAGE:
            prompt, schema, limit = (
                triage_prompt(workflow["repo"], issue, comments),
                TRIAGE_SCHEMA,
                self.settings.triage_acu_limit,
            )
        elif role == Role.INVESTIGATOR:
            prompt, schema, limit = investigator_prompt(workflow["repo"], issue, comments), INVESTIGATION_SCHEMA, self.settings.investigator_acu_limit
        elif role == Role.REMEDIATOR:
            investigator = next(iter(self.store.get_sessions(workflow["id"], Role.INVESTIGATOR)), {})
            prompt, schema, limit = remediator_prompt(workflow["repo"], issue, inv, investigator.get("url", "")), REMEDIATION_SCHEMA, self.settings.remediator_acu_limit
            self.store.set_state(workflow["id"], State.REMEDIATING,
                                 remediation_started_at=utcnow(), waiting_since=None)
        elif role == Role.DEDUP:
            prompt, schema, limit = (
                dedup_prompt(workflow["repo"], issue, inv,
                             (prompt_extra or {}).get("candidates") or []),
                DEDUP_SCHEMA,
                self.settings.dedup_acu_limit,
            )
        else:
            prompt, schema, limit = analyst_prompt(workflow["repo"], issue, inv), ANALYSIS_SCHEMA, self.settings.analyst_acu_limit
        if extra_context:
            prompt += f"\n\n## Recovery context\n{extra_context}\n"
        tags = ["superset-x-devin", f"repo:{workflow['repo']}",
                f"issue:{workflow['issue_number']}", f"role:{role.value}"]
        session = await self.devin.create_session(
            prompt, title=f"[{role.value}] {workflow['repo']}#{workflow['issue_number']}: {workflow.get('title', '')[:60]}",
            tags=tags, structured_output_schema=schema,
            max_acu_limit=limit if self.settings.max_acu_limit is None else min(limit, self.settings.max_acu_limit),
        )
        self.store.record_session(session_id=session["session_id"], workflow_id=workflow["id"],
                                  role=role.value, url=session.get("url", ""), active=1)
        self.store.add_event(workflow["id"], "session_created", detail={"session_id": session["session_id"],
                                                                        "role": role.value})
        return session

    async def _comment(self, workflow: dict, body: str):
        if self.settings.dry_run:
            logger.info("[dry-run] would comment on #%s: %s", workflow["issue_number"], body)
            self.store.add_event(workflow["id"], "comment_posted", detail=body)
            return
        comment = await self.github.post_issue_comment(workflow["repo"], workflow["issue_number"], body)
        self.store.add_event(workflow["id"], "comment_posted", detail={"comment_id": comment.get("id")})

    @staticmethod
    def _pull_url(pulls) -> str | None:
        for pull in pulls or []:
            if isinstance(pull, str):
                logger.info("pull_requests item shape: str")
                return pull
            if isinstance(pull, dict) and pull.get("url"):
                logger.info("pull_requests item shape: dict keys=%s", sorted(pull))
                return pull["url"]
        if pulls:
            logger.info("pull_requests item shape: %s", type(pulls[0]).__name__)
        return None

    @staticmethod
    def _pr_number(url: str | None) -> int | None:
        match = re.search(r"/pull/(\d+)", url or "")
        return int(match.group(1)) if match else None

    async def handle_issue_event(self, payload: dict) -> dict:
        if payload.get("action") not in {"opened", "reopened"}:
            return {"handled": False, "reason": "unsupported action"}
        issue = payload.get("issue") or {}
        repo = (payload.get("repository") or {}).get("full_name", self.settings.target_repo)
        if repo != self.settings.target_repo or "pull_request" in issue:
            return {"handled": False, "reason": "ineligible repository or pull request"}
        workflow = self.store.get_workflow(repo, issue["number"]) or self.store.upsert_workflow(
            repo, issue["number"], title=issue.get("title", ""), issue_url=issue.get("html_url", ""),
            author=(issue.get("user") or {}).get("login", ""), labels=list(_labels(issue)),
            state=State.DISCOVERED.value, discovered_at=utcnow(),
        )
        filtered = self._intake_filter(issue)
        if filtered:
            self.store.set_state(workflow["id"], State.SKIPPED,
                                 needs_info_kind=SkipReason.FILTERED.value,
                                 failure_reason=filtered, completed_at=utcnow())
            return {"handled": False, "reason": filtered}
        if not self.settings.triage_enabled and workflow["state"] == State.DISCOVERED.value:
            self.store.set_state(workflow["id"], State.QUEUED)
        await self.tick()
        return {"handled": True, "workflow_id": workflow["id"]}

    async def handle_issue_comment_event(self, payload: dict) -> dict:
        if payload.get("action") != "created":
            return {"handled": False, "reason": "not a created comment"}
        await self.tick()
        return {"handled": True}
